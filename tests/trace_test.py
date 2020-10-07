from nuodb_test_class import *


class TestNuoDBInternal(NuoDBTelegrafTestClass):
    database_name = "nuodb_internal"

    def test_measurements_nuodb_msgtrace(self):
        client = InfluxDBClient('localhost', 8086)
        client.switch_database(self.database_name)
        assert_await(lambda: self.assertMeasurementPresent(client, "nuodb_msgtrace"), timeout=120, interval=10)

    def test_measurements_nuodb_synctrace(self):
        client = InfluxDBClient('localhost', 8086)
        client.switch_database(self.database_name)
        assert_await(lambda: self.assertMeasurementPresent(client, "nuodb_synctrace"), timeout=120, interval=10)

    def test_msgtrace_TRANSACTION_START(self):
        client = InfluxDBClient('localhost', 8086)
        client.switch_database(self.database_name)

        assert_await(lambda: self.assertMeasurementPresent(client, "nuodb_msgtrace"), timeout=120, interval=10)

        assert_await(lambda: self.assertTraceInMeasurement(client, "nuodb_msgtrace", "TRANSACTION_START"), timeout=120, interval=10)

    def test_msgtrace_TRANSACTION_STATE(self):
        client = InfluxDBClient('localhost', 8086)
        client.switch_database(self.database_name)

        assert_await(lambda: self.assertMeasurementPresent(client, "nuodb_msgtrace"), timeout=120, interval=10)

        assert_await(lambda: self.assertTraceInMeasurement(client, "nuodb_msgtrace", "TRANSACTION_STATE"), timeout=120, interval=10)

    def test_synctrace_not_empty(self):
        client = InfluxDBClient('localhost', 8086)
        client.switch_database(self.database_name)
        assert_await(lambda: self.assertMeasurementPresent(client, "nuodb_synctrace"), timeout=120, interval=10)
        assert_await(lambda: self.assertMeasurementNotEmpty(client, "nuodb_synctrace"), timeout=120, interval=10)
