from datetime import datetime
from xml.etree import ElementTree

from pynuoadmin import nuodb_mgmt

"""
<MessageTrace NodeId="1" PID="1313">
   <MessageApplyTime NodeId="2" SampleTime="4392757541">
      <Trace AverageStallTime="90" MaxStallTime="2880" MinStallTime="13" Name="OBJECT_REQUEST" NumStalls="9067" TotalStallTime="823661" />
      <Trace AverageStallTime="226" MaxStallTime="15582" MinStallTime="12" Name="PING" NumStalls="454362" TotalStallTime="102694912" />
      ...
   </MessageApplyTime>
</MessageTrace>
"""


# for each nuodb process
class Monitor:
    # (ntime,startId,hostname,pid,dbname,timedelta,fromNodeId,total-ototal,name,numStalls,totalTimeStalls,maxStallTime)
    format = "%s,%s,%s,%s,%d,%s,%d,%s,%d,%d,%s,%d,%d,%d"
    header = "#time,id,nodeId,listenerId,startId,host,pid,dbname,timedelta,totalSumStalls,message,numStalls,totalTimeStalls,maxStallTime"

    def __init__(self, nuodb_process, conn, relative, args):
        self._process = nuodb_process
        self._conn = conn
        self._relative = relative
        self._lastnow = None
        self._stalls = {}
        print(Monitor.header)

    def execute_query(self):
        now = datetime.now()
        st = self._conn.send_query_request(self._process.start_id, "MessageTrace")
        mats = st.findall("MessageApplyTime")
        laststalls = self._stalls
        self._stalls = {}
        for mat in mats:
            total = int(mat.get("SampleTime"))
            fromNodeId = mat.get("NodeId")
            totals = mat.findall("Trace")
            stalls = dict([(tr.get("Name"), (int(tr.get("NumStalls")),
                                             int(tr.get("TotalStallTime")),
                                             int(tr.get("MaxStallTime")))) for tr in totals])
            self._stalls[fromNodeId] = (total, stalls)
            ototal, ostalls = laststalls[fromNodeId] if fromNodeId in laststalls else (0, {})
            self.process(now, fromNodeId, total, stalls, ototal, ostalls)
        self._lastnow = now

    def process(self, now, fromNodeId, total, stalls, ototal, ostalls):
        """ process one node listener (fromNodeId) at time now"""
        startId = int(self._process.start_id)
        dbname = self._process.db_name
        pid = int(self._process.pid)
        hostname = self._process.get('hostname')
        ntime = now.strftime("%s")
        id = "%s:%s" % (self._process.node_id, fromNodeId)
        node_id = self._process.node_id
        listener_id = fromNodeId

        if self._relative:
            if self._lastnow:
                # output delta
                timedelta = int((now - self._lastnow).total_seconds() * 1000000)
                for name, (numStalls, totalTimeStalls, maxStallTime) in stalls.iteritems():
                    if name in ostalls:
                        numStalls -= ostalls[name][0]
                        totalTimeStalls -= ostalls[name][1]
                        print(Monitor.format % (
                            ntime, id, node_id, listener_id, startId, hostname, pid, dbname, timedelta,
                            total - ototal, name, numStalls, totalTimeStalls, maxStallTime))
        else:
            if self._lastnow:
                timedelta = int((now - self._lastnow).total_seconds() * 1000000)
            else:
                timedelta = 0
            for name, (numStalls, totalTimeStalls, maxStallTime) in stalls.iteritems():
                print(Monitor.format % (
                    ntime, id, node_id, listener_id, startId, hostname, pid, dbname, timedelta, total, name, numStalls,
                    totalTimeStalls, maxStallTime))
