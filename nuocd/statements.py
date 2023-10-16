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
    description = [ ( "db_name", str ),
                    ( "start_id", str ),
                    ( "collection_interval", float),
                    ( "avgexectime", int ),
                    ( "snapshottime", datetime.datetime) ,
                    ( "lastexecuted", datetime.datetime) ,
                    ( "id" , str),
                    ( "sqlstring" , str ),
                    ( "numexecutes", int ),
                    ( "exectime", int ),
                    ( "compiletime", int ),
                    ( "indexhits", int ),
                    ( "indexfetches", int ),
                    ( "exhaustivefetches", int ),
                    ( "indexbatchfetches", int ),
                    ( "exhaustivebatchfetches", int ),
                    ( "recordsfetched", int ),
                    ( "recordsreturned", int ),
                    ( "inserts", int ),
                    ( "updates", int ),
                    ( "deletions", int ),
                    ( "replaces", int ),
                    ( "recordssorted", int ),
                    ( "updatecount", int ),
                    ( "evicted", int ),
                    ( "locked" , int ),
                    ( "rejectedindexhits", int )
                   ]
    result_columns = [ name for name,_ in description[4:] ]

    def __init__(self,process, **entries):
        self.__dict__.update(entries)
        self.collection_interval = 0.0
        self.avgexectime = 0
        setattr(self,"db_name",process.db_name)
        setattr(self,"start_id",str(process.start_id))

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
        return str.join([self.__attr(field) for field in Statement.description])

# for each nuodb process
class Monitor:
    def __init__(self, nuodb_process, conn, relative, args):
        self._process = nuodb_process
        self._relative = relative
        if self._process.engine_type == "TE":
            server = conn.get_server(self._process.server_id)
            startid = self._process.start_id
            hostname = server.address[:-1]+"4"
            options = dict(schema="system"
                           , LBQuery=f"random(start_id({startid}))")
            self._connection = pynuodb.connect(database=self._process.db_name
                                               , host=hostname
                                               , user='dba'
                                               , password="dba"
                                               , options=options)
        else:
            #only execute queries where process is transaction engine
            self._connection = None
        self._last = None
        
    def __get_latest(self):
        results = {}
        try:
            cursor = self._connection.cursor()
            cursor.execute(sql)
            for row in cursor.fetchall():
                stmt = Statement(self._process,**dict(zip(Statement.result_columns,row)))
                results[stmt.id] = stmt
        finally:
            if cursor is not None:
                self._connection.commit()
            pass
        return results

    def _get_delta(self,previous,current):
        delta = copy.deepcopy(current)
        for name,coltype in Statement.description:
            if coltype == int:
                oldvalue = getattr(previous,name)
                newvalue = getattr(current,name)
                setattr(delta,name,newvalue-oldvalue)
        td = (current.snapshottime-previous.snapshottime).total_seconds()
        setattr(delta,"collection_interval",td)
        ne = (current.numexecutes - previous.numexecutes)
        if ne != 0:
            if ne < 0:
                aet = current.exectime / current.numexecutes
            else:
                aet = (current.exectime - previous.exectime) / ne
            setattr(delta,"avgexectime",round(aet))
        else:
            setattr(delta,"avgexectime",0)
        return delta

    def execute_query(self):
        if self._connection is not None:
            latest = self.__get_latest()
        else:
            latest = None
            
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

