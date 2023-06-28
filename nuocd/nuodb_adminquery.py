#!/usr/bin/python
import importlib
import optparse
import os
import re
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime

from pynuoadmin import nuodb_mgmt
import six
from six import print_


def get_admin_conn(pid):
    """ searches for NUOCMD_ variables in nuoadmin or nuodocker then overrides whatever is set from
        command line or environment.  If unable to read environment from nuoadmin or nuodocker then
        defaults to settings passed in or environment variables."""
    admin_conn = None
    try:
        env = {}
        ROOT = ""
        try:
            # starting with 4.1 you can no longer getenv from nuodb process.  So look at parent
            # process. docker.py
            ppid = None
            with open('/proc/%s/status' % (pid,), 'r') as e:
                for l in e:
                    k, v = l[:-1].split(":\t")
                    if k == "PPid":
                        ppid = v
                        break
            if ppid is None:
                raise "Unable to determine ppid for %s" % (pid)
            with open('/proc/%s/environ' % (ppid,), 'r') as e:
                x = e.read()
                env = dict([tuple(y.split('=', 1)) for y in x.split('\0') if '=' in y and y.startswith('NUOCMD_')])
            ROOT = '/proc/%s/root' % (pid,)

            print_("%s: Parsed Environmental Variables for NuoDB Process pid (%s): %s" % (module, pid, env), file=sys.stderr)
        except IOError:
            pass

        protocol = None
        address = None
        api_server = env.get('NUOCMD_API_SERVER', os.environ.get('NUOCMD_API_SERVER'))
        match = re.match('(https?://|)([^:]+)(:[0-9]+|)', api_server)
        if match:
            protocol = match.group(1)
            adminhost = match.group(2)
            port = match.group(3)
            if len(port) == 0:
                port = ':8888'
            address = adminhost + port

        client_key = env.get('NUOCMD_CLIENT_KEY', os.environ.get('NUOCMD_CLIENT_KEY'))
        if client_key:
            client_key = ROOT + client_key
            # downgrade if protocol is unspecified and client key does not exist
            if not protocol and not os.path.exists(client_key):
                client_key = None

        # this assumes set to a path. It could be set to True in which case we need
        # to return the default path

        server_cert = env.get('NUOCMD_VERIFY_SERVER', os.environ.get('NUOCMD_VERIFY_SERVER', False))
        if server_cert and server_cert is not True:
            server_cert = ROOT + server_cert

        if protocol == 'http://' or (
                not protocol and not client_key):
            # downgrade to http://
            protocol = 'http://'
            client_key = None
            server_cert = None
        else:
            protocol = 'https://'

        api_server = protocol + address
        nuodb_mgmt.disable_ssl_warnings()
        admin_conn = nuodb_mgmt.AdminConnection(api_server, client_key=client_key, verify=server_cert)
    except Exception as x:
        print_("%s: Connection to admin failed due to exception: %s" % (module, x), file=sys.stderr)
    finally:
        return admin_conn


parser = optparse.OptionParser(usage="%prog [options] module [-- <module args>]")
parser.add_option('-k',
                  '--client-key',
                  dest='client_key',
                  default=os.getenv('NUOCMD_CLIENT_KEY', '/etc/nuodb/keys/nuocmd.pem'),
                  help='PEM file containing client key to verify with admin')
parser.add_option('-a',
                  '--api-server',
                  dest='api_server',
                  default=os.getenv('NUOCMD_API_SERVER', 'localhost:8888'),
                  help='ADMIN url defaults to $NUOCMD_API_SERVER if set')
parser.add_option('-v',
                  '--verify-server',
                  dest='verify_server',
                  default=os.getenv('NUOCMD_VERIFY_SERVER', '/etc/nuodb/keys/ca.cert'),
                  help='trusted certificate used to verify the server when using HTTPS.')
parser.add_option('-n',
                  '--hostname',
                  dest='hostname',
                  default=os.getenv('NUOCD_HOSTNAME', socket.gethostname()),
                  help='name of this host, as known by admin layer.')
parser.add_option('-i',
                  '--interval',
                  dest='interval',
                  default=10,
                  type=int,
                  help='interval in seconds between measurements.')
(options, module_args) = parser.parse_args()

os.environ['NUOCMD_CLIENT_KEY'] = options.client_key
os.environ['NUOCMD_API_SERVER'] = options.api_server
os.environ['NUOCMD_VERIFY_SERVER'] = options.verify_server
if len(module_args) == 0:
    parser.print_help(sys.stderr)
    sys.exit(1)

module = module_args.pop(0)
m = importlib.import_module(module)
Monitor = m.Monitor

nuodb_mgmt.disable_ssl_warnings()

running_local_processes = {}
while True:
    pids = None
    latency = datetime.now()
    try:
        # only interested in nuodb process on localhost, and don't
        # want to make nuoadmin rest call unless a new process is discovered.

        _processes = subprocess.check_output(["pgrep", "^nuodb$"])
        pids = six.ensure_text(_processes).split()

        # check if found processes are already known or new
        for pid in pids:
            if pid not in running_local_processes:
                print_("%s: Found new NuoDB process with pid (%s). Attempting to start collection now." % (module, pid), file=sys.stderr)
                filter_by = dict(hostname=options.hostname, pid=str(pid))
                conn = get_admin_conn(pid)
                ps = conn.get_processes(**filter_by)
                if ps:

                    local_process = list(ps)[0]
                    running_local_processes[pid] = Monitor(local_process, conn, True, module_args)
                    print_("%s: Collection of NuoDB process with pid (%s) successfully established." % (module, pid), file=sys.stderr)
                else:
                    print_("%s: No known running processes found in NuoDB domain. Ignoring NuoDB process with pid (%s)" % (module, pid), file=sys.stderr)

        # check if any known processes are no longer available
        for key in list(running_local_processes):
            if key not in pids:
                print_("%s: NuoDB process with pid (%s) exited. Stopping collection." % (module, pid), file=sys.stderr)
                del running_local_processes[key]

        # for each nuodb process, execute query
        #  this is done sequentially.  There are two types of monitors
        #  - poller, listener
        #  - poller monitor will query the engine each invocation of execute_query
        #    - msgtrace, synctrace
        #  - listener monitor will read next message from keep alive engine connection
        #    - metric
        #  if running_local_processes a long wait for engine response will delay request or read
        #  of subsequent monitors.
        #
        # should this be done in parallel?
        for key, monitor in list(running_local_processes.items()):
            try:
                sys.stdout.flush()
                monitor.execute_query()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print_("%s: Failure when executing monitoring query: %s" % (module, e), file=sys.stderr)
                del running_local_processes[key]

    except subprocess.CalledProcessError:
        # no nuodb process found
        pass
    except KeyboardInterrupt:
        # ctlr-c exit
        for key in list(running_local_processes):
            del running_local_processes[key]
        raise
    except:
        print_('unknown exception', file=sys.stderr)
        traceback.print_exc()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        _sleep = options.interval - (datetime.now() - latency).total_seconds()
        try:
            # this might not work in Python3 but, does then most of this
            # code does not work in Python3
            if sys.exc_info()[0] == KeyboardInterrupt:
                raise KeyboardInterrupt

            if _sleep > 0:
                time.sleep(_sleep)
            else:
                if len(running_local_processes) == 0:
                    time.sleep(10.0)
        except KeyboardInterrupt:
            for key in list(running_local_processes):
                del running_local_processes[key]
            sys.exit(1)
