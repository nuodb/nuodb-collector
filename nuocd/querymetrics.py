import pynuodb
import copy
import hashlib

sql = """
SELECT STATEMENT, COUNT, MINEXECTIME, MAXEXECTIME, AVGEXECTIME,
TOTALEXECTIME, MINCOMPILETIME, MAXCOMPILETIME, AVGCOMPILETIME, TOTALCOMPILETIME,
RECORDSFETCHEDATMINTIME, RECORDSFETCHEDATMAXTIME FROM SYSTEM.LOCALQUERYPERFORMANCEMETRICS
""".strip().replace("\n"," ")

def Counter(prev, value):
    if prev:
        diff = value - prev
        if diff < 0:
            # Counter reset
            return value
        return diff
    return value

def Gauge(_, value):
    return value

def String(_, value):
    s=repr(value)[1:-1].replace('"','\\"')
    return f'"{s}"'

class QueryMetric:
    description = [ ( "db_name", String ),
                    ( "start_id", String ),
                    ( "avgexectime", Gauge ),
                    ( "id" , String),
                    ( "statement" , String ),
                    ( "count", Counter ),
                    ( "minexectime", Gauge ),
                    ( "maxexectime", Gauge ),
                    ( "avgexectime", Gauge ),
                    ( "totalexectime", Counter ),
                    ( "mincompiletime", Gauge ),
                    ( "maxcompiletime", Gauge ),
                    ( "avgcompiletime", Gauge ),
                    ( "totalcompiletime", Counter ),
                    ( "recordsfetchedatmintime", Gauge ),
                    ( "recordsfetchedatmaxtime", Gauge ),
                   ]
    columns = [ name for name, _ in description[4:] ]

    def __init__(self, process, **entries):
        self.__dict__.update(entries)
        setattr(self, "id", self._generate_id())
        setattr(self, "db_name", process.db_name)
        setattr(self, "start_id", str(process.start_id))

    def _generate_id(self):
        return f"S{hashlib.sha1(self.statement.encode()).hexdigest()}"

    def __attr(self, field):
        name, _ = field
        value = self.__dict__.get(name, "")
        return f"{name}={value}"

    def __repr__(self):
        str = " "
        return str.join([self.__attr(field) for field in QueryMetric.description])

# for each nuodb process
class Monitor:
    def __init__(self, nuodb_process, conn, relative, args):
        self._process = nuodb_process
        if self._process.engine_type == "TE":
            server = conn.get_server(self._process.server_id)
            startid = self._process.start_id
            hostname = server.address[:-1]+"4"
            options = dict(schema="system", 
                           LBQuery=f"random(start_id({startid}))")
            self._connection = pynuodb.connect(database=self._process.db_name, 
                                               host=hostname, 
                                               user='dba',
                                               password="secret",
                                               options=options)
        else:
            # The process is not a TE
            self._connection = None
        self._last = None
        
    def _get_latest_metrics(self):
        results = {}
        try:
            cursor = self._connection.cursor()
            cursor.execute(sql)
            for row in cursor.fetchall():
                if sql not in row[0]:
                    stmt = QueryMetric(self._process, **dict(zip(QueryMetric.columns, row)))
                    results[stmt.id] = stmt
        finally:
            if cursor is not None:
                self._connection.commit()
        return results

    def _normalize_metric(self, previous, current):
        norm = copy.deepcopy(current)
        for name, coltype in QueryMetric.description:
            oldvalue = None
            if previous:
                oldvalue = getattr(previous, name)
            newvalue = getattr(current, name)
            setattr(norm, name, coltype(oldvalue, newvalue))
        return norm

    def execute_query(self):
        latest = None
        if self._connection is not None:
            latest = self._get_latest_metrics()
        
        if self._last is not None:
            results=[]
            for id, current in latest.items():
                previous = None
                if id in self._last:
                    previous = self._last[id]
                norm = self._normalize_metric(previous, current)
                if norm.count > 0:
                    results.append(norm)
            for row in sorted(results, reverse=True, key=lambda x: x.totalexectime):
                print(row)
        self._last = latest
