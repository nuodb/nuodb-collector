#!/usr/bin/env python
# (C) Copyright NuoDB, Inc. 2018-2021  All Rights Reserved.

import argparse
import base64
import datetime
import getpass
import hashlib
import imp
import inspect
import json
import os
import re
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import textwrap
import time
import threading
import traceback
import uuid
import zipfile

from xml.etree import ElementTree

try:
    # python 2 imports
    from StringIO import StringIO
    import __builtin__ as builtins
except ImportError:
    # python 3 imports
    from io import StringIO
    import builtins


try:
    import requests
    from . import nuodb_mgmt
    requests_installed = True
except ImportError as e:
    # we expect an ImportError for requests if it is not installed; re-raise if
    # that is not the cause.

    # The .* here is used to match the quote around the module name in python3
    if re.search(r"No module named .*\brequests\b", str(e)) is None:
        raise e

    # we want to be able to do all checking in main method;
    # set nuodb_mgmt to be a dummy object that just raises this ImportError
    # whenever an attribute is accessed on it
    class _DelayImportError(object):
        def __init__(self, err):
            self.err = err
        def __getattr__(self, name):
            raise self.err

    nuodb_mgmt = _DelayImportError(e)
    requests_installed = False

try:
    # use argcomplete if it is available to enable tab-completion
    import argcomplete

    class NonrepeatingCompleter(argcomplete.CompletionFinder):
        """
        CompletionFinder subclass that removes arguments already specified.
        """

        def collect_completions(self, active_parsers, parsed_args,
                                cword_prefix, debug):
            # get completions from superclass and filter out any repeats

            # silence bogus pylint error, 'Use of super on an old style class'
            # pylint: disable=E1002
            completions = super(
                NonrepeatingCompleter, self).collect_completions(
                    active_parsers, parsed_args, cword_prefix, debug)
            return [arg for arg in completions
                    if not NonrepeatingCompleter._is_repeat(
                            arg, parsed_args, active_parsers[-1])]

        @staticmethod
        def _is_repeat(arg, parsed_args, parser):
            # ignore any tokens that don't have prefix
            if not arg.startswith('--'):
                return False
            dest = arg.lstrip('-').replace('-', '_')
            # since argparse injects defaults, we have to check that the value
            # is different from the default to see if it has been specified
            default = parser.get_default(dest)
            return getattr(parsed_args, dest, default) != default

    autocomplete = NonrepeatingCompleter()
    argcomplete_installed = True
except ImportError:
    # argcomplete is not available, so define autocomplete to be a no-op
    def autocomplete(parser):
        pass

    argcomplete_installed = False

try:
    from pynuodb import entity
except ImportError as e:
    class _NotAvailable(object):
        def __init__(self, msg):
            self.msg = msg

        def __getattr__(self, name, default=None):
            raise RuntimeError('pynuodb module is not available: ' + self.msg)

    entity = _NotAvailable(str(e))

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


__home_dir = None


def check_version():
    major, minor, micro = sys.version_info[:3]
    if (major not in [2,3]) or (major == 2 and minor < 7) or (major == 3 and minor < 6) :
        sys.stderr.write('NuoDB Management CLI requires Python 2.7 or Python 3.6+\n')
        sys.stderr.write('Invoked with Python version {}.{}.{} ({})\n'.format(
            major, minor, micro, sys.executable))
        sys.exit(1)


def get_home_dir():
    global __home_dir
    if __home_dir is None:
        if 'NUODB_HOME' in os.environ:
            __home_dir = os.environ['NUODB_HOME']
        elif 'NUOCLIENT_HOME' in os.environ:
            __home_dir = os.environ['NUOCLIENT_HOME']
        else:
            # No env.var. set so search for bin/nuocmd
            base = os.path.dirname(os.path.abspath(__file__))
            while True:
                if os.path.isfile(os.path.join(base, 'bin', 'nuocmd')):
                    __home_dir = base
                    break
                old = base
                base = os.path.dirname(old)
                if old == base:
                    break
    return __home_dir


def from_nuodb_home(*args):
    home_dir = get_home_dir()
    return os.path.join(home_dir, *args) if home_dir else None


def get_java_version():
    out = subprocess.check_output(['java', '-version'], stderr=subprocess.STDOUT, universal_newlines=True)
    m = re.match('(java|openjdk) version "([0-9]+)[.]([0-9]+).*"', out)

    # assume Java 11 if we are unable to parse version
    if not m:
        return 11

    # starting with Java 11, sematic versioning is used; prior to that,
    # versions were prefixed with '1.', e.g. 8 is shown is 1.8
    if m.group(2) != '1':
        return int(m.group(2))
    else:
        return int(m.group(3))


def get_java_opts():
    # DB-32007: this suppresses "WARNING: All illegal access operations will be
    # denied in a future release"; these messages are emitted due to the Guice
    # library, which has not addressed the issue as of release 4.2.3; see
    # https://github.com/google/guice/issues/1133
    # DB-31159: also allow reflection to be used to obtain file descriptor from
    # FileChannel object
    if get_java_version() >= 11:
        return ['--add-opens', 'java.base/java.lang=ALL-UNNAMED',
                '--add-opens', 'java.base/java.io=ALL-UNNAMED',
                '--add-opens', 'java.base/sun.nio.ch=ALL-UNNAMED']
    return []


def from_root(*args):
    root = os.path.abspath('.')
    while root != os.path.dirname(root):
        root = os.path.dirname(root)
    return os.path.join(root, *args)


def get_raftlog_dir():
    if 'NUODB_VARDIR' in os.environ:
        return os.environ['NUODB_VARDIR']

    for vardir in [from_root('var', 'opt', 'nuodb'),
                   from_nuodb_home('var', 'opt')]:
        if vardir is not None and os.path.exists(os.path.join(vardir, 'raftlog')):
            return vardir


def get_install_commands():
    # if setup script does not exist, there is nothing to do
    nuocmd_dir = os.path.dirname(os.path.dirname(__file__))
    setup_script = os.path.join(nuocmd_dir, 'setup.py')
    if not os.path.exists(setup_script):
        return []
    install_cmd = '{} -m pip install "{}[completion]"'.format(
        sys.executable, nuocmd_dir)
    try:
        # if pip is already installed, just invoke it with the same Python
        # interpreter this is running in (we're assuming nuocmd chose it
        # because its the most suitable one) to install all dependencies
        imp.find_module('pip')
        return [install_cmd]
    except ImportError:
        # otherwise the user has to download pip and install it; also
        # install all nuocmd dependencies
        major, minor, micro = sys.version_info[:3]
        if micro >= 9:
            # ensurepip introduced in version 2.7.9
            return [sys.executable + ' -m ensurepip', install_cmd]
        else:
            # ensurepip not available, so use get-pip.py
            return ['curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py',
                    sys.executable + ' get-pip.py', install_cmd]


def check_dependencies():
    # requests is the only required non-standard dependency; since we're
    # requiring Python 2.7, we know that argparse is available
    if not requests_installed:
        sys.stderr.write('NuoDB Management CLI requires \'requests\' module\n')
        install_commands = get_install_commands()
        if len(install_commands) != 0:
            sys.stderr.write('To install dependencies run the following:\n')
            for command in install_commands:
                sys.stderr.write('  {}\n'.format(command))
        sys.exit(1)


def exceptional_fn(callable):
    def catch_exc(*args, **kwargs):
        try:
            callable(*args, **kwargs)
        except Exception:
            threading.current_thread().exc = sys.exc_info()
    return catch_exc


class ShowInstallCommandsAction(argparse.Action):
    """
    Action invoked if --show-install-commands is specified. Displays commands
    to install dependencies and exit.
    """

    def __init__(self, option_strings, dest=argparse.SUPPRESS,
                 default=argparse.SUPPRESS, help=None):
        super(ShowInstallCommandsAction, self).__init__(
            option_strings=option_strings, dest=dest, default=default, nargs=0,
            help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        for line in get_install_commands():
            print(line)
        sys.exit(0)


class CustomHelpFormatter(argparse.RawTextHelpFormatter):
    """
    HelpFormatter that shows defaults similarly to
    argparse.ArgumentDefaultsHelpFormatter but with some minor tweaks.
    """

    def _get_help_string(self, action):
        help = '\n'.join(textwrap.wrap(action.help))
        if (action.default and '%(default)' not in action.help and
            action.default is not argparse.SUPPRESS and
            action.dest not in ['password', 'dba_password']):  # noqa
            defaulting_nargs = [argparse.OPTIONAL, argparse.ZERO_OR_MORE]
            if action.option_strings or action.nargs in defaulting_nargs:
                if (is_string(action.default) and
                    len(action.default.split()) > 1):  # noqa
                    # default value is a string that has spaces in it; add
                    # quotes around it for clarity
                    help += '\n(default: \'%(default)s\')'
                else:
                    help += '\n(default: %(default)s)'
        return help


class Subcommand(object):
    """
    Decorator applied to methods of AdminCommands to associate them with
    subcommands.
    """

    def __init__(self, action, entity, **kwargs):
        """
        :param str action: the action
        :param str entity: the object to perform action on
        :param dict kwargs: kwargs applicable to ArgumentParser.add_parser()
        """

        self.action = action
        self.entity = entity
        self.kwargs = kwargs

    def __call__(self, func):
        func._is_subcommand = True
        func._action = self.action
        func._entity = self.entity
        func._kwargs = self.kwargs
        return func

    @staticmethod
    def add_subcommand(processor, func):
        if not getattr(func, '_is_subcommand', False):
            return

        if func._action not in processor.sp_dict:
            processor.sp_dict[func._action] = processor.subparsers.add_parser(
                func._action).add_subparsers(
                    title='\'{}\' subcommands'.format(func._action))
        parser = processor.sp_dict[func._action].add_parser(
            func._entity, formatter_class=CustomHelpFormatter, **func._kwargs)
        processor.sp_dict[(func._action, func._entity)] = (parser, func)
        # associate `func` with the function to invoke for this subcommand
        parser.set_defaults(func=func)
        # add arguments that the function was decorated with
        for arg_spec in Argument.get_arg_specs(func):
            arg_spec.add_argument(parser)


class EnvironmentalDefault(object):

    ENV_PREFIX = 'NUOCMD_'

    def __init__(self, default=None):
        """
        :param object default: the default to use if there is no value inferred
                               from the environment
        """

        self.default = default

    def get_env_vars(self, arg_names):
        env_vars = []
        for arg_name in arg_names:
            if not arg_name.startswith('--'):
                continue
            field_name = arg_name[2:].replace('-', '_')
            env_vars.append(EnvironmentalDefault.ENV_PREFIX + field_name.upper())
        return env_vars

    def __call__(self, arg_names):
        for env_var in self.get_env_vars(arg_names):
            if env_var in os.environ:
                return os.environ[env_var]
        return self.default


class Argument(object):
    """
    Decorator to apply to methods to specify arguments. All arguments
    appearing in the signature of the method must have a corresponding
    Argument decorator.

    Example:

    ```
    @Subcommand('do', 'something')
    @Argument('--the-arg')
    def do_something(self, the_arg):
        ...
    ```

    """

    def __init__(self, *args, **kwargs):
        """
        :param tuple args: args applicable to ArgumentParser.add_argument()
        :param dict kwargs: kwargs applicable to ArgumentParser.add_argument()
        """

        self.args = args
        self.kwargs = kwargs

    def __call__(self, func):
        func.arg_specs = [self] + Argument.get_arg_specs(func)
        return func

    def add_argument(self, parser):
        kwargs, completer = self._get_kwargs()
        ret = parser.add_argument(*self.args, **kwargs)
        # add value completer if one was specified
        if completer is not None:
            ret.completer = completer

    def _get_kwargs(self):
        kwargs = self.kwargs.copy()
        default = kwargs.get('default')
        if isinstance(default, EnvironmentalDefault):
            kwargs['default'] = default(self.args)
            # if a default was inferred, remove required=True so that the
            # parameter does not have to be explicitly specified
            if kwargs['default'] is not None and kwargs.get('required', False):
                del kwargs['required']
        completer = kwargs.pop('completer', None)
        return kwargs, completer

    @staticmethod
    def get_arg_specs(func):
        return getattr(func, 'arg_specs', [])

    def _expand(self):
        if self.kwargs.get('help') == argparse.SUPPRESS:
            return []
        return [self]

    @staticmethod
    def expand_list(arg_specs):
        expanded = []
        for arg_spec in arg_specs:
            expanded.extend(arg_spec._expand())
        return expanded


class MutuallyExclusive(Argument):
    """
    Decorator to wrap multiple Arguments to specify that the arguments are
    mutually exclusive.

    Example:

    ```
    @Subcommand('do', 'something')
    @MutuallyExclusive(Argument('--this'), Argument('--that'))
    def do_something(self, this=None, that=None):
        # either this or that is specified, but not both
        ...
    ```

    """

    def __init__(self, *arg_specs, **kwargs):
        self.arg_specs = arg_specs
        self.kwargs = kwargs

    def add_argument(self, parser):
        group = parser.add_mutually_exclusive_group(**self.kwargs)
        for arg_spec in self.arg_specs:
            arg_spec.add_argument(group)

    def _expand(self):
        return Argument.expand_list(self.arg_specs)


class TimeUnit(object):

    SECOND = 1
    MINUTE = 60
    HOUR = 60 * MINUTE
    DAY = 24 * HOUR

    UNITS = {'s': SECOND,
             'm': MINUTE,
             'h': HOUR,
             'd': DAY}

    PATTERN = '([0-9]+)([smhd]?)'

    def __init__(self, value, default_suffix='s'):
        m = re.match(self.PATTERN, str(value))
        if not m:
            raise TypeError('"{}" does not match pattern "{}"'.format(value, self.PATTERN))

        self.value = int(m.group(1))
        self.suffix = m.group(2)
        if not self.suffix:
            self.suffix = default_suffix

    @property
    def seconds(self):
        return self.value * self.UNITS[self.suffix]


class CommandProcessor(object):
    """
    The main class used to build argument parser and execute commands.
    """

    DEFAULT_API_SERVER = 'localhost:8888'
    FOREVER = 9999999999
    DEFAULT_DATETIME_FORMAT_STRING = '%Y-%m-%dT%H:%M:%S'
    DEFAULT_DATETIME_EXAMPLE_STRING = 'YYYY-MM-DDThh:mm:ss'

    @classmethod
    def _get_admin_conn(cls, parsed_args):
        client_key = parsed_args.client_key
        basic_creds = parsed_args.basic_creds
        verify_server = parsed_args.verify_server
        if client_key is not None and basic_creds is not None:
            # if different types of credentials are specified, credentials are
            # chosen based on the following precedence rules:
            # 1. if one credential type is specified explicitly and the other
            #    is resolved from an environment variable; prefer the
            #    explicitly-specified one
            # 2. if --client-key and --basic-creds were both specified
            #    explicitly, prefer --basic-creds
            # 3. if --client-key and --basic-creds were both resolved from
            #    environment variables (NUOCMD_CLIENT_KEY and
            #    NUOCMD_BASIC_CREDS, respectively), prefer
            #    --basic-creds/NUOCMD_BASIC_CREDS
            if basic_creds != os.environ.get('NUOCMD_BASIC_CREDS'):
                # --basic-creds was specified explicitly; give precedence to
                # --basic-creds
                client_key = None
            elif client_key != os.environ.get('NUOCMD_CLIENT_KEY'):
                # --client-key was specified explicitly and --basic-creds was
                # not; give precedence to --client-key
                basic_creds = None
            else:
                # --client-key and --basic-creds were both resolved from
                # environment variables; give precedence to --basic-creds
                client_key = None
        if client_key is not None:
            if ',' in client_key:
                client_key = client_key.split(',')
                if len(client_key) > 2:
                    raise ValueError(
                        'Expected at most two tokens for --client-key')
            # Validate that the files specified by client_key exist or else unset it.
            # It can be a single file or a list of certificate files
            if not all(os.path.isfile(certificate_file) for certificate_file in
                       ([client_key] if is_string(client_key) else client_key)):
                client_key = None

        if basic_creds is not None:
            basic_creds = basic_creds.split(':', 1)
            if len(basic_creds) != 2:
                raise ValueError(
                    'Expected format for --basic-creds is \'<username>:<password>\'')  # noqa
        if verify_server is not None and not os.path.isfile(verify_server):
            # Validate that the file specified by verify_server exist or else unset it
            verify_server = None

        api_server = parsed_args.api_server
        if (not api_server.startswith('http://') and
            not api_server.startswith('https://')):  # noqa
            if client_key is None and verify_server is None:
                api_server = 'http://' + api_server
            else:
                api_server = 'https://' + api_server
        # set server verification as follows:
        # 1. if HTTP, set to None
        # 2. if --no-verify specified, set to False and disable any warning
        #    messages emitted about unverified HTTPS requests
        # 3. if trusted certificate specified with --verify-server, use it
        # 4. otherwise, set to True so that system CA certificates are used to
        #    verify server
        verify = None
        if api_server.startswith('https://'):
            if parsed_args.no_verify:
                verify = False
                nuodb_mgmt.disable_ssl_warnings()
            elif verify_server is not None:
                verify = verify_server
            else:
                verify = True
        # open file for request and response logging
        f = None
        if parsed_args.show_http:
            try:
                # if file descriptor 5 is writable, log to it
                f = os.fdopen(5, 'w')
            except Exception:
                # expect an OSError if file descriptor 5 is not writable and
                # log to standard output
                f = sys.stdout
        # build logging filter if there is a log file
        req_filter = None
        if f is not None:
            request_format = os.environ.get(
                'NUOCMD_REQUEST_FORMAT', '{method} {full_url} {json}')
            response_format = os.environ.get(
                'NUOCMD_RESPONSE_FORMAT', '{status_code} {json}')
            req_filter = nuodb_mgmt.FileLoggingRequestFilter(
                f, request_format, response_format)

        return nuodb_mgmt.AdminConnection(api_server, client_key, verify,
                                          basic_creds=basic_creds,
                                          req_filter=req_filter)

    @staticmethod
    def get_empty(**kwargs):
        """
        :returns []: Simple completer that returns nothing
        """
        return []

    @staticmethod
    def get_archive(parsed_args):
        """
        :returns nuodb_mgmt.Archive:
        """

        try:
            # if --archive-id is specified, return the archive
            if (hasattr(parsed_args, 'archive_id') and
                parsed_args.archive_id is not None):  # noqa
                return CommandProcessor._get_admin_conn(
                    parsed_args).get_archive(
                        parsed_args.archive_id)
        except Exception:
            pass

    @staticmethod
    def get_server_ids(prefix, parsed_args, **kwargs):
        try:
            # if --archive-id is specified, return the server it belongs to
            archive = CommandProcessor.get_archive(parsed_args)
            if archive is not None and archive.server_id is not None:
                return [archive.server_id]

            servers = CommandProcessor._get_admin_conn(
                parsed_args).get_servers(
                    id='{}.*'.format(prefix))
            return [server.id for server in servers]
        except Exception:
            return []

    @staticmethod
    def get_db_names(prefix, parsed_args, **kwargs):
        try:
            # if --archive-id is specified, return the database it belongs to
            archive = CommandProcessor.get_archive(parsed_args)
            if archive is not None:
                return [archive.db_name]

            databases = CommandProcessor._get_admin_conn(
                parsed_args).get_databases(
                    name='{}.*'.format(prefix))
            return [db.name for db in databases]
        except Exception:
            return []

    @staticmethod
    def get_db_names_from_archives(prefix, parsed_args, **kwargs):
        try:
            conn = CommandProcessor._get_admin_conn(parsed_args)
            archives = conn.get_archives()
            databases = conn.get_databases(name='{}.*'.format(prefix))
            db_names = [db.name for db in databases if db.state != 'TOMBSTONE']
            return set(archive.db_name for archive in archives
                       if archive.db_name not in db_names)
        except Exception:
            return []

    @staticmethod
    def get_archive_ids(prefix, parsed_args, **kwargs):
        try:
            archives = CommandProcessor._get_admin_conn(
                parsed_args).get_archives(
                    parsed_args.db_name, id='{}.*'.format(prefix))
            return [str(archive.id) for archive in archives]
        except Exception:
            return []

    @staticmethod
    def get_non_running_archive_ids(prefix, parsed_args, **kwargs):
        try:
            archives = CommandProcessor._get_admin_conn(
                parsed_args).get_archives(
                    parsed_args.db_name, id='{}.*'.format(prefix))
            return [str(archive.id) for archive in archives
                    if getattr(parsed_args, 'server_id', None) is None
                    or archive.server_id == parsed_args.server_id
                    or archive.server_id is None]
        except Exception:
            return []

    @staticmethod
    def get_running_archive_ids(prefix, parsed_args, **kwargs):
        try:
            archives = CommandProcessor._get_admin_conn(
                parsed_args).get_archives(
                    parsed_args.db_name, id='{}.*'.format(prefix),
                    state='RUNNING')
            return [str(archive.id) for archive in archives]
        except Exception:
            return []

    @staticmethod
    def get_region_names(prefix, parsed_args, **kwargs):
        try:
            regions = CommandProcessor._get_admin_conn(
                parsed_args).get_regions(
                    name='{}.*'.format(prefix))
            return [str(region.name) for region in regions]
        except Exception:
            return []

    @staticmethod
    def get_region_ids(prefix, parsed_args, **kwargs):
        try:
            regions = CommandProcessor._get_admin_conn(
                parsed_args).get_regions(
                    id='{}.*'.format(prefix))
            return [str(region.id) for region in regions]
        except Exception:
            return []

    @staticmethod
    def get_dict_completer(arg_name, key_completer, value_completer,
                           unique_values=False):
        def func(prefix, parsed_args, **kwargs):
            try:
                # check if we have an odd or even number of tokens
                tokens = getattr(parsed_args, arg_name, None)
                if tokens is not None and len(tokens) % 2 == 1:
                    # use value completer if odd
                    values = value_completer(prefix, parsed_args,
                                             **kwargs)
                    if unique_values:
                        # filter out values already entered
                        values_seen = CommandProcessor.dict_from_tokens(
                            tokens[:-1]).values()
                        return [value for value in values
                                if value not in values_seen]
                    return values
                # use key completer if even; filter out keys already entered
                keys_seen = CommandProcessor.dict_from_tokens(tokens).keys()
                return [key for key
                        in key_completer(prefix, parsed_args, **kwargs)
                        if key not in keys_seen]
            except Exception:
                return []
        return func

    @staticmethod
    def get_region_assignment_token(prefix, parsed_args, **kwargs):
        return CommandProcessor.get_dict_completer(
            'region_assignment',
            CommandProcessor.get_server_ids,
            CommandProcessor.get_region_names)(prefix, parsed_args, **kwargs)

    @staticmethod
    def get_archive_assignment_token(prefix, parsed_args, **kwargs):
        return CommandProcessor.get_dict_completer(
            'archive_assignment',
            CommandProcessor.get_non_running_archive_ids,
            CommandProcessor.get_server_ids)(prefix, parsed_args, **kwargs)

    @staticmethod
    def get_backup_dirs_token(prefix, parsed_args, **kwargs):
        return CommandProcessor.get_dict_completer(
            'backup_dirs',
            CommandProcessor.get_running_archive_ids,
            lambda *args, **kwargs: [])(prefix, parsed_args, **kwargs)

    @staticmethod
    def get_option_keys(prefix, parsed_args, engine_options=False):
        try:
            types = ['SIMPLE']
            if engine_options:
                types += ['PROCESS_ONLY']
            else:
                types += ['DATABASE_ONLY']
            options = CommandProcessor._get_admin_conn(
                parsed_args).get_process_options()
            return [option[2:] for option, option_type in options.items()
                    if option[2:].startswith(prefix) and option_type in types]
        except Exception:
            return []

    @staticmethod
    def get_engine_options_completer(arg_name, engine_options=True):
        def key_fn(prefix, parsed_args, **kwargs):
            return CommandProcessor.get_option_keys(
                prefix, parsed_args, engine_options=engine_options)
        return CommandProcessor.get_dict_completer(
            arg_name, key_fn, lambda *args, **kwargs: [])

    @staticmethod
    def get_captured_stable_ids(prefix, parsed_args, **kwargs):
        try:
            capture_xml = ElementTree.parse(parsed_args.capture_file)
            return [host.attrib['StableId']
                    for host in capture_xml.findall('./Hosts/Host')
                    if 'StableId' in host.attrib]
        except Exception:
            return []

    @staticmethod
    def get_server_mapping_token(prefix, parsed_args, **kwargs):
        return CommandProcessor.get_dict_completer(
            'server_mapping',
            CommandProcessor.get_captured_stable_ids,
            CommandProcessor.get_server_ids,
            True)(prefix, parsed_args, **kwargs)

    @staticmethod
    def get_user_names(prefix, parsed_args, **kwargs):
        try:
            users = CommandProcessor._get_admin_conn(
                parsed_args).get_users(
                    name='{}.*'.format(prefix))
            return [user.name for user in users]
        except Exception:
            return []

    @staticmethod
    def get_role_names(prefix, parsed_args, **kwargs):
        try:
            roles = CommandProcessor._get_admin_conn(
                parsed_args).get_roles(
                    name='{}.*'.format(prefix))
            return [role.name for role in roles]
        except Exception:
            return []

    @staticmethod
    def get_epilog():
        """
        Return a message that is appended to help output describing how to
        enable to tab completion.
        """

        home = get_home_dir()
        if home:
            complete = os.path.join(home, 'etc', 'nuocmd-complete')
            if os.path.exists(complete):
                return 'To enable tab completion in bash:\n  . ' + complete
        # if the complete file isn't found for some reason, don't bother
        return None

    @staticmethod
    def as_dict(obj):
        if isinstance(obj, Mapping):
            return obj
        return CommandProcessor.dict_from_tokens(obj)

    @staticmethod
    def dict_from_tokens(tokens, key_type=str, value_type=str):
        if tokens is None or len(tokens) == 0:
            return {}

        if len(tokens) % 2 != 0:
            raise ValueError('Cannot convert odd number of tokens to dict: '
                             + str(tokens))
        ret = {}
        for i in range(0, len(tokens), 2):
            key = key_type(tokens[i])
            if key in ret:
                raise ValueError('Duplicate key in dict: ' + str(key))
            ret[key] = value_type(tokens[i + 1])
        return ret

    @staticmethod
    def incarnation_from_str(s):
        parts = s.split('.')
        if len(parts) == 1 and parts[0].isdigit():
            return dict(major=int(parts[0]))
        if len(parts) == 2 and all(map(str.isdigit, parts)):
            major, minor = map(int, parts)
            return dict(major=major, minor=minor)
        raise argparse.ArgumentTypeError(
            'Cannot convert \'{}\' to incarnation. Expected format \'<major>[.<minor>]\' where \'major\' and \'minor\' are both integers'.format(s))  # noqa

    @staticmethod
    def datetime_from_str(date_fmt=DEFAULT_DATETIME_FORMAT_STRING,
                          example=DEFAULT_DATETIME_EXAMPLE_STRING):
        def convertion_fn(s):
            if s is None:
                return
            try:
                return datetime.datetime.strptime(s, date_fmt)
            except ValueError:
                raise argparse.ArgumentTypeError(
                    "Error parsing provided timestamp '{}'. Timestamp must have form '{}'".format(s, example))  # noqa
        return convertion_fn

    @staticmethod
    def handle_command_error(command_handler, e):
        # Invoke error handler if defined in the command handler class
        method_names = [pair[0] for pair in inspect.getmembers(command_handler, inspect.ismethod)]
        if 'handle_command_error' in method_names:
            command_handler.handle_command_error(e)
        # Explicitly flush the i/O streams
        command_handler.out.flush()
        command_handler.err.flush()

    def _add_argument(self, *args, **kwargs):
        arg_spec = Argument(*args, **kwargs)
        arg_spec.add_argument(self.parser)
        self.global_arg_specs.append(arg_spec)

    def __init__(self):
        # global arguments relevant for all commands
        self.parser = argparse.ArgumentParser(
            'nuocmd', description='NuoDB Management CLI',
            epilog=CommandProcessor.get_epilog(),
            formatter_class=CustomHelpFormatter)
        self.global_arg_specs = []
        self._add_argument('--api-server',
                           default=EnvironmentalDefault(CommandProcessor.DEFAULT_API_SERVER),  # noqa
                           help='the REST API server to send request to')
        self._add_argument('--show-http', action='store_true',
                           help='emit HTTP request and response logging; to emit this logging separately from standard output, the file descriptor 5 can be redirected, e.g. `nuocmd --show-http show domain 5>/tmp/http-logging`')  # noqa
        self._add_argument('--show-json', action='store_true',
                           help='show the full JSON response; if several responses are generated by a command, they are output as concatenated JSON')
        self._add_argument('--show-json-fields', required=False,
                           help='show the specified fields of the JSON response; multiple fields can be specified as a comma-separated list, and nested fields can be specified using the syntax `<parent>.<child>`; if several responses are generated by a command, they are output as concatenated JSON')
        self._add_argument('--debug', action='store_true',
                           default=EnvironmentalDefault(),
                           help=argparse.SUPPRESS)
        self._add_argument('--show-install-commands',
                           action=ShowInstallCommandsAction,
                           help=argparse.SUPPRESS)
        self._add_argument('--client-key', required=False,
                           default=EnvironmentalDefault(),
                           help='client key-pair if server requires client authentication (see \'needClientAuth\' REST setting); specified as a single file containing both certificate and private key, or as \'<certificate>,<private key>\'')  # noqa
        self._add_argument('--basic-creds', required=False,
                           default=EnvironmentalDefault(),
                           help='client basic credentials in the format \'<username>:<password>\'; this takes precedence over --client-key')  # noqa
        self._add_argument('--verify-server', required=False,
                           default=EnvironmentalDefault(),
                           help='trusted certificate used to verify the server when using HTTPS; if no argument is specified then the default set of trusted CA certificates for the system is used')  # noqa
        self._add_argument('--no-verify', action='store_true',
                           help='if specified, then server verification is disabled; this takes precedence over --verify-server')  # noqa

        # add subcommands for each action and build dictionary of subparsers
        self.subparsers = self.parser.add_subparsers(title='subcommands')
        self.sp_dict = {}

    def add_subcommands(self, command_handler, defined_only=False):
        # sort methods by name so that subcommands are displayed in a
        # consistent order

        methods = inspect.getmembers(command_handler, inspect.isfunction)
        methodFunction = lambda x: x

        # in python2 unbound methods are still considered methods
        if sys.version[0] == '2':
            methods = inspect.getmembers(command_handler, inspect.ismethod)
            # have to pass in `__func__` since that is the object that metadata
            # was added to by the decorator
            methodFunction = lambda x: x.__func__

        methods.sort(key=lambda pair: pair[0])
        for pair in methods:
            name, method = pair
            if defined_only and name not in command_handler.__dict__:
                continue
            Subcommand.add_subcommand(self, methodFunction(method))

    def execute(self, argv=None, out=sys.stdout, err=sys.stderr,
                command_handler=None):
        """
        Parse arguments and execute command using registered command method
        for supplied subcommand.

        :param list[str] argv: arguments to parse; if None, use `sys.argv[1:]`
        """

        if command_handler is None:
            command_handler = AdminCommands
        self.add_subcommands(command_handler)

        autocomplete(self.parser)
        args = self.parser.parse_args(argv)
        conn = self._get_admin_conn(args)
        # combine --show-json and --show-json-fields to form a ternary value to
        # indicate whether we should dump raw JSON response, and if so, should
        # we filter the set of attributes to dump
        show_json = args.show_json_fields or args.show_json
        commands = command_handler(conn, show_json, out, err, args.debug)
        # args.func is set to the AdminCommands method associated with
        # the invoked subcommand
        if 'func' in args:
            # create kwargs for method by filtering out all global parameters
            kwargs = dict((k, v) for k, v in args._get_kwargs()
                          if k not in ['api_server', 'show_http', 'show_json',
                                       'show_json_fields', 'debug', 'func',
                                       'client_key', 'basic_creds',
                                       'verify_server', 'no_verify'])
            try:
                return args.func(commands, **kwargs)
            except ExitCommandException as e:
                # if debug is set, show stacktrace
                if args.debug:
                    traceback.print_exc(file=commands.err)
                CommandProcessor.handle_command_error(commands, e)
                sys.exit(e.exit_code)
            except Exception as e:
                # if debug is set, show stacktrace
                if args.debug:
                    traceback.print_exc(file=commands.err)
                # avoid cryptic error message from requests
                if isinstance(e, requests.exceptions.ConnectionError):
                    commands.err.write('Unable to connect to {}: {}\n'.format(
                        conn.url_base, str(e)))
                else:
                    commands.err.write('\'{} {}\' failed: {}\n'.format(
                        args.func._action, args.func._entity, str(e)))
                CommandProcessor.handle_command_error(commands, e)
                sys.exit(1)
            except SystemExit as e:
                # if debug is set, show stacktrace; this is in a separate
                # `except` clause because SystemExit does not subclass
                # Exception
                if args.debug:
                    traceback.print_exc(file=commands.err)
                CommandProcessor.handle_command_error(commands, e)
                sys.exit(e.code)
            except KeyboardInterrupt as e:
                if args.debug:
                    traceback.print_exc(file=commands.err)
                    commands.err.write("Exiting due to interrupt...\n")
                CommandProcessor.handle_command_error(commands, e)
                sys.exit(1)


class AdminCommands(object):
    """
    Class containing all registered subcommands.
    """

    def __init__(self, conn, show_json=False, out=sys.stdout, err=sys.stderr, debug=False):
        """
        :param nuodb_mgmt.AdminConnection conn:
        """

        self.conn = conn
        self.show_json = show_json
        self.out = out
        self.err = err
        self.debug = debug
        self.disable_print = False

    @Subcommand('get', 'processes', help='get database processes')
    @Argument('--db-name', required=False, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database to list processes for')
    @Argument('--exited', action='store_true', help='get exited processes')
    def get_processes(self, db_name=None, exited=False):
        func = (self.conn.get_exited_processes if exited
                else self.conn.get_processes)
        procs = func() if db_name is None else func(db_name)
        for proc in procs:
            self._show(proc)
        return procs

    DB_OPTIONS_URL = 'https://doc.nuodb.com/nuodb/latest/reference-information/database-options'

    @Subcommand('start', 'process', help='start a database process')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database for the process')
    @MutuallyExclusive(
        Argument('--server-id',
                 completer=CommandProcessor.get_server_ids,
                 help='the ID of the server the process belongs to'),
        Argument('--this-server', action='store_true',
                 help='whether to start process through API server'),
        required=True)
    @Argument('--engine-type', choices=['TE', 'SM', 'SSM'],
              help='the engine type')
    @Argument('--archive-id', required=False,
              completer=CommandProcessor.get_non_running_archive_ids,
              help='the ID of the archive if engine-type is SM or SSM')
    @Argument('--expected-incarnation', required=False,
              type=CommandProcessor.incarnation_from_str,
              help='the expected incarnation for the database in the form \'<major>[.<minor>]\'')  # noqa
    @Argument('--nuodb-cmd', required=False,
              help='the command to invoke the NuoDB executable, if starting externally (deprecated)')  # noqa
    @Argument('--options', required=False, nargs='*',
              help='options for database process; see ' + DB_OPTIONS_URL)
    @Argument('--labels', required=False, nargs='*',
              help='labels for engine process')
    def start_process(self, db_name, server_id, engine_type, archive_id=None,
                      nuodb_cmd=None, options=None, this_server=False,
                      expected_incarnation=None, labels=None):
        server_id, config = self._get_server_id(server_id, this_server)
        if not isinstance(options, Mapping):
            options = CommandProcessor.dict_from_tokens(options)
        if not isinstance(labels, Mapping):
            labels = CommandProcessor.dict_from_tokens(labels)
        major = None
        minor = None
        if expected_incarnation is not None:
            major = expected_incarnation.get('major')
            minor = expected_incarnation.get('minor')
        return self._start_process(
            db_name, server_id, engine_type, archive_id=archive_id,
            incarnation_major=major, incarnation_minor=minor,
            nuodb_cmd=nuodb_cmd, is_cmd=True, labels=labels, **options)

    def _start_process(self, db_name, server_id, engine_type, archive_id=None,
                       incarnation_major=None, incarnation_minor=None,
                       nuodb_cmd=None, is_cmd=False, labels=None, **options):
        is_external = nuodb_cmd is not None
        proc_info = self.conn.start_process(
            db_name, server_id, engine_type,
            archive_id, is_external,
            incarnation_major=incarnation_major,
            incarnation_minor=incarnation_minor,
            labels=labels, **options)
        if is_external:
            # start process externally
            # define environment variables that `nuodb_cmd` can use to get
            # information about the process that's being started
            env = dict(os.environ,
                       DB_NAME=db_name,
                       ENGINE_TYPE=engine_type,
                       START_ID=proc_info.start_id)
            if archive_id is not None:
                env['ARCHIVE_ID'] = str(archive_id)
                env['ARCHIVE'] = self.conn.get_archive(archive_id).archive_path
            ret = subprocess.call(
                nuodb_cmd.split() + proc_info['commandLineArgs'], env=env, universal_newlines=True)
            if is_cmd:
                # invoked as "start process" subcommand; otherwise, `nuodb_cmd`
                # needs to be asynchronous (i.e. it forks off the nuodb
                # process, possibly in a container, and then exits), and in
                # that case we do not care about the exit code and we want to
                # start other nuodb processes since we are starting the entire
                # database using "start/enforce/create database" subcommands
                sys.exit(ret)
        elif is_cmd:
            # invoked as "start process" subcommand, so print the started
            # process; otherwise, this was invoked inside of
            # "start/enforce/create database" subcommands, which print started
            # processes themselves
            self._show(proc_info)
        return proc_info

    @Subcommand('shutdown', 'process', help='shutdown a database process')
    @Argument('--start-id', required=True,
              help='the start ID of the process to shutdown')
    @MutuallyExclusive(
        Argument('--evict', action='store_true',
                 help='evict process from database and remove it from the domain state, even if it is unresponsive or no longer running; this command may not actually cause an unresponsive process to shutdown, in which case it would have to be killed manually by the user'),  # noqa
        Argument('--kill', action='store_true',
                 help='forcibly kill process'),
        Argument('--kill-with-core', action='store_true',
                 help='forcibly kill process and dump core'),
        required=False)
    @Argument('--timeout', required=False, type=int,
              help='the timeout in seconds to wait for the process to disconnect from the admin server; if not specified, the shutdown request is issued without waiting')  # noqa
    def shutdown_process(self, start_id, evict=False, kill=False,
                         kill_with_core=False, timeout=None):
        if timeout is None:
            timeout = 0
        self.conn.shutdown_process(start_id, evict, kill, kill_with_core,
                                   timeout)

    @Subcommand('shutdown', 'server-processes',
                help='shutdown database processes connected to an admin server')
    @Argument('--server-id', required=True,
              completer=CommandProcessor.get_server_ids,
              help='shut down all processes connected to the admin server with this server id')
    @Argument('--db-name', required=False,
              help='only shut down processes if they belong to this database')
    @MutuallyExclusive(
        Argument('--evict', action='store_true',
                 help='evict processes from database and remove them from the domain state, even if they are unresponsive or no longer running; this command may not actually cause an unresponsive process to shutdown, in which case it would have to be killed manually by the user'),  # noqa
        Argument('--kill', action='store_true',
                 help='forcibly kill processes'),
        Argument('--kill-with-core', action='store_true',
                 help='forcibly kill processes and dump core'),
        required=False)
    @Argument('--timeout', required=False, type=int,
              help='the timeout in seconds to wait for a process to disconnect from its admin server; if not specified, the shutdown request is issued without waiting')  # noqa
    def shutdown_server_processes(self, server_id, db_name=None,
                                  evict=False, kill=False, kill_with_core=False, timeout=None):
        for process in sorted(self.conn.get_processes(db_name),
                              key=lambda p: 0 if p.engine_type == 'TE' else 1):
            if process.server_id == server_id:
                self.conn.shutdown_process(process.start_id, evict, kill,
                                           kill_with_core, timeout)

    @Subcommand('get', 'databases', help='list all databases')
    def get_databases(self):
        databases = self.conn.get_databases()
        for db in databases:
            self._show(db)
        return databases

    @Subcommand('get', 'database',
                help='get database state for a specific database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database')
    def get_database(self, db_name):
        db = self.conn.get_database(db_name)
        self._show(db)
        return db

    @Subcommand('create', 'database', help='create a database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names_from_archives,
              help='the name of the database')
    @Argument('--dba-user', required=True, default=EnvironmentalDefault(),
              help='the database administrator user')
    @Argument('--dba-password', required=True, default=EnvironmentalDefault(),
              help='the database administrator password')
    @Argument('--te-server-ids', nargs='+', required=False,
              completer=CommandProcessor.get_server_ids,
              help='the IDs of the servers to start TEs on')
    @MutuallyExclusive(
        Argument('--capture-file',
                 help='the path to the file to capture database to'),
        Argument('--nuodb-cmd',
                 help='the command to invoke the NuoDB executable, if starting externally (deprecated)'),  # noqa
        required=False)
    @Argument('--no-autostart', action='store_true',
              help='do not automatically start database')
    @Argument('--is-external', action='store_true',
              help='whether database processes will be started externally (deprecated)')
    @Argument('--archive-assignment', nargs='+', required=False,
              completer=CommandProcessor.get_archive_assignment_token,
              help='mapping of archive ID to server ID, for external start (deprecated)')
    @Argument('--region-assignment', required=False, nargs='*',
              completer=CommandProcessor.get_region_assignment_token,
              help='mapping of server ID to region name (deprecated)')
    @Argument('--default-options', required=False, nargs='*',
              help='default options for database processes; see ' + DB_OPTIONS_URL)
    @Argument('--sms-running-timeout', required=False, type=float,
              help='timeout for SMs to become RUNNING before starting TEs')
    def create_database(self, db_name, dba_user, dba_password,
                        default_options=None, te_server_ids=None,
                        capture_file=None, is_external=False, nuodb_cmd=None,
                        archive_assignment=None, region_assignment=None,
                        sms_running_timeout=None, no_autostart=False):
        if not isinstance(default_options, Mapping):
            options = CommandProcessor.dict_from_tokens(default_options)
        else:
            options = default_options
        if is_external and capture_file is None and nuodb_cmd is None:
            raise ValueError('If --is-external is specified, one of --capture-file, --nuodb-cmd must be specified')  # noqa
        if is_external or nuodb_cmd is not None:
            options['ext-start'] = True
        elif archive_assignment is not None:
            raise ValueError('--archive-assignment requires --is-external')
        if not isinstance(archive_assignment, Mapping):
            archive_assignment = CommandProcessor.dict_from_tokens(
                archive_assignment, key_type=int)
        if not region_assignment:
            region_assignment = {}
        elif not isinstance(region_assignment, Mapping):
            regions_by_name = self._get_regions_by_name()
            region_assignment = CommandProcessor.dict_from_tokens(
                region_assignment,
                value_type=lambda region_name: self._get_region_id(
                    region_name, regions_by_name))
        startplan = self.conn.create_database(
            db_name, dba_user, dba_password, te_server_ids=te_server_ids,
            host_assignments=region_assignment,
            archive_assignment=archive_assignment, **options)

        if capture_file is None and not no_autostart:
            # start database automatically
            self._start_database(startplan, sms_running_timeout, nuodb_cmd)
        elif capture_file is not None:
            self._write_json(startplan, capture_file)

    @Subcommand('update', 'database-options',
                help='update database options')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database')
    @Argument('--default-options', required=False, nargs='*',
              help='default options for database processes; see ' + DB_OPTIONS_URL)
    @Argument('--replace-all', action='store_true',
              help='whether to completely replace the existing options with the supplied options, rather than merging the two')  # noqa
    def update_database_options(self, db_name, replace_all=False,
                                default_options=None):
        if not isinstance(default_options, Mapping):
            options = CommandProcessor.dict_from_tokens(default_options)
        else:
            options = default_options
        self.conn.update_database_options(db_name, replace_all, **options)

    @Subcommand('delete', 'database', help='delete a non-running database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database to delete')
    def delete_database(self, db_name):
        self.conn.delete_database(db_name)

    @Subcommand('capture', 'database',
                help='capture the state of a running database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database to capture')
    @Argument('--capture-file', required=True,
              help='the path to the file to write startplan to')
    def capture_database(self, db_name, capture_file):
        self._write_json(self.conn.capture_database(db_name),
                         capture_file)

    @Subcommand('get', 'startplan', help='get the startplan for a database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database')
    @Argument('--output-file', required=True,
              help='the path to the file to capture database to')
    @Argument('--te-server-ids', nargs='+', required=False,
              completer=CommandProcessor.get_server_ids,
              help='the IDs of the servers to start additional TEs on')
    @Argument('--archive-assignment', nargs='+', required=False,
              completer=CommandProcessor.get_archive_assignment_token,
              help='mapping of archive ID to server ID, for external start')
    @Argument('--one-time', action='store_true',
              help='get a startplan for one-time use')
    def get_startplan(self, db_name, output_file, te_server_ids=None,
                      archive_assignment=None, one_time=False):
        if not isinstance(archive_assignment, Mapping):
            archive_assignment = CommandProcessor.dict_from_tokens(
                archive_assignment, key_type=int)
        startplan = self.conn.get_database_startplan(
            db_name, not one_time, te_server_ids, archive_assignment)
        self._write_json(startplan, output_file)

    def _write_json(self, data, output_file):
        with open(output_file, 'w') as f:
            f.write(json.dumps(data, indent=2, sort_keys=True))
            f.write('\n')

    @Subcommand('start', 'database', help='start a database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database to start')
    @Argument('--te-server-ids', nargs='+', required=False,
              completer=CommandProcessor.get_server_ids,
              help='the IDs of the servers to start additional TEs on')
    @Argument('--archive-assignment', nargs='+', required=False,
              completer=CommandProcessor.get_archive_assignment_token,
              help='mapping of archive ID to server ID, for external start (deprecated)')  # noqa
    @MutuallyExclusive(
        Argument('--incremental', action='store_true',
                 help='start the database incrementally based on running state'),
        Argument('--unconditional', action='store_true',
                 help='start any non-running database processes regardless of the database state'))  # noqa
    @Argument('--nuodb-cmd', required=False,
              help='the command to invoke the NuoDB executable, if starting externally (deprecated)')  # noqa
    @Argument('--sms-running-timeout', required=False, type=float,
              help='timeout for SMs to become RUNNING before starting TEs')
    def start_database(self, db_name=None, te_server_ids=None,
                       archive_assignment=None, incremental=False,
                       nuodb_cmd=None, unconditional=False,
                       sms_running_timeout=None):
        if not isinstance(archive_assignment, Mapping):
            archive_assignment = CommandProcessor.dict_from_tokens(
                archive_assignment, key_type=int)
        startplan = self.conn.get_database_startplan(
            db_name, reusable=False, te_server_ids=te_server_ids,
            archive_assignment=archive_assignment)
        if not unconditional:
            AdminCommands._check_incremental(
                db_name, incremental, startplan.get('incremental', False))
        return self._start_database(startplan, sms_running_timeout, nuodb_cmd)

    def _start_database(self, startplan, sms_running_timeout, nuodb_cmd):
        proc_specs = list(map(nuodb_mgmt.StartProcessRequest, startplan['processes']))
        # order process specifications so that TEs appear last
        proc_specs.sort(key=lambda p: (p.engine_type, p.archive_id))
        all_sms_running = False
        for proc in proc_specs:
            try:
                if sms_running_timeout is not None and (
                        not all_sms_running and proc.engine_type == 'TE'):
                    self.check_database(
                        proc.db_name, check_running=True,
                        fail_on_process_exit=True,
                        timeout=sms_running_timeout)
                    all_sms_running = True
                self._start_process_iter(proc, nuodb_cmd=nuodb_cmd)
            except Exception as e:
                self._raise_on_failed_process(
                    proc.db_name, proc.expected_incarnation_major, str(e))
        return proc_specs

    @staticmethod
    def _check_incremental(db_name, expected, actual):
        if expected and not actual:
            raise RuntimeError(
                'Database ' + db_name +
                ' is not running and --incremental was specified')
        elif not expected and actual:
            raise RuntimeError(
                'Database ' + db_name +
                ' is running and --incremental was not specified')

    @Subcommand('enforce', 'database', help='start a database')
    @Argument('--capture-file', required=True,
              help='the path to the capture file to enforce')
    @MutuallyExclusive(
        Argument('--incremental', action='store_true',
                 help='start the database incrementally based on running state'),
        Argument('--unconditional', action='store_true',
                 help='start any non-running database processes regardless of the database state'))  # noqa
    @Argument('--nuodb-cmd', required=False,
              help='the command to invoke the NuoDB executable, if starting externally (deprecated)')  # noqa
    @Argument('--sms-running-timeout', required=False, type=float,
              help='timeout for SMs to become RUNNING before starting TEs')
    def enforce_database(self, capture_file=None, incremental=False,
                         nuodb_cmd=None, unconditional=False,
                         sms_running_timeout=None):
        db_name, processes = read_capture_file(capture_file)
        # make sure archives in capture file match provisioned archives
        archive_ids = set(proc['archiveId'] for proc in processes
                          if proc.get('archiveId') is not None)
        expected_archive_ids = set(archive.id for archive in
                                   self.conn.get_archives(db_name))
        if archive_ids != expected_archive_ids:
            unexpected = archive_ids.difference(expected_archive_ids)
            missing = expected_archive_ids.difference(archive_ids)
            messages = []
            if len(unexpected) != 0:
                messages.append(
                    'Unexpected archives found in capture file: {}'
                    .format(list(unexpected)))
            if len(missing) != 0:
                messages.append(
                    'Expected archives not found in capture file: {}'
                    .format(list(missing)))
            raise ValueError(
                'Archives in capture file do not match provisioned archives: '
                + '; '.join(messages))
        # get the expected incarnation after starting database
        db_info = self.conn.get_database(db_name)
        expected_incarnation = db_info.incarnation
        if not incremental and db_info.state == 'NOT_RUNNING':
            expected_incarnation = (expected_incarnation[0] + 1, 0)

        # capture running processes
        _, running = read_capture_data(
            self.conn.capture_database(db_name, check_state=False))
        if not unconditional:
            AdminCommands._check_incremental(
                db_name, incremental, len(running) != 0)

        # keep track of remaining TEs for each server
        num_remaining = {}
        for proc in processes:
            if proc.engine_type != 'TE':
                continue
            if proc.server_id not in num_remaining:
                num_remaining[proc.server_id] = 0
            num_remaining[proc.server_id] += 1

        running_archives = []
        for proc in running:
            if proc.engine_type != 'TE':
                running_archives.append(proc.archive_id)
            else:
                if proc.server_id in num_remaining:
                    num_remaining[proc.server_id] -= 1

        all_sms_running = False
        for proc in processes:
            if proc in running:
                # exact match; remove corresponding element from running list
                running.remove(proc)
                self._print('SKIPPING: {}: already running', proc)
            elif proc.archive_id in running_archives:
                # archive is running, but with different options
                self._print('SKIPPING: {}: archive ID {} is already running',
                            proc, proc.archive_id)
            elif (proc.engine_type == 'TE' and
                  num_remaining[proc.server_id] <= 0):  # noqa
                # not an exact match, but TE count on server reached
                self._print('SKIPPING: {}: engine count on {} reached',
                            proc, proc.server_id)
            else:
                if proc.engine_type == 'TE':
                    num_remaining[proc.server_id] -= 1
                try:
                    if sms_running_timeout is not None and (
                            not all_sms_running and proc.engine_type == 'TE'):
                        self.check_database(
                            db_name, check_running=True,
                            fail_on_process_exit=True,
                            timeout=sms_running_timeout)
                        all_sms_running = True
                    proc._dict['expectedIncarnation'] = dict(
                        major=expected_incarnation[0],
                        minor=expected_incarnation[1])
                    self._start_process_iter(proc, nuodb_cmd=nuodb_cmd)
                except Exception as e:
                    self._raise_on_failed_process(
                        db_name, expected_incarnation[0], str(e))

    def _start_process_iter(self, proc, nuodb_cmd=None):
        """
        :param nuodb_mgmt.StartProcessRequest proc: specification of process
        :param str nuodb_cmd: if starting externally, the command to invoke the
                              NuoDB executable

        :returns bool: whether there is a TE running
        """

        self._print('STARTING: {}', proc)
        self._start_process(
            proc.db_name, proc.server_id,
            proc.engine_type,
            archive_id=proc.archive_id,
            incarnation_major=proc.expected_incarnation_major,
            incarnation_minor=proc.expected_incarnation_minor,
            nuodb_cmd=nuodb_cmd,
            is_cmd=False,
            labels=proc.labels,
            **proc.options)

    @Subcommand('shutdown', 'database',
                help='shutdown all database processes for a database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database to shutdown')
    def shutdown_database(self, db_name):
        self.conn.shutdown_database(db_name)

    @Subcommand('connect', 'database',
                help='establish a SQL connection with a database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database to connect to')
    @MutuallyExclusive(
        Argument('--server-id',
                 completer=CommandProcessor.get_server_ids,
                 help='the ID of the server to use to obtain SQL connection'),
        Argument('--this-server', action='store_true',
                 help='whether to obtain SQL connection through API server'),
        required=True)
    @MutuallyExclusive(
        Argument('--user', help='the database user; alternatively, can be specified using the NUOSQL_USER environment variable'),  # noqa
        Argument('--dba-user', dest='user', help=argparse.SUPPRESS),  # deprecated
        required=False)
    @MutuallyExclusive(
        Argument('--password', help='the database password; alternatively, can be specified using the NUOSQL_PASSWORD environment variable, or via standard input'),  # noqa
        Argument('--dba-password', dest='password', help=argparse.SUPPRESS),  # deprecated
        required=False)
    @Argument('--nuosql-cmd', required=True,
              default=EnvironmentalDefault(from_nuodb_home('bin', 'nuosql')),
              help='the command to invoke the NuoSQL executable')
    @Argument('--connection-properties', required=False, nargs='*',
              default=EnvironmentalDefault(),
              help='connection properties for connection request')
    def connect_database(self, db_name, server_id, user, password,
                         nuosql_cmd, this_server=False,
                         connection_properties=None):
        server_id, config = self._get_server_id(server_id, this_server)
        if config is None:
            config = self.conn.get_admin_config(server_id)
        port = config.get_nested('properties', 'agentPort')
        server = self.conn.get_server(server_id)
        address = server.address.split(':')[0]
        args = ['{}@{}:{}'.format(db_name, address, port)]
        if user is not None:
            args.extend(['--user', user])
        if password is not None:
            args.extend(['--password', password])
        # add connection properties
        if not isinstance(connection_properties, Mapping):
            connection_properties = CommandProcessor.dict_from_tokens(
                connection_properties)
        for k, v in connection_properties.items():
            args.append('--connection-property')
            args.append('{}={}'.format(k, v))
        # invoke nuosql and exit with its exit value
        ret = subprocess.call(nuosql_cmd.split() + args, universal_newlines=True)
        sys.exit(ret)

    def _get_server_id_from_host_entry(self, servers, address, stable_id=None,
                                       server_mapping=None):
        if stable_id is not None and server_mapping:
            # server mapping was defined and stable ID is in capture file
            server_id = server_mapping.get(stable_id)
            if server_id is None:
                raise RuntimeError(
                    'No entry in --server-mapping for stable ID ' + stable_id)
            return server_id
        # server mapping not defined or stable ID is not in capture file; find
        # server with matching address
        server_ids = [server.id for server in servers
                      if server.address.split(':')[0] == address]
        if len(server_ids) == 0:
            raise RuntimeError('No servers found with address ' + address)
        if len(server_ids) != 1:
            raise RuntimeError('Multiple servers found with address {}: {}'
                               .format(address, ', '.join(server_ids)))
        return server_ids[0]

    def _check_one_to_one(self, server_map):
        inverse_multimap = {}
        for host_num, server_id in server_map.items():
            inverse_multimap[server_id] = inverse_multimap.get(server_id, [])
            inverse_multimap[server_id].append(host_num)
        errors = []
        for server_id, host_nums in sorted(inverse_multimap.items()):
            if len(host_nums) != 1:
                errors.append('Multiple hosts mapped to {}: {}\n'
                              .format(server_id, ', '.join(sorted(host_nums))))
        if len(errors) != 0:
            raise RuntimeError(
                'Unable to map hosts in capture file to nuoadmin servers:' +
                ''.join('\n  ' + error for error in errors))

    def _with_prefix(self, name):
        return name if name.startswith('--') else ('--' + name)

    def _without_prefix(self, name):
        return name[2:] if name.startswith('--') else name

    def _get_options(self, proc_spec,
                     ignored_options=('jvm-lib', 'nuodb-home-dir')):
        """
        Get options from a process entry in a nuoagent capture file. Each
        option entry has the form `<Option Name="...">...</Option>`, where the
        inner text is the value, unless the option is a switch, in which case
        there is no inner text.

        :param xml.etree.ElementTree proc_spec:
        """

        options = {}
        for option_entry in proc_spec.findall('Option'):
            name = self._without_prefix(option_entry.attrib['Name'])
            if name in ignored_options:
                continue
            value = option_entry.text
            if not value:
                value = 'true'
            options[name] = value
        return options

    def _is_legal_db_option(self, name, option_types):
        return option_types.get(
            self._with_prefix(name)) in ['SIMPLE', 'DATABASE_ONLY']

    def _normalize_options(self, proc_options_list, option_types):
        """
        Return the dictionary containing all common options among
        `proc_options_list` that can be specified as database options and
        normalize all options in `proc_options_list` so that they are disjoint
        with the database options. Also remove any restricted options or
        database options that are specified inconsistently.

        :param list[dict[str, str]] proc_options_list:
        """

        if len(proc_options_list) == 0:
            return {}
        # copy all database-legal options from some arbitrary process
        db_options = dict(
            (name, value) for name, value in proc_options_list[0].items()
            if self._is_legal_db_option(name, option_types))
        # eliminate any options we encounter that are not specified identically
        # for all other processes
        for name, value in list(db_options.items()):
            for proc_options in proc_options_list[1:]:
                if name in db_options and proc_options.get(name) != value:
                    del db_options[name]
        # normalize process options by removing any options we've factored out
        # into `db_options` and removing any illegal options; also emit warning
        # messages about any illegal options
        restricted_options = set()
        illegal_db_options = set()
        for proc_options in proc_options_list:
            # remove all database options
            for name in db_options.keys():
                if name in proc_options:
                    del proc_options[name]
            # remove any illegal options; which are options that are either
            # RESTRICTED or DATABASE_ONLY; any DATABASE_ONLY option should have
            # been specified identically for all processes and should be in
            # `db_options`
            for name, type in option_types.items():
                name = self._without_prefix(name)
                if name in proc_options and type in ['DATABASE_ONLY', 'RESTRICTED']:  # noqa
                    del proc_options[name]
                    if type == 'DATABASE_ONLY':
                        illegal_db_options.add(name)
                    else:
                        restricted_options.add(name)
        if len(restricted_options) != 0:
            self._print(
                'WARNING: Restricted options appearing in capture file: {}',
                ', '.join(restricted_options))
        if len(illegal_db_options) != 0:
            self._print(
                'WARNING: Database options specified inconsistently in capture file: {}',  # noqa
                ', '.join(illegal_db_options))
        return db_options

    @Subcommand('prepare', 'database',
                help='display commands to create a nuoadmin database from a nuoagent capture file')  # noqa
    @Argument('--capture-file', required=True,
              help='capture file describing a nuoagent database')
    @Argument('--server-mapping', nargs='*',
              completer=CommandProcessor.get_server_mapping_token,
              help='mapping of stable ID in nuoagent to corresponding server ID in nuoadmin')  # noqa
    def prepare_database(self, capture_file, server_mapping=None):
        if not isinstance(server_mapping, Mapping):
            server_mapping = CommandProcessor.dict_from_tokens(server_mapping)
        db_spec = ElementTree.parse(capture_file)
        db_name = db_spec.getroot().attrib['Name']
        # host entries have the format `<Host Id="..." Address="..."
        # StableId="..." />`, where `Id` is referenced in `HostId` attribute of
        # process entries
        host_specs = []
        for host_spec in db_spec.findall('./Hosts/Host'):
            host_specs.append((host_spec.attrib['Id'],
                               host_spec.attrib['Address'],
                               host_spec.attrib.get('StableId')))
        # using either explicit mapping of stable ID to server ID or by using
        # addresses, map every host entry to an admin server
        servers = self.conn.get_servers()
        server_map = dict(
            (host_num, self._get_server_id_from_host_entry(
                servers, address, stable_id, server_mapping))
            for host_num, address, stable_id in host_specs)
        self._check_one_to_one(server_map)
        # process all archives/SMs in capture file; for each archive/SM, we
        # need to generate a create-archive command with the associated server
        # ID and path; SM entries have the form `<StorageManager
        # ArchivePath="..." HostId="..."/>`
        archive_specs = []
        sm_options = []
        for sm_spec in db_spec.findall('./StorageManagers/StorageManager'):
            server_id = server_map[sm_spec.attrib['HostId']]
            archive_path = sm_spec.attrib['ArchivePath']
            options = self._get_options(sm_spec)
            # get journal directory from options; default journal directory is
            # `<archive>/journal`; only set the journal path if it is set to a
            # non-default location
            journal_path = options.pop('journal-dir', None)
            if journal_path == os.path.join(archive_path, 'journal'):
                journal_path = None
            archive_specs.append((server_id, archive_path, journal_path))
            sm_options.append(options)
        # process all TEs in capture file
        te_server_ids = []
        te_options = []
        for te_spec in db_spec.findall('./TransactionEngines/TransactionEngine'):  # noqa
            host_num = te_spec.attrib['HostId']
            te_server_ids.append(server_map[host_num])
            te_options.append(self._get_options(te_spec))
        # normalize the process options; factor out all common options as
        # database-level options (specified on create-database)
        option_types = self.conn._get_data(
            'processes', 'availableOptions')['options']
        db_options = self._normalize_options(sm_options + te_options,
                                             option_types)
        # prepare commands to create archives and database in nuoadmin
        commands = []
        commands.append('  export NUOCMD_DBA_USER=theDbaUser          # ATTENTION: replace with your actual DBA username')  # noqa
        commands.append('  export NUOCMD_DBA_PASSWORD=theDbaPassword  # ATTENTION: replace with your actual DBA password')  # noqa
        for server_id, archive_path, journal_path in archive_specs:
            if journal_path is None:
                commands.append(
                    '  nuocmd create archive --db-name {} --server-id {} --archive-path {} --restored'  # noqa
                    .format(db_name, server_id, archive_path))
            else:
                commands.append(
                    '  nuocmd create archive --db-name {} --server-id {} --archive-path {} --journal-path {} --restored'  # noqa
                    .format(db_name, server_id, archive_path, journal_path))
        options_str = ''
        for name, value in sorted(db_options.items()):
            options_str += ' \\\n      {} {}'.format(name, value)
        commands.append(
            '  nuocmd create database --db-name {} --te-server-ids {} --default-options{}'  # noqa
            .format(db_name, ' '.join(te_server_ids), options_str))
        # output commands
        self._print('To start database using nuoadmin, execute the following commands:')  # noqa
        for command in commands:
            self._print(command)
        return commands

    def _send_nuoagent_request(self, address, domain_user, domain_password,
                               service, attributes):
        session = entity.Session(address, service=service, timeout=20)
        try:
            session.authorize(domain_user, domain_password)
            return ElementTree.fromstring(
                session.doRequest(attributes=attributes))
        finally:
            session.close()

    def _get_canonical_hostname(self, hostname):
        if hostname in ['localhost', '127.0.0.1']:
            hostname = socket.gethostname()
        canonname = socket.gethostbyaddr(hostname)[0]
        if canonname in ['localhost', '127.0.0.1']:
            return hostname
        return canonname

    def _get_nuoagent_stable_ids(self, address, domain_user, domain_password):
        resp = self._send_nuoagent_request(
            address, domain_user, domain_password, 'DomainState',
            dict(Action='ShowStateMachineState', StateMachineNames='ALL'))
        stable_ids = {}
        for elem in resp.findall('PeerMembershipState/EncodableServer'):
            address = elem.find('Address').attrib['host']
            server_id = self._get_canonical_hostname(address)
            stable_ids[elem.attrib['PeerStableId']] = (server_id, address)
        return stable_ids

    def _get_nuoagent_blacklist(self, address, domain_user, domain_password,
                                server_id_mapping):
        key_prefix = 'configuration/loadbalancer/blacklist/'
        resp = self._send_nuoagent_request(
            address, domain_user, domain_password, 'Configuration',
            dict(Action='GetConfigs', Prefix=key_prefix))
        blacklist = set()
        for elem in resp.findall('Configuration'):
            if elem.text == 'true':
                stable_id = elem.attrib['Key'][len(key_prefix):]
                if stable_id in server_id_mapping:
                    blacklist.add(server_id_mapping[stable_id][0])
        return blacklist

    def _get_nuoagent_database_password(
            self, address, domain_user, domain_password, db_name):
        resp = self._send_nuoagent_request(
            address, domain_user, domain_password, 'Manager',
            dict(Type='GetDatabaseCredentials', Database=db_name))
        return resp.find('Password').text

    def _get_nuoagent_engine_options(self, process):
        resp = ElementTree.fromstring(process.query('Configuration'))
        options = {}
        for elem in resp.findall('Option'):
            if elem.text is not None and elem.text != 'false':
                name = self._without_prefix(elem.get('Name'))
                options[name] = elem.text
        return options

    _VERSION = '0:10000'

    def _build_dpsm_state(
            self, domain, nuoagent_address, domain_user, domain_password,
            server_ids):
        last_archive_id = 0
        last_start_id = 0
        databases = {}
        archives = {}
        processes = {}
        for db in domain.databases:
            # get the password used to encrypted communication among database
            # processes and build database object
            db_password = self._get_nuoagent_database_password(
                nuoagent_address, domain_user, domain_password, db.name)
            databases[db.name] = dict(
                hostAssignments={},
                incarnation=dict(
                    major=1,
                    minor=0),
                name=db.name,
                options={
                    'dba-user': '<UNKNOWN>',
                    'dba-password': '<UNKNOWN>'
                },
                password=db_password,
                state='RUNNING',
                defaultRegionId={})

            # populate map of archive and process objects
            for process in db.processes:
                server_id = self._get_canonical_hostname(process.peer.hostname)
                if server_id not in server_ids:
                    raise RuntimeError('Unknown server ID: ' + server_id)

                # get engine configuration
                options = self._get_nuoagent_engine_options(process)
                # add any database options to database object
                for key, value in options.items():
                    if key in ['ping-timeout', 'dba-user', 'dba-password', 'commit']:
                        databases[db.name]['options'][key] = value

                # since --connect-key is private, the management request to
                # fetch engine configuration does not return it, even if we
                # request hidden options; admins use the connect key to
                # identify local processes, so inject the start ID in its place
                start_id = str(last_start_id)
                if 'connect-key' not in options:
                    options['connect-key'] = start_id

                # remove --geo-region, which is a restricted option
                if 'geo-region' in options:
                    del options['geo-region']

                # add archive object if --archive is present
                if 'archive' in options:
                    archive_id = str(last_archive_id)
                    archives[archive_id] = dict(
                        dbName=db.name,
                        hostId=dict(value=server_id),
                        journalPath=options.get('journal-dir'),
                        path=options['archive'],
                        state='RUNNING',
                        otherArchiveInfos=[])
                    last_archive_id += 1
                    # add pseudo-options used by nuoadmin
                    options['archive-id'] = archive_id
                    options['engine-type'] = 'SM'
                else:
                    options['engine-type'] = 'TE'

                # annoyingly, addresses seem to be advertised from the point of
                # view of the client, so 'localhost' may appear instead of the
                # actual address of the engine; use the address of the host
                # that is stored in the Raft membership
                address = process.address
                if address in ['localhost', '127.0.0.1']:
                    address = server_ids[server_id]

                # add process object
                processes[start_id] = dict(
                    hostId=dict(
                        value=server_id),
                    node=dict(
                        address=address,
                        databaseName=db.name,
                        hostname=process.hostname,
                        nodeId=process.node_id,
                        pid=process.pid,
                        port=process.port,
                        state=process.status,
                        type='TE' if process.is_transactional else 'SM',
                        version=process.version),
                    options=options,
                    requestState='MONITORED',
                    startId=dict(value=start_id))
                last_start_id += 1

        return dict(
            allNodes=processes,
            archivesMap=archives,
            databases=databases,
            exitedNodes={},
            lastArchiveId=last_archive_id,
            lastStartId=last_start_id,
            removedArchives={},
            snapshotVersion=self._VERSION,
            storageGroups=[])

    def _build_kvsm_state(self, blacklist):
        mappings = {}
        if len(blacklist) != 0:
            lb_prefilter = 'and({})'.format(
                ' '.join('not(server_id({}))'.format(server_id)
                         for server_id in blacklist))
            mappings['config/loadBalancerPolicy/__prefilter'] = lb_prefilter
        return dict(
            map=mappings,
            versionName=self._VERSION)

    def _build_membership_state(self, server_ids):
        domain_id = str(uuid.uuid4())
        init_membership = dict(
            id2Address={},
            id2State={},
            id2VersionRange={})
        member_address = {}
        member_state = {}
        member_version = {}

        for server_id in server_ids:
            address = server_id + ':48005'
            init_membership['id2Address'][server_id] = base64.b64encode(address).decode('utf-8')
            init_membership['id2State'][server_id] = 'active'
            init_membership['id2VersionRange'][server_id] = self._VERSION

            member_address[server_id] = address
            member_state[server_id] = 'active'
            member_version[server_id] = self._VERSION

        return dict(
            domainId=domain_id,
            initialMembership=init_membership,
            memberAddress=member_address,
            memberState=member_state,
            memberVersion=member_version,
            version=self._VERSION)

    def _build_state_machine_snapshot(
            self, dpsm_state, kvsm_state, membership_state):
        registry = dict(
            name='BasicRegistry',
            registry=dict(
                (name, dict(name=name))
                for name in ['CatalogStateMachine',
                             'DomainProcessStateMachine',
                             'DomainRegionStateMachine',
                             'KeyValueStore',
                             'MembershipStateMachine']))
        catalog_state = dict(
            activeSMs={},
            initializedSMs=[],
            version=self._VERSION)
        region_state = dict(
            lastRegionId=1,
            regions={
                "0": dict(name='Default')
            },
            snapshotVersionName=self._VERSION)

        return dict(
            lastIncludedIndex=0,
            lastIncludedTerm=0,
            smState=dict(
                registry=registry,
                stateMap={
                    'com.nuodb.nraft.swift.machines.CatalogStateMachine': catalog_state,
                    'com.nuodb.host.raft.DomainRegionStateMachine': region_state,
                    'com.nuodb.host.raft.DomainProcessStateMachine': dpsm_state,
                    'com.nuodb.keyval.statemachine.KeyValueStateMachine': kvsm_state,
                    'com.nuodb.nraft.swift.machines.MembershipStateMachine': membership_state
                }))

    @Subcommand('prepare', 'domain',
                help='generate a nuoadmin domain state capture file from nuoagent state')  # noqa
    @Argument('--nuoagent-address', default='localhost',
              help='the address and port of the nuoagent server')
    @Argument('--domain-user', default=EnvironmentalDefault('domain'),
              help='the domain user for the nuoagent domain')
    @Argument('--domain-password', default=EnvironmentalDefault(),
              help='the domain password for the nuoagent domain')
    def prepare_domain(self, domain_password, domain_user='domain',
                       nuoagent_address='localhost:48004'):
        # assume that all nuoagent servers are on separate hosts and use the
        # hostname as the nuoadmin server ID
        stable_ids = self._get_nuoagent_stable_ids(
            nuoagent_address, domain_user, domain_password)
        server_ids = {}
        duplicates = {}
        for stable_id, (server_id, address) in stable_ids.items():
            if server_id in server_ids:
                duplicates[stable_id] = (server_id, address)
            else:
                server_ids[server_id] = address
        if len(duplicates) != 0:
            raise RuntimeError(
                'Duplicate hostnames found: {}'.format(duplicates))

        # get the hostnames (server IDs) of blacklisted servers and build a
        # prefilter in the LBQuery expression syntax for nuoadmin
        blacklist = self._get_nuoagent_blacklist(
            nuoagent_address, domain_user, domain_password, stable_ids)

        # build state machine snapshot in the format of a domain dump
        domain = entity.Domain(nuoagent_address, domain_user, domain_password)
        try:
            dpsm_state = self._build_dpsm_state(
                domain, nuoagent_address, domain_user, domain_password,
                server_ids)
            kvsm_state = self._build_kvsm_state(blacklist)
            membership_state = self._build_membership_state(server_ids.keys())
            raft_snapshot = self._build_state_machine_snapshot(dpsm_state, kvsm_state,
                                                               membership_state)
            self._print(json.dumps(raft_snapshot, indent=2, sort_keys=True))
        finally:
            domain.disconnect()

    def _get_server_id(self, server_id, this_server):
        """
        Check that `server_id` and `this_server` are not both specified, and if
        necessary get the server ID of the API server. Returns a tuple of the
        server ID and the API server configuration, if the API server
        configuration was obtained.

        :param str server_id:
        :param bool this_server:

        :returns tuple(str, nuodb_mgmt.AdminServerConfig):
        """

        if server_id is not None and this_server:
            raise ValueError('Server ID specified with this_server flag')
        elif server_id is None and not this_server:
            raise ValueError('Server ID not specified without this_server flag')  # noqa

        if server_id is not None:
            return server_id, None

        config = self.conn.get_admin_config()
        server_id = config.get_nested('properties', 'ThisServerId')
        return server_id, config

    # default formatting for 'show' subcommands
    SERVER_FMT = '[{id}] {address} [last_ack = {last_ack:.2f:NEVER}] {raft_state} ({role}, Leader={leader}, log={log_term::?}/{commit_index::?}/{log_index::?}) {observed_state}'  # noqa
    DATABASE_FMT = '{name} [state = {state}]'
    COMMON_PROCESS_FMT = '[{engine_type}] {address::<UNKNOWN ADDRESS>} [start_id = {start_id}] [server_id = {server_id}] [pid = {pid::}] [node_id = {node_id::}]'  # noqa
    PROCESS_FMT = COMMON_PROCESS_FMT + ' [last_ack = {last_ack:5.2f:>60}] {durable_state}:{engine_state}'  # noqa
    # add 'process.' to every field in `COMMON_PROCESS_FMT`
    EXITED_PROCESS_FMT = re.sub(r'\{([^}]*)\}', r'{process.\1}', COMMON_PROCESS_FMT) + ' EXITED({process.durable_state}:{process.engine_state}):{reason} ({exit_code::?})'  # noqa
    COMMON_ARCHIVE_FMT = '[{id}] {server_id} : {archive_path} @ {db_name} [journal_path = {journal_path::}] [observer_storage_groups = {observer_storage_groups::}]'  # noqa
    ARCHIVE_FMT = COMMON_ARCHIVE_FMT + ' {state}'
    REMOVED_ARCHIVE_FMT = COMMON_ARCHIVE_FMT + ' REMOVED({state})'
    REGION_FMT = '[region = {name}]'
    SHORT_PROCESS_FMT = '[{engine_type}] {address} [start_id = {start_id}] [pid = {pid::}] [node_id = {node_id::}] {durable_state}:{engine_state}'  # noqa
    STORAGE_GROUP_FMT = '[{id:>4}] [name = {name}] [state = {state}]'  # noqa
    STORAGE_GROUP_LEADERS_FMT = '[{id:>4}] [name = {name}] [state = {state}]'  # noqa

    @Subcommand('get', 'archives', help='show database archives')
    @Argument('--db-name', required=False, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names_from_archives,
              help='the name of the database to list archives for')
    @Argument('--removed', action='store_true', help='show removed archives')
    def get_archives(self, db_name=None, removed=False):
        archives = self.conn.get_archives(db_name=db_name, removed=removed)
        for archive in archives:
            self._show(archive)
        return archives

    @Subcommand('show', 'archives', help='show database archives')
    @Argument('--db-name', required=False, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names_from_archives,
              help='the name of the database to list archives for')
    @Argument('--removed', action='store_true', help='show removed archives')
    @Argument('--archive-format', default=EnvironmentalDefault(ARCHIVE_FMT),
              help='format string for archives')
    @Argument('--removed-archive-format',
              default=EnvironmentalDefault(REMOVED_ARCHIVE_FMT),
              help='format string for removed archives')
    @Argument('--process-format', default=EnvironmentalDefault(PROCESS_FMT),
              help='format string for processes')
    @Argument('--exited-process-format',
              default=EnvironmentalDefault(EXITED_PROCESS_FMT),
              help='format string for exited processes')
    def show_archives(self, db_name=None, removed=False,
                      archive_format=ARCHIVE_FMT,
                      removed_archive_format=REMOVED_ARCHIVE_FMT,
                      process_format=PROCESS_FMT,
                      exited_process_format=EXITED_PROCESS_FMT):
        archives = self.conn.get_archives(db_name=db_name, removed=removed)
        for archive in archives:
            self._print(get_formatted(removed_archive_format
                                      if removed else archive_format, archive))
            running, exited = self._get_process_for_archive(db_name, archive.id)
            if running:
                self._print('  ' + get_formatted(process_format, running))
            elif exited:
                self._print('  ' + get_formatted(exited_process_format, exited))

    def _get_process_for_archive(self, db_name, archive_id):
        running_processes = list(self.conn.get_processes(
                                    db_name=db_name,
                                    **{'type': 'SM', 'archiveId': str(archive_id)}))
        # if there is a running process on this archive, return it
        if len(running_processes) != 0:
            return running_processes[0], None
        else:
            # if there are exited processes on this archive, return the
            # last one
            exited_processes = self.conn.get_exited_processes(
                db_name=db_name, order_by='databaseIncarnation',
                **{'process.type': 'SM', 'process.archiveId': str(archive_id)})
            if len(exited_processes) != 0:
                return None, exited_processes[-1]
        return None, None

    CORE_FILE_OUTPUT_FMT = 'core.nuodb.{db_name}.{hostname::}.{start_id}.gz'

    @Subcommand('get', 'core-file',
                help='download the core file for a running database process')
    @Argument('--start-id', required=True,
              help='the start ID of the process to get core file for')
    @Argument('--output', default=EnvironmentalDefault(CORE_FILE_OUTPUT_FMT),
              help='the output file or format for output file')
    @Argument('--output-dir', required=False,
              help='the directory to store core file in; the actual file path will be resolved relative to this directory')  # noqa
    @Argument('--socket-read-timeout', default=None, type=float,
              help="The maximum time (in seconds) to block waiting for data from an engine")
    def get_core_file(self, start_id, output=CORE_FILE_OUTPUT_FMT,
                      output_dir=None, socket_read_timeout=None):
        process = self.conn.get_process(start_id)
        stream = self.conn.stream_core_file(start_id, read_timeout=socket_read_timeout)
        filename = get_formatted(output, process)
        if output_dir is not None:
            filename = os.path.join(output_dir, filename)
        self._download_stream(stream, filename)

    def _download_stream(self, stream, filename):
        with open(filename, 'wb') as f:
            for msg in stream:
                # Data is streamed from a pynuodb session which uses the RC4Cipher, which is using latin-1 encoding.
                f.write(msg.encode('latin-1'))

    def _supports_query_request(self, process):
        """
        Predicate to determine if a process can service query requests, which
        are requests of the form `<Request Service="Query"/>`. The message
        dispatching was changed with 4.0 to support servicing of these
        requests.
        """

        version = process.get('version')
        if version is None:
            return False
        match = re.match('([0-9]+).*', version)
        if not match:
            return False
        return int(match.group(1)) >= 4

    def _send_query_request(self, db_name, request_type, child_msg=None):
        """
        Send a query request to some database process.
        """

        processes = list(self.conn.get_processes(db_name))
        request_processes = list(filter(self._supports_query_request, processes))
        if len(request_processes) == 0:
            raise RuntimeError(
                'Database {} has no processes able to service request'
                .format(db_name))

        for process in request_processes:
            try:
                return processes, self.conn.send_query_request(
                    process.start_id, request_type, child_msg)
            except Exception as e:
                self._print(
                    'WARNING: Unable to send request to start ID {}: {}',
                    process.start_id, e)

        raise RuntimeError('Unable to send request to any process')

    @Subcommand('upgrade', 'database-version',
                help='upgrade the database version')
    @Argument('--db-name', required=True,
              completer=CommandProcessor.get_db_names,
              help='the database upgrade')
    @MutuallyExclusive(
        Argument('--version', help='the version to upgrade to'),
        Argument('--max-version', action='store_true',
                 help='upgrade to maximum available version'),
        required=True)
    def upgrade_database_version(self, db_name, version=None,
                                 max_version=False):
        try:
            self.conn.upgrade_database_version(db_name, version, max_version)
        except nuodb_mgmt.AdminException as e:
            if e.code == 404:
                # We don't have newly enough version of NuoAdmin where the REST method is available
                # or NuoAdmin domain is not fully upgraded.
                # Use the legacy approach of sending management request directly to engine
                try:
                    if max_version:
                        version = 'MAX_VERSION'
                    if not version:
                        raise ValueError('No version supplied')
                    _, response = self._send_query_request(
                        db_name, 'Upgrade', '<Args version="{}"/>'.format(version))
                    succeeded = response.attrib.get('succeeded')
                    if succeeded != '1':
                        raise RuntimeError(str(response.attrib.get('message')))
                except Exception as e:
                    raise RuntimeError('Unable to upgrade database: ' + str(e))
            else:
                raise

    @Subcommand('show', 'database-versions',
                help='show the database version for all processes in the database')  # noqa
    @Argument('--db-name', required=True,
              completer=CommandProcessor.get_db_names,
              help='the database to show database versions for')
    @Argument('--process-format', default=EnvironmentalDefault(PROCESS_FMT),
              help='format string for processes')
    def show_database_versions(self, db_name, process_format=PROCESS_FMT):
        try:
            processes = self.conn.get_processes(db_name)
            database_versions = self.conn.get_database_versions(db_name)
            processes_map = {process.start_id: process for process in processes}
            available_versions = []
            for version in database_versions.available_versions:
                available_versions.append(
                    (version['versionId'], version['name']))
            versions = {}
            for release, version in database_versions.all_versions.items():
                versions[release] = (version['version'], version['versionId'])
            process_versions = {}
            for release, startIds in database_versions.process_versions.items():
                if release not in process_versions:
                    process_versions[release] = []
                for startId in startIds:
                    process_versions[release].append(processes_map[startId])

            effective_version_id = database_versions.effective_version
            effective_version = database_versions.version
            max_version_id = database_versions.max_version
        except nuodb_mgmt.AdminException as e:
            if e.code == 404:
                # We don't have newly enough version of NuoAdmin where the REST method is available
                # or NuoAdmin domain is not fully upgraded.
                # Use the legacy approach of sending management request directly to engine
                processes, response = self._send_query_request(db_name, 'DbVersion')
                available_versions = []
                for version in response.findall('Versions/Version'):
                    available_versions.append(
                        (version.attrib['id'], version.attrib['name']))
                versions = {}
                process_versions = {}
                for process_version in response.findall('Nodes/Node'):
                    node_id = process_version.attrib['id']
                    version_id = process_version.attrib['versionId']
                    version = process_version.attrib['version']
                    release = process_version.attrib['release']
                    versions[release] = (version, version_id)
                    if release not in process_versions:
                        process_versions[release] = []
                    for process in processes:
                        if process.node_id == int(node_id):
                            process_versions[release].append(process)

                effective_version_id = response.attrib['EffectiveVersion']
                effective_version = response.attrib['Version']
                max_version_id = response.attrib['MaxVersion']
            else:
                raise

        self._print(
            'effective version ID: {}, effective version: {}, max version ID: {}',  # noqa
            effective_version_id, effective_version, max_version_id)

        self._print('Available versions:')
        for version_id, version in available_versions:
            self._print('  version ID: {}, version: {}', version_id, version)

        self._print('Process versions:')
        for release, (version, version_id) in sorted(versions.items()):
            self._print('  version ID: {}, version: {}, release: {}',
                        version_id, version, release)
            for process in sorted(process_versions[release],
                                  key=lambda p: p.start_id):
                self._print('    ' + get_formatted(process_format, process))

    @Subcommand('get', 'database-connectivity',
                help='get the connectivity graph for a database')
    @Argument('--db-name', required=True,
              completer=CommandProcessor.get_db_names,
              help='the database to get connectivity information for')
    @Argument('--with-node-ids', action='store_true',
              help='whether to return connectivity graph with node IDs as keys instead of start IDs')  # noqa
    @Argument('--suppress-errors', action='store_true',
              help='whether to suppress errors messages when collecting connectivity information')  # noqa
    def get_database_connectivity(self, db_name, with_node_ids=False,
                                  suppress_errors=False):
        db_connectivity, errors = self.conn.get_database_connectivity(
            db_name, with_node_ids=with_node_ids)
        self._show(db_connectivity)
        if not suppress_errors and len(errors) != 0:
            self._show(errors)

    @Subcommand('show', 'database-connectivity',
                help='show the connectivity graph for a database')
    @Argument('--db-name', required=True,
              completer=CommandProcessor.get_db_names,
              help='the database to show connectivity graph for')
    @Argument('--with-node-ids', action='store_true',
              help='whether to show connectivity graph with node IDs as keys instead of start IDs')  # noqa
    @Argument('--last-ack-threshold', default='5', type=int,
              help='the threshold in seconds to display time since last ack')
    def show_database_connectivity(self, db_name, with_node_ids=False,
                                   last_ack_threshold=5):
        db_connectivity, errors = self.conn.get_database_connectivity(
            db_name, with_node_ids=with_node_ids)
        # define constants to signal error conditions
        unreachable_by_engine = 'X'
        unreachable_by_admin = '?'
        unexpected_message_format = '!'
        keys = sorted(list(db_connectivity.keys()) + list(errors.keys()))
        normalized = {}
        for i, j in ((i, j) for i in keys for j in keys):
            if i not in db_connectivity:
                # no row for this process means the admin was not able to query
                # it for connectivity information
                normalized[(i, j)] = unreachable_by_admin
            elif i == j:
                # node does not connect to itself; leave cell blank
                normalized[(i, j)] = ''
            elif j not in db_connectivity[i]:
                # no column for this node in this row means node i does not
                # have node j in its node list
                normalized[(i, j)] = unreachable_by_engine
            elif 'lastAckDeltaInMilliSeconds' not in db_connectivity[i][j]:
                # node j is in node i's node list, but we do not have last ack
                # information for some reason
                normalized[(i, j)] = unexpected_message_format
            else:
                # add last ack to table if it is larger than threshold
                time_ms = db_connectivity[i][j]['lastAckDeltaInMilliSeconds']
                if time_ms / 1000.0 >= last_ack_threshold:
                    normalized[(i, j)] = AdminCommands._get_best_time_unit(time_ms)  # noqa
                else:
                    normalized[(i, j)] = ''
        # get cell width and output header
        max_len = max(map(len, list(map(str, keys)) + list(normalized.values())))
        cell_width = max_len + 2
        self.out.write(cell_width * ' ')
        for key in keys:
            self.out.write(str(key).center(cell_width))
        self.out.write('\n')
        # output each row of connectivity table
        for i in keys:
            self.out.write(str(i).center(cell_width))
            for j in keys:
                self.out.write(normalized[(i, j)].center(cell_width))
            self.out.write('\n')
        # output legend describing what each symbol means
        self.out.writelines([
            '\n',
            'Legend:\n',
            '  {}: node at this row does not consider node at this column a peer\n'.format(unreachable_by_engine),  # noqa
            '  {}: node at this row could not be queried for connectivity information\n'.format(unreachable_by_admin),  # noqa
            '  {}: node at this row does not have expected metadata for node at this column\n'.format(unexpected_message_format),  # noqa
            '  [0-9]+[hms]: time since node at this row last heard from node at this column\n'])  # noqa

    @staticmethod
    def _get_best_time_unit(time_ms):
        millisecond = 1
        second = 1000 * millisecond
        minute = 60 * second
        hour = 60 * minute
        day = 24 * hour
        units = [('d', day),
                 ('h', hour),
                 ('m', minute),
                 ('s', second),
                 ('ms', millisecond)]
        for suffix, unit in units:
            if time_ms >= unit:
                break
        return str(time_ms // unit) + suffix

    LOG_FMT = '{msg[Time]}: ' + SHORT_PROCESS_FMT + ' =>[{msg[Options]}] {msg[LogMessage]}'  # noqa

    @Subcommand('show', 'log-messages',
                help='show log messages from running database processes')
    @Argument('--log-options', required=True, nargs='+',
              default=EnvironmentalDefault(),
              help='the log levels and categories to enable')
    @MutuallyExclusive(
        Argument('--db-name', help='the database to log messages for'),
        Argument('--start-id', help='the running process to log messages for'),
        required=False)
    @Argument('--log-format', default=EnvironmentalDefault(LOG_FMT),
              help='format string for process log messages')
    @Argument('--engine-file', help='write logging to this file on engine hosts')
    def show_log_messages(self, log_options, db_name=None, start_id=None,
                          log_format=LOG_FMT, engine_file=None):
        for process_msg in self._get_messages(
                log_options, db_name, start_id, include_process=True,
                engine_file=engine_file):
            # remove trailing newline from log message
            process_msg['msg']['LogMessage'] = process_msg['msg'].get(
                'LogMessage', '').rstrip()
            self._print(get_formatted(log_format, process_msg))

    @Subcommand('get', 'log-messages',
                help='get log messages from running database processes')
    @Argument('--log-options', required=True, nargs='+',
              default=EnvironmentalDefault(),
              help='the log levels and categories to enable')
    @MutuallyExclusive(
        Argument('--db-name', help='the database to log messages for'),
        Argument('--start-id', help='the running process to log messages for'),
        required=False)
    @Argument('--engine-file', help='write logging to this file on engine hosts')
    def get_log_messages(self, log_options, db_name=None, start_id=None,
                         engine_file=None):
        for process_msg in self._get_messages(log_options, db_name, start_id,
                                              engine_file=engine_file):
            self._show(process_msg)

    @Subcommand('get', 'stats',
                help='get stats from running database processes')
    @MutuallyExclusive(
        Argument('--db-name', help='the database to get stats for'),
        Argument('--start-id', help='the running process to get stats for'),
        required=False)
    def get_stats(self, db_name=None, start_id=None):
        for process_msg in self._get_messages(None, db_name, start_id):
            self._show(process_msg)

    def _get_messages(self, log_options, db_name, start_id,
                      include_process=False, engine_file=None):
        # message generator is for a single process if `start_id` is specified;
        # otherwise, it is aggregated
        if start_id is not None:
            msg_stream = self.conn.monitor_process(
                start_id, log_options, engine_file)
        else:
            msg_stream = self.conn.monitor_database(
                db_name, log_options, engine_file, keep_open=True, err=self.err)
        # filter messages by name based on whether we are streaming stats or
        # log messages.  For logs we want to catch messages and failures
        message_name = 'Status' if log_options is None else 'LogMessage'
        for process, message in msg_stream:
            if message.tag == 'LogFailure':
                # Since there is no way to fix this, quit on error and let the
                # user retry.
                raise RuntimeError(message.attrib['Text'])
            json_msg = nuodb_mgmt.xml_to_json(message, message_name)
            if len(json_msg) != 0:
                # add timestamp to message attributes; TODO: in the future, we
                # may want to include timestamp on the sending side
                if 'Time' not in json_msg:
                    json_msg['Time'] = self._get_timestamp()
                # combine process and message; if `include_process`, return all
                # process attributes; otherwise just return start ID
                if include_process:
                    process._dict['msg'] = json_msg
                    yield process
                else:
                    yield dict(startId=process.start_id, msg=json_msg)

    @Subcommand('create', 'archive', help='create a database archive')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database for the archive')
    @MutuallyExclusive(
        Argument('--server-id', default=None,
                 completer=CommandProcessor.get_server_ids,
                 help='the ID of the server for the archive'),
        Argument('--is-external', action='store_true',
                 help='whether this archive will be externally started'),
        required=True)
    @Argument('--archive-path', required=True,
              help='the path of the archive directory')
    @Argument('--journal-path', required=False,
              help='the path of the journal directory')
    @Argument('--snapshot-archive-path', required=False,
              help='the path of the snapshot archive directory')
    @Argument('--restored', action='store_true',
              help='whether the archive being restored from existing data')
    @Argument('--archive-id', required=False,
              help='if specified, the archive ID of the removed archive')
    @MutuallyExclusive(
        Argument('--passive', action='store_true',
                 help='whether the archive should passively observe all storage groups that it serves'),  # noqa
        Argument('--observes', nargs='*',
                 help='list of storage groups the archive should observe passively'),
        Argument('--observers', nargs='*',
                 dest='observes', help=argparse.SUPPRESS),  # deprecated; this is kept for backward-compatibility
        required=False)
    def create_archive(self, db_name, server_id, archive_path,
                       journal_path=None, snapshot_archive_path=None,
                       restored=False, archive_id=None, is_external=False,
                       observes=None, passive=False):
        # this is a sanity check; argparse should prevent this from happening
        if is_external and server_id is not None:
            raise ValueError('Server ID specified with is_external flag')
        elif not is_external and server_id is None:
            raise ValueError('Server ID unspecified without is_external flag')

        if passive:
            observes = ['*']

        archive = self.conn.create_archive(
            db_name, server_id, archive_path, journal_path,
            snapshot_archive_path, restored, archive_id, observes)
        self._show(archive)
        return archive

    @Subcommand('delete', 'archive',
                help='delete a non-running database archive')
    @Argument('--archive-id', required=True,
              completer=CommandProcessor.get_non_running_archive_ids,
              help='the ID of the archive to delete')
    @Argument('--purge', action='store_true',
              help='whether to remove the archive permanently')
    def delete_archive(self, archive_id, purge=False):
        self.conn.delete_archive(archive_id, purge)

    @Subcommand('get', 'storage-groups',
                help='get storage groups for a database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database to list storage groups for')
    def get_storage_groups(self, db_name):
        storage_groups = self.conn.get_storage_groups(db_name)
        for sg in storage_groups:
            self._show(sg)
        return storage_groups

    @Subcommand('show', 'storage-groups',
                help='show storage groups for a database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database to list storage groups for')
    @Argument('--storage-group-format',
              default=EnvironmentalDefault(STORAGE_GROUP_FMT),
              help='format string for storage groups')
    @Argument('--process-format',
              default=EnvironmentalDefault(SHORT_PROCESS_FMT),
              help='format string for leader candidate processes')
    def show_storage_groups(self, db_name,
                            storage_group_format=STORAGE_GROUP_FMT,
                            process_format=SHORT_PROCESS_FMT):
        storage_groups = self.conn.get_storage_groups(db_name)
        processes = {}
        for p in self.conn.get_processes(db_name):
            processes[p.start_id] = p

        self._print('Storage groups for database: ' + db_name)
        for sg in storage_groups:
            self._print('  ' + get_formatted(storage_group_format, sg))

            if not sg.leader_candidates:
                self._print('    ' + 'No Leader Candidates available')
            else:
                self._print('    ' + 'Leader Candidates:')
                for lc in sg.leader_candidates:
                    process = processes.get(lc, None)
                    if not process:
                        self._print('Unknown process ID ' + lc)
                    else:
                        self._print('      ' + get_formatted(process_format,
                                                             process))

    # TODO: add completion for storage groups

    @Subcommand('add', 'storage-group',
                help='add a storage group to a set of archives')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database')
    @Argument('--sg-name', required=True,
              help='the name of the storage group to add')
    @Argument('--archive-ids', nargs='+', required=True,
              completer=CommandProcessor.get_archive_ids,
              help='the IDs of the archives to add storage group to')
    def add_storage_group(self, db_name, sg_name, archive_ids):
        for archive_id in archive_ids:
            self.conn.add_storage_group(db_name, sg_name, archive_id)

    @Subcommand('remove', 'storage-group',
                help='remove a storage group from a set of archives')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database')
    @Argument('--sg-name', required=True,
              help='the name of the storage group to remove')
    @Argument('--archive-ids', nargs='+', required=True,
              completer=CommandProcessor.get_archive_ids,
              help='the IDs of the archives to remove storage group from')
    def remove_storage_group(self, db_name, sg_name, archive_ids):
        for archive_id in archive_ids:
            self.conn.remove_storage_group(db_name, sg_name, archive_id)

    @Subcommand('delete', 'storage-group',
                help='remove a storage group from the database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database')
    @Argument('--sg-name', required=True,
              help='the name of the storage group to delete')
    def delete_storage_group(self, db_name, sg_name):
        self.conn.delete_storage_group(db_name, sg_name)

    @Subcommand('get', 'servers', help='get admin servers')
    def get_servers(self):
        servers = self.conn.get_servers()
        for server in servers:
            self._show(server)
        return servers

    @Subcommand('delete', 'server',
                help='remove a server from the raft membership remove all process and archive state for a server')  # noqa
    @Argument('--server-id', required=True,
              completer=CommandProcessor.get_server_ids,
              help='the ID of the server to remove from the membership')
    def delete_server(self, server_id):
        self.conn.delete_server(server_id)

    @Subcommand('delete', 'server-state',
                help='remove all process and archive state for a server')
    @Argument('--server-id', required=True,
              completer=CommandProcessor.get_server_ids,
              help='the ID of the server to remove from domain state')
    def delete_server_state(self, server_id):
        self.conn.delete_server_state(server_id)

    @Subcommand('shutdown', 'server', help='shutdown a server')
    @MutuallyExclusive(
        Argument('--server-id',
                 completer=CommandProcessor.get_server_ids,
                 help='the ID of the server to shut down'),
        Argument('--this-server', action='store_true',
                 help='shutdown the server hosting this API server'),
        required=True)
    @Argument('--evict-local', action='store_true', required=False,
              help='evict processes local to the server being shutdown; this command may not actually cause an unresponsive process to shutdown, in which case it would have to be killed manually by the user')  # noqa
    def shutdown_server(self, server_id, this_server=False, evict_local=False):
        server_id, config = self._get_server_id(server_id, this_server)
        self.conn.shutdown_server(server_id, evict_local=evict_local)

    @staticmethod
    def _get_timestamp(time_sec=None, precision='ms'):
        """
        :param int time_sec: the time to generate timestamp for; if not
                             specified, use the current time
        :param str precision: one of 's', 'ms', 'us'

        :returns str:
        """

        if time_sec is None:
            time_sec = time.time()
        dt = datetime.datetime.fromtimestamp(time_sec)
        ret = dt.strftime('%Y-%m-%dT%H:%M:%S.%f')
        if precision == 'us':
            # timestamp is already in microseconds precision
            return ret
        ts_sec, frac = ret.rsplit('.', 1)
        if precision == 's':
            # return timestamp not including fractions of a second
            return ts_sec
        else:
            # include three decimal places for millisecond precision
            return '.'.join([ts_sec, frac[:3]])

    @Subcommand('show', 'domain', help='show summary of domain state')
    @Argument('--server-format', default=EnvironmentalDefault(SERVER_FMT),
              help='format string for servers')
    @Argument('--db-format', default=EnvironmentalDefault(DATABASE_FMT),
              help='format string for databases')
    @Argument('--process-format', default=EnvironmentalDefault(PROCESS_FMT),
              help='format string for processes')
    @Argument('--client-token', required=False,
              help='message to display in admin log')
    def show_domain(self, server_format=SERVER_FMT,
                    db_format=DATABASE_FMT,
                    process_format=PROCESS_FMT, client_token=None):
        # display version info
        if client_token is None:
            client_token = hashlib.sha1(str.encode(str(time.time()))).hexdigest()
        info = self.conn.log_message(client_token)
        timestamp = self._get_timestamp(info['serverTimeMillis'] / 1000.0)
        self._print('server version: {}, server license: {}',
                    info['serverVersion'], info.get('licenseType', 'None'))
        self._print('server time: {}, client token: {}',
                    timestamp, client_token)

        # display server state
        self._print('Servers:')
        servers = self.conn.get_servers()
        for server in servers:
            fmt = server_format
            if server.is_local:
                fmt += ' *'
            self._print('  ' + get_formatted(fmt, server))

        # display database and process state
        self._print('Databases:')
        disconnected_servers = self._get_disconnected_servers(servers)
        for database in self.conn.get_databases():
            self._print('  ' + get_formatted(db_format, database))
            self._print_database_processes(
                database, process_format, disconnected_servers)

    def _get_disconnected_servers(self, servers=None):
        if servers is None:
            servers = self.conn.get_servers()
        return set(server.id for server in servers if not server.is_connected)

    @Subcommand('show', 'database', help='show database state')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the database name')
    @MutuallyExclusive(
        Argument('--num-incarnations', type=int, default=1,
                 help='the number of incarnations to show starting from current'),  # noqa
        Argument('--all-incarnations', action='store_true',
                 help='show all incarnations'),
        Argument('--skip-exited', action='store_true',
                 help='skip exited processes'),
        required=False)
    @Argument('--db-format', default=EnvironmentalDefault(DATABASE_FMT),
              help='format string for databases')
    @Argument('--process-format', default=EnvironmentalDefault(PROCESS_FMT),
              help='format string for processes')
    @Argument('--exited-process-format',
              default=EnvironmentalDefault(EXITED_PROCESS_FMT),
              help='format string for exited processes')
    def show_database(self, db_name, num_incarnations=1,
                      all_incarnations=False, skip_exited=False,
                      db_format=DATABASE_FMT, process_format=PROCESS_FMT,
                      exited_process_format=EXITED_PROCESS_FMT):
        disconnected_servers = self._get_disconnected_servers()
        database = self.conn.get_database(db_name)
        self._show(database)
        self._print('  ' + get_formatted(db_format, database))
        self._print_database_processes(
            database, process_format, disconnected_servers)
        if skip_exited is False:
            low_incarnation = (
                max(database.incarnation[0] - num_incarnations + 1, 1)
                if not all_incarnations else 1)
            self._print_exited_database_processes(
                db_name, low_incarnation, database.incarnation[0],
                exited_process_format)

    def _raise_on_failed_process(self, db_name, low_incarnation, msg):
        # replace output stream with string stream so that we can capture
        # output and raise an exception with it as in the message
        out = self.out
        self.out = StringIO()
        try:
            # show database incarnations starting from low_incarnation
            database = self.conn.get_database(db_name)
            self._show(database)
            self._print('  ' + get_formatted(AdminCommands.DATABASE_FMT,
                                             database))
            self._print_database_processes(database, AdminCommands.PROCESS_FMT,
                                           self._get_disconnected_servers())
            self._print_exited_database_processes(
                db_name, low_incarnation, database.incarnation[0],
                AdminCommands.EXITED_PROCESS_FMT)
            raise RuntimeError('{}\n\n{}'.format(msg, self.out.getvalue()))
        finally:
            self.out.close()
            self.out = out

    @Subcommand('get', 'server-config',
                help='get the NuoAdmin service server configuration')
    @MutuallyExclusive(
        Argument('--server-id',
                 completer=CommandProcessor.get_server_ids,
                 help='the ID of the NuoAdmin server'),
        Argument('--this-server', action='store_true',
                 help='get the configuration of the API server'),
        required=True)
    @Argument('--property-key', required=False,
              help='get only a specific property')
    def get_server_config(self, server_id, property_key=None,
                          this_server=False):
        server_id, config = self._get_server_id(server_id, this_server)
        if config is None:
            config = self.conn.get_admin_config(server_id)

        if property_key is None and self.show_json:
            self._show(config._dict)
            return config

        prefix = '   '
        prefix2 = 2 * prefix
        sep = ' = '
        if property_key is not None:
            if property_key in config.properties.keys():
                v = config.properties[property_key]
                self._print('{}{}{}', property_key, sep, v)
            else:
                self._print('Property key [{}] does not exist',
                            property_key)
            return

        self._print('properties:')
        self._print_map(config.properties, prefix, sep)

        self._print('\ninitialMembership:')
        d = config.initial_membership
        for key in d:
            self._print(prefix + key)
            self._print_map(d[key], prefix2, sep)

        self._print('\notherServices:')
        for key in config.other_services:
            self._print(prefix + key)

        self._print('\nstatsPlugins:')
        d = config.stats_plugins
        for key in d:
            self._print(prefix + key)
            for d2 in d[key]:
                self._print_map(d2, prefix2, sep)

        self._print('\nloadBalancers:')
        d = config.load_balancers
        for key in d:
            self._print(prefix + key)
            d2 = d[key]
            self._print_map(d2, prefix2, sep)
        return config

    def _print_map(self, d, prefix, sep='='):
        for key in sorted(d):
            self._print('{}{}{}{}', prefix, key, sep, d[key])

    @Subcommand('get', 'kubernetes-config',
                help='get the NuoDB Kubernetes configuration')
    def get_kubernetes_config(self):
        config = self.conn.get_kubernetes_config()
        if self.show_json:
            self._show(config)
        else:
            self._print('stateful sets:')
            self._show(config.stateful_sets)

            self._print('\ndeployments:')
            self._show(config.deployments)

            self._print('\ndaemon sets:')
            self._show(config.daemon_sets)

            self._print('\nvolumes:')
            self._show(config.volumes)

            self._print('\npods:')
            self._show(config.pods)
        return config

    @Subcommand('get', 'server-logs',
                help='download the server logs for the API server')
    @Argument('--output', required=False,
              help='the output file or directory to save the ZIP file containing server logs to')  # noqa
    @Argument('--include-core-files', action='store_true',
                  help='whether to include core files of exited engine processes in the logs ZIP file')  # noqa
    @Argument('--dump-threads', action='store_true',
              help='whether to include a thread dump of the admin process in the logs ZIP file')  # noqa
    @Argument('--unpack', action='store_true',
              help='whether to unpack the downloaded ZIP file')
    @Argument('--modified-after', required=False,
              type=CommandProcessor.datetime_from_str(),
              help="collect files modified after specified date. The value is converted to epoch timestamp using the client timezone. Timestamp must be in the form '{}'".format(CommandProcessor.DEFAULT_DATETIME_EXAMPLE_STRING))
    def get_server_logs(self, output=None, include_core_files=False,
                        dump_threads=False, unpack=False, modified_after=None):
        if modified_after is not None:
            # Convert it to epoch timestamp
            modified_after = int(time.mktime(modified_after.timetuple()))
        self._download_logs(self.conn, include_core_files, dump_threads, output=output,
                            logger=self.out, unpack=unpack, modified_after_epoch=modified_after)

    @Subcommand('capture', 'domain-state',
                help='capture the domain state of the API server')
    @Argument('--output', required=False,
              help='the output file to save captured domain state to')
    def capture_domain_state(self, output=None):
        domain_state = self.conn.capture_domain_state()
        formatted = json.dumps(domain_state, indent=2, sort_keys=True)
        if output is not None:
            with open(output, 'w') as f:
                f.write(formatted)
        else:
            self._print(formatted)
        return domain_state

    @Subcommand('show', 'raftlog',
                help='display contents of Raft log')
    @Argument('--nuoadmin-jar', required=True,
              default=EnvironmentalDefault(from_nuodb_home('jar', 'nuoadmin.jar')),
              help='path to nuoadmin.jar')
    @Argument('--raftlog-dir', required=True,
              default=EnvironmentalDefault(get_raftlog_dir()),
              help='path to raftlog file')
    @Argument('--one-line', action='store_true',
              help='whether to display the contents of each entry in one line')
    def show_raftlog(self, nuoadmin_jar, raftlog_dir, one_line=False):
        args = ['java'] + get_java_opts()
        args.extend(['-jar', nuoadmin_jar, '--show-raft-log', raftlog_dir])
        if one_line:
            args.append('--one-line')
        subprocess.call(args)

    @Subcommand('add', 'trusted-certificate',
                help='add a trusted certificate to all admin servers and engine processes')  # noqa
    @Argument('--alias', required=True,
              help='the alias of the certificate; this is a name used to refer to the certificate')  # noqa
    @Argument('--cert', required=True,
              help='the certificate to add to truststore as a PEM-encoded file')  # noqa
    @MutuallyExclusive(
        Argument('--timeout', type=int,
                 help='the number of seconds to wait for the certificate to be propagated to all admin servers and engine processes'),  # noqa
        Argument('--no-wait', action='store_true',
                 help='do not wait for certificate to be propagated'),
        required=True)
    def add_trusted_certificate(self, alias, cert, timeout=None,
                                no_wait=False):
        with open(cert) as f:
            if no_wait:
                timeout = 0
            self._add_trusted_certificate(alias, f.read(), timeout)

    def _extract_certificate(self, pem_encoded_data):
        header = '-----BEGIN CERTIFICATE-----'
        footer = '-----END CERTIFICATE-----'
        start_index = pem_encoded_data.find(header)
        end_index = pem_encoded_data.rfind(footer)
        if start_index < 0 or end_index < 0:
            raise ValueError('Unexpected format for certificate PEM')
        end_index += len(footer)
        return pem_encoded_data[start_index:end_index]

    def _add_trusted_certificate(self, alias, certificate_pem, timeout):
        certificate_pem = self._extract_certificate(certificate_pem)
        self.conn.add_trusted_certificate(alias, certificate_pem, timeout)

    @Subcommand('remove', 'trusted-certificate',
                help='remove a trusted certificate from all admin servers and engine processes')  # noqa
    @Argument('--alias', required=True,
              help='the alias of the trusted certificate to remove')
    def remove_trusted_certificate(self, alias):
        self.conn.remove_trusted_certificate(alias)

    @Subcommand('get', 'certificate-info',
                help='get certificate data for the entire domain, including all admin server certificates, all database process certificates, and trusted certificates for all admin servers and database processes')  # noqa
    def get_certificate_info(self):
        cert_info = self.conn.get_certificate_info()
        self._show(cert_info)
        return cert_info

    def _download_logs(self, conn, include_core_files, dump_threads, output=None, logger=None,
                       unpack=False, modified_after_epoch=None, ignore=None):
        """
        :param nuodb_mgmt.AdminConnection conn: the client object to use
        :param include_core_files: whether to collect core files in the crash dir
        :param dump_threads: whether to dump threads of running processes
        :param str output: output file or directory
        :param file logger: the file-like object to log messages to
        :param bool unpack: whether to unpack the ZIP file
        """

        downloader = conn.get_admin_logs(dump_threads, include_core_files,
                                         modified_after_epoch, ignore)
        if output is not None and os.path.isdir(output):
            downloader.set_output_dir(output)
        else:
            downloader.set_output_file(output)
        downloader.download(logger=logger)
        # if --unpack was specified, unpack ZIP file and delete it
        if unpack:
            self._print('Unpacking {} ...', downloader.output_file)
            with zipfile.ZipFile(downloader.output_file) as zf:
                zf.extractall(os.path.dirname(downloader.output_file))
            os.remove(downloader.output_file)

    @Subcommand('get', 'diagnose-info',
                help='download server logs for all reachable admin processes and cores for all reachable engine processes')  # noqa
    @Argument('--output-dir', default=os.getcwd(),
              help='the directory to store diagnose info in')
    @MutuallyExclusive(
        Argument('--no-pack', action='store_true',
                  help='if specified, do not pack all diagnose info into a single ZIP file'),  # noqa
        Argument('--print-to-stdout', action='store_true',
                  help='Print packed zip to stdout'),
        required=False)
    @Argument('--include-cores', action='store_true',
              help='whether to include cores from engine processes; this is shorthand for "--collect-cores --live-cores"')
    @Argument('--live-cores', action='store_true',
              help='whether to stream live cores from running engine processes')
    @Argument('--collect-cores', action='store_true',
              help='whether to collect cores created by engine processes')
    @Argument('--modified-after', required=False,
              type=CommandProcessor.datetime_from_str(),
              help="collect files modified after specified date. The value is converted to epoch timestamp using the client timezone. Timestamp must be in the form '{}'".format(CommandProcessor.DEFAULT_DATETIME_EXAMPLE_STRING))
    @Argument('--socket-read-timeout', default=None, type=float,
              help="The maximum time (in seconds) that we will block waiting for data from an engine")  # noqa
    # TODO: re-enable collecting of metadata once DB-23096 is fixed
    # @Argument('--include-metadata', action='store_true',
    #           help='whether to include metadata from engine processes')
    # @Argument('--include-all', action='store_true',
    #           help='whether to include metadata and cores from engine processes; this is shorthand for "--include-metadata --include-cores"')  # noqa
    def get_diagnose_info(self, output_dir=os.getcwd(),
                          no_pack=False, print_to_stdout=False,
                          include_metadata=False, include_cores=False,
                          include_all=False, collect_cores=False,
                          live_cores=False, modified_after=None,
                          socket_read_timeout=None):
        timestamp = self._get_timestamp(precision='s').replace(':', '-')
        base = os.path.join(output_dir, 'diagnose-' + timestamp)
        admin_dir = os.path.join(base, 'admin')
        engine_dir = os.path.join(base, 'engine')
        os.makedirs(admin_dir)
        os.makedirs(engine_dir)
        threads = []

        self.disable_print = print_to_stdout

        if modified_after is not None:
            # Convert it to epoch timestamp
            modified_after = int(time.mktime(modified_after.timetuple()))

        # exclude all directories with names 'diagnose-<timestamp>', including
        # the one just created, that may be within NUODB_LOGDIR; this prevents
        # an indefinite loop where the admin discovers files that were
        # generated by the diagnose and then streams those to the client, which
        # generate more files for the admin to discover
        diagnose_regex = r'^diagnose-[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}([.]zip)?$'
        # create threads to download all admin logs
        for server in self.conn.get_servers():
            thread = threading.Thread(
                name=server.id,
                target=self._get_admin_diagnose_info,
                args=(server, admin_dir,
                      include_cores or include_all or collect_cores,
                      modified_after, diagnose_regex))
            thread.start()
            threads.append(thread)
        # create threads to download all engine cores; only MONITORED engine
        # processes can service management requests
        for process in self.conn.get_processes(order_by='dbName',
                                               durableState='MONITORED'):
            # diagnose info for engine will be stored in directory
            # '<DB name>_<hostname>_<start ID>'
            hostname = process.get('hostname', process.get('address'))
            descriptor = '{}_{}_{}'.format(
                process.db_name,
                hostname,
                process.start_id)
            thread = threading.Thread(
                name=descriptor,
                target=self._get_engine_diagnose_info,
                args=(process, engine_dir, descriptor,
                      include_metadata or include_all,
                      include_cores or include_all or live_cores,
                      socket_read_timeout))
            thread.start()
            threads.append(thread)

        # wait for all threads to finish
        self._wait_for_threads(threads)

        # unless --no-pack was specified, pack all files into a single ZIP file
        # and delete directory containing intermediate files
        zip_file = base + '.zip'

        if not no_pack:
            with zipfile.ZipFile(zip_file if not print_to_stdout else self.out, 'w', allowZip64=True) as zf:
                self._print('Packing all files into {} ...', base + '.zip')
                for dirname, _, filenames in os.walk(base):
                    for filename in filenames:
                        absfile = os.path.join(dirname, filename)
                        relfile = os.path.relpath(absfile, output_dir)
                        zf.write(absfile, relfile)
            shutil.rmtree(base)

        self._print('Diagnose complete: {}',
                    base if no_pack else zip_file)

    @exceptional_fn
    def _get_admin_diagnose_info(self, server, admin_dir, include_cores=False,
                                 modified_after_epoch=None, ignore=None):
        directory = os.path.join(admin_dir, server.id)
        os.makedirs(directory)
        # save config and check that server is configured with REST service
        config = self.conn.get_admin_config(server.id)
        with open(os.path.join(directory, 'nuoadmin.conf'), 'w') as f:
            json.dump(config._dict, f, indent=4)
        if 'rest' not in (s.lower() for s in config.other_services):
            raise RuntimeError('{} is not configured with REST service'
                               .format(server.id))
        address = server.address.split(':')[0]
        # assume that all servers are configured with the same protocol and
        # port, and replace hostname from --api-server with server's address
        url_base = re.sub(
            r'(https?://)[^:/]*(.*)', r'\g<1>{}\g<2>'.format(address),
            self.conn.url_base)
        conn = nuodb_mgmt.AdminConnection(
            url_base, self.conn.client_key, self.conn.verify)
        self._print('Downloading logs for {} ...', server.id)
        # logs should be extracted into directory since all files are prefixed
        # with '<server ID>/'
        self._download_logs(conn, include_core_files=include_cores,
                            dump_threads=True, output=admin_dir, unpack=True,
                            modified_after_epoch=modified_after_epoch,
                            ignore=ignore)
        # save 'show domain' output
        with open(os.path.join(directory, 'show-domain.txt'), 'w') as f:
            AdminCommands(conn, out=f, err=f).show_domain()
        # save 'show archives' output for all databases
        with open(os.path.join(directory, 'show-archives.txt'), 'w') as f:
            for db in conn.get_databases():
                command = '$ show archives --db-name ' + db.name
                header = format('{}\n{}\n').format('*' * len(command), command)
                f.write(header)
                AdminCommands(conn, out=f, err=f).show_archives(db.name)
                f.write('\n')
        # save 'show database' output for all databases
        with open(os.path.join(directory, 'show-databases.txt'), 'w') as f:
            for db in conn.get_databases():
                command = ('$ show database --db-name {} --all-incarnations'
                           .format(db.name))
                header = format('{}\n{}\n').format('*' * len(command), command)
                f.write(header)
                AdminCommands(conn, out=f, err=f).show_database(
                    db.name, all_incarnations=True)
                f.write('\n')
        # save 'get kubernetes-config' output
        with open(os.path.join(directory, 'kubernetes-config.json'), 'w') as f:
            try:
                kube_config = self.conn.get_kubernetes_config()
                kube_config.show(show_json=True, out=f)
            except Exception as e:
                f.write(str(e))

    @exceptional_fn
    def _get_engine_diagnose_info(self, process, engine_dir, descriptor,
                                  include_metadata, include_cores, socket_read_timeout):
        directory = os.path.join(engine_dir, descriptor)
        os.makedirs(directory)
        # save version
        with open(os.path.join(directory, 'version.txt'), 'w') as f:
            f.write(process.get('version', 'UNKNOWN'))
        if include_metadata:
            # download metadata
            try:
                self._print('Downloading metadata for {} ... ', descriptor)
                with open(os.path.join(directory, 'metadata.xml'), 'w') as f:
                    session = self.conn.get_authorized_session(
                        process.start_id, 'Query')
                    session.send(
                        '<Request Service="Query" Type="Instrumentation"/>')
                    f.write(session.recv())
                    session.close()
            except Exception:
                self._print('Unable to download metadata for {}', descriptor)
                self._print_exc()
        if include_cores:
            # download core file
            try:
                stream = self.conn.stream_core_file(process.start_id,
                                                    read_timeout=socket_read_timeout)
                self._print('Downloading core file for {} ...', descriptor)
                self._download_stream(stream, os.path.join(directory, 'core.gz'))
            except Exception as e:
                self._print(
                    'Unable to download core file for {}: {}',
                    descriptor,
                    str(e))
                raise
            try:
                stream = self.conn.stream_system_dependencies(
                    process.start_id, read_timeout=socket_read_timeout)
                self._print(
                    'Downloading system dependencies and binary for {} ...',
                    descriptor)
                self._download_stream(
                    stream, os.path.join(directory, 'sysdepends.tgz'))
            except Exception:
                # Don't fail if we can't get system dependencies
                self._print(
                    'Unable to download system dependencies and binary for {}',
                    descriptor)
                self._print_exc()

    def _wait_for_threads(self, threads, timeout=None):
        """
        :param list[threading.Thread] threads:
        """

        start_time = time.time()
        while len(threads) > 0:
            if timeout is not None and timeout < time.time() - start_time:
                raise RuntimeError(
                    'Timed out downloading diagnose info for ' +
                    ', '.join(t.name for t in threads))
            # remove first thread and wait short amount of time for it to
            # complete
            thread = threads.pop(0)
            thread.join(0.1)
            if thread.is_alive():
                # thread is still running; append to thread list
                threads.append(thread)
            elif not hasattr(thread, 'exc'):
                self._print('Downloaded diagnose info for {}', thread.name)
            else:
                # thread exited with an exception; print stacktrace
                self._print('Unable to download diagnose info for {}',
                            thread.name)
                self._print_exc(getattr(thread, 'exc'))

    def _print_exc(self, exc=None):
        if exc is None:
            exc = sys.exc_info()
        for line in traceback.format_exception(*exc):
            self.out.write(line)
        self.out.write('\n')

    @Subcommand('get', 'value',
                help='get the value for a key in the key-value store')
    @Argument('--key', required=True, help='the key to get the value of')
    def get_value(self, key):
        value = self.conn.get_value(key)
        if value is not None:
            self._print(value)
        return value

    @Subcommand('set', 'value',
                help='associate a key with a value in the key-value store')
    @Argument('--key', required=True, help='the key to set a value for')
    @Argument('--value', required=True,
              help='the new value to associate with key')
    @MutuallyExclusive(
        Argument('--expected-value', help='expected current value for key'),
        Argument('--unconditional', action='store_true',
                 help='set value unconditionally'),
        required=False)
    def set_value(self, key, value, expected_value=None, unconditional=False):
        self.conn.set_value(key, value, expected_value, not unconditional)

    @Subcommand('get', 'effective-license',
                help='get the current effective license for the server or domain')  # noqa
    def get_effective_license(self):
        effective_license = self.conn.get_effective_license()
        self._show(effective_license)
        return effective_license

    @Subcommand('check', 'license',
                help='check supplied license')
    @Argument('--license-file', required=True, help='the license file to check')  # noqa
    def check_license(self, license_file):
        with open(license_file) as f:
            license_info = self.conn.check_license(f.read())
            self._show(license_info)
            return license_info

    @Subcommand('set', 'license',
                help='set the license for the domain')
    @Argument('--license-file', required=True, help='the license file to install')  # noqa
    @Argument('--allow-downgrade', action='store_true',
              help='whether to install the license regardless of whether it is a downgrade from the current license')  # noqa
    def set_license(self, license_file, allow_downgrade=False):
        with open(license_file) as f:
            return self.conn.set_license(f.read(), allow_downgrade)

    @Subcommand('get', 'users', help='get all users')
    def get_users(self):
        users = self.conn.get_users()
        for user in users:
            self._show(user)
        return users

    @Subcommand('create', 'user', help='create a new user')
    @Argument('--name', required=True, help='the name of the user to create')
    @Argument('--roles', nargs='+',
              completer=CommandProcessor.get_role_names,
              help='the roles for the user')
    @MutuallyExclusive(
        Argument('--password', help='the user password, if basic authentication is to be used'),  # noqa
        Argument('--cert', help='the user PEM file, if certificate authentication is to be used; if the file does not exist, a new key-pair is generated'),  # noqa
        required=True)
    @Argument('--nuokey-cmd', default=EnvironmentalDefault(from_nuodb_home('etc', 'nuokeymgr')),  # noqa
              help='the path to the nuokeymgr executable')
    def create_user(self, name, roles, password=None, cert=None,
                    nuokey_cmd=None):
        # get certificate PEM if a certificate file was specified
        certificate_pem = self._get_user_certificate_pem(
            name, cert, nuokey_cmd)
        # first create user, to avoid adding a trusted certificate if user
        # creation fails for some reason
        self.conn.create_user(name, roles, password, certificate_pem)
        # add trusted certificate if using certificate authentication
        if certificate_pem is not None:
            # first replace any existing certificate with the same alias; this
            # should have no effect if there is no certificate with the same
            # alias
            self.conn.remove_trusted_certificate('user.' + name)
            self._add_trusted_certificate('user.' + name, certificate_pem, 0)

    @Subcommand('update', 'user-roles',
                help='update roles for an existing user')
    @Argument('--name', required=True,
              completer=CommandProcessor.get_user_names,
              help='the name of the user to update')
    @Argument('--roles-to-add', nargs='*',
              completer=CommandProcessor.get_role_names,
              help='the roles to add to the user')
    @Argument('--roles-to-remove', nargs='*',
              completer=CommandProcessor.get_role_names,
              help='the roles to remove from the user')
    def update_user_roles(self, name, roles_to_add=None, roles_to_remove=None):
        user = self.conn.update_user_roles(name, roles_to_add, roles_to_remove)
        self._show(user)
        return user

    @Subcommand('update', 'user-credentials',
                help='update credentials for an existing user')
    @Argument('--name', required=True,
              completer=CommandProcessor.get_user_names,
              help='the name of the user to update')
    @MutuallyExclusive(
        Argument('--password', help='the user password, if basic authentication is to be used'),  # noqa
        Argument('--cert', help='the user PEM file, if certificate authentication is to be used; if the file does not exist, a new key-pair is generated'),  # noqa
        required=True)
    @Argument('--nuokey-cmd', default=EnvironmentalDefault(from_nuodb_home('etc', 'nuokeymgr')),  # noqa
              help='the path to the nuokeymgr executable')
    def update_user_credentials(self, name, password=None, cert=None,
                                nuokey_cmd=None):
        # get certificate PEM if a certificate file was specified
        certificate_pem = self._get_user_certificate_pem(
            name, cert, nuokey_cmd)
        # first create user, to avoid adding a trusted certificate if user
        # creation fails for some reason
        user = self.conn.update_user_credentials(
            name, password, certificate_pem)
        # add trusted certificate if using certificate authentication
        if certificate_pem is not None:
            # first replace any existing certificate with the same alias; this
            # should have no effect if there is no certificate with the same
            # alias
            self.conn.remove_trusted_certificate('user.' + name)
            self._add_trusted_certificate('user.' + name, certificate_pem, 0)
        self._show(user)
        return user

    @Subcommand('delete', 'user', help='delete an existing user')
    @Argument('--name', required=True,
              completer=CommandProcessor.get_user_names,
              help='the name of the user to delete')
    def delete_user(self, name):
        self.conn.delete_user(name)

    def _get_user_certificate_pem(
            self, name, cert_file, nuokey_cmd):
        if cert_file is not None and not os.path.exists(cert_file):
            # certificate file was specified but does not exist; create a
            # key-pair for the new user
            keystore = tempfile.mktemp('.p12')
            store_password = str(uuid.uuid4())
            try:
                self._create_keypair(keystore, store_password,
                                     dname='CN=' + name)
                key_pem = self._get_cert(
                    nuokey_cmd, keystore, store_password)
                with open(cert_file, 'w') as f:
                    f.write(key_pem)
                return self._extract_certificate(key_pem)
            finally:
                os.remove(keystore)
        elif cert_file is not None:
            # certificate file exists; read it
            with open(cert_file) as f:
                return self._extract_certificate(f.read())

    @Subcommand('get', 'roles', help='get all roles')
    def get_roles(self):
        roles = self.conn.get_roles()
        for role in roles:
            self._show(role)
        return roles

    @Subcommand('create', 'role', help='create a new role')
    @Argument('--name', required=True, help='the name of the role to create')
    @Argument('--sub-roles', nargs='*',
              completer=CommandProcessor.get_role_names,
              help='the sub-roles for the role')
    @MutuallyExclusive(
        Argument('--any-method', action='store_const', const='*',
                 dest='method', help='authorize all request methods for role'),
        Argument('--method', choices=['PUT', 'GET', 'POST', 'DELETE'],
                 help='the request method to authorize for the role'))
    @Argument('--url', help='the URL to authorize for the role, excluding the hostname and port (e.g. /api/1/databases)')  # noqa
    @Argument('--query-param-constraints', nargs='*', help='constraints to enforce on query parameters')  # noqa
    @Argument('--path-param-constraints', nargs='*', help='constraints to enforce on path parameters')  # noqa
    @Argument('--payload-param-constraints', nargs='*', help='constraints to enforce on payload parameters')  # noqa
    def create_role(self, name, sub_roles=None, method=None, url=None,
                    query_param_constraints=None, path_param_constraints=None,
                    payload_param_constraints=None):
        authorized_requests = self._get_authorized_requests(
            method, url, query_param_constraints, path_param_constraints,
            payload_param_constraints)
        self.conn.create_role(name, sub_roles, authorized_requests)

    @Subcommand('delete', 'role', help='delete an existing role')
    @Argument('--name', required=True,
              completer=CommandProcessor.get_role_names,
              help='the name of the role to delete')
    def delete_role(self, name):
        self.conn.delete_role(name)

    @classmethod
    def _get_authorized_requests(cls, method=None, url=None,
                                 query_param_constraints=None,
                                 path_param_constraints=None,
                                 payload_param_constraints=None):
        authorized_requests = []
        if method is not None and url is not None:
            authorized_requests.append(dict(
                method=method, url=url,
                queryParamConstraints=CommandProcessor.as_dict(
                    query_param_constraints),
                pathParamConstraints=CommandProcessor.as_dict(
                    path_param_constraints),
                payloadParamConstraints=CommandProcessor.as_dict(
                    payload_param_constraints)))
        elif method is not None:
            raise ValueError('Specified --method={} but no --url'.format(method))  # noqa
        elif url is not None:
            raise ValueError('Specified --url={} but no --method'.format(url))
        else:
            if query_param_constraints:
                raise ValueError('Specified --query-param-constraints={} but no --method or --url'.format(query_param_constraints))  # noqa
            if path_param_constraints:
                raise ValueError('Specified --path-param-constraints={} but no --method or --url'.format(path_param_constraints))  # noqa
            if payload_param_constraints:
                raise ValueError('Specified --payload-param-constraints={} but no --method or --url'.format(payload_param_constraints))  # noqa
        return authorized_requests

    @classmethod
    def _get_access_privileges(cls):
        return (
            cls._get_authorized_requests('GET', '*') +
            cls._get_authorized_requests(
                'POST', nuodb_mgmt.get_url(1, 'diagnostics/log')))

    @classmethod
    def _get_domain_privileges(cls):
        return (
            cls._get_authorized_requests(
                '*', nuodb_mgmt.get_url(1, 'peers/*')) +
            cls._get_authorized_requests(
                '*', nuodb_mgmt.get_url(1, 'policies/*')) +
            cls._get_authorized_requests(
                '*', nuodb_mgmt.get_url(1, 'databases/loadBalancerPolicy/*')))

    @classmethod
    def _get_database_privileges(cls, db_name):
        return (
            cls._get_authorized_requests(
                'GET', nuodb_mgmt.get_url(1, 'databases/*')) +
            cls._get_authorized_requests(
                'GET', nuodb_mgmt.get_url(1, 'archives/*')) +
            cls._get_authorized_requests(
                'GET', nuodb_mgmt.get_url(1, 'processes/*')) +
            cls._get_authorized_requests(
                '*', nuodb_mgmt.get_url(1, 'databases/*'),
                path_param_constraints=dict(dbName=db_name)) +
            cls._get_authorized_requests(
                '*', nuodb_mgmt.get_url(1, 'archives/*'),
                payload_param_constraints=dict(dbName=db_name)) +
            cls._get_authorized_requests(
                '*', nuodb_mgmt.get_url(1, 'processes/*'),
                payload_param_constraints=dict(dbName=db_name)))

    @classmethod
    def _get_hotcopy_privileges(cls, db_name):
        return (
            cls._get_authorized_requests(
                'GET', nuodb_mgmt.get_url(1, 'databases/hotCopyStatus')) +
            cls._get_authorized_requests(
                'POST', nuodb_mgmt.get_url(1, 'databases/*/hotCopy'),
                path_param_constraints=dict(dbName=db_name)) +
            cls._get_authorized_requests(
                'POST', nuodb_mgmt.get_url(1, 'databases/*/hotCopySimple'),
                path_param_constraints=dict(dbName=db_name)))

    @Subcommand('add', 'role-templates',
                help='add set of privileges to an existing role')
    @Argument('--name', required=True,
              completer=CommandProcessor.get_role_names,
              help='the name of the role to update')
    @Argument('--accessor', action='store_true',
              help='Add privileges to invoke `get` and `show` subcommands')
    @Argument('--domain-admin', action='store_true',
              help='Add privileges to manage admin layer')
    @Argument('--db-admin', metavar='DB_NAME', default=None,
              completer=CommandProcessor.get_db_names,
              help='Add privileges to manage a database')
    @Argument('--hotcopy-admin', metavar='DB_NAME', default=None,
              completer=CommandProcessor.get_db_names,
              help='Add privileges to invoke hotcopy on a database and view hotcopy status')  # noqa
    def add_role_templates(self, name, accessor=False, domain_admin=False,
                           db_admin=None, hotcopy_admin=None):
        authorized_requests = []
        if accessor:
            authorized_requests += self._get_access_privileges()
        if domain_admin:
            authorized_requests += self._get_domain_privileges()
        if db_admin is not None:
            authorized_requests += self._get_database_privileges(db_admin)
        if hotcopy_admin is not None:
            authorized_requests += self._get_hotcopy_privileges(hotcopy_admin)
        role = self.conn.update_role(name, policies_to_add=authorized_requests)
        self._show(role)
        return role

    @Subcommand('remove', 'role-templates',
                help='remove set of privileges from an existing role')
    @Argument('--name', required=True,
              completer=CommandProcessor.get_role_names,
              help='the name of the role to update')
    @Argument('--accessor', action='store_true',
              help='Remove privileges to invoke `get` and `show` subcommands')
    @Argument('--domain-admin', action='store_true',
              help='Remove privileges to manage admin layer')
    @Argument('--db-admin', metavar='DB_NAME', default=None,
              completer=CommandProcessor.get_db_names,
              help='Remove privileges to manage a database')
    @Argument('--hotcopy-admin', metavar='DB_NAME', default=None,
              completer=CommandProcessor.get_db_names,
              help='Remove privileges to invoke hotcopy on a database and view hotcopy status')  # noqa
    def remove_role_templates(self, name, accessor=False, domain_admin=False,
                              db_admin=None, hotcopy_admin=None):
        authorized_requests = []
        if accessor:
            authorized_requests += self._get_access_privileges()
        if domain_admin:
            authorized_requests += self._get_domain_privileges()
        if db_admin is not None:
            authorized_requests += self._get_database_privileges(db_admin)
        if hotcopy_admin is not None:
            authorized_requests += self._get_hotcopy_privileges(hotcopy_admin)
        role = self.conn.update_role(
            name, policies_to_remove=authorized_requests)
        self._show(role)
        return role

    @Subcommand('add', 'role-privileges',
                help='add privileges to an existing role')
    @Argument('--name', required=True,
              completer=CommandProcessor.get_role_names,
              help='the name of the role to update')
    @Argument('--sub-roles', nargs='*',
              completer=CommandProcessor.get_role_names,
              help='the sub-roles to add')
    @MutuallyExclusive(
        Argument('--any-method', action='store_const', const='*',
                 dest='method', help='authorize all request methods for role'),
        Argument('--method', choices=['PUT', 'GET', 'POST', 'DELETE'],
                 help='the request method to authorize for the role'))
    @Argument('--url', help='the URL to authorize for the role, excluding the hostname and port (e.g. /api/1/databases)')  # noqa
    @Argument('--query-param-constraints', nargs='*', help='constraints to enforce on query parameters')  # noqa
    @Argument('--path-param-constraints', nargs='*', help='constraints to enforce on path parameters')  # noqa
    @Argument('--payload-param-constraints', nargs='*', help='constraints to enforce on payload parameters')  # noqa
    def add_role_privileges(self, name, sub_roles=None, method=None, url=None,
                            query_param_constraints=None,
                            path_param_constraints=None,
                            payload_param_constraints=None):
        authorized_requests = self._get_authorized_requests(
            method, url, query_param_constraints, path_param_constraints,
            payload_param_constraints)
        role = self.conn.update_role(name, sub_roles_to_add=sub_roles,
                                     policies_to_add=authorized_requests)
        self._show(role)
        return role

    @Subcommand('remove', 'role-privileges',
                help='remove privileges from an existing role')
    @Argument('--name', required=True,
              completer=CommandProcessor.get_role_names,
              help='the name of the role to update')
    @Argument('--sub-roles', nargs='*',
              completer=CommandProcessor.get_role_names,
              help='the sub-roles to remove')
    @MutuallyExclusive(
        Argument('--any-method', action='store_const', const='*',
                 dest='method', help='authorize all request methods for role'),
        Argument('--method', choices=['PUT', 'GET', 'POST', 'DELETE'],
                 help='the request method to authorize for the role'))
    @Argument('--url', help='the URL to authorize for the role, excluding the hostname and port (e.g. /api/1/databases)')  # noqa
    @Argument('--query-param-constraints', nargs='*', help='constraints to enforce on query parameters')  # noqa
    @Argument('--path-param-constraints', nargs='*', help='constraints to enforce on path parameters')  # noqa
    @Argument('--payload-param-constraints', nargs='*', help='constraints to enforce on payload parameters')  # noqa
    def remove_role_privileges(self, name, sub_roles=None, method=None,
                               url=None, query_param_constraints=None,
                               path_param_constraints=None,
                               payload_param_constraints=None):
        authorized_requests = self._get_authorized_requests(
            method, url, query_param_constraints, path_param_constraints,
            payload_param_constraints)
        role = self.conn.update_role(name, sub_roles_to_remove=sub_roles,
                                     policies_to_remove=authorized_requests)
        self._show(role)
        return role

    @Subcommand('update', 'data-encryption',
                help='update storage password used for transparent data encryption of archives')  # noqa
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database whose storage password to update')
    @Argument('--new-password', required=True,
              help='the new storage password to use with the specified database')  # noqa
    @MutuallyExclusive(
        Argument('--current-password',
                 help='the current storage password, used for verification'),
        Argument('--is-initial-password', action='store_true',
                 help='whether the --new-password value is the initial storage password for the database'),  # noqa
        required=True)
    @Argument('--existing-passwords', nargs='*',
              help='any other storage passwords needed to decrypt archives in the specified database')  # noqa
    @Argument('--config-update-timeout', default=None, type=int,
              help="How long (in seconds) to wait for configuration to be updated for each process")  # noqa
    def update_data_encryption(self, db_name, new_password,
                               current_password=None,
                               is_initial_password=False,
                               existing_passwords=None,
                               config_update_timeout=None):
        self.conn.update_data_encryption(
            db_name, new_password, current_password, existing_passwords, config_update_timeout)

    @Subcommand('check', 'data-encryption',
                help='check that the specified storage password is current and ensure that it is propagated to all admin servers and database processes')  # noqa
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database to check')
    @Argument('--password', required=True,
              help='the storage password to verify and propagate to all admin servers and database processes')  # noqa
    @Argument('--existing-passwords', nargs='*',
              help='any other storage passwords needed to decrypt archives in the specified database')  # noqa
    @Argument('--config-update-timeout', default=None, type=int,
                 help="How long (in seconds) to wait for configuration to be updated for each process")  # noqa
    @MutuallyExclusive(
        Argument('--timeout', default=None, type=int,
                 help="How long (in seconds) to wait for data encryption check to pass"),  # noqa
        Argument('--wait-forever', action='store_true', default=False,
                 help='Wait forever for data encryption check to pass'))
    def check_data_encryption(self, db_name, password,
                              existing_passwords=None,
                              config_update_timeout=None,
                              timeout=None,
                              wait_forever=False):
        if timeout is None and wait_forever:
            timeout = CommandProcessor.FOREVER
        if timeout is not None:
            self._await(
                lambda: self.conn.update_data_encryption(
                    db_name, password, password, existing_passwords,
                    config_update_timeout),
                timeout)
        else:
            self.conn.update_data_encryption(
                db_name, password, password, existing_passwords,
                config_update_timeout)

    @Subcommand('hotcopy', 'database',
                help='issue a hot-copy request on a database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database to hot-copy')
    @Argument('--type', required=True,
              choices=['simple', 'full', 'incremental', 'journal'],
              help='the type of hot-copy to perform; `--type simple` will produce an archive at the backup location; all other `--type` values will produce a backup set at the backup location')  # noqa
    @Argument('--shared', action='store_true',
              help='whether the backup location specified by --default-backup-dir is on shared storage')  # noqa
    @Argument('--default-backup-dir', required=False,
              help='backup location to use for any archive that does not have a backup location associated with it by --backup-dirs; if --process-filter is used, the --default-backup-dir should be supplied and provides the backup location prefix for filtered archives')  # noqa
    @MutuallyExclusive(
        Argument('--process-filter',required=False,
                help='if specified, filter database processes to only hot-copy those that match'),  # noqa
        Argument('--backup-dirs', nargs='*',
                completer=CommandProcessor.get_backup_dirs_token,
                help='map of archive ID to backup location to use for that archive'),  # noqa
    )
    @Argument('--max-rate', required=False, type=int,
              help='maximum I/O rate in MB/sec')  # noqa
    @Argument('--timeout', required=False, type=int,
              help='if specified, the time to wait in seconds for the hot-copy to complete; if not specified, the request is issued asynchronously')  # noqa
    def hotcopy_database(self, db_name, type, shared=False,
                         default_backup_dir=None, backup_dirs=None,
                         max_rate=None, timeout=None, process_filter=None):
        if not isinstance(backup_dirs, Mapping):
            backup_dirs = CommandProcessor.dict_from_tokens(backup_dirs)
        if process_filter:
            if not default_backup_dir:
                raise RuntimeError("Process filter can not be applied without --default-backup-dir")
            # filter the archiveIds using the process query and build the
            # backup_dirs manually
            archive_ids = self._get_archive_ids_by_filter(
                db_name, process_filter, include_observers=False,
                include_not_running_state=False)
            if shared:
                # the client might be running on any OS so 'os.path.join()' can't be
                # used here; instead add the archive ID as a suffix
                backup_dirs = { archive_id : "{}-{}".format(default_backup_dir, archive_id)
                               for archive_id in archive_ids }
            else:
                backup_dirs = { archive_id : default_backup_dir for archive_id in archive_ids }
            # clear the default_backup_dir and shared flag as only the filtered
            # archives should be hot-copied
            default_backup_dir = None
            shared = False
        if max_rate is not None:
            # Convert MB used by user interface to bytes used internally
            max_rate = str(int(max_rate) * 1000000)
        try:
            if type == 'simple':
                hotcopy_response = self.conn.hotcopy_database_simple(
                    db_name, default_backup_dir=default_backup_dir,
                    backup_dirs=backup_dirs)
            else:
                hotcopy_response = self.conn.hotcopy_database(
                    db_name, type, shared=shared,
                    default_backup_dir=default_backup_dir,
                    backup_dirs=backup_dirs, max_rate=max_rate)
            if timeout is not None and float(timeout) > 0:
                # Instead of passing the timeout to hotcopy request REST
                # service, use the client to wait for the hotcopy to complete in
                # the specified timeout
                hotcopy_response = self.await_hotcopy_complete(
                    hotcopy_response.coordinator_start_id, hotcopy_response.id, timeout)
                if hotcopy_response.status != "completed":
                    self._show(hotcopy_response)
                    raise ExitCommandException(hotcopy_response._dict)
            self._show(hotcopy_response)
            return hotcopy_response
        except nuodb_mgmt.AdminTimeoutException as e:
            entity = nuodb_mgmt.HotCopyResponse(e._dict)
            self._show(entity)
            raise ExitCommandException(e._dict)

    def _get_archive_ids_by_filter(self, db_name, filter, include_exited=False,
                                   include_observers=True,
                                   include_not_running_state=True):
        filter_by = dict(type='SM')
        if not include_not_running_state:
            filter_by['state'] = 'RUNNING'
        processes = list(self.conn.get_processes(
            db_name, filter_query=filter, **filter_by))
        if include_exited:
            processes += [p.process for p in self.conn.get_exited_processes(
                db_name, filter_query=filter, **{'process.type': 'SM'})]
        return set(str(p.archive_id) for p in processes
                   if include_observers or not p.options.get('storage-group-observer'))

    @Subcommand('get', 'hotcopy-info',
                help='get all hot-copy info for a database or an SM')
    @MutuallyExclusive(
        Argument('--db-name', completer=CommandProcessor.get_db_names,
                 help='the name of the database to get hot-copy statuses for'),
        Argument('--coordinator-start-id',
                 help='the start ID of the SM to get hot-copy statuses for'),
        required=True)
    @Argument('--timeout', required=False, type=int,
              help='the timeout for the management request sent to the SMs to obtain hot-copy statuses')
    @Argument('--running-for', required=False, type=TimeUnit,
              help='if specified, filter hot-copy statuses to only include ones that have been running for the specified amount of time or longer; this value can be specified using `d`, `h`, `m`, or `s` as a suffix, with seconds (`s`) being the default if the suffix is omitted')
    def get_hotcopy_info(self, db_name, coordinator_start_id, timeout=None,
                         running_for=None):
        if db_name is not None:
            if running_for is not None and not isinstance(running_for, TimeUnit):
                running_for = TimeUnit(running_for)
            min_seconds_running = None if running_for is None else running_for.seconds
            resp = self.conn.get_hotcopy_statuses(db_name, timeout, min_seconds_running)
        else:
            if running_for is not None:
                raise RuntimeError('--running-for can only be specified with --db-name')
            resp = self.conn.get_hotcopy_status(coordinator_start_id, None, timeout)
        self._show(resp)
        return resp

    HOTCOPY_FMT = '{id} [{status}] {timestamp_range} : {message} ({stage})'

    @Subcommand('show', 'hotcopy-info',
                help='show all hot-copy info for a database')
    @Argument('--db-name', required=True,
              completer=CommandProcessor.get_db_names,
              help='the name of the database to show hot-copy statuses for')
    @Argument('--timeout', required=False, type=int,
              help='the timeout for the management request sent to the SMs to obtain hot-copy statuses')
    @Argument('--running-for', required=False, type=TimeUnit,
              help='if specified, filter hot-copy statuses to only include ones that have been running for the specified amount of time or longer; this value can be specified using `d`, `h`, `m`, or `s` as a suffix, with seconds (`s`) being the default if the suffix is omitted')
    @Argument('--process-format', default=EnvironmentalDefault(PROCESS_FMT),
              help='format string for processes')
    @Argument('--hotcopy-format', default=EnvironmentalDefault(HOTCOPY_FMT),
              help='format string for hot-copy statuses')
    def show_hotcopy_info(self, db_name, timeout=None, running_for=None,
                          process_format=PROCESS_FMT,
                          hotcopy_format=HOTCOPY_FMT):
        if running_for is not None and not isinstance(running_for, TimeUnit):
            running_for = TimeUnit(running_for)
        min_seconds_running = None if running_for is None else running_for.seconds
        resp = self.conn.get_hotcopy_statuses(db_name, timeout, min_seconds_running)
        running_sms = self.conn.get_processes(db_name=db_name, type='SM')
        for sm in running_sms:
            if sm.start_id in resp.hotcopy_statuses:
                self._print(get_formatted(process_format, sm))
                for hcs in resp.hotcopy_statuses[sm.start_id]:
                    self._print('  ' + get_formatted(hotcopy_format, hcs))
            elif sm.start_id in resp.errors:
                self._print(get_formatted(process_format, sm))
                self._print('  ' + resp.errors[sm.start_id])

    @Subcommand('get', 'hotcopy-status',
                help='get the status of a hot-copy request')
    @Argument('--coordinator-start-id', required=True,
              help='the start ID of the SM coordinating the hot-copy')
    @Argument('--hotcopy-id', required=True,
              help='the ID of the hot-copy request')
    @Argument('--timeout', required=False, type=int,
              help='if specified, the time to wait in seconds for the hot-copy to complete; if not specified, the status is polled without waiting')  # noqa
    def get_hotcopy_status(self, coordinator_start_id, hotcopy_id,
                           timeout=None):
        try:
            if timeout is not None and float(timeout) > 0:
                # Instead of passing the timeout to hotcopy request REST
                # service, use the client to wait for the hotcopy to complete in
                # the specified timeout
                hotcopy_response = self.await_hotcopy_complete(
                    coordinator_start_id, hotcopy_id, timeout)
            else:
                hotcopy_response = self.conn.get_hotcopy_status(
                    coordinator_start_id, hotcopy_id)
            self._show(hotcopy_response)
            return hotcopy_response
        except nuodb_mgmt.AdminTimeoutException as e:
            entity = nuodb_mgmt.HotCopyResponse(e._dict)
            self._show(entity)
            raise ExitCommandException(e._dict)

    def await_hotcopy_complete(self, coordinator_start_id, hotcopy_id, timeout):
        def hotcopy_complete():
            try:
                hotcopy_response = self.conn.get_hotcopy_status(
                    coordinator_start_id, hotcopy_id)
                if hotcopy_response.status in ["completed", "failed", "cancelled"]:
                    return hotcopy_response
            except nuodb_mgmt.AdminException as e:
                raise UnrecoverableException(str(e))
            messages = ['timeout while waiting for hot copy operation to finish']
            if hotcopy_response.message:
                messages.insert(0, hotcopy_response.message)
            hotcopy_response.set('message', ": ".join(messages))
            raise nuodb_mgmt.AdminTimeoutException(hotcopy_response._dict)
        return self._await(hotcopy_complete, timeout)

    @Subcommand('cancel', 'hotcopy', help='cancel a running hot-copy')
    @Argument('--coordinator-start-id', required=True,
              help='the start ID of the SM coordinating the hot-copy to cancel')
    @Argument('--hotcopy-id', required=True,
              help='the ID of the hot-copy request to cancel')
    @Argument('--timeout', required=False, type=int,
              help='if specified, the time to wait in seconds for the cancellation to be processed; it not specified, cancellation is issued asynchronously')  # noqa
    def cancel_hotcopy(self, coordinator_start_id, hotcopy_id,
                       timeout=None):
        try:
            hotcopy_response = self.conn.cancel_hotcopy(
                coordinator_start_id, hotcopy_id, timeout)
            self._show(hotcopy_response)
            return hotcopy_response
        except nuodb_mgmt.AdminTimeoutException as e:
            entity = nuodb_mgmt.HotCopyResponse(e._dict)
            self._show(entity)
            raise ExitCommandException(e._dict)

    @Subcommand('get', 'sql-connection',
                help='get TE to service a SQL connection')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database to get a SQL connection for')
    @Argument('--connection-properties', required=False, nargs='*',
              default=EnvironmentalDefault(),
              help='connection properties for connection request')
    def get_sql_connection(self, db_name, connection_properties=None):
        connection_properties = CommandProcessor.dict_from_tokens(
            connection_properties)
        te = self.conn.get_sql_connection(db_name, **connection_properties)
        if te is not None:
            self._show(te)
            return te

    @Subcommand('set', 'load-balancer-config',
                help='set a load-balancing configuration')
    @MutuallyExclusive(
        Argument('--db-name',
                 help='the name to register default for'),
        Argument('--is-global', action='store_true',
                 help='whether to register global default'),
        required=True)
    @MutuallyExclusive(
        Argument('--default',
                 help='the default load-balancer register'),
        Argument('--unregister-default', action='store_true',
                 help='whether to unregister current default'))
    @MutuallyExclusive(
        Argument('--prefilter',
                 help='the prefilter to register'),
        Argument('--unregister-prefilter', action='store_true',
                 help='whether to unregister the current prefilter'))
    def set_load_balancer_config(self, db_name=None, is_global=False,
                                 default=None, unregister_default=False,
                                 prefilter=None, unregister_prefilter=False):
        self.conn.set_load_balancer_config(
            db_name=db_name, is_global=is_global,
            default=default, unregister_default=unregister_default,
            prefilter=prefilter, unregister_prefilter=unregister_prefilter)

    @Subcommand('get', 'load-balancer-config',
                help='get load-balancer configuration for all databases')
    def get_load_balancer_config(self):
        lb_configs = self.conn.get_load_balancer_configs()
        for config in lb_configs:
            self._show(config)
        return lb_configs

    @Subcommand('set', 'load-balancer',
                help='set a load-balancing policy')
    @Argument('--policy-name', required=True,
              help='the name of the load-balancer policy to set')
    @Argument('--lb-query', required=True,
              help='the load-balancer query to set')
    def set_load_balancer_policy(self, policy_name, lb_query):
        self.conn.set_load_balancer_policy(policy_name, lb_query)

    @Subcommand('delete', 'load-balancer',
                help='remove a load-balancer policy')
    @Argument('--policy-name', required=True,
              completer=CommandProcessor.get_db_names,
              help='the name of the load-balancer policy to delete')
    def delete_load_balancer_policy(self, policy_name):
        self.conn.remove_load_balancer_policy(policy_name)

    @Subcommand('get', 'load-balancers',
                help='get all load-balancing policies')
    def get_load_balancer_policies(self):
        lb_policies = self.conn.get_load_balancer_policies()
        for policy in lb_policies:
            self._show(policy)
        return lb_policies

    HOSTNAME = nuodb_mgmt.resolve_hostname()
    DEFAULT_DNAME = 'CN=' + HOSTNAME
    STORE_TYPES = dict(jks='JKS', p12='PKCS12')

    def _get_type_and_alias(self, keystore, store_type, alias):
        if store_type is None:
            ext = keystore.split(os.path.extsep)[-1]
            store_type = self.STORE_TYPES.get(ext, 'JKS')
        if alias is None:
            basename = os.path.basename(keystore)
            alias = os.path.splitext(basename)[0]
        return (store_type, alias)

    def _get_san_args(self, sub_altnames, resolve_san):
        if sub_altnames is None:
            sub_altnames = []
        if resolve_san:
            sub_altnames.append('dns:' + self.HOSTNAME)
        return (['-ext', 'san=' + ','.join(sub_altnames)]
                if len(sub_altnames) != 0 else
                [])

    def _get_password(self, arg_value, prompt):
        if arg_value:
            return arg_value
        if sys.stdin.isatty():
            return getpass.getpass(prompt)
        return sys.stdin.readline().rstrip('\n')

    @Subcommand('create', 'keypair',
                help='create a key-pair')
    @Argument('--keystore', required=True,
              help='the keystore to store the generated key-pair into, which will be created if it does not exist')  # noqa
    @Argument('--store-type', required=False, choices=['JKS', 'PKCS12'],
              help='the type of the keystore; is derived from --keystore value if not specified')  # noqa
    @Argument('--alias', required=False,
              help='the alias to store the key-pair as; is derived from --keystore value if not specified')  # noqa
    @Argument('--store-password', required=False,
              help='the password for the keystore; can also be specified via standard input')
    @MutuallyExclusive(
        Argument('--key-password',
                 help='the password for the private key; if --key-password and --prompt-key-password are not specified, defaults to --store-password value'),  # noqa
        Argument('--prompt-key-password', action='store_true',
                 help='whether to prompt for private key password if --key-password is not specified'),  # noqa
        required=False)
    @Argument('--dname', default=DEFAULT_DNAME,
              help='the distinguished name of the certificate')
    @Argument('--sub-altnames', nargs='*',
              help='list of subjectAltNames for the certificate; must be prefixed by type, e.g. dns:hostname')  # noqa
    @Argument('--resolve-san', action='store_true',
              help='whether to resolve subjectAltName using DNS, e.g. dns:hostname')  # noqa
    @Argument('--algorithm', default='RSA', choices=['RSA', 'DSA', 'EC'],
              help='the key algorithm')
    @Argument('--validity', default=365, type=int,
              help='the validity in days of the certificate')
    @Argument('--start-date', required=False,
              help='start date of the certificate')
    @Argument('--ca', action='store_true',
              help='whether the generated certificate should be used as CA')
    def create_keypair(self, keystore, store_password, key_password=None,
                       store_type=None, alias=None, dname=DEFAULT_DNAME,
                       sub_altnames=None, resolve_san=False, algorithm='RSA',
                       validity=365, ca=False, start_date=None,
                       prompt_key_password=False):
        store_password = self._get_password(store_password, 'Keystore password: ')
        if prompt_key_password:
            key_password = self._get_password(key_password, 'Key password: ')
        ret = self._create_keypair(
            keystore, store_password, key_password, store_type, alias, dname,
            sub_altnames, resolve_san, algorithm, validity, ca, start_date)
        if ret != 0:
            raise ExitCommandException(dict(), ret)

    def _create_keypair(self, keystore, store_password, key_password=None,
                        store_type=None, alias=None, dname=DEFAULT_DNAME,
                        sub_altnames=None, resolve_san=False, algorithm='RSA',
                        validity=365, ca=False, start_date=None):
        store_type, alias = self._get_type_and_alias(
            keystore, store_type, alias)
        if key_password is None:
            key_password = store_password
        args = ['keytool', '-genkeypair', '-keystore', keystore,
                '-storetype', store_type, '-alias', alias,
                '-storepass:env', 'NUOCMD_STOREPASS',
                '-keypass:env', 'NUOCMD_KEYPASS',
                '-dname', dname, '-keyalg', algorithm,
                '-validity', str(validity)]
        args += self._get_san_args(sub_altnames, resolve_san)
        if ca:
            args += ['-ext', 'bc:c']
        if start_date:
            args += ['-startdate', start_date.replace('\\', '')]
        return subprocess.call(
            args, env=dict(NUOCMD_STOREPASS=store_password,
                           NUOCMD_KEYPASS=key_password))

    def _get_cert(self, nuokey_cmd, keystore, store_password,
                  key_password=None, store_type=None, alias=None,
                  cert_only=False):
        if not nuokey_cmd:
            raise ValueError('No --nuokey-cmd specified')
        if not os.path.exists(nuokey_cmd):
            raise ValueError(
                '--nuokey-cmd={} does not exist'.format(nuokey_cmd))
        store_type, alias = self._get_type_and_alias(
            keystore, store_type, alias)
        if not key_password:
            key_password = store_password
        args = [nuokey_cmd, 'export', '--keystore', keystore,
                '--storetype', store_type, '--alias', alias,
                '--storepass', store_password, '--keypass', key_password]
        if cert_only:
            args += ['--cert-only']
        try:
            return subprocess.check_output(args, stderr=subprocess.STDOUT, universal_newlines=True)
        except subprocess.CalledProcessError as e:
            # DB-23738: do not propagate CalledProcessError, which contains
            # command-line
            raise RuntimeError('Unable to extract PEM-encoded certificate: ' + e.output)

    def _get_csr(self, keystore, store_password, store_type=None, alias=None):
        store_type, alias = self._get_type_and_alias(
            keystore, store_type, alias)
        args = ['keytool', '-certreq', '-keystore', keystore,
                '-storetype', store_type, '-alias', alias,
                '-storepass:env', 'NUOCMD_STOREPASS']
        try:
            return subprocess.check_output(
                args, stderr=subprocess.STDOUT,
                env=dict(NUOCMD_STOREPASS=store_password), universal_newlines=True)
        except subprocess.CalledProcessError as e:
            # DB-23738: do not propagate CalledProcessError, which contains
            # command-line
            raise RuntimeError('Unable to create certificate signing request: ' + e.output)

    def _get_signed_cert(self, csr, ca_keystore, ca_store_password,
                         ca_store_type=None, ca_alias=None, validity=365,
                         ca=False, start_date=None, sub_altnames=None,
                         resolve_san=False):
        ca_store_type, ca_alias = self._get_type_and_alias(
            ca_keystore, ca_store_type, ca_alias)
        args = ['keytool', '-gencert', '-keystore', ca_keystore,
                '-storetype', ca_store_type, '-alias', ca_alias,
                '-storepass:env', 'NUOCMD_STOREPASS', '-rfc',
                '-validity', str(validity)]
        args += self._get_san_args(sub_altnames, resolve_san)
        if ca:
            args += ['-ext', 'bc:c']
        if start_date:
            args += ['-startdate', start_date.replace('\\', '')]
        # fix keytool output, which prints base64 with Windows line-endings
        # (CRLF) for some reason
        return self._check_output(
            args, csr, env=dict(NUOCMD_STOREPASS=ca_store_password)).replace('\r\n', '\n')

    def _import_cert(self, keystore, store_password, store_type=None,
                     alias=None, cert=None, cert_file=None, interactive=False):
        store_type, alias = self._get_type_and_alias(
            keystore, store_type, alias)
        if cert_file is None:
            cert_file = tempfile.mktemp()
            with open(cert_file, 'w') as f:
                f.write(cert)
        args = ['keytool', '-import', '-keystore', keystore,
                '-storetype', store_type, '-alias', alias,
                '-storepass:env', 'NUOCMD_STOREPASS', '-file', cert_file,
                '-trustcacerts']
        # if non-interactive, add -noprompt option and suppress output
        if not interactive:
            return self._check_output(
                args, 'y', env=dict(NUOCMD_STOREPASS=store_password))
        subprocess.call(args, env=dict(NUOCMD_STOREPASS=store_password))

    @Subcommand('show', 'certificate',
                help='output key-pair or certificate from a keystore as a base64-encoded string according to RFC-7468')  # noqa
    @Argument('--keystore', required=True,
              help='the keystore to extract key-pair or certificate from')  # noqa
    @Argument('--store-type', required=False, choices=['JKS', 'PKCS12'],
              help='the type of the keystore; is derived from --keystore value if not specified')  # noqa
    @Argument('--alias', required=False,
              help='the alias of the key-pair or certificate; is derived from --keystore value if not specified')  # noqa
    @Argument('--store-password', required=False,
              help='the password for the keystore; can also be specified via standard input')
    @MutuallyExclusive(
        Argument('--key-password',
                 help='the password for the private key; if --key-password and --prompt-key-password are not specified, defaults to --store-password value'),  # noqa
        Argument('--prompt-key-password', action='store_true',
                 help='whether to prompt for private key password if --key-password is not specified'),  # noqa
        required=False)
    @Argument('--cert-only', action='store_true',
              help='whether to only export the certificate')
    @Argument('--nuokey-cmd', required=True,
              default=EnvironmentalDefault(from_nuodb_home('etc', 'nuokeymgr')),  # noqa
              help='the path to the nuokeymgr executable')
    def show_certificate(self, nuokey_cmd, keystore, store_password,
                         key_password=None, store_type=None, alias=None,
                         cert_only=False, prompt_key_password=False):
        store_password = self._get_password(store_password, 'Keystore password: ')
        if prompt_key_password and not cert_only:
            key_password = self._get_password(key_password, 'Key password: ')
        self.out.write(self._get_cert(
            nuokey_cmd, keystore, store_password, key_password=key_password,
            store_type=store_type, alias=alias, cert_only=cert_only))

    @Subcommand('sign', 'certificate',
                help='sign a certificate in a keystore with a CA certificate')  # noqa
    @Argument('--keystore', required=True,
              help='the keystore containing the certificate to sign')
    @Argument('--store-type', required=False, choices=['JKS', 'PKCS12'],
              help='the type of the keystore; is derived from --keystore value if not specified')  # noqa
    @Argument('--alias', required=False,
              help='the alias of the certificate; is derived from --keystore value if not specified')  # noqa
    @Argument('--store-password', required=False,
              help='the password for the keystore; can also be specified via standard input')
    @Argument('--ca-keystore', required=True,
              help='the keystore containing the CA certificate')
    @Argument('--ca-store-type', required=False, choices=['JKS', 'PKCS12'],
              help='the type of the CA keystore; is derived from --ca-keystore value if not specified')  # noqa
    @Argument('--ca-alias', required=False,
              help='the alias of the CA certificate; is derived from --ca-keystore value if not specified')  # noqa
    @Argument('--ca-store-password', required=False,
              help='the password for the CA keystore; can also be specified via standard input')
    @Argument('--update', action='store_true',
              help='whether to update the keystore with the signed certificate; if not specified, the certificate chain is written to standard output')  # noqa
    @Argument('--sub-altnames', nargs='*',
              help='list of subjectAltNames for the certificate; must be prefixed by type, e.g. dns:hostname')  # noqa
    @Argument('--resolve-san', action='store_true',
              help='whether to resolve subjectAltName using DNS, e.g. dns:hostname')  # noqa
    @Argument('--validity', default=365, type=int,
              help='the validity in days of the certificate')
    @Argument('--start-date', required=False,
              help='start date of the certificate')
    @Argument('--ca', action='store_true',
              help='whether the generated certificate should be used as CA')
    @Argument('--nuokey-cmd', required=True,
              default=EnvironmentalDefault(from_nuodb_home('etc', 'nuokeymgr')),  # noqa
              help='the path to the nuokeymgr executable')
    def sign_certificate(self, nuokey_cmd, keystore, store_password,
                         ca_keystore, ca_store_password, store_type=None,
                         alias=None, ca_store_type=None, ca_alias=None,
                         update=False, validity=365, ca=False,
                         start_date=None, sub_altnames=None,
                         resolve_san=False):
        store_password = self._get_password(store_password, 'Keystore password: ')
        ca_store_password = self._get_password(ca_store_password, 'CA keystore password: ')
        # generate a certificate signing request
        csr = self._get_csr(keystore, store_password, store_type, alias)
        # sign certificate using CA certificate
        cert = self._get_signed_cert(csr, ca_keystore, ca_store_password,
                                     ca_store_type, ca_alias, validity, ca,
                                     start_date, sub_altnames, resolve_san)
        # add CA certificate to client certificate
        ca_cert = self._get_cert(
            nuokey_cmd, ca_keystore, ca_store_password,
            store_type=ca_store_type, alias=ca_alias, cert_only=True)
        cert_chain = '{}\n{}\n'.format(cert.strip(), ca_cert.strip())
        if update:
            # update certificate in keystore
            self._import_cert(keystore, store_password, store_type, alias,
                              cert=cert_chain)
        else:
            self.out.write(str.encode(cert_chain))

    @Subcommand('import', 'certificate',
                help='import a certificate into a truststore')
    @Argument('--keystore', required=True,
              help='the keystore containing the certificate')
    @Argument('--store-type', required=False, choices=['JKS', 'PKCS12'],
              help='the type of the keystore; is derived from --keystore value if not specified')  # noqa
    @Argument('--alias', required=False,
              help='the alias of the certificate; is derived from --keystore value if not specified')  # noqa
    @Argument('--store-password', required=False,
              help='the password for the keystore; can also be specified via standard input')
    @Argument('--truststore', required=True,
              help='the keystore containing trusted certificates')
    @Argument('--truststore-type', required=False, choices=['JKS', 'PKCS12'],
              help='the type of the truststore; is derived from --truststore value if not specified')  # noqa
    @Argument('--truststore-password', required=False,
              help='the password for the truststore; can also be specified via standard input')
    @Argument('--for-recovery', action='store_true',
              help='store the certificate so that it is protected from truststore updates made using `nuocmd add/remove trusted-certificates`')  # noqa
    @Argument('--nuokey-cmd', required=True,
              default=EnvironmentalDefault(from_nuodb_home('etc', 'nuokeymgr')),  # noqa
              help='the path to the nuokeymgr executable')
    def import_certificate(self, nuokey_cmd, keystore, store_password,
                           truststore, truststore_password, store_type=None,
                           alias=None, truststore_type=None,
                           for_recovery=False):
        store_password = self._get_password(store_password, 'Keystore password: ')
        truststore_password = self._get_password(truststore_password, 'Truststore password: ')
        store_type, alias = self._get_type_and_alias(
            keystore, store_type, alias)
        # get certificate chain
        cert = self._get_cert(nuokey_cmd, keystore, store_password,
                              store_type=store_type, alias=alias,
                              cert_only=True)
        # if `--for-recovery` was specified, add prefix to alias that protects
        # it from changes made using REST API
        if for_recovery:
            alias = '__recovery__' + alias
        # update certificate in keystore
        self._import_cert(truststore, truststore_password, truststore_type,
                          alias, cert=cert)

    def _check_output(self, args, stdin=None, env=None):
        p = subprocess.Popen(args, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             env=env,
                             universal_newlines=True)
        out, err = p.communicate(stdin)
        ret = p.poll()
        if ret != 0:
            self.out.write(out)
            raise ExitCommandException(dict(output=out, error=err), ret)
        return out

    @Subcommand('create', 'region', help='create a region (deprecated)')
    @Argument('--name', required=True, help='the name of the region')
    def create_region(self, name):
        region = self.conn.create_region(name)
        self._show(region)
        return region

    @Subcommand('delete', 'region', help='delete a region (deprecated)')
    @Argument('--region-id', required=True,
              completer=CommandProcessor.get_region_ids,
              help='the ID of the region to delete')
    def delete_region(self, region_id):
        self.conn.delete_region(region_id)

    @Subcommand('get', 'regions', help='get regions (deprecated)')
    def get_regions(self):
        regions = self.conn.get_regions()
        for region in regions:
            self._show(region)
        return regions

    @Subcommand('show', 'regions', help='show regions (deprecated)')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the database to show regions for')
    @Argument('--region-format', default=EnvironmentalDefault(REGION_FMT),
              help='format string for regions')
    @Argument('--process-format',
              default=EnvironmentalDefault(SHORT_PROCESS_FMT),
              help='format string for processes')
    def show_regions(self, db_name, region_format=REGION_FMT,
                     process_format=SHORT_PROCESS_FMT):
        database = self.conn.get_database(db_name)
        # invert map of server ID to region ID
        region_map = {}
        for server_id, region_id in database.server_assignments.items():
            if region_id not in region_map:
                region_map[region_id] = [server_id]
            else:
                region_map[region_id].append(server_id)
        # also build map of server ID to process
        server_map = {}
        for process in self.conn.get_processes(db_name):
            if process.server_id not in server_map:
                server_map[process.server_id] = [process]
            else:
                server_map[process.server_id].append(process)

        # add unassigned servers to default region
        region_map[database.default_region_id] = [
            server_id for server_id in server_map.keys()
            if server_id not in database.server_assignments]
        # for each region, show all servers assigned to it and all running
        # processes on those servers
        for region in self.conn.get_regions():
            self._print(get_formatted(region_format, region))
            server_ids = region_map.get(region.id, [])
            server_ids.sort()
            for server_id in server_ids:
                self._print('  [server_id = {}]', server_id)
                for process in server_map.get(server_id, []):
                    self._print('    ' + get_formatted(process_format,
                                                       process))

    @Subcommand('add', 'region-assignment', help='add servers to a region (deprecated)')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database to add the region mapping for')
    @MutuallyExclusive(
        Argument('--region-name',
                 completer=CommandProcessor.get_region_names,
                 help='the name of the region to add servers to'),
        Argument('--region-id',
                 completer=CommandProcessor.get_region_ids,
                 help='the ID of the region to add servers to'),
        required=True)
    @Argument('--server-ids', nargs='+', required=True,
              completer=CommandProcessor.get_server_ids,
              help='the IDs of the servers to add to region')
    def add_region_assignment(self, db_name, server_ids, region_id=None,
                              region_name=None):
        if region_id is None:
            # get the region ID if specified by name
            if region_name is None:
                # this can only happen if we invoke this method directly
                raise ValueError(
                    'One of region_id, region_name must be specified')
            region_id = self._get_region_id(region_name)
        for server_id in server_ids:
            self.conn.add_server_assignment(db_name, region_id, server_id)

    def _get_region_id(self, region_name, regions_by_name=None):
        if regions_by_name is None:
            regions_by_name = self._get_regions_by_name()
        if region_name not in regions_by_name:
            # make sure region exists
            raise ValueError('No region named \'{}\''.format(region_name))
        return regions_by_name[region_name]

    def _get_regions_by_name(self):
        regions = self.conn.get_regions()
        return dict((region.name, region.id) for region in regions)

    @Subcommand('remove', 'region-assignment',
                help='remove servers from their current region (deprecated)')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='the name of the database to remove the region mapping for')
    @Argument('--server-ids', nargs='+', required=True,
              completer=CommandProcessor.get_server_ids,
              help='the IDs of the servers to remove from region')
    def remove_region_assignment(self, db_name, server_ids):
        for server_id in server_ids:
            self.conn.remove_server_assignment(db_name, server_id)

    def _liveness_threshold_generator(self, wait_for_acks=False, limit=None):
        if wait_for_acks:
            # wait_for_acks was specified, which signals that threshold should
            # be derived from start time of command, clamped at a limit of 20
            # seconds by default
            if limit is None:
                limit = 20
            start_time = time.time()
            elapsed = time.time() - start_time
            while elapsed < limit:
                yield elapsed
                elapsed = time.time() - start_time
        # elapsed time is greater than upper limit or wait_for_acks was not
        # specified at all; yield limit every time
        while True:
            yield limit

    @Subcommand('check', 'process',
                help='Check the status of a process')
    @Argument('--start-id', required=True,
              help='the Start ID of the process to check')
    @MutuallyExclusive(
        Argument('--check-running', action='store_true', default=False,
                 help='Check that the process is in the RUNNING state'),
        Argument('--check-exited', action='store_true', default=False,
                 help='Check that the process has exited'))
    @Argument('--check-liveness', default=None, type=float,
              help='Check that the process has responded within the specified number of seconds')  # noqa
    @Argument('--wait-for-ack', action='store_true',
              help='If specified along with --timeout, wait for a message to be received from the process while the command is running')  # noqa
    @MutuallyExclusive(
        Argument('--timeout', default=None, type=int,
                 help="How long (in seconds) to wait for process check to pass"),  # noqa
        Argument('--wait-forever', action='store_true', default=False,
                 help='Wait forever for process check to pass'))
    def check_process(self, start_id, check_running=False, check_exited=False,
                      check_liveness=None, timeout=None, wait_forever=False,
                      wait_for_ack=False):
        if check_exited and check_liveness is not None:
            raise RuntimeError('argument --check-exited not allowed with argument --check-liveness')  # noqa
        if timeout is None and wait_forever:
            timeout = CommandProcessor.FOREVER
        proc = None
        if timeout is not None:
            proc = self._await(lambda: self.conn.get_process(start_id),
                               timeout)
        if not check_exited and type(proc) is nuodb_mgmt.ExitedProcess:
            raise RuntimeError(
                'Process with Start ID {} has already exited: \'{}\''
                .format(start_id, proc.reason))
        if timeout is not None:
            liveness_gen = self._liveness_threshold_generator(wait_for_ack, check_liveness)
            return self._await(
                lambda: self._check_process(
                    start_id, check_running, check_exited, next(liveness_gen)),
                timeout)
        return self._check_process(start_id, check_running, check_exited,
                                   check_liveness)

    def _check_process(self, start_id, check_running, check_exited,
                       check_liveness=None):
        proc = self.conn.get_process(start_id)
        if not check_exited and type(proc) is nuodb_mgmt.ExitedProcess:
            raise UnrecoverableException(
                'Process with Start ID {} has already exited: \'{}\''
                .format(start_id, proc.reason))
        if check_exited and type(proc) is not nuodb_mgmt.ExitedProcess:
            raise RuntimeError(
                'Process with Start ID {} has not exited. Has state \'{}\''
                .format(start_id, proc.engine_state))
        if check_running:
            if proc.engine_state != 'RUNNING':
                raise RuntimeError(
                    'Process with Start ID {} not in \'RUNNING\' state. Has state \'{}\''  # noqa
                    .format(start_id, proc.engine_state))
        if check_liveness is not None:
            if proc.last_ack is None:
                raise RuntimeError(
                    'Process with Start ID {} unresponsive for longer than 60 seconds'  # noqa
                    .format(start_id))
            if proc.last_ack > check_liveness:
                raise RuntimeError(
                    'Process with Start ID {} unresponsive for the last {} seconds'  # noqa
                    .format(start_id, proc.last_ack))
        return proc

    @Subcommand('check', 'server', help='Check the status of the API server')
    @MutuallyExclusive(
        Argument('--timeout', default=None, type=int,
                 help='How long (in seconds) to wait for server check to pass'),  # noqa
        Argument('--wait-forever', action='store_true',
                 help='Wait forever for server check to pass'))
    @Argument('--check-active', action='store_true',
              help='Check that server is ACTIVE')
    @Argument('--check-connected', action='store_true',
              help='Check that server is CONNECTED, to verify that it is reachable by its advertised address')  # noqa
    @Argument('--check-converged', action='store_true',
              help='Check that server has the same commit index as the leader')
    def check_server(self, check_active=False, check_connected=False,
                     check_leader=False, num_servers=None, timeout=None,
                     wait_forever=False, check_liveness=None,
                     check_converged=False):
        if timeout is None and wait_forever:
            timeout = CommandProcessor.FOREVER
        if timeout is not None:
            return self._await(
                lambda: self._check_server(
                    check_active, check_connected, check_converged),
                timeout)
        return self._check_server(
            check_active, check_connected, check_converged)

    def _check_server(self, check_active=False, check_connected=False,
                      check_converged=False):
        servers = self.conn.get_servers()
        api_server = None
        leader = None
        leader_id = None
        for server in servers:
            if server.is_local:
                api_server = server
            if server.role == 'LEADER' and server.connected_state == "Connected":
                leader = server
                leader_id = server.id

        if api_server is None:
            raise RuntimeError('API server info not found')

        msg = ''
        # if `check_active`, check that server is ACTIVE
        if check_active and api_server.raft_state != 'ACTIVE':
            msg += api_server.id + ' is not ACTIVE\n'
        # if `check_connected`, check that server is CONNECTED
        if check_connected and api_server.connected_state != 'Connected':
            msg += api_server.id + ' is not reachable by its advertised address\n'
        # if `check_converged`, check that server is caught up with leader
        if check_converged:
            if api_server.leader != leader_id:
                msg += '{} has inconsistent leader: expected={}, actual={}\n'.format(
                    api_server.id, api_server.leader, leader_id)
            elif leader is None:
                msg += api_server.id + ' has no leader\n'
            elif api_server.commit_index < leader.commit_index:
                msg += '{} has lower commit index than the leader {}:\n'.format(
                    api_server.id, leader.id)
                msg += '  {}->{}\n'.format(api_server.id, api_server.commit_index)
                msg += '  {}->{}\n'.format(leader.id, leader.commit_index)
            elif api_server.commit_index is None:
                msg += '{} and {} have unknown commit indexes\n'.format(
                    api_server.id, leader.id)
            elif api_server == leader:
                # if the API server is the leader, make sure it is connected to
                # a majority of servers; this is meant to detect the case where
                # the API server would trivially pass the `check_converged`
                # test because it thinks it is the leader while being
                # partitioned from the majority of the domain (leaders do not
                # step down until they receive a Raft message with a higher log
                # term, which may not happen until a partition is healed)

                # exclude evicted servers from consideration, since their
                # absence does not affect quorum
                voters = [s for s in servers if not s.is_evicted]
                disconnected_voters = [s.id for s in voters
                                       if s.connected_state != 'Connected']
                if len(disconnected_voters) >= len(voters) / 2.0:
                    # display all disconnected servers, including evicted
                    disconnected = [s.id for s in servers
                                    if s.connected_state != 'Connected']
                    msg += 'Servers not connected to leader {}: {}\n'.format(
                        api_server.id, ', '.join(sorted(disconnected)))

        if msg != '':
            raise RuntimeError('Health-check failed:\n' + msg)

        return api_server

    @Subcommand('check', 'servers',
                help='Check the status of admin servers')
    @MutuallyExclusive(
        Argument('--timeout', default=None, type=int,
                 help='How long (in seconds) to wait for server check to pass'),  # noqa
        Argument('--wait-forever', action='store_true', default=False,
                 help='Wait forever for server check to pass'))
    @Argument('--check-active', action='store_true', default=False,
              help='Check that all servers are ACTIVE')
    @Argument('--check-connected', action='store_true', default=False,
              help='Check that all servers are CONNECTED to the server performing check')  # noqa
    @Argument('--check-leader', action='store_true', default=False,
              help='Check that all servers agree on a leader')
    @Argument('--num-servers', default=None,
              help='Check that membership size equals specified number')
    @Argument('--check-liveness', default=None, type=float,
              help='Check that all servers have responded within the specified number of seconds')  # noqa
    @Argument('--wait-for-acks', action='store_true',
              help='If specified along with --timeout, wait for messages to be received from all servers while the command is running')  # noqa
    @Argument('--check-converged', action='store_true',
              help='Check that all servers have the same commit index. This implies --check-leader.')
    def check_servers(self, check_active=False, check_connected=False,
                      check_leader=False, num_servers=None, timeout=None,
                      wait_forever=False, check_liveness=None,
                      wait_for_acks=False, check_converged=False):
        if timeout is None and wait_forever:
            timeout = CommandProcessor.FOREVER
        if check_converged:
            check_leader = True
        if timeout is not None:
            liveness_gen = self._liveness_threshold_generator(wait_for_acks, check_liveness)
            return self._await(
                lambda: self._check_servers(
                    check_active, check_connected, check_leader, num_servers,
                    next(liveness_gen), check_converged),
                timeout)
        return self._check_servers(
            check_active, check_connected, check_leader, num_servers,
            check_liveness, check_converged)

    def _check_servers(self, check_active=False, check_connected=False,
                       check_leader=False, num_servers=None,
                       check_liveness=None, check_converged=False):
        servers = self.conn.get_servers()
        api_server = None
        inactive = set()
        disconnected = set()
        leader_terms = {}
        leaderless = set()
        commit_indexes = {}
        for server in servers:
            if server.is_local:
                api_server = server
            if server.raft_state != 'ACTIVE':
                inactive.add(server.id)
            if server.connected_state != 'Connected' or (
                    server.last_ack is not None and
                    check_liveness is not None and
                    server.last_ack > check_liveness):
                disconnected.add(server.id)
            key = (server.leader, server.log_term)
            leader_terms[key] = leader_terms.get(key, set())
            leader_terms[key].add(server.id)
            if server.leader is None:
                leaderless.add(server.id)
            commit_indexes[server.commit_index] = commit_indexes.get(
                server.commit_index, set())
            commit_indexes[server.commit_index].add(server.id)

        msg = ''
        # if `check_active`, check that all server are ACTIVE; otherwise
        # only check server performing health-check
        if check_active and len(inactive) != 0:
            msg += 'Servers not ACTIVE: {}\n'.format(
                ', '.join(sorted(inactive)))
        elif api_server.id in inactive:
            msg += '{} is not ACTIVE\n'.format(api_server.id)
        # if `check_connected`, check that all servers are CONNECTED to the
        # server performing health-check
        if (check_connected or check_liveness is not None) and len(disconnected) != 0:
            msg += 'Servers not CONNECTED to {}: {}\n'.format(
                api_server.id, ', '.join(sorted(disconnected)))
        # if `check_leader`, check that all servers agree on leader and
        # term; otherwise, only check that the server performing
        # health-check recognizes some server as leader
        if check_leader and len(leader_terms) != 1:
            msg += 'Non-unique LEADER:\n'
            for (leader, term), server_ids in leader_terms.items():
                msg += '  (leader={},term={})->[{}]\n'.format(
                    leader, term, ','.join(sorted(server_ids)))
        elif check_leader and len(leaderless) != 0:
            msg += 'Servers without LEADER: {}\n'.format(
                ', '.join(sorted(leaderless)))
        elif check_leader and check_converged and (
                len(commit_indexes) != 1 or None in commit_indexes):
            # unpack unique (leader, term) from key set
            leader_term, = list(leader_terms.keys())
            leader, _ = leader_term
            msg += 'Non-unique commit indexes:\n'
            for commit_index, server_ids in commit_indexes.items():
                # denote leader with brackets
                server_list = ','.join(
                    ('<{}>'.format(s) if s == leader else s)
                    for s in sorted(server_ids))
                msg += '  (commit={})->[{}]\n'.format(commit_index, server_list)
        # if `num_servers` was specified, check membership size
        if num_servers is not None and int(num_servers) != len(servers):
            msg += ('Unexpected number of servers: expected={}, actual={}'
                    .format(num_servers, len(servers)))

        if msg != '':
            raise RuntimeError('Health-check failed:\n' + msg)

        return servers

    @Subcommand('check', 'database',
                help='Check the status of a database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='The name of the database to check')
    @MutuallyExclusive(
        Argument('--timeout', default=None, type=int,
                 help='How long (in seconds) to wait for database check to pass'),  # noqa
        Argument('--wait-forever', action='store_true', default=False,
                 help='Wait forever for database check to pass'))
    @MutuallyExclusive(
        Argument('--check-running', action='store_true', default=False,
                  help='Check that the database is in the RUNNING state'),
        Argument('--check-syncing', action='store_true', default=False,
                  help='Check that all processes in the database are in the SYNCING or RUNNING state'))
    @Argument('--num-processes', default=None,
              help='Check that the database contains the specified number of processes')  # noqa
    @Argument('--check-liveness', default=None, type=float,
              help='Check that all processes have responded within the specified number of seconds')  # noqa
    @Argument('--wait-for-acks', action='store_true',
              help='If specified along with --timeout, wait for messages to be received from all processes while the command is running')  # noqa
    @Argument('--fail-on-process-exit', action='store_true', default=False,
              help='If specified along with --timeout, fail-fast if a process exits while the command is running')  # noqa
    def check_database(self, db_name, check_running=False, check_syncing=False, num_processes=None,
                       check_liveness=None, timeout=None, wait_forever=False,
                       wait_for_acks=False, fail_on_process_exit=False):
        if timeout is None and wait_forever:
            timeout = CommandProcessor.FOREVER
        if timeout is not None:
            liveness_gen = self._liveness_threshold_generator(wait_for_acks, check_liveness)
            expected_incarnation = None
            if fail_on_process_exit:
                database = self.conn.get_database(db_name)
                expected_incarnation = database.incarnation
                if database.state == 'NOT_RUNNING':
                    expected_incarnation = (expected_incarnation[0] + 1, 0)
            return self._await(
                lambda: self._check_database(
                    db_name, check_running, check_syncing, num_processes,
                    check_liveness=next(liveness_gen),
                    expected_incarnation=expected_incarnation),
                timeout)
        return self._check_database(
            db_name, check_running, check_syncing, num_processes, check_liveness)

    def _check_database(self, db_name, check_running=False, check_syncing=False, num_processes=None,
                        check_liveness=None, expected_incarnation=None):
        database = self.conn.get_database(db_name)
        if check_running and database.state != 'RUNNING':
            raise RuntimeError('Database {} is not RUNNING: {}'.format(
                db_name, database))
        if expected_incarnation is not None and expected_incarnation != database.incarnation:
            raise UnrecoverableException(
                'Process failures detected due to unexpected incarnation for database {}: expected={}, actual={}'.format(
                    db_name, expected_incarnation, database.incarnation))

        # check that all processes have expected state
        processes = list(self.conn.get_processes(db_name))

        if check_syncing:
            non_running_or_syncing = [str(p) for p in processes
                                      if p.durable_state != 'MONITORED'
                                      or p.engine_state not in('RUNNING', 'SYNCING')]
            if len(non_running_or_syncing) != 0:
                raise RuntimeError(
                    'Database {} has processes that are not syncing or running: \n{}'.format(
                        db_name, '\n'.join(non_running_or_syncing)))
        elif check_running or num_processes:
            non_running = [str(p) for p in processes
                           if p.durable_state != 'MONITORED'
                           or p.engine_state != 'RUNNING']
            if len(non_running) != 0:
                raise RuntimeError(
                    'Database {} has non-running processes: \n{}'.format(
                        db_name, '\n'.join(non_running)))

        # check that there are no unresponsive process if `check_liveness`
        # was specified; `check_liveness` should be the maximum number of
        # seconds since the last message from a process
        if check_liveness is not None:
            check_liveness = float(check_liveness)
            unresponsive = [str(p) for p in processes
                            if p.last_ack is None
                            or p.last_ack > check_liveness]
            if len(unresponsive) != 0:
                raise RuntimeError(
                    'Database {} has unresponsive processes: \n{}'.format(
                        db_name, '\n'.join(unresponsive)))
        if num_processes is not None:
            # check number of processes
            num_processes = int(num_processes)
            if num_processes != len(processes):
                raise RuntimeError(
                    'Unexpected number of processes: expected={}, actual={}'
                    .format(num_processes, len(processes)))
        if database.state == 'TOMBSTONE':
            raise RuntimeError('Database {} in TOMBSTONE state: {}'.format(
                db_name, database))
        return database

    def _print_database_processes(self, database, process_format, disconnected_servers):
        for process in self.conn.get_processes(database.name):
            if (process.engine_state == 'UNKNOWN'
                and process.durable_state == 'CONFIGURED'
                and process.journal_recovery_progress is not None):
                process._dict['state'] = 'RECOVERING_JOURNAL({:.1f}%)'.format(
                    100 * process.journal_recovery_progress)
            elif (process.engine_state == 'UNKNOWN'
                  and process.durable_state != 'MONITORED'
                  and process.durable_state != 'REQUESTED_SHUTDOWN'):
                process._dict['state'] = 'STARTING_UP'
            if self._is_process_unreachable(process, disconnected_servers):
                process._dict['state'] = 'UNREACHABLE({})'.format(
                    process.engine_state)
            self._print('    ' + get_formatted(process_format, process))

    def _is_process_unreachable(self, process, disconnected_servers):
        if process.server_id in disconnected_servers:
            return True
        if process.durable_state in ['MONITORED', 'REQUESTED_SHUTDOWN']:
            return process.last_ack is None or process.last_ack > 20
        return False

    def _print_exited_database_process(self, exited, exited_process_format,
                                       low_incarnation, current_incarnation):
        if exited.db_incarnation[0] < low_incarnation:
            return

        if current_incarnation != exited.db_incarnation[0]:
            current_incarnation = exited.db_incarnation[0]
            self._print('  [incarnation = {}]', current_incarnation)
        self._print('    ' + get_formatted(exited_process_format, exited))
        return current_incarnation

    def _print_exited_database_processes(self, db_name, low_incarnation,
                                         db_incarnation,
                                         exited_process_format):
        current_incarnation = db_incarnation
        try:
            for exited in self.conn.stream_exited_processes(db_name):
                current_incarnation = self._print_exited_database_process(
                    exited, exited_process_format, low_incarnation,
                    current_incarnation)
                if current_incarnation is None:
                    break
        except nuodb_mgmt.AdminException as e:
            if 'Unable to get all fields' not in str(e):
                raise

            # server does not support ordering on multiple fields; fallback to
            # old method of collecting all tombstones and sorting locally
            exited_processes = self.conn.get_exited_processes(db_name)
            # sort processes by (incarnation, start ID) in descending order of
            # incarnation
            exited_processes.sort(key=lambda exited: (-exited.db_incarnation[0],
                                                      -exited.db_incarnation[1],
                                                      exited.process.start_id))
            for exited in exited_processes:
                current_incarnation = self._print_exited_database_process(
                    exited, exited_process_format, low_incarnation,
                    current_incarnation)
                if current_incarnation is None:
                    break

    @Subcommand('handoff', 'report-timestamp',
                help='Query the storage managers to report latest state that will remain after handoff from active to passive datacenter')  # noqa
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='The name of the database to hand off')
    @Argument('--archive-ids', nargs='+', required=True, metavar='ID',
              completer=CommandProcessor.get_archive_ids,
              help='All archives in the passive datacenter')
    @Argument('--timeout', default=10, required=False, type=int,
              help='The number of seconds to wait for all specified archives to be online')  # noqa
    def handoff_report_timestamp(self, db_name, archive_ids, timeout=10):
        MILLISECOND_PER_SECOND = 1000
        report_timestamp_response = self.conn.handoff_database_report_timestamp(db_name, archive_ids, int(timeout) * MILLISECOND_PER_SECOND)  # noqa
        if not self.show_json:
            # If not displaying raw json show the response with
            # leaders formatted in the format expected from
            # reset-state
            new_format = ""
            for k in report_timestamp_response.leaders:
                new_format += '{} {}'.format(
                    k, report_timestamp_response.leaders[k])
            report_timestamp_response.set("leaders", new_format)

        self._show(report_timestamp_response)
        return report_timestamp_response

    @Subcommand('handoff', 'reset-state',
                help='Reset the state of the database for handoff from active to passive datacenter')  # noqa
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='The name of the database to hand off')
    @Argument('--commits', required=True, type=int, nargs='+', metavar='COMMIT',
              help='The list of commit sequence numbers of the restored state as reported by handoff report-timestamp (in the form x x x ... with no commas)')  # noqa
    @Argument('--epoch', type=int, required=True,
              help='The epoch of the restored state as reported by handoff report-timestamp')  # noqa
    @Argument('--leaders', required=True, type=int, nargs='+', metavar='SG_ID AR_ID',
              help='The mapping of storage groups to leader archives (in the form <storage group id> <archive id> <storage group id> <archive id> ...) as reported by handoff report-timestamp')  # noqa
    def handoff_reset_state(self, db_name, commits, epoch, leaders):
        leaders = CommandProcessor.dict_from_tokens(leaders)
        self.conn.handoff_database_reset_state(
            db_name, commits, epoch, leaders)
        self._print("State successfully reset")

    @Subcommand('set', 'archive',
                help='Set the specified archive to either regular server or passive observer of storage groups')  # noqa
    @Argument('--archive-id', required=True,
              completer=CommandProcessor.get_running_archive_ids,
              help='The id of the archive')
    @MutuallyExclusive(
        Argument('--active', action='store_true',
                 help='Promote the specified archive to an active server of all storage groups that it serves'),  # noqa
        Argument('--serves', nargs='+', metavar='SG_NAME',
                 help='Promote the specified archive to an active server of the named storage groups'),  # noqa
        Argument('--no-observers', nargs='+',
                 dest='serves', help=argparse.SUPPRESS),  # deprecated; this is kept for backward-compatibility
        required=False)
    @MutuallyExclusive(
        Argument('--passive', action='store_true',
                 help='Demote the specified archive to a passive observer of all storage groups that it serves'),  # noqa
        Argument('--observes', nargs='+', metavar='SG_NAME',
                 help='Demote the specified archive to a passive observer of the named storage groups'),  # noqa
        Argument('--observers', nargs='+',
                 dest='observes', help=argparse.SUPPRESS),  # deprecated; this is kept for backward-compatibility
        required=False)
    def set_archive(self, archive_id, serves=None, observes=None,
                    active=False, passive=False):
        if active:
            serves = ['*']
        if passive:
            observes = ['*']
        if observes is not None or serves is not None:
            observes = observes if observes else []
            serves = serves if serves else []
            self.conn.modify_observer_status(archive_id, serves, observes)

    @Subcommand('handoff', 'database', help='Handoff the database, making the passive datacenter active')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='The name of the database to hand off')
    @MutuallyExclusive(
        Argument('--archive-ids', nargs='+', metavar='ID', required=False,
                 completer=CommandProcessor.get_archive_ids,
                 help='The archive ids in the passive datacenter being promoted'), #noqa
        Argument('--all-observer-archive-ids', action='store_true',
                 help='Use all observers in handoff'),
        required=True)
    @Argument('--oldest-acceptable', required=False,
              type=CommandProcessor.datetime_from_str(
                  nuodb_mgmt.ReportTimestamp.TIMESTAMP_FORMAT_STRING,
                  nuodb_mgmt.ReportTimestamp.TIMESTAMP_EXAMPLE_STRING),
              help="Optional. If provided, handoff will only proceed if the most recent consistent state available, to which the databse state would be reset, is after the provided date/time. Timestamp must be in the form '{}'".format(nuodb_mgmt.ReportTimestamp.TIMESTAMP_EXAMPLE_STRING)) #noqa
    @Argument('--timeout', default=300, required=False, type=int,
              help='The number of seconds to wait for each step of handoff')
    def handoff_database(self, db_name, archive_ids=None, all_observer_archive_ids=False, oldest_acceptable=None, timeout=300):
        database = self.conn.get_database(db_name)

        if oldest_acceptable is None:
            oldest_acceptable = datetime.datetime.min

        if all_observer_archive_ids:
            archive_ids = []
            archives = self.conn.get_archives(db_name)
            for archive in archives:
                if len(archive.observer_storage_groups) > 0:
                    archive_ids.append(archive.id)
            if len(archive_ids) == 0:
                raise RuntimeError("No observer archives found in database '{}'".format(db_name))
        else:
            if len(archive_ids) == 0:
                raise RuntimeError("No archive ids provided with '--archive-ids'")
            # Double check that all supplied archive ids are observers
            not_observers = []
            for archive_id in archive_ids:
                archive = self.conn.get_archive(archive_id)
                if len(archive.observer_storage_groups) == 0:
                    not_observers.append(archive_id)
            if len(not_observers) != 0:
                raise RuntimeError("The following archive ids are not observers: " + str(not_observers))

        if database.default_options.get('ext-start') != 'true':
            # We are not using external start. Double check the database is NOT_RUNNING and then start the appropriate SMs
            db_state = self.conn.get_database(db_name).state
            if db_state != 'NOT_RUNNING':
                raise RuntimeError("Database must be in 'NOT_RUNNING' state. Currently in " + str(db_state))

            for archive_id in archive_ids:
                archive = self.conn.get_archive(archive_id)
                self._print("STARTING: SM process on archive " + str(archive_id))
                self._start_process(db_name, archive.server_id, 'SM', archive_id=archive_id)

        MILLISECOND_PER_SECOND = 1000
        report_timestamp_response = self.conn.handoff_database_report_timestamp(db_name, archive_ids, int(timeout) * MILLISECOND_PER_SECOND)  # noqa

        actual_timestamp = datetime.datetime.strptime(report_timestamp_response.timestamp, nuodb_mgmt.ReportTimestamp.TIMESTAMP_FORMAT_STRING)
        if actual_timestamp < oldest_acceptable:
            raise RuntimeError("Time of the most recent consistent state {} is earlier than supplied '--oldest-acceptable' {}. Aborting handoff".format(report_timestamp_response.timestamp, oldest_acceptable))

        self._print("Time of the most recent consistent state: {}".format(report_timestamp_response.timestamp))

        # Make sure engines are monitored and SYNCING before trying to
        # send reset state command
        self.check_database(db_name, check_syncing=True, timeout=timeout)

        # Reset the state
        key_values = report_timestamp_response.leaders.items()
        leaders = {int(key):int(value) for key, value in key_values}
        commits = [int(c) for c in report_timestamp_response.commits.split(',')]
        self.conn.handoff_database_reset_state(db_name, commits, report_timestamp_response.epoch, leaders)
        self._print("Reset state run successfully")

        # Promote the archives to leaders/leader candidates
        for archive_id in archive_ids:
            archive = self.conn.get_archive(archive_id)
            self.conn.modify_observer_status(archive_id, promote_storage_groups=archive.observer_storage_groups, demote_storage_groups=[])

        #  Make sure the SMs made it into the RUNNING state
        def check_engines_running(db_name):
            non_running = []
            for p in self.conn.get_processes(db_name):
                if p.engine_state != 'RUNNING':
                    non_running.append(str(p))
            if len(non_running) != 0:
                raise RuntimeError('Database {} has processes that are not running: \n{}'.format(db_name, '\n'.join(non_running)))
        self._await(lambda: check_engines_running(db_name), timeout)

        self._print("Successfully handed off database, you may proceed with the next handoff steps")

    @Subcommand('log', 'message',
                help='Emit a log message to the admin server')  # noqa
    @Argument('--message', required=True,
              help='The message to log')
    def log_message(self, message):
        output = self.conn.log_message(message)

        if self.show_json:
            self._show(output)
        else:
            timestamp = self._get_timestamp(output['serverTimeMillis'] / 1000.0)
            self._print("Message was successfully logged at {}", timestamp)

    @Subcommand('diagnose', 'database', help='diagnose database')
    @Argument('--db-name', required=True, default=EnvironmentalDefault(),
              completer=CommandProcessor.get_db_names,
              help='name of the database to diagnose')
    @Argument('--output-dir', required=False, help='directory for output files')
    @Argument('--timeout', required=False, help='timeout in seconds')
    @Argument('action', completer=CommandProcessor.get_empty,
              help="diagnose action to perform")
    @Argument('target', completer=CommandProcessor.get_empty,
              help="target of diagnose action")
    @Argument('arguments', nargs='*', completer=CommandProcessor.get_empty,
              help='diagnose operation arguments')
    def diagnose_database(self, db_name, action, target, output_dir=None,
                          timeout=None, arguments=None):
        if not self.conn.diagnose_processes(db_name, action, target,
                                            timeout=timeout,
                                            output=self.out,
                                            arguments=arguments,
                                            output_dir=output_dir):
            raise RuntimeError('{}: {} {} failed'
                               .format(db_name, action, target))

    @Subcommand('diagnose', 'process', help='diagnose process')
    @Argument('--start-id', required=True,
              help='name of the database to diagnose')
    @Argument('--output-dir', required=False, help='directory for output files')
    @Argument('--timeout', required=False, help='timeout in seconds')
    @Argument('action', completer=CommandProcessor.get_empty,
              help="diagnose action to perform")
    @Argument('target', completer=CommandProcessor.get_empty,
              help="target of diagnose action")
    @Argument('arguments', nargs='*', completer=CommandProcessor.get_empty,
              help='diagnose operation arguments')
    def diagnose_process(self, start_id, action, target, output_dir=None,
                         timeout=None, arguments=None):
        if not self.conn.diagnose_process(start_id, action, target,
                                          timeout=timeout,
                                          output=self.out,
                                          arguments=arguments,
                                          output_dir=output_dir):
            raise RuntimeError('start-id {}: {} {} failed'
                               .format(start_id, action, target))

    @Subcommand('generate', 'documentation',
                help='generate documentation for all nuocmd subcommands in AsciiDoc format')
    @Argument('--group-by-noun', action='store_true',
              help='if specified, group generated documentation by noun rather than verb')
    @Argument('--command-handler', required=False,
              help='name of command handler class to generate documentation for')
    @Argument('--title', required=False, help='title to use for page')
    def generate_documentation(self, group_by_noun=False, command_handler=None,
                               title=None):
        processor = CommandProcessor()
        if command_handler is None:
            processor.add_subcommands(AdminCommands)
        else:
            for cls in inspect.getmro(type(self)):
                if cls.__name__ == command_handler:
                    processor.add_subcommands(cls, defined_only=True)

        # print page header and table of global arguments
        if title is None:
            self.out.write('= NuoDB Command Reference\n\n')
        else:
            self.out.write('= {}\n\n'.format(title))
        self.out.write('== Global Arguments\n\n')
        self._generate_arg_table(processor.global_arg_specs)

        # define custom sort key if --group-by-noun was specified
        def sort_key(args):
            verb, noun = args
            if group_by_noun:
                return (noun, verb)
            return (verb, noun)
        subcommand_keys = sorted((key for key in processor.sp_dict.keys()
                                  if isinstance(key, tuple)),
                                 key=sort_key)

        current = None
        for verb, noun in subcommand_keys:
            parser, func = processor.sp_dict[(verb, noun)]
            # print section header for subcommand group
            if not group_by_noun and current != verb:
                current = verb
                self.out.write('== `{}` Subcommands\n\n'.format(verb))
            elif group_by_noun and current != noun:
                current = noun
                self.out.write('== `... {}` Subcommands\n\n'.format(noun))
            # include anchor so that other pages can reference this subsection
            self.out.write('[#{}-{}]\n'.format(verb, noun))
            self.out.write('=== `{} {}`\n\n'.format(verb, noun))
            # print description of subcommand
            subcommand_help = func._kwargs.get('help')
            if subcommand_help:
                self.out.write('{}\n\n'.format(subcommand_help))
            # print usage string describing syntax
            usage = parser.format_usage()
            if usage.startswith('usage: '):
                usage = '\n'.join(
                    line[len('usage: '):] for line in usage.split('\n'))
            self.out.write('[source,bash]\n----\n{}\n----\n\n'.format(usage))
            # print table of subcommand arguments
            arg_specs = Argument.get_arg_specs(func)
            self._generate_arg_table(arg_specs)

    def _generate_arg_table(self, arg_specs):
        arg_specs = Argument.expand_list(arg_specs)
        if len(arg_specs) != 0:
            self.out.write('[cols="1,2,1", options="header"]\n')
            self.out.write('|===\n')
            self.out.write('|Argument\n')
            self.out.write('|Description\n')
            self.out.write('|Default\n\n')
            for arg_spec in arg_specs:
                arg_help = arg_spec.kwargs.get('help')
                choices = arg_spec.kwargs.get('choices')
                if choices is not None:
                    arg_help = '{} (choices: {})'.format(
                        arg_help, ', '.join('`{}`'.format(c) for c in choices))
                # display default value, the environment variable that can be
                # used to define a default, and whether the argument is
                # required
                arg_name = arg_spec.args[0]
                arg_default = arg_spec.kwargs.get('default')
                if isinstance(arg_default, EnvironmentalDefault):
                    env_default = arg_default
                    arg_default = ' / '.join(
                        '`${}`'.format(env_var) for env_var in
                        arg_default.get_env_vars(arg_spec.args))
                    if env_default.default:
                        arg_default += ' / ' + (
                            self._make_symbolic(arg_name, env_default.default))
                    elif arg_spec.kwargs.get('required'):
                        arg_default += ' (required)'
                elif arg_spec.kwargs.get('required'):
                    arg_default = '(required)'
                elif not arg_default:
                    arg_default = ''
                else:
                    arg_default = self._make_symbolic(arg_name, arg_default)
                self.out.write('|`{}`\n'.format(arg_name))
                self.out.write('|{}\n'.format(arg_help))
                self.out.write('|{}\n\n'.format(arg_default))
            self.out.write('|===\n\n')

    def _make_symbolic(self, arg_name, default):
        # replace any values derived from the local environment (e.g. paths)
        # with symbolic values
        if arg_name in ['--nuokey-cmd', '--nuosql-cmd', '--nuoadmin-jar'] and (
                default.startswith(get_home_dir())):
            default = default.replace(get_home_dir(), '$NUODB_HOME')
        if arg_name in ['--raftlog-dir'] and (
                default.startswith(get_raftlog_dir())):
            default = default.replace(get_raftlog_dir(), '$NUODB_VARDIR')
        if arg_name == '--dname' and default == self.DEFAULT_DNAME:
            default = 'CN=<hostname>'
        if arg_name == '--output-dir' and default == os.getcwd():
            default = '<current directory>'
        return '`+{}+`'.format(default)

    def _show(self, obj):
        if isinstance(obj, nuodb_mgmt.Entity):
            obj.show(self.show_json, self.out)
        elif isinstance(obj, Mapping):
            self._print(json.dumps(obj, indent=2, sort_keys=True))
        else:
            self._print(obj)

    def _print(self, fmt, *args):
        if self.disable_print:
            return

        if len(args) != 0:
            if not is_string(fmt):
                raise ValueError('First argument should be a format string' +
                                 ' if multiple values are supplied')
            msg = fmt.format(*args)
        else:
            msg = str(fmt)

        self.out.write(msg)
        self.out.write('\n')

    @staticmethod
    def _await(check_fn, timeout, initial_delay=0.25, max_delay=5):
        delay = initial_delay
        start_time = time.time()
        while True:
            try:
                return check_fn()
            except UnrecoverableException:
                raise
            except Exception:
                remaining = timeout - (time.time() - start_time)
                if remaining > 0:
                    time.sleep(max(initial_delay, min(remaining, delay)))
                    delay = min(max_delay, 2 * delay)
                else:
                    raise


class UnrecoverableException(RuntimeError):
    pass


class ExitCommandException(nuodb_mgmt.Entity, Exception):
    """
    Raised by command handler methods whenever the command should exit without
    printing the usial error message. The actual command is responsible for
    printing all relevant information. It also holds the response entity so that
    it can be inspected in tests.
    """
    def __init__(self, _dict, exit_code=1):
        nuodb_mgmt.Entity.__init__(self, _dict)
        Exception.__init__(self, '')
        self._exit_code = exit_code

    @property
    def exit_code(self):
        return self._exit_code


PROCESSOR = CommandProcessor()


def read_capture_file(capture_file):
    """
    Return database name and list of processes from capture file.

    :param str capture_file: path to capture file

    :returns str, list[nuodb_mgmt.StartProcessRequest]: database name and list
                                                        of processes
    """

    with open(capture_file) as f:
        return read_capture_data(json.loads(f.read()))


def read_capture_data(capture_data):
    """
    Return list of processes from capture file.

    :param dict capture_data: data from capture file

    :returns str, list[nuodb_mgmt.StartProcessRequest]: database name and list
                                                        of processes
    """

    if capture_data.get('incremental', False):
        raise ValueError('Cannot enforce database using incremental startplan')

    proc_specs = list(map(nuodb_mgmt.StartProcessRequest, capture_data['processes']))
    # order process specifications so that TEs appear last
    proc_specs.sort(key=lambda p: (p.engine_type, p.archive_id))
    if len(proc_specs) == 0:
        return None, []

    # make sure that a unique database name appears in capture file
    db_name = set(spec.db_name for spec in proc_specs)
    if len(db_name) == 0:
        raise ValueError('Invalid capture file: ' + json.dumps(capture_data))
    if len(db_name) > 1:
        raise ValueError(
            'Inconsistent database names in capture file: {}\n{}'.format(
                db_name, capture_data))
    return db_name.pop(), proc_specs


class OptionalFormatter(string.Formatter):
    """
    Custom formatter that leaves out unspecified keys and allows defaults to be
    specified using the syntax '{<key>::<default>}'.
    """

    def get_value(self, key, args, kwargs):
        if is_string(key):
            return kwargs.get(key)
        return super(OptionalFormatter, self).get_value(key, args, kwargs)

    def format_field(self, value, spec):
        if ':' in spec:
            # conversion specifiers are already denoted using ':', e.g.
            # {number:.2f}, so any ':' inside of it is denoting the default
            # value using our custom syntax, e.g. {number:.2f:0.00}; so we need
            # to strip away the ':<default>' part from it so that the
            # superclass receives the real specifier
            spec, default = spec.rsplit(':', 1)
        else:
            default = '<NO VALUE>'

        if value is None:
            # value is None, so return default
            return default
        # value was found, so use do normal conversion with real specifier
        return super(OptionalFormatter, self).format_field(value, spec)


def get_formatted(fmt, entity):
    """
    Format an entity.

    :param str fmt: format string
    :param nuodb_mgmt.Entity entity: entity to format

    :returns str: formatted entity
    """

    if isinstance(entity, nuodb_mgmt.Entity):
        params = dict(entity._dict)
        params.update(entity.get_declared())
    elif isinstance(entity, Mapping):
        params = entity
    else:
        raise ValueError('Cannot format object of type {}'
                         .format(type(entity)))
    return OptionalFormatter().format(fmt, **params)


def subcommand(func, processor=PROCESSOR):
    """
    Create a subcommand on the supplied CommandProcessor by inspecting the
    function signature to determine the subcommand name and arguments. This is
    a simpler but less flexible approach than using
    `@CommandProcessor.subcommand()` and `@Argument` directly.

    Example:

    ```
    #!/usr/bin/env python
    # ~ cmd.py ~
    from pynuoadmin import nuodb_cli

    class CustomSubcommands(nuodb_cli.AdminCommands):

        @nuodb_cli.subcommand
        def do_custom_thing(self, required, optional=None, switch=False):
            ...

    if __name__ == '__main__':
        nuodb_cli.execute(CustomSubcommands)
    ```

    This creates a subcommand that is invoked as follows:

        ./cmd.py do custom-thing --required value --switch

    :param CommandProcessor processor: the CommandProcessor object
    :param function func: the function to create a subcommand from, which is
                          assumed to be an AdminCommands method or an ordinary
                          function that takes an AdminCommands object as the
                          first argument
    """

    # get action and entity from function name; action is the first word
    # (delimited by '_') and entity is all remaining words (with '_' replaced
    # by '-')
    action, entity = func.__name__.split('_', 1)
    entity = entity.replace('_', '-')

    # get all argument names and default values
    argspec = inspect.getargspec(func)
    if len(argspec.args) == 0:
        raise ValueError('Subcommand method must take at least one argument of type AdminCommands')  # noqa
    if argspec.keywords is not None or argspec.varargs is not None:
        raise ValueError('Subcommand method cannot take varargs or kwargs')
    # create kwargs for each call to Argument decorator;
    # argspec.defaults contains defaults corresponding to suffix of
    # argspec.args; if there are no defaults in function signature,
    # argspec.defaults is None
    defaults = argspec.defaults
    if defaults is None:
        defaults = ()
    kwargs_list = [dict(required=True) for _ in
                   range(0, len(argspec.args) - len(defaults))]
    for default in defaults:
        kwargs = dict(default=default)
        # if default is False, make argument a switch
        if default is False:
            kwargs['action'] = 'store_true'
        kwargs_list.append(kwargs)

    # extract metadata out of docstring
    subcommand_help, extra_params = extract_help_text(func)

    # manually apply Argument decorator to function for every argument except
    # the first (which should be an AdminCommands object)
    for argname, kwargs in list(zip(argspec.args, kwargs_list))[1:]:
        extra_kwargs = extra_params.pop(argname, {})
        kwargs.update(extra_kwargs)
        Argument('--' + argname.replace('_', '-'), **kwargs)(func)

    # finally, apply the Subcommand decorator to function
    Subcommand(action, entity, help=subcommand_help)(func)
    return func


def extract_help_text(func):
    if not func.__doc__:
        return None, {}

    subcommand_help = current = []
    arg_params = {}
    for line in func.__doc__.split('\n'):
        # use `:param <type> <name>:` syntax to find argument documentation
        m = re.match('^ *:param ([^ ]+) ([^ ]+):', line)
        if m:
            kwargs = {}
            kwargs['help'] = current = []
            # use argument type to inject more metadata for parameter, such as
            # type and whether the argument accepts a stream of tokens
            arg_type = m.group(1)
            # allow `*` and `+` to be used as a suffix to the type to specify
            # whether the argument accepts a stream of tokens; for example,
            # `:param int* numbers:` specifies an argument that accepts 0 or
            # more integers, and `:param str+ text:` accepts 1 or more strings
            if arg_type[-1] in ['*', '+']:
                kwargs['nargs'] = arg_type[-1]
                arg_type = arg_type[:-1]
            # allow int, float, bool, and str to be specified as types; the
            # bool and str types are ignored (we expect the fact that the
            # argument is a switch to be signaled by also specifying a default
            # value of False in the method signature, so we do not rely on the
            # type to control that)
            if arg_type in ['int', 'float']:
                kwargs['type'] = getattr(builtins, arg_type)
            elif arg_type not in ['bool', 'str']:
                raise RuntimeError('Unexpected argument type: ' + arg_type)
            # add argument to dictionary and strip away `:param ...:` prefix
            arg_name = m.group(2)
            arg_params[arg_name] = kwargs
            line = line[len(m.group(0)):]

        current.append(line)

    # combine all lines collected for the subcommand and argument help text
    subcommand_help = join_lines(subcommand_help)
    for kwargs in arg_params.values():
        kwargs['help'] = join_lines(kwargs['help'])

    return subcommand_help, arg_params


PAR_DELIM = '\n\n'


def join_lines(lines):
    if len(lines) == 0:
        return ''

    verbatim_padding = None
    ret = ''
    for line in lines:
        # look for verbatim delimiter (```)
        verbatim_match = re.match('^( *)```', line)
        if verbatim_match and verbatim_padding is None:
            # begin verbatim block; save leading whitespace and add paragraph
            # delimiter
            verbatim_padding = len(verbatim_match.group(1))
            if not ret.endswith(PAR_DELIM):
                ret += PAR_DELIM
        elif verbatim_match:
            # end verbatim block
            verbatim_padding = None

        if verbatim_padding is None:
            stripped = line.strip()
            if len(stripped) == 0 and not ret.endswith(PAR_DELIM):
                # empty line; add paragraph delimiter
                ret += PAR_DELIM
            elif len(ret) == 0 or ret.endswith(PAR_DELIM) or verbatim_match:
                # first line of a new paragraph or end of verbatim block
                ret += stripped
            else:
                # new line in an existing paragraph; add a space and combine
                # with paragraph
                ret += ' ' + stripped

            if verbatim_match and not ret.endswith(PAR_DELIM):
                ret += PAR_DELIM
        else:
            # strip away leading whitespace based on minimum padding
            # encountered so far
            padding = len(re.match('^ *', line).group(0))
            verbatim_padding = min(verbatim_padding, padding)
            ret += line[verbatim_padding:].rstrip() + '\n'

    return ret.strip()


def execute(command_handler=None):
    check_version()
    check_dependencies()
    PROCESSOR.execute(command_handler=command_handler)


if __name__ == '__main__':
    execute()
