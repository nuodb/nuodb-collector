import os
import time
import unittest
from influxdb_client import InfluxDBClient


def extract_names(l):
    names = []
    for o in l:
        names.append(o.name)

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
    
def getClient():
     influxToken = os.getenv("INFLUXDB_TOKEN", "admint0ken")
     org = os.getenv("INFLUXDB_ORG", "nuodb")
     client = InfluxDBClient(url='http://localhost:8086', token=influxToken, org=org)
     return client

def getMeasurenment(client, bucket):
    query = f"""import "influxdata/influxdb/schema"
    schema.measurements(bucket: "{bucket}")"""

    query_api = client.query_api()
    tables = query_api.query(query=query)

    # Flatten output tables into list of measurements
    measurements = [row.values["_value"] for table in tables for row in table]
    return measurements


class NuoDBTelegrafTestClass(unittest.TestCase):
    bucket_name = None

    @classmethod
    def isDatabaseRunning(cls, client, database):
        buckets_api = client.buckets_api()
        buckets = buckets_api.find_buckets().buckets
        databases = extract_names(buckets)
        
        if database not in databases:
            raise EnvironmentError("%s database not created" % database)

        return True

    @classmethod
    def setUpClass(cls):
        # ensure that the database exists
        client = getClient()
        assert_await(lambda: cls.isDatabaseRunning(client, cls.bucket_name), timeout=60)
    
    def assertMeasurementPresent(self, client, bucket, measurement):
        measurements = getMeasurenment(client, bucket)
        self.assertIn(measurement, measurements)

    def assertMeasurementCountGt0(self, client, bucket, measurement):
        query_api = client.query_api()
        query = f"""from(bucket: "{bucket}")
            |> range(start: -5m)  
            |> filter(fn: (r) => r["_measurement"] == "{measurement}")"""
        result = query_api.query(query)
        self.assertGreater(len(result),0)

    def assertTraceInMeasurement(self, client, bucket, measurement, trace):
        query_api = client.query_api()
        query = f"""from(bucket: "{bucket}")
            |> range(start: -5m)  
            |> filter(fn: (r) => r["_measurement"] == "{measurement}")
            |> filter(fn: (r) => r["msg_trace_metric"] == \"{trace}")"""
        result = query_api.query(query)
        self.assertGreater(len(result),0, f"Measurement {measurement} does not contain an trace {trace}")
