import pynuodb
import pprint
import copy, sys
import datetime

sql = """
select CURRENT_TIMESTAMP as SNAPSHOTTIME,LASTEXECUTED,ID,SQLSTRING,NUMEXECUTES,EXECTIME,
COMPILETIME,INDEXHITS,INDEXFETCHES,EXHAUSTIVEFETCHES,INDEXBATCHFETCHES,
EXHAUSTIVEBATCHFETCHES,RECORDSFETCHED,RECORDSRETURNED,INSERTS,UPDATES,DELETIONS,REPLACES,
RECORDSSORTED,UPDATECOUNT,EVICTED,LOCKED,REJECTEDINDEXHITS from system.localstatementcache
"""
sql = sql.replace("\n"," ")

class Statement:
    def __init__(self,**entries):
        self.__dict__.update(entries)
        self.collection_interval = 0.0

    def __value(self,coltype,colvalue):
        if colvalue is None:
            return ""
        elif coltype == datetime.datetime:
            return f'{colvalue.strftime("%Y%m%dT%H:%M:%S.%f000")}'
        elif coltype == str:
            s=repr(colvalue)[1:-1]
            s=s.replace('"','\\"')
            return f'"{s}"'
        return colvalue

    def __attr(self,field):
        name,type = field
        return f"{name}={self.__value(type,self.__dict__.get(name,None))}"

    def __repr__(self):
        str = " "
        return str.join([self.__attr(field) for field in self._coltypes])

def nuodb_type(coltype):
    if coltype == pynuodb.NUMBER:
        return int
    elif coltype == pynuodb.DATETIME:
        return datetime.datetime
    elif coltype == pynuodb.STRING:
        return str
    else:
        return str

# for each nuodb process
class Monitor:
    def __init__(self, nuodb_process, conn, relative, args):
        self._process = nuodb_process
        self._conn = conn
        self._relative = relative

        server = self._conn.get_server(self._process.server_id)
        startid = self._process.start_id
        hostname = server.address[:-1]+"4"
        options = dict(schema="system"
                       , LBQuery=f"random(start_id({startid}))")
        self._connection = pynuodb.connect(database=self._process.db_name
                                          , host=hostname
                                          , user='dba'
                                          , password="dba"
                                          , options=options)
        self._cursor = None
        self._last = None # self.__get_latest()
        
    def __get_latest(self):
        results = {}
        try:
            self._cursor = self._connection.cursor()
            self._cursor.execute(sql)
            coltypes = [ (col[0].lower(),nuodb_type(col[1])) for col in self._cursor.description ]
            columns = [ name for name,_ in coltypes ]
            coltypes.append(("collection_interval", float))
            for row in self._cursor.fetchall():
                stmt = Statement(**dict(zip(columns,row)))
                stmt._coltypes = coltypes
                results[stmt.id] = stmt
        finally:
            if self._cursor is not None:
                self._connection.commit()
            self._cursor = None
            pass
        return results

    def _get_delta(self,previous,current):
        delta = copy.deepcopy(current)
        for name,coltype in current._coltypes:
            if coltype == int:
                oldvalue = getattr(previous,name)
                newvalue = getattr(current,name)
                setattr(delta,name,newvalue-oldvalue)
        td = (current.snapshottime-previous.snapshottime).total_seconds()
        setattr(delta,"collection_interval",td)
        return delta

    def execute_query(self):
        latest = self.__get_latest()
        if self._last is not None:
            results=[]
            for id,current in latest.items():
                if id in self._last:
                    previous = self._last[id]
                    if previous.numexecutes != current.numexecutes:
                        delta = self._get_delta(previous,current)
                        results.append(delta)
                else:
                    results.append(current)
            for row in sorted(results,reverse=True,key=lambda x: x.lastexecuted):
                print(row)
        self._last = latest

