import time
import unittest
from influxdb import InfluxDBClient


def extract_names(l):
    names = []
    for o in l:
        names.append(o["name"])

    return names


def extract_traces(points):
    metrics = set()
    for point in points:
        metrics.add(point["msg_trace_metric"])
    return metrics

class NuoDBTelegrafTestClass(unittest.TestCase):
    database_name = None

    @classmethod
    def setUpClass(cls):
        # ensure that the database exists
        client = InfluxDBClient('localhost', 8086)

        timeout = 60  # [seconds]
        timeout_start = time.time()

        while time.time() < timeout_start + timeout:
            databases = extract_names(client.get_list_database())
            if cls.database_name in databases:
                print "'%s' is up and running" % cls.database_name
                return
            time.sleep(1)

        raise EnvironmentError("%s database not created" % cls.database_name)