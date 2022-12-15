# (C) Copyright NuoDB, Inc. 2016-2021  All Rights Reserved.

import base64
from json import dumps
import os
import re
import select
import socket
import sys
import time
from xml.etree import ElementTree

import requests

try:
    from urllib.parse import urlencode, quote
except ImportError:
    # python 2 imports
    from urllib import urlencode, quote

try:
    # python 2 import
    from StringIO import StringIO
except ImportError:
    # python 3 import
    from io import StringIO

try:
    from pynuodb.session import Session
except ImportError:
    def Session(*args, **kwargs):
        raise NotImplementedError('pynuodb module is not available')

try:
    from collections.abc import Mapping
except ImportError:
    from collections import Mapping


def is_string(s):
    # workaround for py3 removal of basestring class
    stringbase = str
    if sys.version[0] == '2':
        stringbase = basestring
    return isinstance(s,stringbase)


def _get_authorized_session(address, db_password, service,
                            connect_timeout=5.0, read_timeout=None, options=None):
    # `address` is of the form [<hostname>/]<address>:<port>, if hostname and
    # address differ
    address = address.split('/')[-1]
    session = Session(address, service=service,
                      connect_timeout=connect_timeout,
                      read_timeout=read_timeout, options=options)
    session.authorize('Cloud', db_password)
    return session


def _get_monitor_session(address, db_password, log_options=None,
                         engine_file=None, options=None):
    session = _get_authorized_session(address, db_password, 'Monitor', options=options)
    session.doConnect()
    # enable logging if log options are specified
    if log_options is not None:
        # if `log_options` is not a string, assume it is an iterable and build
        # comma-delimited options string
        if not is_string(log_options):
            log_options = ','.join(log_options)
        fattr = ''
        if engine_file is not None:
            fattr = ' Filename="{}" interval="10000"'.format(engine_file)
        session.send(
            '<Request Action="log" Options="{}"{}/>'.format(log_options, fattr))
    return session


def _monitor_process(address, db_password, log_options, engine_file, timeout, options=None):
    session = _get_monitor_session(address, db_password, log_options, engine_file, options=options)
    try:
        while True:
            # This is for compatibility with older versions of pynuodb
            if timeout is None:
                msg = session.recv()
            else:
                msg = session.recv(timeout=timeout)
            if msg is not None:
                msg = ElementTree.fromstring(msg)
            yield msg
    finally:
        session.close(force=True)


class _AggregatingProcessMonitor(object):

    def __init__(self, process_supplier, password_supplier, log_options=None,
                 engine_file=None, update_interval=5, keep_open=False,
                 err=sys.stderr, timeout=None, options=None):
        """
        :param function process_supplier: function that returns list of
                                          processes
        :param function password_supplier: function that returns dictionary of
                                           database name to database password
        :param str log_options: the log options to use if logging is enabled
        :param str engine_file: write logging to a file on the engine's host
        :param update_interval: the interval at which to update set of sessions
                                in seconds
        :param bool keep_open: whether to keep monitoring if there are no
                               running processes
        :param file err: where to write error messages to
        :param map options: connection options for the Session
        """

        self.process_supplier = process_supplier
        self.password_supplier = password_supplier
        self.log_options = log_options
        self.engine_file = engine_file
        self.update_interval = update_interval
        self.keep_open = keep_open
        self.last_update = 0
        self.processes = {}
        self.sessions = {}
        self.err = err
        self.timeout = timeout
        self.options = options
        self._update()

    def _update(self):
        """
        Check process state if the update interval has elapsed and connect to
        any processes that are not already being monitored.
        """

        cur_time = time.time()
        if cur_time - self.last_update < self.update_interval:
            return
        self.last_update = cur_time
        self.processes = dict((process.start_id, process)
                              for process in self.process_supplier())
        passwords = self.password_supplier()
        for start_id, process in self.processes.items():
            # create session for process if we do not have one
            if start_id not in self.sessions and process.address is not None:
                try:
                    self.sessions[start_id] = _get_monitor_session(
                        process.address, passwords[process.db_name],
                        self.log_options, self.engine_file, options=self.options)
                except Exception as e:
                    self.err.write('Unable to connect to start_id={}: {}\n'
                                   .format(start_id, e))

    def messages(self):
        try:
            while self.keep_open or len(self.sessions) != 0:
                self._update()
                # create map of socket to start ID so that we can obtain
                # process information and session for the readable socket
                sockets = dict((session._Session__sock, start_id)
                               for start_id, session in self.sessions.items())
                readable, _, exceptional = select.select(
                    sockets.keys(), [], sockets.keys(),
                    self.update_interval)
                # yield messages from readable sockets
                for sock in readable:
                    try:
                        start_id = sockets[sock]
                        session = self.sessions[start_id]
                        process = self.processes[start_id]
                        # This is for compatibility with old versions of pynuodb
                        if self.timeout is None:
                            msg = session.recv()
                        else:
                            msg = session.recv(timeout=self.timeout)
                        if msg is not None:
                            msg = ElementTree.fromstring(msg)
                        yield process, msg
                    except Exception:
                        # an error occurred during reading, so close the socket
                        self.sessions.pop(start_id).close(True)
                # close any sockets with exceptional conditions
                for sock in exceptional:
                    start_id = sockets[sock]
                    self.sessions.pop(start_id).close(True)
        finally:
            # aggregated monitor closed; close all connections
            for session in self.sessions.values():
                session.close(True)


def _monitor_processes(process_supplier, password_supplier, log_options,
                       engine_file, update_interval, keep_open, err, timeout, options=None):
    monitor = _AggregatingProcessMonitor(process_supplier, password_supplier,
                                         log_options, engine_file,
                                         update_interval, keep_open, err,
                                         timeout, options)
    return monitor.messages()


def resolve_hostname():
    # get IPv4 address info (including canonical name) for TCP connections to
    # this host/container
    addrs = socket.getaddrinfo(
        socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM,
        socket.IPPROTO_TCP, socket.AI_CANONNAME)
    for addr in addrs:
        # fourth element of tuple is canonical name
        if addr[3]:
            return addr[3]

def unicode_to_str(value):
    """
    Convert all unicode strings occurring in a JSON object to utf-8 encoded
    string of type str.

    :param object value: a JSON node

    :returns object:
    """
    if sys.version[0] != '2':
        # all python3 strings are already utf-8 encoded by default,
        # including strings inside lists and dictionaries.
        return value
    if isinstance(value, list):
        return list(map(unicode_to_str, value))
    if isinstance(value, dict):
        return dict((unicode_to_str(k), unicode_to_str(v))
                    for k, v in value.items())
    if is_string(value):
        value = value.encode('utf-8')
    return value


def xml_to_json(xml_root, expected_name=None):
    """
    Converts an XML message to a dict. The message is assumed not to have any
    children, and any inner text is added to the returned dict under the
    message name. If an expected name is specified and does not match the
    message name, then an empty dict is returned.

    :param ElementTree xml_root: the XML message
    :param str expected_name: expected message name

    :returns dict[str, object]:
    """

    ret = {}
    if expected_name is None or expected_name == xml_root.tag:
        # if XML has inner text, add it to dict under tag name
        if xml_root.text is not None:
            ret[xml_root.tag] = xml_root.text
        ret.update(xml_root.attrib)
    return unicode_to_str(ret)


def get_json(resp):
    return unicode_to_str(resp.json())


class Downloader(object):

    KB = 1 << 10
    MB = KB << 10
    GB = MB << 10

    UNITS = [('GB', GB), ('MB', MB), ('KB', KB)]

    def __init__(self, response):
        """
        :param requests.Response response:
        """

        self.response = response
        self.output_file = None

    @staticmethod
    def get_best_units(num_bytes):
        for unit_suffix, unit_size in Downloader.UNITS:
            if num_bytes > unit_size:
                return '%.2f%s' % (num_bytes / float(unit_size), unit_suffix)
        return '{}B'.format(num_bytes)

    def get_filename_from_response(self):
        cd = self.response.headers.get('Content-Disposition')
        if cd is None:
            raise RuntimeError(
                'No output file specified and none found in response')
        matches = re.findall('attachment; filename=(.*)', cd)
        if len(matches) == 0:
            raise RuntimeError(
                'No output file specified and none found in response: ' + cd)
        return matches[0]

    def set_output_dir(self, output_dir):
        if self.output_file is not None:
            raise RuntimeError(
                'Output file already set to ' + self.output_file)
        self.output_file = os.path.join(
            output_dir, self.get_filename_from_response())

    def set_output_file(self, output_file):
        if self.output_file is not None:
            raise RuntimeError(
                'Output file already set to ' + self.output_file)
        self.output_file = output_file

    def download(self, chunk_size=16 * MB, logger=None):
        """
        :param int chunk_size:
        """

        if logger is None:
            logger = StringIO()
        if self.output_file is None:
            self.output_file = self.get_filename_from_response()
        with open(self.output_file, 'wb') as f:
            logger.write('Downloading server logs to ' +
                         os.path.abspath(self.output_file))
            try:
                bytes_downloaded = 0
                chunks_downloaded = 0
                for data in self.response.iter_content(
                        chunk_size=chunk_size):
                    f.write(data)
                    bytes_downloaded += len(data)
                    chunks_downloaded += 1
                    if chunks_downloaded % 10 == 0:
                        logger.write('.')
                        logger.flush()
                logger.write('\n')
                logger.write('{} downloaded\n'.format(
                    Downloader.get_best_units(bytes_downloaded)))
            finally:
                if self.response is not None:
                    self.response.close()


URL_PATTERN = '/api/{version}/{resource}'


def get_url(version, resource):
    return URL_PATTERN.format(version=version, resource=resource)


# urllib3 is a transitive dependency from requests, and we can find it in
# either requests.urllib3 or requests.packages.urllib3 depending on version
# pylint: disable=E1101
def urllib3():
    if hasattr(requests, 'urllib3'):
        return requests.urllib3
    if hasattr(requests, 'packages'):
        if hasattr(requests.packages, 'urllib3'):
            return requests.packages.urllib3


urllib3 = urllib3()


class HostNameIgnoringHTTPAdapter(requests.adapters.HTTPAdapter):
    """
    Transport adapter that ignores hostname when verifying certificate.
    """

    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = urllib3.poolmanager.PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            assert_hostname=False)


HTTPAdapter = requests.adapters.HTTPAdapter

# unless NUOCMD_VERIFY_HOSTNAME=1 is set, disable hostname verification
if urllib3 is not None and os.environ.get('NUOCMD_VERIFY_HOSTNAME') != '1':
    HTTPAdapter = HostNameIgnoringHTTPAdapter


def disable_ssl_warnings():
    """
    Disable any warning messages emitted for unverified HTTPS requests.
    """

    if urllib3 is not None:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class NoopRequestFilter(object):

    def request(self, method, url, **request_args):
        """
        :param str method: the request method (PUT, GET, POST, DELETE)
        :param str url: the request URL
        """

        pass

    def response(self, response):
        """
        :param requests.Response response: the server response
        """

        pass


class FileLoggingRequestFilter(object):

    def __init__(self, f, request_format=None, response_format=None):
        self.f = f
        self.request_format = request_format
        self.response_format = response_format

    def request(self, method, url, **request_args):
        """
        :param str method: the request method (PUT, GET, POST, DELETE)
        :param str url: the request URL
        """

        if not self.request_format:
            return

        try:
            request_args = dict(request_args)
            request_args['method'] = method
            request_args['url'] = url
            if 'params' in request_args:
                params = urlencode(request_args['params'])
                request_args['params'] = params
                request_args['encoded_url'] = url + '?' + params
                if 'full_url' in request_args:
                    request_args['full_url'] += '?' + params
            else:
                request_args['params'] = ''
            if 'json' in request_args:
                request_args['json'] = dumps(request_args['json'], indent=2)
            else:
                request_args['json'] = ''

            self.f.write(self.request_format.format(**request_args) + '\n')
        except Exception:
            pass

    def response(self, response):
        """
        :param requests.Response response: the server response
        """

        if not self.response_format:
            return

        try:
            try:
                json = dumps(get_json(response), indent=2)
            except Exception:
                json = ''
            response_args = dict(
                status_code=response.status_code, json=json)

            self.f.write(self.response_format.format(**response_args) + '\n')
        except Exception:
            pass


class AdminConnection(object):
    """
    Simple wrapper for NuoDB admin layer REST API.
    """

    def __init__(self, url_base, client_key=None, verify=None,
                 response_timeout=30, basic_creds=None,
                 req_filter=None):
        """  # noqa
        Constructor for REST API wrapper.

        :param str url_base: the base of the URL, e.g. http://localhost:8888
        :param client_key: if str, the path to SSL client certificate and private key as a single file,
                           e.g. `client_key='/path/to/client.pem'`
                           if (str, str), the pair of paths to client certificate and private key,
                           e.g. `client_key=('/path/to/client.cert', '/path/to/client.key')`
        :param verify: the path to the trusted certificate used to verify the server or `True`
                       if the default set of trusted CAs should be used
        """

        self.url_base = url_base
        self.version = 1
        self.response_timeout = response_timeout
        self.client_key = client_key
        self.verify = verify
        self.basic_creds = basic_creds
        if req_filter is None:
            self.req_filter = NoopRequestFilter()
        else:
            self.req_filter = req_filter

    def _raise_for_status(self, resp):
        """
        :param requests.Response resp:
        """

        try:
            resp.raise_for_status()
        except requests.HTTPError:
            try:
                json = get_json(resp)
            except ValueError:
                # payload is not JSON, rethrow as is
                raise

            # if payload has our format, throw as an AdminException
            if 'code' in json and ('messages' in json or 'message' in json):
                raise AdminException(json)

            # if the response status code is 408 (Request Timeout), throw as an
            # AdminTimeoutException
            if resp.status_code == requests.codes.timeout:
                raise AdminTimeoutException(get_json(resp))

            # otherwise rethrow as is
            raise

    def _get_url(self, *resource_path):
        """
        :param tuple[str] resource_path:

        :returns str:
        """

        resource = '/'.join(map(str, resource_path))
        return get_url(version=self.version, resource=resource)

    def _send_request(self, method, *resource_path, **kwargs):
        request_kwargs = dict(timeout=self.response_timeout)
        if self.verify is not None:
            request_kwargs['verify'] = self.verify
        if self.client_key is not None:
            request_kwargs['cert'] = self.client_key
        if self.basic_creds is not None:
            request_kwargs['auth'] = requests.auth.HTTPBasicAuth(
                *self.basic_creds)
        request_kwargs.update(kwargs)
        kwargs.setdefault('allow_redirects', True)
        url = self._get_url(*resource_path)
        with self._get_session() as session:
            # invoke filter on request to send
            self.req_filter.request(
                method, url, full_url=self.url_base + url,
                **request_kwargs)
            # send request
            resp = session.request(
                method, self.url_base + url, **request_kwargs)
            # invoke filter on response received
            self.req_filter.response(resp)
            return resp

    def _get_session(self):
        session = requests.sessions.Session()
        session.mount('https://', HTTPAdapter())
        return session

    def _get(self, *resource_path, **kwargs):
        return self._send_request('GET', *resource_path, **kwargs)

    def _post(self, *resource_path, **kwargs):
        return self._send_request('POST', *resource_path, **kwargs)

    def _put(self, *resource_path, **kwargs):
        return self._send_request('PUT', *resource_path, **kwargs)

    def _delete(self, *resource_path, **kwargs):
        return self._send_request('DELETE', *resource_path, **kwargs)

    def _get_paged(self, resource, offset, limit, order_by=None,
                   descending_fields=None, **kwargs):
        """
        :param str resource:
        :param int offset:
        :param int limit:

        :returns dict[str, object]:
        """

        # get rid of any keyword args with value None
        kwargs = dict((k, v) for k, v, in kwargs.items() if v is not None)
        # use params supplied by caller; the rest are filter_by params
        params = kwargs.pop('params', {})
        params.update(offset=offset, limit=limit, filterBy=list(kwargs.keys()))
        params.update(kwargs)
        # set query parameter for fields to order by
        if is_string(order_by):
            params['orderBy'] = order_by
        elif order_by:
            params['orderBy'] = ','.join(order_by)
        # set query parameter for fields to order by in descending order
        if is_string(descending_fields):
            params['descendingFields'] = descending_fields
        elif descending_fields:
            params['descendingFields'] = ','.join(descending_fields)
        resp = self._get(resource, params=params)
        self._raise_for_status(resp)
        return get_json(resp)

    def _get_all(self, resource, order_by=None, descending_fields=None,
                 **filter_by):
        """
        :param str resource:

        :returns list[dict]:
        """

        offset = 0
        limit = 20
        data = []
        has_next = True
        while has_next:
            # measure request time
            start_time = time.time()
            json = self._get_paged(resource, offset, limit, order_by,
                                   descending_fields, **filter_by)
            data += json['data']
            has_next = json['hasNext']
            offset += limit
            # if request time was less than five seconds, increase page size;
            # this is to avoid making the page size too large
            if time.time() - start_time < 5:
                # maxLimit response field is the maximum page size supported by
                # the server; previously we had a maximum page size of 100, but
                # did not include that information in the response
                max_limit = json.get('maxLimit', 100)
                limit = min(2 * limit, max_limit)
        return data

    def _stream_all(self, resource, order_by=None, descending_fields=None,
                    **filter_by):
        offset = 0
        limit = 20
        has_next = True
        while has_next:
            # measure request time
            start_time = time.time()
            json = self._get_paged(resource, offset, limit, order_by,
                                   descending_fields, **filter_by)
            end_time = time.time()
            for elem in json['data']:
                yield elem

            has_next = json['hasNext']
            offset += limit
            # if request time was less than five seconds, increase page size;
            # this is to avoid making the page size too large
            if end_time - start_time < 5:
                # maxLimit response field is the maximum page size supported by
                # the server; previously we had a maximum page size of 100, but
                # did not include that information in the response
                max_limit = json.get('maxLimit', 100)
                limit = min(2 * limit, max_limit)

    def _get_data(self, *resource_path):
        """
        :param tuple[str] resource_path:

        :returns dict:
        """

        resp = self._get(*resource_path)
        self._raise_for_status(resp)
        return get_json(resp)

    def get_servers(self, order_by='id', **filter_by):
        """
        Get all servers in the domain.

        :returns list[Server]: list of servers
        """

        return list(map(Server, self._get_all('peers', order_by, **filter_by)))

    def get_server(self, server_id):
        """
        Get a server by ID.

        :param str server_id: the ID of the server

        :returns Server: the server metadata
        """

        resp = self._get('peers', server_id)
        self._raise_for_status(resp)
        return Server(get_json(resp))

    def delete_server(self, server_id):
        """
        Remove a server from the raft membership. This command can reduce the
        number of servers required for consensus.

        :param str server_id: the ID of the server to remove from the raft
                              membership
        """

        resp = self._delete('peers', server_id)
        self._raise_for_status(resp)

    def shutdown_server(self, server_id, evict_local=False):
        """
        Shutdown a server.

        :param str server_id: the ID of the server to shut down
        """

        params = dict(evictLocal=evict_local)
        resp = self._post('peers', server_id, 'shutdown', params=params)
        self._raise_for_status(resp)

    def delete_server_state(self, server_id):
        """
        Remove process and archive state associated with a server, and also
        forcibly evict all running processes on the host.

        :param str server_id: the ID of the server to remove domain state for
        """

        resp = self._post('peers', server_id, 'removeDomainState')
        self._raise_for_status(resp)

    def get_processes(self, db_name=None, order_by='startId', filter_query=None,
                      **filter_by):
        """
        Get all running or requested processes in the domain.

        :param str db_name: the name of the database to filter by
        :param str order_by: field to order by
        :param str filter_query: LBQuery like syntax to filter by
        :param dict[str, str] filter_by: map of field to regex to filter by

        :returns list[Process]: list of running or requested processes
        """

        if db_name is not None:
            filter_by['dbName'] = db_name
        params = dict(filterQuery=filter_query)
        return list(map(Process, self._get_all('processes', order_by, params=params,
                                               **filter_by)))

    def get_process_options(self, start_id=None):
        """
        Get the engine options for a process, or get the list of all available
        engine options if no process is specified.

        :param str start_id: the start ID of the process

        :returns dict[str, str]: the process options
        """

        if start_id is None:
            return self._get_data('processes', 'availableOptions')['options']
        resp = self._get('processes', start_id, 'options')
        self._raise_for_status(resp)
        return get_json(resp)

    def get_process(self, start_id):
        """
        Get a process by start ID.

        :param str start_id: the start ID of the process

        :returns Process: the process metadata
        """

        resp = self._get('processes', start_id)
        try:
            self._raise_for_status(resp)
        except AdminException as e:
            # if this is a tombstone, return it
            if 'process' in e and 'databaseIncarnation' in e:
                return ExitedProcess(e._dict)
            # otherwise rethrow
            raise

        return Process(get_json(resp))

    def start_process(self, db_name, server_id, engine_type='TE',
                      archive_id=None, is_external=False,
                      incarnation_major=None, incarnation_minor=None,
                      labels=None, **options):
        """
        Start a database process.

        :param str db_name: the name of the database
        :param str server_id: the ID of the server
        :param str engine_type: the type of the engine ('TE', 'SM', or 'SSM')
        :param int archive_id: the ID of the archive if 'SM' or 'SSM'
        :param bool is_external: whether this is an external process start
        :param int incarnation_major: the incarnation that we are expecting the
                                      database to be in
        :param int incarnation_minor: the incarnation that we are expecting the
                                      database to be in; can only be specified
                                      if `incarnation_major` is also specified
        :param dict labels: labels for engine process

        :returns Process: the metadata for the new process
        """

        payload = dict(dbName=db_name, host=server_id, engineType=engine_type)
        if archive_id is not None:
            payload['archiveId'] = archive_id
        if labels is not None:
            payload['labels'] = labels
        if len(options) != 0:
            payload['overrideOptions'] = options
        if incarnation_major is not None:
            expected_incarnation = dict(major=incarnation_major)
            if incarnation_minor is not None:
                expected_incarnation['minor'] = incarnation_minor
            payload['expectedIncarnation'] = expected_incarnation
        elif incarnation_minor is not None:
            raise ValueError(
                'Cannot specify minor incarnation if major is not specified')

        resource_path = (['processes', 'externalStartup']
                         if is_external else ['processes'])
        resp = self._post(*resource_path, json=payload)
        self._raise_for_status(resp)
        return Process(get_json(resp))

    def shutdown_process(self, start_id, evict=False, kill=False,
                         kill_with_core=False, timeout=10):
        """
        Shutdown a running process or delete/cancel a requested process.

        :param str start_id: the start ID of the process
        :param bool evict: whether to evict process
        :param bool kill: whether to forcibly kill process (kill -9 on UNIX)
        :param bool kill_with_core: whether to dump core
        :param int timeout: the timeout in seconds to wait for the process to
                            disconnect from its admin process; if timeout <= 0,
                            then issue shutdown request without waiting

        :returns Process: the process metadata if it was requested to shutdown
                          but is still running at the time the request
                          finished, or None if the process is already gone
        """

        params = dict(evict=evict, kill=kill, killWithCore=kill_with_core,
                      timeout=timeout)
        if timeout is not None:
            response_timeout = int(timeout) + self.response_timeout
        else:
            response_timeout = self.response_timeout
        resp = self._delete('processes', start_id, params=params,
                            timeout=response_timeout)
        self._raise_for_status(resp)

    def get_exited_processes(self, db_name=None, order_by='process.startId',
                             filter_query=None, **filter_by):
        """
        Get all exited processes.

        :param str db_name: the name of the database to filter by
        :param str order_by: field to order by
        :param str filter_query: LBQuery like syntax to filter by
        :param dict[str, str] filter_by: map of field to regex to filter by

        :returns list[ExitedProcess]: list of all exited processes
        """

        if db_name is not None:
            filter_by['process.dbName'] = db_name
        params = dict(filterQuery=filter_query)
        return list(map(ExitedProcess, self._get_all('processes/exited',
                                                     order_by, params=params,
                                                     **filter_by)))

    def stream_exited_processes(self, db_name=None, order_by=None,
                                descending_fields=None, **filter_by):
        """
        Stream all exited processes.
        """

        if db_name is not None:
            filter_by['process.dbName'] = db_name
        if order_by is None:
            order_by = ['databaseIncarnation.major',
                        'databaseIncarnation.minor',
                        'process.startId']
        if descending_fields is None:
            descending_fields = ['databaseIncarnation.major',
                                 'databaseIncarnation.minor']
        for elem in self._stream_all('processes/exited', order_by,
                                     descending_fields, **filter_by):
            yield ExitedProcess(elem)

    def get_databases(self, order_by='name', **filter_by):
        """
        Get all databases.

        :returns list[Database]: list of databases
        """

        return list(map(Database, self._get_all('databases', order_by, **filter_by)))

    def get_database(self, db_name):
        """
        Get a database.

        :param str db_name: the database name

        :returns Database: the database metadata
        """

        resp = self._get('databases', db_name)
        self._raise_for_status(resp)
        return Database(get_json(resp))

    def capture_database(self, db_name, check_state=True):
        """
        Return the current state of a database.

        :param str db_name: the database name
        :param bool check_state: whether to require that the database is in
                                 RUNNING state

        :returns dict[str, object]: list of process objects
        """

        resp = self._get('databases', db_name, 'capture',
                         params=dict(checkState=check_state))
        self._raise_for_status(resp)
        return get_json(resp)

    def create_database(self, db_name, dba_user, dba_password,
                        te_server_ids=None, default_region_id=None,
                        host_assignments=None, archive_assignment=None,
                        may_exist=False, **default_options):
        """
        Create a database.

        :param str db_name: the name of the database
        :param str db_user: the DBA user
        :param str db_password: the DBA password
        :param list[str] te_server_ids: the server IDs to start TEs on
        :param int default_region_id: the ID of the default region
        :param dict[str, int] host_assignments: assignments of server to region
        :param dict[int, str] archive_ids:
                mapping of archive IDs to server IDs; can only be passed in for
                a database with external-start
        :param bool may_exist: whether we should issue an idempotent
                               create-database request
        :param dict[str, str] default_options:
                default options for all processes and options in database scope
                that have to be defined consistently among all processes such
                as ping-timeout

        :returns dict[str, object]: list of process objects
        """

        payload = dict(databaseDbaUser=dba_user,
                       databaseDbaPassword=dba_password)
        if te_server_ids is not None:
            payload['teHosts'] = te_server_ids
        if default_region_id is not None:
            payload['defaultRegionId'] = default_region_id
        if host_assignments is not None:
            payload['hostAssignments'] = host_assignments
        if archive_assignment is not None:
            payload['smHosts'] = archive_assignment
        if len(default_options) != 0:
            payload['defaultOptions'] = default_options
        resp = self._put('databases', db_name,
                         params=dict(mayExist=may_exist),
                         json=payload)
        self._raise_for_status(resp)
        if len(resp.text) != 0:
            return get_json(resp)

    def update_database_options(self, db_name, replace_all=False,
                                **default_options):
        """
        Update database options.

        :param str db_name: the name of the database
        :param bool replace_all: whether to replace the existing database
                                 options with the supplied options, as opposed
                                 to treating the supplied options like a sparse
                                 mapping and merging it with the existing
                                 options, which is the default behavior
        :param dict[str, str] default_options:
                default options for all processes and options in database scope
                that have to be defined consistently among all processes such
                as ping-timeout
        """

        resp = self._post('databases', db_name, 'options',
                          params=dict(replaceAll=replace_all),
                          json=default_options)
        self._raise_for_status(resp)

    def delete_database(self, db_name):
        """
        Delete a database.

        :param str db_name: the database to delete
        """

        resp = self._delete('databases', db_name)
        self._raise_for_status(resp)

    def get_database_startplan(self, db_name, reusable=True,
                               te_server_ids=None,
                               archive_assignment=None):
        """
        Get a database start-plan.

        :param str db_name: the name of the database
        :param bool reusable: whether the start-plan is reusable or only usable
                              the next time a database start is attempted
        :param list[str] te_server_ids: the server IDs to start TEs on
        :param dict[int, str]: mapping of archive IDs to server IDs; can only
                               be passed in for a database with external-start

        :returns dict[str, object]: the database start-plan
        """

        payload = dict(reusable=reusable)
        if te_server_ids is not None:
            payload['teHosts'] = te_server_ids
        if archive_assignment is not None:
            payload['smHosts'] = archive_assignment
        resp = self._post('databases', db_name, 'startplan', json=payload)
        self._raise_for_status(resp)
        return get_json(resp)

    def shutdown_database(self, db_name):
        """
        Shutdown a database.

        :param str db_name: the database to shutdown
        """

        resp = self._post('databases', db_name, 'shutdown')
        self._raise_for_status(resp)

    def handoff_database_report_timestamp(self, db_name, archive_ids, timeout):
        """
        Query database to report timestamp of data that will be kept after
        active/passive handoff

        :param str db_name: the database to query
        :param list archive_ids: list of archive ids of observers
        :param int timeout: timeout in milliseconds
        """
        payload = dict(archiveIds=archive_ids, timeoutMs=timeout)
        # Wait for response from admin for the admin's timeout plus response_timeout
        response_timeout = self.response_timeout
        if timeout is not None:
            response_timeout += timeout / 1000
        resp = self._get('databases', db_name, 'reportTimestamp', json=payload, timeout=response_timeout)
        self._raise_for_status(resp)
        return ReportTimestamp(get_json(resp))

    def handoff_database_reset_state(self, db_name, commits, epoch, leaders):
        payload = dict(commits=commits, epoch=epoch, leaders=leaders)
        resp = self._post('databases', db_name, 'resetState', json=payload)
        self._raise_for_status(resp)

    def modify_observer_status(self, archive_id, promote_storage_groups, demote_storage_groups):
        payload = dict(promoteStorageGroups=promote_storage_groups,
                       demoteStorageGroups=demote_storage_groups)
        resp = self._post('archives/modifyObserverStatus', archive_id, json=payload)
        self._raise_for_status(resp)

    def get_database_versions(self, db_name):
        """
        Get database version and process version information

        :param str db_name: the database to query
        """
        resp = self._get('databases', db_name, 'versions')
        self._raise_for_status(resp)
        return DatabaseVersions(get_json(resp))

    def upgrade_database_version(self, db_name, version, max_version=False):
        resp = self._post('databases', db_name, 'upgrade',
                          params=dict(version=version, maxVersion=max_version))
        self._raise_for_status(resp)

    #
    # Hot-copy APIs
    #

    def hotcopy_database_simple(
            self, db_name, default_backup_dir=None, backup_dirs=None,
            default_journal_dir=None, journal_dirs=None,
            timeout_sec=None):
        """
        Issue a simple hot-copy request to backup a running database. Unlike
        hot-copies made with `hotcopy_database()`, this produces a backup that
        can be used as an archive directory, without having to be restored
        using `nuoarchive restore`.

        :param str db_name: the database to backup
        :param str default_backup_dir: the default backup location to use if
                                       one is not specified in `backup_dirs`
        :param str default_backup_dir: the default journal location to use if
                                       one is not specified in `journal_dirs`;
                                       `default_backup_dirs` must be specified
        :param dict[int, str] backup_dirs:
                map of archive ID to backup location to use for that archive,
                on the file-system of the engine process
        :param dict[int, str] journal_dirs:
                map of archive ID to journal location to use for that archive,
                on the file-system of the engine process; each archive ID must
                also be specified in `backup_dirs`
        :param int timeout_sec: if specified, the number of seconds to wait for
                                the hot-copy request to complete

        :returns HotCopyResponse: hot-copy response containing start ID of
                                  coordinator SM and hot-copy ID
        """

        payload = {}
        # add default backup location
        if default_backup_dir is not None:
            payload['defaultBackupLocation'] = dict(copyArchiveDir=default_backup_dir)
        # add default journal location and make sure default backup location was specified
        if default_journal_dir is not None:
            if 'defaultBackupLocation' not in payload:
                raise RuntimeError('Cannot specify default_journal_dir without default_archive_dir')
            payload['defaultBackupLocation']['copyJournalDir'] = default_journal_dir
        # add backup locations
        if backup_dirs is not None:
            payload['backupLocations'] = {}
            for archive_id, backup_dir in backup_dirs.items():
                payload['backupLocations'][archive_id] = dict(copyArchiveDir=backup_dir)
        # add journal locations and make sure each specified archive ID appears in backup locations
        if journal_dirs is not None:
            for archive_id, journal_dir in journal_dirs.items():
                if 'backupLocations' not in payload or archive_id not in payload['backupLocations']:
                    raise RuntimeError('Archive ID {} specified in journal_dirs but not in backup_dirs'.format(archive_id))
                payload['backupLocations'][archive_id]['copyJournalDir'] = journal_dir
        # specify hotcopy timeout and bump HTTP response timeout by that value
        if timeout_sec is not None:
            payload['timeoutSec'] = timeout_sec
            response_timeout = int(timeout_sec) + self.response_timeout
        else:
            response_timeout = self.response_timeout
        resp = self._post('databases', db_name, 'hotCopySimple', json=payload,
                          timeout=response_timeout)
        self._raise_for_status(resp)
        return HotCopyResponse(get_json(resp))

    def hotcopy_database(self, db_name, type, shared=False,
                         default_backup_dir=None, backup_dirs=None,
                         max_rate=None, timeout_sec=None):
        """
        Issue a hot-copy request to backup a running database.

        :param str db_name: the database to backup
        :param str type: the type of hot-copy to perform; one of 'full',
                         'incremental', and 'journal'
        :param bool shared: whether the backup location specified by
                            `default_backup_dir` is on shared storage shared
                            exposed to multiple SMs; if `True`, each archive is
                            backed up to a sub-directory under
                            `default_backup_dir`
        :param str default_backup_dir: the default backup location to use if
                                       one is not specified in `backup_dirs`
        :param dict[int, str] backup_dirs:
                map of archive ID to backup location to use for that archive,
                on the file-system of the engine process
        :param int max_rate: if specified, maximum I/O rate in bytes/sec
        :param int timeout_sec: if specified, the number of seconds to wait for
                                the hot-copy request to complete

        :returns HotCopyResponse: hot-copy response containing start ID of
                                  coordinator SM and hot-copy ID
        """

        payload = dict(type=type, shared=shared)
        if default_backup_dir is not None:
            payload['defaultBackupSetDir'] = default_backup_dir
        if backup_dirs is not None:
            payload['backupSetDirs'] = backup_dirs
        if max_rate is not None:
            payload['maxRate'] = max_rate
        if timeout_sec is not None:
            payload['timeoutSec'] = timeout_sec
            response_timeout = int(timeout_sec) + self.response_timeout
        else:
            response_timeout = self.response_timeout
        resp = self._post('databases', db_name, 'hotCopy', json=payload,
                          timeout=response_timeout)
        self._raise_for_status(resp)
        return HotCopyResponse(get_json(resp))

    def get_hotcopy_statuses(self, db_name, timeout_sec=None, min_seconds_running=None):
        """
        Get the status of a previously issued hot-copy request.

        :param str db_name: the name of the database to obtain hot-copy
                            statuses for
        :param int timeout_sec: the number of seconds to wait for the
                                management requests sent to SMs to obtain
                                hot-copy status
        :param int min_seconds_running: if specified, the response should
                                        include only running hot-copies that
                                        began more than this number of seconds
                                        ago

        :returns HotCopyDatabaseStatuses: hot-copy statuses for all running SMs
                                          for the database
        """

        params = {}
        # Wait for response from admin for the admin's timeout plus response_timeout
        if timeout_sec is not None:
            params['timeoutSec'] = timeout_sec
            response_timeout = int(timeout_sec) + self.response_timeout
        else:
            response_timeout = self.response_timeout
        if min_seconds_running is not None:
            params['minSecondsRunning'] = min_seconds_running
        resp = self._get('databases', db_name, 'hotCopyStatus', params=params,
                         timeout=response_timeout)
        self._raise_for_status(resp)
        return HotCopyDatabaseStatuses(get_json(resp))

    def get_hotcopy_status(self, coordinator_start_id, hotcopy_id,
                           timeout_sec=None):
        """
        Get the status of a previously issued hot-copy request.

        :param str coordinator_start_id: the start ID of the coordinator SM,
                                         which is present in the hot-copy
                                         response
        :param str hotcopy_id: the hot-copy ID, also present in the hot-copy
                               response; if not specified, all hot-copy
                               statuses for the SM are returned
        :param int timeout_sec: if specified, the number of seconds to wait for
                                the hot-copy request to complete

        :returns HotCopyResponse: hot-copy response containing start ID of
                                  coordinator SM and hot-copy ID
        """

        params = dict(coordinatorStartId=coordinator_start_id,
                      hotCopyId=hotcopy_id)
        # Wait for response from admin for the admin's timeout plus response_timeout
        if timeout_sec is not None:
            params['timeoutSec'] = timeout_sec
            response_timeout = int(timeout_sec) + self.response_timeout
        else:
            response_timeout = self.response_timeout
        resp = self._get('databases', 'hotCopyStatus', params=params,
                         timeout=response_timeout)
        self._raise_for_status(resp)

        # return all hot-copy statuses if hot-copy ID was not supplied
        if hotcopy_id is None:
            return HotCopyDatabaseStatuses(get_json(resp))

        return HotCopyResponse(get_json(resp))

    def cancel_hotcopy(self, coordinator_start_id, hotcopy_id,
                       timeout_sec=None):
        """
        Cancel a running hot-copy.

        :param str coordinator_start_id: the start ID of the coordinator SM,
                                         which is present in the hot-copy
                                         response
        :param str hotcopy_id: the hot-copy ID, also present in the hot-copy
                               response
        :param int timeout_sec: if specified, the number of seconds to wait for
                                the hot-copy canellation to be processesd

        :returns HotCopyResponse: hot-copy response containing start ID of
                                  coordinator SM and hot-copy ID
        """

        params = dict(coordinatorStartId=coordinator_start_id,
                      hotCopyId=hotcopy_id)
        # Wait for response from admin for the admin's timeout plus response_timeout
        if timeout_sec is not None:
            params['timeoutSec'] = timeout_sec
            response_timeout = int(timeout_sec) + self.response_timeout
        else:
            response_timeout = self.response_timeout
        resp = self._post('databases', 'cancelHotCopy', params=params,
                          timeout=response_timeout)
        self._raise_for_status(resp)
        return HotCopyResponse(get_json(resp))

    #
    # Load-balancer APIs
    #

    def get_sql_connection(self, db_name, **connection_properties):
        """
        :param str db_name: the name of the database
        """

        resp = self._get('databases', db_name, 'sqlConnection',
                         params=connection_properties)
        self._raise_for_status(resp)
        if len(resp.text) != 0:
            return Process(get_json(resp))

    def set_load_balancer_policy(self, policy_name, lb_query):
        """
        :param str policy_name: the name of the load-balancer policy to set
        :param str lb_query: the load-balancer policy to set
        """

        payload = dict(policyName=policy_name, lbQuery=lb_query)
        resp = self._post('databases', 'loadBalancerPolicy', policy_name,
                          json=payload)
        self._raise_for_status(resp)

    def remove_load_balancer_policy(self, policy_name):
        """
        :param str policy_name: the name of the load-balancer policy to remove
        """

        resp = self._delete('databases', 'loadBalancerPolicy', policy_name)
        self._raise_for_status(resp)

    def get_load_balancer_policies(self, order_by='policyName', **filter_by):
        """
        Get all load-balancer policies.

        :returns list[LoadBalancerPolicy]: list of load-balancer policies
        """

        return list(map(LoadBalancerPolicy,
                        self._get_all('databases/loadBalancerPolicies',
                                      order_by, **filter_by)))

    def set_load_balancer_config(
            self, db_name=None, is_global=False,
            default=None, unregister_default=False,
            prefilter=None, unregister_prefilter=False):
        """
        Update load-balancer configuration.

        :param str db_name: the database to update configuration for
        :param bool is_global: whether to update global configuration; cannot
                               be specified with `db_name`
        :param str default: expression in LBQuery syntax for default
                            load-balancer policy to register
        :param bool unregister_default: whether to unregister the current
                                        default; cannot be specified with
                                        `default`
        :param str prefilter: filter expression to register as prefilter
        :param bool unregister_prefilter: whether to unregister the current
                                          prefilter; cannot be specified with
                                          `prefilter`
        """

        params = dict()
        if unregister_default:
            params['unregisterDefaultLbQuery'] = True
        if unregister_prefilter:
            params['unregisterPrefilter'] = True
        resp = self._post(
            'databases/loadBalancerConfig', params=params,
            json=dict(isGlobal=is_global,
                      dbName=db_name,
                      defaultLbQuery=default,
                      prefilter=prefilter))
        self._raise_for_status(resp)

    def get_load_balancer_configs(self, order_by='dbName', **filter_by):
        """
        Get all load-balancer configurations.

        :returns list[LoadBalancerConfig]: list of load-balancer configurations
        """

        return list(map(LoadBalancerConfig,
                        self._get_all('databases/loadBalancerConfigs',
                                      order_by, **filter_by)))

    #
    # Region APIs
    #

    def create_region(self, name):
        """
        Create a region.

        :param str name: the name of the new region

        :returns Region: the region metadata
        """

        resp = self._post('regions', json=dict(name=name))
        self._raise_for_status(resp)
        return Region(get_json(resp))

    def rename_region(self, region_id, old_name, new_name):
        """
        Rename an existing region.

        :param int region_id: the ID of the region
        :param str old_name: the current name of the region
        :param str new_name: the new name of the region

        :returns Region: the region metadata
        """

        resp = self._put('regions', region_id,
                         json=dict(oldName=old_name, name=new_name))
        self._raise_for_status(resp)
        return Region(get_json(resp))

    def delete_region(self, region_id):
        """
        Delete a region.

        :param int region_id: the ID of the region
        """

        resp = self._delete('regions', region_id)
        self._raise_for_status(resp)

    def get_region(self, region_id):
        """
        Get a region.

        :param int region_id: the ID of the region

        :returns Region: the region metadata
        """

        resp = self._get('regions', region_id)
        self._raise_for_status(resp)
        return Region(get_json(resp))

    def get_regions(self, order_by='id', **filter_by):
        """
        Get all regions.

        :returns list[Region]: list of all regions in the domain
        """

        return list(map(Region, self._get_all('regions', order_by, **filter_by)))

    #
    # Region assignments to DB
    #

    def set_default_region(self, db_name, region_id):
        """
        Set the default region for a database.

        :param str db_name: the database name
        :param int region_id: the region ID
        """

        resp = self._put('databases', db_name, 'defaultRegion',
                         json=dict(regionId=region_id))
        self._raise_for_status(resp)

    def add_server_assignment(self, db_name, region_id, server_id):
        """
        Add a server to a region.

        :param str db_name: the database name
        :param int region_id: the region ID
        :param int server_id: the server ID
        """

        resp = self._put('databases', db_name, 'hosts',
                         json=dict(regionId=region_id, serverId=server_id))
        self._raise_for_status(resp)

    def remove_server_assignment(self, db_name, server_id):
        """
        Remove a server assignment for a database.

        :param str db_name: the database name
        :param int server_id: the server ID
        """

        resp = self._delete('databases', db_name, 'hosts',
                            json=dict(serverId=server_id))
        self._raise_for_status(resp)

    #
    # Storage Group APIs
    #

    def add_storage_group(self, db_name, sg_name, archive_id, timeout=None):
        """
        Add a storage group to an archive.

        :param str db_name: the name of the database
        :param str sg_name: the name of the storage group to add
        :param int archive_id: the ID of archive to add storage group to
        """

        params = {}
        if timeout is not None:
            params['timeout'] = timeout
            response_timeout = int(timeout) + self.response_timeout
        else:
            response_timeout = self.response_timeout
        resp = self._post('archives/addStorageGroup', params=params,
                          json=dict(dbName=db_name, sgName=sg_name,
                                    archiveId=archive_id),
                          timeout=response_timeout)
        self._raise_for_status(resp)
        if get_json(resp) is not None:
            return StorageGroup(get_json(resp))

    def remove_storage_group(self, db_name, sg_name, archive_id, timeout=None):
        """
        Remove a storage group from an archive.

        :param str db_name: the name of the database
        :param str sg_name: the name of the storage group to remove
        :param int archive_id: the ID of archive to remove storage group from
        """

        params = {}
        if timeout is not None:
            params['timeout'] = timeout
            response_timeout = int(timeout) + self.response_timeout
        else:
            response_timeout = self.response_timeout
        resp = self._post('archives/removeStorageGroup', params=params,
                          json=dict(dbName=db_name, sgName=sg_name,
                                    archiveId=archive_id),
                          timeout=response_timeout)
        self._raise_for_status(resp)
        if get_json(resp) is not None:
            return StorageGroup(get_json(resp))

    def delete_storage_group(self, db_name, sg_name, timeout=None):
        """
        Delete a storage group from database

        :param str db_name: the name of the database
        :param str sg_name: the name of the storage group to remove
        """
        if timeout is not None:
            response_timeout = int(timeout) + self.response_timeout
        else:
            response_timeout = self.response_timeout

        resp = self._delete('databases', db_name, "storageGroups", sg_name,
                            timeout=response_timeout)
        self._raise_for_status(resp)

    def get_storage_groups(self, db_name, order_by='sgName', **filter_by):
        """
        Get all storage groups for a database.

        :param str db_name: the name of the database

        :returns list[StorageGroup]: the list of storage groups
        """

        sgs = self._get_all('databases/{}/storageGroups'.format(db_name),
                            order_by, **filter_by)
        return list(map(StorageGroup, sgs))

    def get_storage_group(self, db_name, sg_name=None, sg_id=None):
        """
        Get a storage group for a database.

        :param str db_name: the name of the database

        :returns list[StorageGroup]: the list of storage groups
        """

        filter_by = {}
        if sg_name is not None:
            filter_by['sgName'] = sg_name
        if sg_id is not None:
            filter_by['sgId'] = sg_id
        if len(filter_by) != 1:
            raise ValueError('Must specify exactly one of sg_name, sg_id')
        sgs = self._get_all('databases/{}/storageGroups'.format(db_name),
                            **filter_by)
        if len(sgs) != 0:
            return StorageGroup(sgs[0])

    #
    # Archive APIs
    #

    def get_archives(self, db_name=None, removed=False, order_by='id',
                     **filter_by):
        """
        Get all database archives.

        :param str db_name: the name of the database to filter by
        :param bool removed: whether to return all removed archives

        :returns list[Archive]: the list of all archives in the domain
        """

        if db_name is not None:
            filter_by['dbName'] = db_name
        return list(map(Archive, self._get_all('archives', order_by,
                                          params=dict(removed=removed),
                                          **filter_by)))

    def get_archive(self, archive_id, removed=False):
        """
        Get a database archive by ID.

        :param int archive_id: the archive ID
        :param bool removed: whether the archive is a removed archive

        :returns Archive: the archive metadata
        """

        resp = self._get('archives', archive_id,
                         params=dict(removed=removed))
        self._raise_for_status(resp)
        return Archive(get_json(resp))

    def create_archive(self, db_name, server_id, archive_path,
                       journal_path=None, snapshot_archive_path=None,
                       restored=False, archive_id=None, storage_group_observers=None):
        """
        Create a database archive.

        :param str db_name: the name of the database the archive belongs to
        :param str server_id: the server associated with the archive
        :param str archive_path: the path to the archive directory
        :param str journal_path: the path to the journal directory
        :param str snapshot_archive_path: the path to the snapshot archive
                                          directory
        :param bool restored: whether this is a restored archive.
        :param int archive_id: if specified, then this is a removed archive
                               that is being resurrected

        :returns Archive: the archive metadata (including archive ID)
        """

        payload = dict(dbName=db_name, host=server_id, path=archive_path,
                       restored=restored)
        if server_id is None:
            payload['remoteStart'] = True
        if journal_path is not None:
            payload['journalPath'] = journal_path
        if snapshot_archive_path is not None:
            payload['snapshotArchivePath'] = snapshot_archive_path
        if storage_group_observers is not None:
            payload['storageGroupObservers'] = storage_group_observers

        if archive_id is None:
            resp = self._post('archives', json=payload)
        else:
            resp = self._put('archives', archive_id, json=payload)

        self._raise_for_status(resp)
        return Archive(get_json(resp))

    def delete_archive(self, archive_id, purge=False):
        """
        Delete a database archive (removes metadata in admin layer but not the
        actual data).

        :param int archive_id: the archive ID
        :param bool purge: whether to remove the archive permanently

        :returns Archive: the deleted archive metadata
        """

        resp = self._delete('archives', archive_id, params=dict(purge=purge))
        self._raise_for_status(resp)
        return Archive(get_json(resp))

    def update_data_encryption(self, db_name, new_password,
                               current_password,
                               existing_passwords=None,
                               timeout_sec=10):
        """
        Update storage password used to encrypt all archives for the specified
        database. The current storage password must be supplied for
        verification.

        :param str db_name: the database to configure data encryption for
        :param str new_password: the new storage password to use for all
                                        archives
        :param str current_password: the current storage password
        :param list[str] existing_passwords: any storage passwords needed to
                                             decrypt archives for this database
        """

        payload = dict(dbName=db_name,
                       newTargetStoragePassword=new_password,
                       currentTargetStoragePassword=current_password)
        if existing_passwords is not None:
            payload['existingStoragePasswords'] = existing_passwords

        # REST request is always synchronous; inject default timeout if None
        # was specified for some reason
        if timeout_sec is None:
            timeout_sec = 10
        params = dict(configUpdateTimeoutSec=timeout_sec)
        response_timeout = timeout_sec + self.response_timeout
        resp = self._post('databases/dataEncryption',
                          params=params, json=payload,
                          timeout=response_timeout)
        self._raise_for_status(resp)

    #
    # Key-value APIs
    #

    def get_value(self, key):
        """
        Get the value associated with a key in the key-value store. If there is
        no value associated with it, returns None.

        :param str key: the key

        :returns str: the value associated with the key
        """

        url_encoded = quote(key, safe='')
        return self._get_data('kvstore', url_encoded).get('value')

    def set_value(self, key, value, expected_value=None, conditional=True):
        """
        Associate a key with a value.

        :param str key: the key
        :param str value: the value to associate with key
        :param str expected_value: the expected current value
        :param bool conditional: whether to require a mapping for the specified
                                 key to be absent if expected_value is None
        """

        expected_absent = expected_value is None and conditional
        payload = dict(key=key, newValue=value,
                       expectedValue=expected_value,
                       expectedAbsent=expected_absent)
        url_encoded = quote(key, safe='')
        resp = self._put('kvstore', url_encoded, json=payload)
        self._raise_for_status(resp)

    def get_effective_license(self):
        """
        Get the effective license for a server or domain.
        """

        return EffectiveLicenseInfo(self._get_data('policies', 'license'))

    def set_license(self, encoded_license, allow_downgrade=False):
        """
        Set the license for the domain.

        :param str encoded_license: the encoded license
        :param bool allow_downgrade: whether to allow license to be downgraded
        """

        return self._post_license(encoded_license, update=True,
                                  allow_downgrade=allow_downgrade)

    def check_license(self, encoded_license):
        """
        Return the license metadata encoded in the supplied encoded license.

        :param str encoded_license: the encoded license
        """

        return self._post_license(encoded_license, update=False,
                                  allow_downgrade=False)

    def _post_license(self, encoded_license, update, allow_downgrade):
        payload = dict(encodedLicense=encoded_license)
        params = dict(update=update, allowDowngrade=allow_downgrade)
        resp = self._post('policies', 'license', params=params, json=payload)
        self._raise_for_status(resp)
        return LicenseInfo(get_json(resp))

    #
    # User management APIs
    #

    def get_users(self, *order_by, **filter_by):
        """
        Get all users.

        :returns list[User]:
        """

        return list(map(
            User, self._get_all('policies/users', order_by, **filter_by)))

    def create_user(self, name, roles, password=None, certificate_pem=None):
        """
        Create a new user.

        :param str name: the name of the user
        :param list[str] roles: the roles assigned to the users
        :param str password: the password for the user, if basic authentication
                             is to be used
        :param str certificate_pem: the certificate PEM, if certificate
                                    authentication is to be used
        """

        payload = dict(name=name, roles=roles)
        if password is not None:
            payload['password'] = password
        if certificate_pem is not None:
            payload['certificatePem'] = certificate_pem
        resp = self._put('policies', 'users', name, json=payload)
        self._raise_for_status(resp)

    def delete_user(self, name):
        """
        Delete an existing user.

        :param str name: the name of the user to delete
        """

        resp = self._delete('policies', 'users', name)
        self._raise_for_status(resp)

    def update_user_roles(self, name, roles_to_add=None, roles_to_remove=None):
        """
        Update roles for a user.

        :param str name: the name of the user
        :param list[str] roles_to_add: the roles to add to the user
        :param list[str] roles_to_remove: the roles to remove from the user
        :returns User:
        """

        payload = {}
        if roles_to_add is not None:
            payload['rolesToAdd'] = roles_to_add
        if roles_to_remove is not None:
            payload['rolesToRemove'] = roles_to_remove
        resp = self._post('policies', 'users', name, 'roles', json=payload)
        self._raise_for_status(resp)
        return User(get_json(resp))

    def update_user_credentials(self, name, new_password=None,
                                new_certificate_pem=None):
        """
        Update credentials for a user.

        :param str name: the name of the user
        :param str new_password: the new password, if basic authentication is
                                 to be used
        :param str new_certificate_pem: the new certificate PEM, if certificate
                                        authentication is to be used
        """

        payload = {}
        if new_password is not None:
            payload['newPassword'] = new_password
        if new_certificate_pem is not None:
            payload['newCertificatePem'] = new_certificate_pem
        resp = self._post('policies', 'users', name, 'credentials',
                          json=payload)
        self._raise_for_status(resp)
        return User(get_json(resp))

    def get_roles(self, *order_by, **filter_by):
        """
        Get all roles.

        :returns list[Role]:
        """

        return list(map(
            Role, self._get_all('policies/roles', order_by, **filter_by)))

    def create_role(self, name, sub_roles=None, authorized_requests=None):
        """
        Create a new role.

        :param str name: the name of the role
        :param list[str] sub_roles: the sub-roles of the role
        :param list[dict] authorized_requests: policy specifications for
                                               requests authorized for role
        """

        payload = dict(name=name)
        if sub_roles is not None:
            payload['subRoles'] = sub_roles
        if authorized_requests is not None:
            payload['authorizedRequests'] = list(map(
                self._request_policy_as_dict, authorized_requests))
        resp = self._put('policies', 'roles', name, json=payload)
        self._raise_for_status(resp)

    def delete_role(self, name):
        """
        Delete an existing role.

        :param str name: the name of the role to delete
        """

        resp = self._delete('policies', 'roles', name)
        self._raise_for_status(resp)

    def update_role(self, name,
                    sub_roles_to_add=None, sub_roles_to_remove=None,
                    policies_to_add=None, policies_to_remove=None):
        """
        Update an existing role.

        :param str name: the name of the role to update
        :param list[str] sub_roles_to_add: the roles to add as sub-roles of the
                                           existing role
        :param list[str] sub_roles_to_remove: the roles to remove as sub-roles
                                              of the existing role
        :param list[dict] policies_to_add: the policy specifications to add as
                                           authorized requests
        :param list[dict] policies_to_remove: the policy specifications to
                                              remove as authorized requests
        :returns Role:
        """

        payload = {}
        if sub_roles_to_add is not None:
            payload['subRolesToAdd'] = sub_roles_to_add
        if sub_roles_to_remove is not None:
            payload['subRolesToRemove'] = sub_roles_to_remove
        if policies_to_add is not None:
            payload['policiesToAdd'] = list(map(
                self._request_policy_as_dict, policies_to_add))
        if policies_to_remove is not None:
            payload['policiesToRemove'] = list(map(
                self._request_policy_as_dict, policies_to_remove))
        resp = self._post('policies', 'roles', name, json=payload)
        self._raise_for_status(resp)
        return Role(get_json(resp))

    def _request_policy_as_dict(self, req_policy):
        if isinstance(req_policy, RequestPolicy):
            return req_policy._dict
        if isinstance(req_policy, Mapping):
            return req_policy
        raise ValueError('Unexpected type for request policy: ' +
                         type(req_policy))

    #
    # Diagnostics APIs
    #

    def log_message(self, message):
        """
        Log a message in the admin log and get server version and time.

        :param str message: message to log in admin log

        :returns dict: version and time from server
        """

        payload = dict(message=message)
        resp = self._post('diagnostics', 'log', json=payload)
        self._raise_for_status(resp)
        return get_json(resp)

    def get_admin_config(self, server_id=None):
        """
        Get the NuoAdmin server configuration. If the server ID is unspecified,
        then the configuration of the server servicing the request is returned.

        :param str server_id: the ID of the server

        :returns AdminServerConfig: the server config
        """

        resource_path = ['diagnostics', 'config']
        if server_id is not None:
            resource_path.append(server_id)
        resp = self._get(*resource_path)
        self._raise_for_status(resp)
        return AdminServerConfig(get_json(resp))

    def get_kubernetes_config(self):
        """
        Get the NuoDB specific Kubernetes configuration
        :return: KubernetesConfig: the NuoDB specific Kubernetes config
        """
        resource_path = ['diagnostics', 'kube']
        resp = self._get(*resource_path)
        self._raise_for_status(resp)
        return KubernetesConfig(get_json(resp))

    def get_admin_logs(self, dump_threads=False, include_core_files=False,
                       modified_after_epoch=None, ignore=None):
        """
        :param dump_threads: whether to dump threads of running processes
        :param include_core_files: whether to collect core files in the crash dir
        :param modified_after: collect only files that are modified after certain date
        :param ignore: regex pattern of files and directories in the log
                       directory to ignore
        :returns Downloader:
        """

        return Downloader(self._get(
            'diagnostics', 'logs',
            params=dict(includeThreadDump=dump_threads,
                        includeCoreFiles=include_core_files,
                        modifiedAfterEpochSec=modified_after_epoch,
                        ignore=ignore),
            stream=True))

    def capture_domain_state(self):
        """
        Capture the domain state of the connected admin.

        :returns dict:
        """

        resp = self._get('diagnostics', 'captureDomainState')
        self._raise_for_status(resp)
        return get_json(resp)

    def add_trusted_certificate(self, alias, certificate_pem, timeout=None):
        self._add_trusted_certificate(alias, certificate_pem, timeout=timeout)

    def remove_trusted_certificate(self, alias):
        self._add_trusted_certificate(alias, remove=True)

    def _add_trusted_certificate(self, alias, certificate_pem=None,
                                 remove=False, timeout=None):
        """
        Add a trusted certificate to all admin servers and engine processes in
        the domain.

        :param alias: the alias for the trusted certificate
        :param certificate_pem: the PEM-encoded certificate
        :param remove: whether to remove the certificate associated with the
                       alias
        :param timeout: the number of seconds to wait for an added trusted
                        certificated to be propagated to all admin servers and
                        engine processes in the domain
        """

        payload = dict(alias=alias, certificatePem=certificate_pem)
        params = dict(remove=remove)
        response_timeout = self.response_timeout
        if timeout is not None:
            params['waitFullyReplicatedSec'] = timeout
            response_timeout += timeout
        resp = self._post('peers', 'certificates', params=params,
                          json=payload, timeout=response_timeout)
        self._raise_for_status(resp)

    def get_certificate_info(self):
        """
        :returns DomainCertificateInfo:
        """

        resp = self._get('peers', 'certificates')
        self._raise_for_status(resp)
        return DomainCertificateInfo(get_json(resp))

    def _get_db_password(self, db_name):
        resp = self._get('databases', db_name,
                         params=dict(maskPassword=False))
        self._raise_for_status(resp)
        return get_json(resp).get('dbPassword')

    def _get_db_passwords(self):
        databases = self.get_databases()
        passwords = dict((db.name, self._get_db_password(db.name))
                         for db in databases)
        # filter out any databases that do not have passwords
        return dict((db_name, password)
                    for db_name, password in list(passwords.items())
                    if password is not None)

    def send_query_request(self, start_id, request_type, child_msg=None):
        session = self.get_authorized_session(start_id, service='Query')
        try:
            msg = '<Request Service="Query" Type="{}"/>'.format(request_type)
            if child_msg is not None:
                xml_msg = ElementTree.fromstring(msg)
                xml_msg.append(
                    ElementTree.fromstring(child_msg)
                    if is_string(child_msg) else
                    child_msg)
                msg = ElementTree.tostring(xml_msg)
            session.send(msg)
            return ElementTree.fromstring(session.recv())
        finally:
            session.close()

    def get_authorized_session(self, start_id, service='Admin',
                               connect_timeout=5.0, read_timeout=None):
        process = self.get_process(start_id)
        if isinstance(process, ExitedProcess):
            raise RuntimeError('Cannot connect to exited process: {}'
                               .format(process))

        if self.verify:
            options = dict(trustStore=self.verify, verifyHostname="False")
        else:
            options = None

        return _get_authorized_session(process.address,
                                       self._get_db_password(process.db_name),
                                       service,
                                       connect_timeout=connect_timeout,
                                       read_timeout=read_timeout, options=options)

    def stream_core_file(self, start_id, read_timeout=None):
        return self.stream_async_request(start_id, "GetCoreDump",
                                         read_timeout=read_timeout)

    def stream_system_dependencies(self, start_id, read_timeout=None):
        return self.stream_async_request(start_id, "GetSysDepends",
                                         read_timeout=read_timeout)

    def stream_async_request(self, start_id, request_type, connect_timeout=5.0,
                             read_timeout=None):
        # DB-29847 - It can take some time for the engine to dump the core
        # before sending it over the socket. Add option to specify
        # `read_timeout` for the session.
        session = self.get_authorized_session(
            start_id, connect_timeout=connect_timeout,
            read_timeout=read_timeout)
        try:
            # send GetCoreDump request and check that response is <Success/>
            session.send(
                '<Request Service="Admin"><Request Type="{}"/></Request>'
                .format(request_type))
            response = ElementTree.fromstring(session.recv())
            if response.find('Success') is None:
                error = response.find('Error')
                if error is None:
                    raise RuntimeError('Unexpected response: ' +
                                       ElementTree.dump(response))
                raise RuntimeError('Unable to request core dump: ' +
                                   error.get('text', 'Unknown error'))
        except Exception:
            session.close(True)
            raise
        return self._stream_receive(session)

    @staticmethod
    def _stream_receive(session):
        # stream bytes from socket until it is closed
        try:
            msg = session._Session__sock.recv(4096)
            while len(msg) != 0:
                if session._Session__cipherIn is not None:
                    msg = session._Session__cipherIn.transform(msg)
                    if sys.version[0] == '2':
                        # Data is streamed from a pynuodb session which uses the RC4Cipher, which is using latin-1 encoding.
                        # which can cause codec problems when py2 interprets the strings as ascii (default)
                        msg = msg.decode('latin-1')
                yield msg
                msg = session._Session__sock.recv(4096)
        finally:
            session.close(True)

    def _get_node_connectivity(self, start_id):
        session = self.get_authorized_session(start_id)
        try:
            session.send(
                '<Request Service="Admin"><Request Type="NodeResponsiveness"/></Request>')  # noqa
            response = ElementTree.fromstring(session.recv())
            node_info = response.find('NodeResponsiveness')
            if node_info is None:
                error = response.find('Error')
                if error is None:
                    raise RuntimeError('Unexpected response: ' +
                                       ElementTree.dump(response))
                raise RuntimeError('Unable to request node responsiveness: ' +
                                   error.get('text', 'Unknown error'))
            return [child.attrib for child in node_info]
        finally:
            session.close()

    def get_database_connectivity(self, db_name, with_node_ids=False):
        """
        Returns a tuple consisting of a dictionary representation of database
        connectivity and any errors encountered while gathering connectivity
        information.

        :param str db_name: the name of the database
        :param bool with_node_ids: whether to key connectivity graph on node ID
                                   as opposed to start ID

        :returns (dict[str, dict], dict[str, str])
        """

        processes = self.get_processes(db_name)
        # build dictionary of node ID to process
        node_id_map = dict((process.node_id, process)
                           for process in processes
                           if process.node_id is not None)
        database_connectivity = {}
        errors = {}
        for node_id, process in node_id_map.items():
            key = node_id if with_node_ids else process.start_id
            try:
                node_connectivity = self._get_node_connectivity(
                    process.start_id)
                this_node_connectivity = {}
                for peer_node in node_connectivity:
                    peer_node_id = peer_node.get('NodeId', '')
                    # skip this peer if node ID is invalid
                    if not peer_node_id.isdigit():
                        continue
                    peer_node_id = int(peer_node_id)
                    # skip this peer if node ID does not map to a known process
                    # and with_node_ids=True
                    if not with_node_ids and peer_node_id not in node_id_map:
                        continue
                    peer_key = (peer_node_id
                                if with_node_ids else
                                node_id_map[peer_node_id].start_id)
                    peer_info = {}
                    for attr_name in ['LastAckDeltaInMilliSeconds',
                                      'LastMsgDeltaInMilliSeconds']:
                        value = peer_node.get(attr_name, '')
                        attr_name = attr_name[0].lower() + attr_name[1:]
                        if value.isdigit():
                            peer_info[attr_name] = int(value)
                    this_node_connectivity[peer_key] = peer_info
                database_connectivity[key] = this_node_connectivity  # noqa
            except Exception as e:
                errors[key] = str(e)
        return database_connectivity, errors

    def get_engine_certificate_info(self, start_id):
        """
        Get engine certificate information from a management request. This is
        mostly for testing purposes.
        """

        session = self.get_authorized_session(start_id)
        try:
            session.send(
                '<Request Service="Admin"><Request Type="TLSCertificates"/></Request>')  # noqa
            response = ElementTree.fromstring(session.recv())
            return CertificateInfo.from_xml(response)
        finally:
            session.close()

    def monitor_process(self, start_id, log_options=None, engine_file=None,
                        timeout=None):
        """
        Returns a generator of messages from the process with the specified
        start ID.

        :param str start_id: the start ID of the process to monitor
        :param str log_options: the log levels and categories to enable
        :param str engine_file: write logging to a file on the engine's host
        :param float timeout: only wait for timeout seconds

        :returns generator: generator of tuples (Process, ElementTree) where
                            the first element is the monitored process and the
                            second is the message it generated.  On timeout,
                            the second tuple value will be None
        """

        process = self.get_process(start_id)

        if self.verify:
            options = dict(trustStore=self.verify, verifyHostname="False")
        else:
            options = None
        msg_stream = _monitor_process(process.address,
                                      self._get_db_password(process.db_name),
                                      log_options, engine_file, timeout,  options=options)
        return ((process, msg) for msg in msg_stream)

    def monitor_database(self, db_name=None, log_options=None, engine_file=None,
                         update_interval=5, keep_open=False, err=sys.stderr,
                         timeout=None):
        """
        Returns a generator of messages from processes in the specified
        database. If no database name is specified, then the generator returns
        messages from all processes in the domain.

        :param str db_name: the name of the database to monitor
        :param str log_options: the log levels and categories to enable
        :param str engine_file: write logging to a file on the engine's host
        :param int update_interval: the interval at which to poll database
                                    state to create process monitors
        :param bool keep_open: whether to keep generator open when there are no
                               processes to monitor
        :param file err: where to write error messages to
        :param float timeout: max time to wait for a message

        :returns generator: generator of tuples (Process, ElementTree) where
                            the first element is the monitored process and the
                            second is the message it generated.  On timeout,
                            the second tuple value will be None
        """

        if self.verify:
            options = dict(trustStore=self.verify, verifyHostname="False")
        else:
            options = None
        return _monitor_processes(lambda: self.get_processes(db_name),
                                  self._get_db_passwords, log_options,
                                  engine_file, update_interval, keep_open, err,
                                  timeout, options=options)

    #
    # Engine Diagnose APIs
    #

    class DiagnoseInfo(object):
        def __init__(self, action, target, output, output_dir, arguments,
                     timeout):
            self.action = action
            self.target = target
            self.output = output if output is not None else sys.stdout
            self.output_dir = output_dir
            self.arguments = arguments
            self.timeout = timeout

            self.request = ElementTree.Element('Request')
            self.request.attrib['action'] = action
            self.request.attrib['target'] = target

            if not arguments:
                return
            if len(arguments) % 2 != 0:
                raise RuntimeError('{} {}: invalid arguments: {}'
                                   .format(action, target, ' '.join(arguments)))

            i = 0
            while i < len(arguments):
                attrib = dict(name=arguments[i])
                text = arguments[i+1]
                i += 2

                if text.startswith('base64:'):
                    attrib['encoding'] = 'base64'
                    text = base64.b64encode(text[len('base64:'):]).decode('utf-8')

                elif text.startswith('file:'):
                    attrib['encoding'] = 'base64'
                    with open(text[len('file:'):], "r") as f:
                        text = base64.b64encode(f.read().encode('utf-8')).decode('utf-8')

                ElementTree.SubElement(self.request, 'Arg',
                                       attrib=attrib).text = text

    def diagnose_process(self, start_id, action, target, arguments=None,
                         output=None, output_dir=None, timeout=None):
        """
        Run an engine Diagnose on a process in the database.

        :param str start_id: the ID of the process to diagnose
        :param str action: the diagnose action to perform
        :param str target: the target of the diagnose action
        :param arguments list[str]: arguments to the diagnose operation
        :param file output: file to print output to (stdout if None)
        :param str output_dir: directory to write files to ("." if None)
        :param float timeout: timeout per engine in seconds

        :returns bool: True if the operation succeeded
        """

        info = AdminConnection.DiagnoseInfo(action, target, output, output_dir,
                                            arguments, timeout)
        proc = self.get_process(start_id)
        if isinstance(proc, ExitedProcess):
            raise RuntimeError('{} sid {}: Process exited:\n{}'
                               .format(proc.process.engine_type,
                                       proc.process.start_id,
                                       proc.reason))
        return self._diagnose_operation(info, proc)

    def diagnose_processes(self, db_name, action, target, arguments=None,
                           output=None, output_dir=None, timeout=None,
                           order_by='startId', **filter_by):
        """
        Run an engine Diagnose on requested processes in the database.

        :param str db_name: the name of the database to filter by
        :param str action: the diagnose action to perform
        :param str target: the target of the diagnose action
        :param arguments list[str]: arguments to the diagnose operation
        :param file output: file to print output to (stdout if None)
        :param str output_dir: directory to write files to ("." if None)
        :param float timeout: timeout per engine in seconds
        :param str order_by: field to order by
        :param dict[str,str] filter_by: map of field to regex to filter by

        :returns bool: True if all operations succeeded
        """

        if db_name is not None:
            filter_by['dbName'] = db_name
        ok = True
        info = AdminConnection.DiagnoseInfo(action, target, output, output_dir,
                                            arguments, timeout)
        for proc in map(Process, self._get_all('processes', order_by,
                                               **filter_by)):
            if not self._diagnose_operation(info, proc):
                ok = False

        return ok

    def _diagnose_operation(self, info, proc):
        session = _get_authorized_session(proc.address,
                                          self._get_db_password(proc.db_name),
                                          'Diagnose')
        try:
            session.doConnect()
            session.send(ElementTree.tostring(info.request))
            resp = ElementTree.fromstring(session.recv(timeout=info.timeout))
            if resp.tag == 'Error':
                raise RuntimeError('failed: {}'
                                   .format(resp.attrib.get('text', 'unknown')))
            if resp.tag != 'Diagnose':
                raise RuntimeError('invalid response: {}'
                                   .format(ElementTree.tostring(resp)))

            self._diagnose_handle_response(info, proc, session, resp)
            return True
        except Exception as e:
            info.output.write('{} {}: {} sid {}: {}\n'
                              .format(info.action, info.target,
                                      proc.engine_type, proc.start_id, str(e)))
            info.output.flush()
            return False
        finally:
            session.close(force=True)

    def _diagnose_handle_response(self, info, proc, session, response):
        info.output.write('{} {}: {} sid {} ({}) effective {} default {}:\n'
                          .format(response.attrib.get('action', '?'),
                                  response.attrib.get('target', '?'),
                                  proc.engine_type, proc.start_id,
                                  response.attrib.get('release', '?'),
                                  response.attrib.get('effectiveVersion', '?'),
                                  response.attrib.get('defaultVersion', '?')))

        def decode(elt):
            encoding = elt.attrib.get('encoding')
            if encoding == 'base64':
                try:
                    return base64.b64decode(elt.text).decode('utf-8')
                except TypeError as e:
                    return 'Error decoding text: {}\n{}'.format(str(e),
                                                                elt.text)
            if encoding is not None:
                return 'Unknown encoding: {}\n{}'.format(encoding, elt.text)
            return elt.text

        def gen_fname(proc, name):
            return '%s-sid%s-%s' % (proc.db_name, proc.start_id, name)

        try:
            for elt in response:
                if elt.tag == 'Output':
                    text = decode(elt)
                    info.output.write('  '+text+'\n')
                    info.output.flush()

                elif elt.tag == 'File':
                    fname = gen_fname(proc, elt.attrib.get('name'))
                    if info.output_dir:
                        fname = os.path.join(info.output_dir, fname)
                    with open(fname, 'w') as f:
                        f.write(decode(elt))
                    info.output.write('  File {} written'.format(fname))
                    desc = elt.attrib.get('description')
                    if desc:
                        info.output.write(': {}'.format(desc))
                    info.output.write('\n')

                elif elt.tag == 'Stream':
                    fname = gen_fname(proc, elt.attrib.get('name'))
                    if info.output_dir:
                        fname = os.path.join(info.output_dir, fname)
                    with open(fname, 'wb') as f:
                        for msg in self._stream_receive(session):
                            # Data is streamed from a pynuodb session which uses the RC4Cipher, which is using latin-1 encoding.
                            f.write(msg.encode('latin-1'))
                    info.output.write('  File {} written'.format(fname))
                    desc = elt.attrib.get('description')
                    if desc:
                        info.output.write(': {}'.format(desc))
                    info.output.write('\n')

                else:
                    info.output.write('Unknown response: {}\n'
                                      .format(ElementTree.tostring(elt)))
        finally:
            info.output.flush()


#
# Object model for REST API, so that we're not working with dictionaries
# everywhere and forcing users to consult the REST API docs. These wrapper
# objects are not supposed to have a comprehensive set of all the attributes,
# but a subset that is useful for scripting.
#

class Entity(object):
    """
    Base class for all entities.
    """

    def __init__(self, _dict):
        self._dict = _dict

    def __getitem__(self, name):
        """
        Implement self[name] to support dictionary lookup for attributes not
        explicitly declared.

        :param str name: the name of the attribute

        :returns object: the associated value in the backing dict
        """

        return self._dict[name]

    def __contains__(self, name):
        """
        More support for dictionary interface.

        :param str name: the name of the attribute

        :returns bool: whether the backing dict contains attribute
        """

        return name in self._dict

    def get(self, name, default=None):
        """
        Get an attribute if present.

        :param str name: the name of the attribute

        :returns object: the associated value or None
        """

        return self._dict.get(name, default)

    def set(self, name, value=None):
        """
        Set an attribute if present

        :param str name: the name of the attribute
        :param str value: the new value of the attribute
        """
        if name in self._dict:
            self._dict[name] = value

    def get_nested(self, *names):
        """
        Get an attribute of some nested dictionary.

        :param tuple[str] names: sequence of attribute names to traverse to get
                                 to the desired value

        :returns object: the associated value or None
        """

        value = self
        for name in names:
            value = value.get(name)
            if value is None:
                break
        return value

    def get_declared(self, show_all=True):
        """
        Return a dict of all the explicitly declared properties of this object.

        :returns dict[str, object]: dict of all explicitly declared properties
        """

        return dict((name, getattr(self, name))
                    for name, value in vars(type(self)).items()
                    if isinstance(value, property) and
                    (getattr(self, name) is not None or show_all))

    def __repr__(self):
        """
        Return a string representation of all explicitly declared properties.

        :returns str: string representation showing declared attributes
        """

        declared_as_str = ", ".join(
            '{}={}'.format(k, Entity._repr_value(v)) for k, v in
            sorted(self.get_declared(False).items()))
        return '{}({})'.format(type(self).__name__, declared_as_str)

    @staticmethod
    def _repr_value(value):
        if isinstance(value, list):
            return '[{}]'.format(', '.join(str(v) for v in value))
        if isinstance(value, dict):
            return '{{{}}}'.format(', '.join('{}: {}'.format(k, v)
                                             for k, v in value.items()))
        return str(value)

    def show(self, show_json=False, out=sys.stdout):
        """
        Display all explicitly declared properties and, optionally, the backing
        dictionary from REST response.

        :param bool show_json: whether to display REST response
        """

        if show_json:
            if is_string(show_json):
                json_obj = {}
                field_names = show_json.split(',')
                for name in field_names:
                    value = self._get_field_value(self._dict, name)
                    if value is not None:
                        json_obj[name] = value
            else:
                json_obj = self._dict
            out.write('{}\n'.format(dumps(json_obj, indent=2,
                                          sort_keys=True)))
        else:
            out.write('{}\n'.format(self))

    @classmethod
    def _get_field_value(cls, json_obj, field_name):
        if '.' not in field_name:
            return json_obj.get(field_name)
        first, rest = field_name.split('.', 1)
        json_subobj = json_obj.get(first)
        if json_subobj is not None:
            return cls._get_field_value(json_subobj, rest)


class AdminException(Entity, Exception):

    def __init__(self, _dict):
        Entity.__init__(self, _dict)
        Exception.__init__(self, ': '.join(self.messages))

    @property
    def code(self):
        """
        :returns int:
        """

        return self.get('code')

    @property
    def messages(self):
        """
        :returns list[str]:
        """

        messages = list(self.get('messages', []))
        if len(messages) == 0:
            message = self.get('message')
            details = self.get('details')
            if message is not None and details is not None:
                return ['{}: {}'.format(message, details)]
            elif message is not None:
                return [message]
        return messages


class AdminTimeoutException(Entity, Exception):
    """
    Exception thrown if NuoAdmin REST service returns status code of 408
    (Request Timeout) and holding the response payload.
    """
    def __init__(self, _dict):
        Entity.__init__(self, _dict)
        Exception.__init__(self, 'Timeout')
        self._code = requests.codes.timeout

    @property
    def code(self):
        return self._code


class Server(Entity):
    """
    Object containing server metadata.
    """

    @property
    def id(self):
        """
        :returns str:
        """

        return self.get('id')

    @property
    def address(self):
        """
        :returns str:
        """

        return self.get('address')

    @property
    def peer_state(self):
        """
        The peer membership state of the server
        server.

        :returns str:
        """

        return self.get('peerState')

    @property
    def role(self):
        """
        The role of the server, known only on the connected server.

        :returns str:
        """

        return self.get_nested('localRoleInfo', 'role')

    @property
    def raft_state(self):
        """
        The raft life-cycle state of the server, known only on the connected
        server.

        :returns str:
        """

        return self.get_nested('localRoleInfo', 'lifeCycle')

    @property
    def leader(self):
        """
        The leader known to this server, known only on the connected server.

        :returns str:
        """

        return self.get_nested('localRoleInfo', 'leaderServerId')

    @property
    def connected_state(self):
        """
        The connected state of this server from the point of view of the
        connected server.

        :returns str:
        """

        return self.get_nested('connectedState', 'state')

    @property
    def is_connected(self):
        """
        Whether the server is connected to the local server.

        :returns bool:
        """

        return self.connected_state == 'Connected'

    @property
    def is_evicted(self):
        """
        Whether the server is evicted from Raft consensus from the point of
        view of the connected server.

        :returns bool:
        """

        return self.get('isEvicted', False)

    @property
    def observed_state(self):
        """
        Combination of connected and evicted state of this server from the
        point of view of the connected server, with evicted state taking
        precedence over connected state.

        :returns str:
        """

        if self.is_evicted:
            return 'Evicted'
        return self.get_nested('connectedState', 'state')

    @property
    def log_term(self):
        """
        The term of the last command in the server's raft log.

        :returns int:
        """

        return self.get_nested('localRoleInfo', 'localPeerTermIndexInfo',
                               'logLastTerm')

    @property
    def commit_index(self):
        """
        The commit index for the server, i.e. the index of the last raft
        command applied to server's state machine.

        :returns int:
        """

        return self.get_nested('localRoleInfo', 'localPeerTermIndexInfo',
                               'commitIndex')

    @property
    def log_index(self):
        """
        The index of the last command in the server's raft log.

        :returns int:
        """

        return self.get_nested('localRoleInfo', 'localPeerTermIndexInfo',
                               'logLastIndex')

    @property
    def is_local(self):
        """
        Returns true iff this server is the one that serviced the REST request
        that it was derived from.

        :param bool:
        """

        return self.get('isLocal', False)

    @property
    def ping_latency(self):
        """
        The ping latency in nanoseconds to this server from the connected
        server.

        :returns int:
        """

        ret = self.get_nested('connectedState', 'latency')
        # response has -1 to signal unknown; don't return it
        if ret is not None and ret >= 0:
            return ret

    @property
    def last_ack(self):
        """
        The time in seconds since the last ping response from this server to
        the connected server.

        :returns float:
        """

        ret = self.get_nested('connectedState', 'lastAckDeltaMillis')
        # response has -1 to signal unknown; don't return it
        if ret is not None and ret >= 0:
            return ret / 1e3

    @property
    def version(self):
        """
        The version of the server.

        :returns str:
        """

        return self.get('version')

    @property
    def full_version(self):
        """
        The version of the server including supported features.

        :returns str:
        """

        return self.get('fullVersion', self.version)


class StartProcessRequest(Entity):
    """
    Object containing fields for a start-process request.
    """

    @property
    def db_name(self):
        """
        :returns str:
        """

        return self.get('dbName')

    @property
    def server_id(self):
        """
        :returns str:
        """

        return self.get('host')

    @property
    def engine_type(self):
        """
        :returns str:
        """

        return self.get('engineType')

    @property
    def archive_id(self):
        """
        :returns int:
        """

        return self.get('archiveId')

    @property
    def labels(self):
        """
        :returns dict:
        """

        return self.get('labels', {})

    @property
    def options(self):
        """
        :returns dict[str, str]:
        """

        return self.get('overrideOptions', {})

    @property
    def expected_incarnation_major(self):
        """
        :returns int:
        """

        return self.get_nested('expectedIncarnation', 'major')

    @property
    def expected_incarnation_minor(self):
        """
        :returns int:
        """

        return self.get_nested('expectedIncarnation', 'minor')

    def __eq__(self, other):
        if not isinstance(other, StartProcessRequest):
            return False
        return self._dict == other._dict


class Process(Entity):
    """
    Object containing process metadata.
    """

    @property
    def start_id(self):
        """
        :returns str:
        """

        return self.get('startId')

    @property
    def db_name(self):
        """
        :returns str:
        """

        return self.get('dbName')

    @property
    def server_id(self):
        """
        :returns str:
        """

        return self.get('host')

    @property
    def durable_state(self):
        """
        :returns str:
        """

        return self.get('durableState')

    @property
    def archive_id(self):
        """
        :returns int:
        """

        return self.get('archiveId')

    @property
    def region_name(self):
        """
        :returns str:
        """

        return self.get('regionName')

    @property
    def engine_type(self):
        """
        :returns str:
        """

        return self.get('type')

    @property
    def engine_state(self):
        """
        :returns str:
        """

        return self.get('state', 'UNKNOWN')

    @property
    def node_id(self):
        """
        :returns int:
        """

        return self.get('nodeId')

    @property
    def address(self):
        """
        :returns str:
        """

        port = self.get('port')
        if port is None:
            return None

        hostname = self.get('hostname')
        address = self.get('address')
        if hostname == address or address is None:
            return '{}:{}'.format(hostname, port)

        if self.ipVersion == 'v6':
            addressAndPort = '[{}]:{}'.format(address, port)
        else:
            addressAndPort = '{}:{}'.format(address, port)

        if hostname is None:
            return addressAndPort
        return '{}/{}'.format(hostname, addressAndPort)

    @property
    def ipVersion(self):
        return self.options.get('ip-version', 'v4')

    @property
    def pid(self):
        """
        :returns int:
        """

        return self.get('pid')

    @property
    def last_ack(self):
        """
        The time in seconds since the last message sent by this process to its
        connected admin process.

        :returns float:
        """

        ret = self.get_nested('lastHeardFrom')
        if ret is not None:
            return ret / 1e3

    @property
    def journal_recovery_progress(self):
        """
        :returns float:
        """

        return self.get_nested('journalRecoveryProgress')

    @property
    def options(self):
        """
        :returns dict:
        """
        return self.get('options', {})

    @property
    def labels(self):
        """
        :returns dict:
        """
        return self.get('labels', {})


class ExitedProcess(Entity):
    """
    Object containing process tombstone metadata.
    """

    @property
    def db_incarnation(self):
        """
        :returns (int, int):
        """

        major = self.get_nested('databaseIncarnation', 'major')
        minor = self.get_nested('databaseIncarnation', 'minor')
        return (major, minor)

    @property
    def reason(self):
        """
        :returns str:
        """

        return self.get('reason')

    @property
    def has_disconnected(self):
        """
        :returns bool:
        """

        return self.get('hasDisconnected', False)

    @property
    def exit_code(self):
        """
        :returns int:
        """

        return self.get('exitCode')

    @property
    def process(self):
        """
        :returns Process:
        """

        return Process(self.get('process', {}))


class Database(Entity):
    """
    Object containing database metadata.
    """

    @property
    def name(self):
        """
        :returns str:
        """

        return self.get('name')

    @property
    def state(self):
        """
        :returns str:
        """

        return self.get('state')

    @property
    def incarnation(self):
        """
        :returns (int, int):
        """

        major = self.get_nested('incarnation', 'major')
        minor = self.get_nested('incarnation', 'minor')
        return (major, minor)

    @property
    def default_region_id(self):
        """
        :returns int:
        """

        return self.get('defaultRegionId')

    @property
    def server_assignments(self):
        """
        :returns dict[str, int]:
        """

        return self.get('hostAssignments', {})

    @property
    def default_options(self):
        """
        :returns dict[str, str]:
        """

        return self.get('databaseOptions', {})


class HotCopyResponse(Entity):
    """
    Object containing result of hot-copy request.
    """

    @property
    def id(self):
        """
        :returns str:
        """

        return self.get('hotCopyId')

    @property
    def coordinator_start_id(self):
        """
        :returns str:
        """

        return self.get('coordinatorStartId')

    @property
    def begin_timestamp(self):
        """
        :returns str:
        """

        return self.get('beginTimestamp')

    @property
    def end_timestamp(self):
        """
        :returns str:
        """

        return self.get('endTimestamp')

    @property
    def timestamp_range(self):
        """
        :returns str:
        """

        if self.begin_timestamp:
            if self.end_timestamp:
                # trim end timestamp if it is has the same date as begin timestamp
                datestr_len = len('yyyy-mm-dd ')
                if self.begin_timestamp[:datestr_len] == self.end_timestamp[:datestr_len]:
                    end_timestamp = self.end_timestamp[datestr_len:]
                else:
                    end_timestamp = self.end_timestamp
                return '{}..{}'.format(self.begin_timestamp, end_timestamp)

            return self.begin_timestamp + '...'

    @property
    def status(self):
        """
        :returns str:
        """

        return self.get('status')

    @property
    def message(self):
        """
        :returns str:
        """

        return self.get('message')


class HotCopyDatabaseStatuses(Entity):
    """
    Object containing hotcopy status information.
    """

    @property
    def errors(self):
        """
        :returns dict[str, str]:
        """

        return self.get('errors')

    @property
    def hotcopy_statuses(self):
        """
        :returns dict[str, HotCopyResponse]:
        """

        return dict((start_id, list(map(HotCopyResponse, hotcopy_statuses)))
                    for start_id, hotcopy_statuses
                    in self.get('hotCopyStatuses').items())


class EffectiveLicenseInfo(Entity):
    """
    Object containing information about the effective license.
    """

    @property
    def decoded_license(self):
        """
        :returns LicenseInfo:
        """

        value = self.get('decodedLicense')
        if value is None:
            return None
        return LicenseInfo(value)

    @property
    def encoded_license(self):
        """
        :returns str:
        """

        return self.get('encodedLicense')

    @property
    def effective_for_domain(self):
        """
        :returns bool:
        """

        return self.get('effectiveForDomain', False)

    @property
    def license_file(self):
        """
        :returns str:
        """

        return self.get('licenseFile')


class LicenseInfo(Entity):
    """
    Object containing license metadata.
    """

    @property
    def type(self):
        """
        :returns str:
        """

        return self.get('type')

    @property
    def holder(self):
        """
        :returns str:
        """

        return self.get('holder')

    @property
    def expires(self):
        """
        :returns str:
        """

        return self.get('expires')


class User(Entity):
    """
    Object containing user metadata.
    """

    @property
    def name(self):
        """
        :returns str:
        """

        return self.get('name')

    @property
    def roles(self):
        """
        :returns list[str]:
        """

        return self.get('roles', [])


class Role(Entity):
    """
    Object containing role metadata.
    """

    @property
    def name(self):
        """
        :returns str:
        """

        return self.get('name')

    @property
    def sub_roles(self):
        """
        :returns list[str]:
        """

        return self.get('subRoles', [])

    @property
    def authorized_requests(self):
        """
        :returns list[RequestPolicy]:
        """

        return list(map(RequestPolicy, self.get('authorizedRequests', [])))


class RequestPolicy(Entity):
    """
    Object containing request policy specification.
    """

    @property
    def method(self):
        """
        :returns str:
        """

        return self.get('method')

    @property
    def url(self):
        """
        :returns str:
        """

        return self.get('url')

    @property
    def query_param_constraints(self):
        """
        :returns dict[str, str]:
        """

        return self.get('queryParamConstraints', {})

    @property
    def path_param_constraints(self):
        """
        :returns dict[str, str]:
        """

        return self.get('pathParamConstraints', {})

    @property
    def payload_param_constraints(self):
        """
        :returns dict[str, str]:
        """

        return self.get('payloadParamConstraints', {})


class LoadBalancerPolicy(Entity):
    """
    Object containing load-balancer policy metadata.
    """

    @property
    def policy_name(self):
        """
        :returns str:
        """

        return self.get('policyName')

    @property
    def lb_query(self):
        """
        :returns str:
        """

        return self.get('lbQuery')


class LoadBalancerConfig(Entity):
    """
    Object containing load-balancer configuration.
    """

    @property
    def is_global(self):
        """
        :returns bool:
        """

        return self.get('isGlobal')

    @property
    def db_name(self):
        """
        :returns str:
        """

        return self.get('dbName')

    @property
    def default(self):
        """
        :returns str:
        """

        return self.get('defaultLbQuery')

    @property
    def prefilter(self):
        """
        :returns str:
        """

        return self.get('prefilter')


class Region(Entity):
    """
    Object containing region metadata.
    """

    @property
    def id(self):
        """
        :returns int:
        """

        return self.get('id')

    @property
    def name(self):
        """
        :returns str:
        """

        return self.get('name')


class Archive(Entity):
    """
    Object containing archive metadata.
    """

    def __init__(self, _dict, id=None):
        super(Archive, self).__init__(_dict)
        self._id = id

    @property
    def id(self):
        """
        :returns int:
        """

        return self.get('id')

    @property
    def db_name(self):
        """
        :returns str:
        """

        return self.get('dbName')

    @property
    def server_id(self):
        """
        :returns str:
        """

        return self.get('host')

    @property
    def archive_path(self):
        """
        :returns str:
        """

        return self.get('path')

    @property
    def journal_path(self):
        """
        :returns str:
        """

        return self.get('journalPath')

    @property
    def snapshot_archive_path(self):
        """
        :returns str:
        """

        return self.get('snapshotArchivePath')

    @property
    def state(self):
        """
        :returns str:
        """

        return self.get('state')

    @property
    def observer_storage_groups(self):
        """
        :returns list[str]:
        """

        return self.get('observerStorageGroups', [])

    @property
    def is_observer(self):
        """
        :returns bool
        """

        return (self.observer_storage_groups is not None and
                len(self.observer_storage_groups) != 0)

class StorageGroup(Entity):
    """
    Object containing storage group metadata.
    """

    @property
    def db_name(self):
        """
        :returns str:
        """

        return self.get('dbName')

    @property
    def name(self):
        """
        :returns str:
        """

        return self.get('sgName')

    @property
    def id(self):
        """
        :returns int:
        """

        return self.get('sgId')

    @property
    def state(self):
        """
        :returns str:
        """

        return self.get('state')

    @property
    def archive_states(self):
        """
        :returns dict[str, str]:
        """

        return self.get('archiveStates', {})

    @property
    def process_states(self):
        """
        :returns dict[str, str]:
        """

        return self.get('processStates', {})

    @property
    def leader_candidates(self):
        """
        :returns list[str]:
        """

        return self.get('leaderCandidates', [])


class AdminServerConfig(Entity):
    """
    Object containing the NuoAdmin server configuration.
    """

    @property
    def properties(self):
        """
        :returns dict[str, str]:
        """
        return self.get('properties', {})

    @property
    def initial_membership(self):
        """
        :returns dict[str, dict[str, str]]:
        """
        return self.get('initialMembership', {})

    @property
    def other_services(self):
        """
        :returns list[str]:
        """
        return self.get('otherServices', [])

    @property
    def stats_plugins(self):
        """
        :returns dict[str, list[dict[str, str]]]:
        """
        return self.get('statsPlugins', {})

    @property
    def load_balancers(self):
        """
        :returns dict[str, dict[str, str]]:
        """
        return self.get('loadBalancers', {})


class DomainCertificateInfo(Entity):
    """
    Object containing domain certificate information.
    """

    @property
    def trusted_certificates(self):
        return dict(
            (alias, CertificateInfo(cert_info)) for alias, cert_info
            in self.get('trustedCertificates', {}).items())

    @property
    def server_certificates(self):
        return dict(
            (server_id, CertificateInfo(cert_info)) for server_id, cert_info
            in self.get('serverCertificates', {}).items())

    @property
    def process_certificates(self):
        return dict(
            (start_id, CertificateInfo(cert_info)) for start_id, cert_info
            in self.get('processCertificates', {}).items())

    @property
    def server_trusted(self):
        return self.get('serverTrusted', [])

    @property
    def process_trusted(self):
        return self.get('processTrusted', [])


class CertificateInfo(Entity):
    """
    Object containing certificate information.
    """

    @property
    def certificate_pem(self):
        return self.get('certificatePem')

    @property
    def expires(self):
        return self.get('expires')

    @property
    def subject_name(self):
        return self.get('subjectName')

    @property
    def issuer_name(self):
        return self.get('issuerName')


class KubernetesConfig(Entity):
    @property
    def stateful_sets(self):
        return self.get('statefulsets', {})

    @property
    def daemon_sets(self):
        return self.get('daemonSets', {})

    @property
    def deployments(self):
        return self.get('deployments', {})

    @property
    def pods(self):
        return self.get('pods', {})

    @property
    def volumes(self):
        return self.get('volumes', {})


class EngineCertificateInfo(Entity):
    """
    Object containing certificate information from an engine management
    request.
    """

    @staticmethod
    def from_xml(response_xml):
        cert_info = response_xml.find('TLSCertificates')
        domain_ca_certs = cert_info.find('DomainCAs').getchildren()
        engine_cert = cert_info.find('EngineCertificate')
        intermediate_certs = None
        if cert_info.find('IntermediateCertificates') is not None:
            intermediate_certs = cert_info.find(
                'IntermediateCertificates').getchildren()
        cert_info_dict = dict(
            DomainCAs=[xml_to_json(cert) for cert in domain_ca_certs],
            EngineCertificate=xml_to_json(engine_cert))
        if intermediate_certs is not None:
            cert_info_dict['IntermediateCertificates'] = [
                xml_to_json(cert) for cert in intermediate_certs]
        return EngineCertificateInfo(cert_info_dict)

    @property
    def domain_ca_certificates(self):
        """
        :returns list[str]:
        """

        domain_ca_certs = self.get('DomainCAs', [])
        return [base64.decodestring(cert.get('value', ''))
                for cert in domain_ca_certs]

    @property
    def intermediate_certificates(self):
        """
        :returns list[str]:
        """

        intermediate_certs = self.get('IntermediateCertificates', [])
        return [base64.decodestring(cert.get('value', ''))
                for cert in intermediate_certs]

    @property
    def engine_certificate(self):
        """
        :returns str:
        """

        engine_cert = self.get('EngineCertificate')
        if engine_cert is not None:
            return base64.decodestring(engine_cert.get('value', ''))


class ReportTimestamp(Entity):
    """
    Object containing information from active/passive handoff's
    report-timestamp
    """
    TIMESTAMP_FORMAT_STRING = '%Y-%m-%dT%H:%M:%S'
    TIMESTAMP_EXAMPLE_STRING = 'YYYY-MM-DDThh:mm:ss'

    @property
    def timestamp(self):
        """
        :returns str
        """
        return self.get('timestamp')

    @property
    def commits(self):
        """
        :returns str
        """
        return self.get('commits')

    @property
    def epoch(self):
        """
        :returns int
        """
        return self.get('epoch')

    @property
    def leaders(self):
        """
        :returns str
        """
        return self.get('leaders')


class DatabaseVersions(Entity):
    """
    Object containing database version information and
    process version association.
    """

    @property
    def db_name(self):
        """
        :returns str
        """
        return self.get('dbName')

    @property
    def effective_version(self):
        """
        :returns str
        """
        return self.get('effectiveVersion')

    @property
    def max_version(self):
        """
        :returns str
        """
        return self.get('maxVersion')

    @property
    def version(self):
        """
        :returns str
        """
        return self.get('version')

    @property
    def all_versions(self):
        """
        :returns dict[str, object]
        """
        return self.get('allVersions', {})

    @property
    def available_versions(self):
        """
        :returns list[object]
        """
        return self.get('availableVersions', [])

    @property
    def process_versions(self):
        """
        :returns dict[str, object]
        """
        return self.get('processVersions', {})
