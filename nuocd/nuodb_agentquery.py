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

import pynuodb
from xml.etree import ElementTree
from pynuodb.entity import Domain
from pynuodb.session import Session
try:
  from pynuoadmin import nuodb_mgmt
except:
  import nuodb_mgmt
from six import print_

options = None

class Process:
    def __init__(self,process):
        self.process = process
        
    @property
    def address(self):
        return "%s:%d" % (self.process.address,self.process.port) 

    @property
    def db_name(self):
        return self.process.database.name
    
    @property
    def region_name(self):
        return "Default" 

    @property
    def start_id(self):
        return self.node_id

    @property
    def pid(self):
        return self.process.pid

    @property
    def node_id(self):
        return self.process.node_id

    @property
    def hostname(self):
        return self.process.hostname

    def get(self,attr):
        return getattr(self,attr,None)
    

class Context:
    def __init__(self,domain):
        self.domain=domain

    def _get_db_password(self,dbname):
        peer = self.domain.find_peer("localhost")
        session = Session(peer.connect_str, service="Manager")
        session.authorize(self.domain.user, self.domain.password)
        pwd_response = session.doRequest(attributes={"Type": "GetDatabaseCredentials",
                                                     "Database": dbname})
        pwd_xml = ElementTree.fromstring(pwd_response)
        pwd = pwd_xml.find("Password").text.strip()
        return pwd


context = None

def get_process(pid):
    hostname = "localhost"
    try:
        for db in context.domain.databases:
            for p in db.processes:
                if p.address == "localhost" and str(p.pid)==str(pid):
                    return Process(p)
    except:
        traceback.print_exc()
    return None


parser = optparse.OptionParser(usage="%prog [options] module [-- <module args>]")
parser.add_option("-u",
                  "--user",
                  dest="user",
                  default="domain",
                  help="Domain user (domain).")

parser.add_option("-p",
                  "--password",
                  dest="password",
                  default="bird",
                  help="Domain password (bird).")

parser.add_option('-i',
                  '--interval',
                  dest='interval',
                  default=10,
                  type=int,
                  help='interval in seconds between measurements.')
(options, module_args) = parser.parse_args()

if len(module_args) == 0:
    parser.print_help(sys.stderr)
    sys.exit(1)


module = module_args.pop(0)
m = importlib.import_module(module)
Monitor = m.Monitor
context = Context(Domain("localhost", options.user, options.password))

running_local_processes = {}
while True:
    pids = None
    latency = datetime.now()
    try:
        # only interested in nuodb process on localhost, and don't
        # want to make nuoadmin rest call unless a new process is discovered.
        _processes = subprocess.check_output(["pgrep", "^nuodb$"])
        pids = _processes.split()

        # check if found processes are already known or new
        for pid in pids:
            if pid not in running_local_processes:
                ps = get_process(pid)
                if ps:
                    local_process = ps
                    running_local_processes[pid] = Monitor(local_process, context, True, module_args)

        # check if any known processes are no longer available
        for key in list(running_local_processes):
            if key not in pids:
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
                print_(e, file=sys.stderr)
                del running_local_processes[key]

    except subprocess.CalledProcessError:
        pass
    except KeyboardInterrupt:
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
