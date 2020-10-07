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


def assert_await(fxn, timeout=120, interval=10):
    last_assert = None

    timeout_start = time.time()

    while time.time() < timeout_start + timeout:
        try:
            fxn()
        except AssertionError as e:
            last_assert = e
            time.sleep(interval)
            continue
        return

    if last_assert:
        raise last_assert


class NuoDBTelegrafTestClass(unittest.TestCase):
    database_name = None

    @classmethod
    def isDatabaseRunning(cls, client, database):
        databases = extract_names(client.get_list_database())

        if database not in databases:
            raise EnvironmentError("%s database not created" % database)

        return True

    @classmethod
    def setUpClass(cls):
        # ensure that the database exists
        client = InfluxDBClient('localhost', 8086)
        assert_await(lambda: cls.isDatabaseRunning(client, cls.database_name), timeout=60)

    def assertMeasurementPresent(self, client, measurement):
        measurements = extract_names(client.get_list_measurements())

        # verify a random set of stats
        self.assertIn(measurement, measurements)

    def assertMeasurementCountGt0(self, client, measurement):
        rs = client.query('select count(*) from %s' % measurement)
        points = list(rs.get_points(measurement=measurement))
        self.assertGreater(points[0]["count_raw"], 0)

    def assertTraceInMeasurement(self, client, measurement, trace):
        rs = client.query("select * from %s" % measurement)
        points = list(rs.get_points(measurement=measurement))
        traces = extract_traces(points)

        self.assertIn(trace, traces)

    def assertMeasurementNotEmpty(self, client, measurement):
        rs = client.query("select * from %s" % measurement)
        points = list(rs.get_points(measurement=measurement))
        self.assertGreater(len(points), 0, "Measurement %s does not contain any points" % measurement)