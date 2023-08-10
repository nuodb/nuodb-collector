import pynuodb
import pprint
import copy, sys
import datetime

sql = """
select CURRENT_TIMESTAMP as SNAPSHOTTIME,"GROUP",SUM(COUNT) as COUNT,SUM(DURATION) as DURATION 
  from system.localclientmessages group by "GROUP"
"""
sql = sql.replace("\n"," ")

class ClientMessage:
    description = [ ( "db_name", str ),
                    ( "start_id", str ),
                    ( "snapshottime", datetime.datetime) ,
                    ( "group",str),
                    ( "count",int), 
                    ( "duration", int ),
                    ( "count_rate", int ),
                    ( "duration_rate", int ),
                   ]
    # field names returned from query (see sql above)
    result_columns = [ "snapshottime", "group", "count", "duration" ]

    def __init__(self,process,**entries):
        self.__dict__.update(entries)
        setattr(self,"db_name",process.db_name)
        setattr(self,"start_id",str(process.start_id))
        setattr(self,"duration_rate",getattr(self,"duration_rate",0))
        setattr(self,"count_rate",getattr(self,"count_rate",0))
        
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
        return str.join([self.__attr(field) for field in ClientMessage.description])

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
        cursor = None
        try:
            cursor = self._connection.cursor()
            cursor.execute(sql)
            for row in cursor.fetchall():
                stmt = ClientMessage(self._process,**dict(zip(ClientMessage.result_columns,row)))
                results[stmt.group] = stmt
        finally:
            if cursor is not None:
                self._connection.commit()
            pass
        return results

    def _get_delta(self,previous,current):
        delta = copy.deepcopy(current)
        for name,coltype in ClientMessage.description:
            if coltype == int:
                oldvalue = getattr(previous,name)
                newvalue = getattr(current,name)
                setattr(delta,name,newvalue-oldvalue)
        td = (current.snapshottime-previous.snapshottime).total_seconds()
        setattr(delta,"duration_rate",round(getattr(delta,"duration",0)/td))
        setattr(delta,"count_rate",round(getattr(delta,"count",0)/td))
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
                    delta = self._get_delta(previous,current)
                    results.append(delta)
                else:
                    results.append(current)
            for row in sorted(results,reverse=True,key=lambda x: x.duration):
                print(row)
        self._last = latest

