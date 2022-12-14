import os
import signal

from flask import Flask

app = Flask(__name__)


@app.route('/reload')
def reload():
    """ reload telegraf """
    ppid = os.getppid()
    if ppid:
        os.kill(ppid, signal.SIGHUP)
    return str(ppid)


if __name__ == '__main__':
    import logging

    logging.getLogger('werkzeug').disabled = True
    #os.environ['WERKZEUG_RUN_MAIN'] = 'true'
    app.run(debug=True, use_reloader=False)
