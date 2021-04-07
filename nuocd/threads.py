from datetime import datetime
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

LINE_FORMAT = "%s,%s,%s,%s,%s,%s,%s,%d,%f,%f,%f,%d,%d,%s"
TIMEFMT = "%s%f"
HOSTNAME = socket.gethostname()
CLK_TCK = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
m = re.compile("(\d+) \((.*)\)")

def transform_to_cpu_percentage(clock_ticks, elapsed_time_in_seconds):
    return float(clock_ticks)/CLK_TCK * 100. / elapsed_time_in_seconds

# for each nuodb process
class Monitor:
    header="#host,startid,dbname,pid,tid,cpustate,exe,lcpu,utime,stime,ttime,minf,majf,time"

    def __init__(self, nuodb_process, conn, relative, args):
        self._process = nuodb_process
        self._conn = conn
        self._relative = relative
        self._lastnow = None
        self._host_pid = int(self._process.pid)
        self._last_measurements = {}
        print(Monitor.header)

    def execute_query(self):
        now = datetime.now()

        start_read = datetime.now()
        visited_this_cycle = set()
        startId = int(self._process.start_id)
        dbname = self._process.db_name

        for stat_file in glob.glob("/proc/%s/task/*/stat" % self._host_pid):
            try:
                with open(stat_file, 'r') as f:
                    line = f.read()
                    result = m.match(line)
                    thread_pid, exe = result.groups()
                    args = line[:-1].split(' ')
                    args = args[-50:]
                    if thread_pid == self._host_pid:
                        continue
                    visited_this_cycle.add(thread_pid)
                    args.insert(0, exe)
                    args.insert(0, start_read)
                    if thread_pid in self._last_measurements.keys():
                        prev_measurement = self._last_measurements[thread_pid]
                        exe = args[1]
                        state = args[2]
                        minf = int(args[9]) - int(prev_measurement[9])
                        majf = int(args[11]) - int(prev_measurement[11])
                        lcpu = int(args[38])
                        deltatime = start_read - prev_measurement[0]
                        dt = deltatime.total_seconds()
                        utime = transform_to_cpu_percentage(float(args[13]) - float(prev_measurement[13]), dt)
                        stime = transform_to_cpu_percentage(float(args[14]) - float(prev_measurement[14]), dt)
                        ttime = utime + stime
                        print(LINE_FORMAT % (HOSTNAME, startId, dbname, self._host_pid, thread_pid, state, exe, lcpu, utime, stime, ttime, minf, majf, start_read.strftime(TIMEFMT)))

                    self._last_measurements[thread_pid] = args
            except Exception as thread_exception:
                print_(thread_exception)
                continue

        # clean out threads that are not running anymore
        for thread_pid in self._last_measurements.keys():
            if thread_pid not in visited_this_cycle:
                del self._last_measurements[thread_pid]


