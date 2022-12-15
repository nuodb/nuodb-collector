#!/usr/bin/env python
# (C) Copyright NuoDB, Inc. 2021  All Rights Reserved.

import inspect
import os

from . import nuodb_cli
try:
    from importlib.machinery import SourceFileLoader
    def load_source(module_name, plugin_path):
        return SourceFileLoader(module_name,plugin_path).load_module()
except ImportError:
    # python 2 import
    import imp
    def load_source(module_name, plugin_path):
        return imp.load_source(module_name,plugin_path)


def execute(command_handler=None):
    nuodb_cli.check_version()
    nuodb_cli.check_dependencies()
    nuodb_cli.PROCESSOR.execute(command_handler=command_handler)


def get_command_handler(command_handlers):
    if len(command_handlers) == 0:
        return nuodb_cli.AdminCommands

    # make sure no AdminCommands methods are overridden and that none of the
    # command handlers have conflicting method definitions
    for i in range(0, len(command_handlers)):
        # make sure that command handler does not override any methods of
        # AdminCommands except the constructor
        defined = set(command_handlers[i].__dict__.keys())
        overridden = defined.intersection(nuodb_cli.AdminCommands.__dict__.keys()).difference(['__module__', '__doc__', '__init__'])
        if len(overridden) != 0:
            raise RuntimeError('AdminCommands attributes overridden by {}.{}: {}'.format(
                command_handlers[i].__module__, command_handlers[i].__name__,
                ', '.join(overridden)))
        # make sure that command handler does not define methods present in any
        # other command handler
        for j in range(i + 1, len(command_handlers)):
            conflicting = defined.intersection(command_handlers[j].__dict__.keys()).difference(['__module__', '__doc__'])
            if len(conflicting) != 0:
                raise RuntimeError('Conflicting attributes for {}.{} and {}.{}: {}'.format(
                    command_handlers[i].__module__,
                    command_handlers[i].__name__,
                    command_handlers[j].__module__,
                    command_handlers[j].__name__, ', '.join(conflicting)))

    return type('CustomAdminCommands', command_handlers, {})


def build_command_handler():
    command_handlers = set()
    modules = []
    # discover any command handlers in Python files explicitly specified with
    # NUOCMD_PLUGINS as a colon-separated list
    plugin_paths = os.environ.get('NUOCMD_PLUGINS')
    if plugin_paths:
        for plugin_path in plugin_paths.split(':'):
            if os.path.isfile(plugin_path):
                basename = os.path.basename(plugin_path)
                module_name = os.path.splitext(basename)[0]
                modules.append(load_source(module_name, plugin_path))

    for module in modules:
        for name in dir(module):
            cls = getattr(module, name)
            if inspect.isclass(cls) and issubclass(cls, nuodb_cli.AdminCommands):
                command_handlers.add(cls)

    return get_command_handler(tuple(command_handlers))


if __name__ == '__main__':
    execute(build_command_handler())
