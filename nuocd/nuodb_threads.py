import glob
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime

from six import print_


def signal_handler(sig, frame):
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)

LINE_FORMAT = "%s,%s,%s,%s,%s,%d,%f,%f,%f,%d,%d,%s"
TIMEFMT = "%s%f"
HOSTNAME = socket.gethostname()
CLK_TCK = os.sysconf(os.sysconf_names['SC_CLK_TCK'])

if len(sys.argv) < 2:
    PROCESS_NAME = "nuodb"
else:
    PROCESS_NAME = sys.argv[1]

pat = "(\d+) \((.*)\)"
m = re.compile(pat)

module = "nuodb_threads"


def transform_to_cpu_percentage(clock_ticks, elapsed_time_in_seconds):
    return float(clock_ticks)/CLK_TCK * 100. / elapsed_time_in_seconds


class NuoDBProcess:
    stat_files = None
    last_measurements = {}
    host_pid = None

    def __init__(self, pid):
        self.host_pid = pid

    def read(self):
        start_read = datetime.now()
        visited_this_cycle = set()

        for stat_file in glob.glob("/proc/%s/task/*/stat" % self.host_pid):
            try:
                with open(stat_file, 'r') as f:
                    line = f.read()
                    result = m.match(line)
                    thread_pid, exe = result.groups()
                    args = line[:-1].split(' ')
                    args = args[-50:]
                    if thread_pid == self.host_pid:
                        continue
                    visited_this_cycle.add(thread_pid)
                    args.insert(0, exe)
                    args.insert(0, start_read)
                    if thread_pid in self.last_measurements.keys():
                        prev_measurement = self.last_measurements[thread_pid]
                        exe = args[1]
                        state = args[2]
                        minf = int(args[9]) - int(prev_measurement[9])
                        majf = int(args[11]) - int(prev_measurement[11])
                        lcpu = int(args[38])
                        deltatime = start_read - prev_measurement[0]
                        dt = deltatime.total_seconds()
                        utime = transform_to_cpu_percentage(args[13] - prev_measurement[13], dt)
                        stime = transform_to_cpu_percentage(args[14] - prev_measurement[14], dt)
                        ttime = utime + stime
                        print(LINE_FORMAT % (HOSTNAME, self.host_pid, thread_pid, state, exe, lcpu, utime, stime, ttime, minf, majf, start_read.strftime(TIMEFMT)))

                    self.last_measurements[thread_pid] = args
            except Exception as thread_exception:
                print_("%s: Could not read thread stats (%s) for NuoDB process with pid (%s) due to: %s " %
                       (module, thread_pid, self.host_pid, thread_exception), file=sys.stderr)
                continue

        # clean out threads that are not running anymore
        for thread_pid in self.last_measurements.keys():
            if thread_pid not in visited_this_cycle:
                del self.last_measurements[thread_pid]


if __name__ == "__main__":
    print("#host,processid,threadid,state,exe,lcpu,utime,stime,ttime,minf,majf,time")

    known_nuodb_processes = {}

    while True:
        process_begin_time = datetime.now()
        try:
            raw_pgrep = subprocess.check_output(["pgrep", '^{}$'.format(PROCESS_NAME)])
            pids = raw_pgrep.split()
            for pid in pids:
                if pid not in known_nuodb_processes.keys():
                    proc = NuoDBProcess(pid)
                    known_nuodb_processes[pid] = proc
                    print_("%s: Found new NuoDB process with pid (%s)." % (module, pid), file=sys.stderr)

            for known_pid in known_nuodb_processes.keys():
                if known_pid not in pids:
                    del known_nuodb_processes[known_pid]
                    print_("%s: NuoDB process with pid (%s) exited." % (module, pid), file=sys.stderr)

        except subprocess.CalledProcessError:
            pass
        except:
            print_("%s: Unexpected error: %s" % (module, sys.exc_info()[0]), file=sys.stderr)
            pass

        for pid, process in known_nuodb_processes.items():
            try:
                process.read()
            except Exception as e:
                print_("%s: Reading thread stats of NuoDB process with pid (%s) failed due to exception: %s" % (module, pid, e), file=sys.stderr)
                del known_nuodb_processes[pid]

        sys.stdout.flush()

        wait = datetime.now() - process_begin_time
        sleepFor = 10 - wait.total_seconds() - 1. / CLK_TCK
        time.sleep(sleepFor)
