import os, hashlib,sys
import signal
from six import print_
from flask import Flask

app = Flask(__name__)

plugins = {}


@app.route('/reload')
def reload():
    """ reload telegraf """
# Check if there is a change in plugins from when we started,  if so then
# reload plugin.  Note, on a reload this program will exit and restart so
# plugins will be updated then to reflex current state.
    files = []
    for r, d, f in os.walk(path):
        for fname in f:
            if not fname.endswith('~'):
                files.append(os.path.join(r, fname))
    if len(files) == len(plugins):
        count = 0
        for file in files:
            if file not in plugins:
                break
            data = None
            try:
                with open(file, 'rb') as fd:
                    data = fd.read()
            except:
                break
            md5 = hashlib.md5(data).hexdigest()
            if plugins[file] != md5:
                break
            count += 1
        if count == len(plugins):
            print_("RELOAD: configuration has not changed. ignoring...",file=sys.stderr)
            return "NOOP"

    ppid = os.getppid()
    if ppid:
        os.kill(ppid, signal.SIGHUP)
    return str(ppid)


if __name__ == '__main__':
    import logging
# there are some issues with reload() is getting called erroneously.  So let's verify
# there is actually a change and reason to reload.
    path = "/etc/telegraf/telegraf.d/dynamic"
    for r, d, f in os.walk(path):
        for fname in f:
            if not fname.endswith('~'):
                file = os.path.join(r, fname);
                with open(file, 'rb') as fd:
                    data = fd.read();
                md5 = hashlib.md5(data).hexdigest()
                plugins[file] = md5
                print_("RELOAD: %s %s" % (md5, file),file=sys.stderr)

    logging.getLogger('werkzeug').disabled = True
    os.environ['WERKZEUG_RUN_MAIN'] = 'true'
    app.run(debug=False)
