from datetime import datetime

"""
<SyncTrace NodeId="1" PID="1313" SampleTime="255193801">
   <Trace AverageStallTime="0" MaxStallTime="42" MinStallTime="0" Name="NoTrace" NumStalls="127107" TotalStallTime="12562" />
   ...
   <Trace AverageStallTime="1" MaxStallTime="74" MinStallTime="1" Name="PlatformObjectMarkComplete" NumLocks="4585118" NumStalls="110" NumUnlocks="4585118" TotalStallTime="192" />
   <Probes>
      <Transaction Duration="26577890078" ID="6194963074" />
      <Transaction Duration="2764147844" ID="5102867202" />
      <Transaction Duration="436200606" ID="6967425282" />
      <Transaction Duration="94011584" ID="5102974466" />
      ...
   </Probes>
</SyncTrace>
"""


# for each nuodb process
class Monitor:
    def __init__(self, nuodb_process, conn, relative, args):
        self._process = nuodb_process
        self._conn = conn
        self._relative = relative
        self._lastnow = None
        self._stalls = (0, {})
        print(Monitor.header)

    format = "%s,%d,%d,%s,%d,%s,%d,%d,%s,%d,%d,%d,%d,%d"
    header = "#time,id,startId,host,pid,dbname,timedelta,totalSumStalls,name,numLocks,numUnlocks,numStalls,totalTimeStalls,maxStallTime"

    def execute_query(self):
        now = datetime.now()
        st = self._conn.send_query_request(self._process.start_id, "SyncTrace")
        total = int(st.get("SampleTime"))
        laststalls = self._stalls

        def value(v):
            return int(v) if v else 0

        stalls = dict([(tr.get("Name"), (value(tr.get("NumLocks")), value(tr.get("NumUnlocks")),
                                         value(tr.get("NumStalls")), value(tr.get("TotalStallTime")),
                                         value(tr.get("MaxStallTime"))
                                         )
                        ) for tr in st.findall("Trace")])
        self._stalls = (total, stalls)
        ototal, ostalls = laststalls
        self.process(now, total, stalls, ototal, ostalls)
        self._lastnow = now

    def process(self, now, total, stalls, ototal, ostalls):
        """ process all sync points """
        startId = int(self._process.start_id)
        dbname = self._process.db_name
        pid = int(self._process.pid)
        hostname = self._process.get('hostname')
        ntime = now.strftime("%s")
        nodeId = self._process.node_id

        if self._relative:
            if self._lastnow:
                # output delta
                timedelta = int((now - self._lastnow).total_seconds() * 1000000)
                for name, (numLocks, numUnlocks, numStalls, totalTimeStalls, maxStallTime) in stalls.items():
                    if name in ostalls:
                        numLocks -= ostalls[name][0]
                        numUnlocks -= ostalls[name][1]
                        numStalls -= ostalls[name][2]
                        totalTimeStalls -= ostalls[name][3]
                    if numStalls > 0:
                        print(Monitor.format % (
                        ntime, nodeId, startId, hostname, pid, dbname, timedelta, total - ototal, name,
                        numLocks, numUnlocks, numStalls, totalTimeStalls, maxStallTime))
        else:
            if self._lastnow:
                timedelta = int((now - self._lastnow).total_seconds() * 1000000)
            else:
                timedelta = 0
            for name, (numLocks, numUnlocks, numStalls, totalTimeStalls, maxStallTime) in stalls.items():
                print(Monitor.format % (ntime, nodeId, startId, hostname, pid, dbname, timedelta, total, name,
                                         numLocks, numUnlocks, numStalls, totalTimeStalls, maxStallTime))
