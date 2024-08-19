import os
import signal
import sys

from flask import Flask
from wsgiref import simple_server

app = Flask(__name__)

@app.route('/reload')
def reload():
    """ reload telegraf """
    ppid = os.getppid()
    if ppid:
        os.kill(ppid, signal.SIGHUP)
    return str(ppid)


if __name__ == '__main__':
    port = 5000
    try:
        with simple_server.make_server("", port, app) as httpd:
            sys.stderr.write("telegraf_reload: Running HTTP server on port {}\n".format(port))
            if sys.stdin.isatty():
                sys.stderr.write("Press CTRL+C to quit...\n")
            httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("Exiting...\n")
