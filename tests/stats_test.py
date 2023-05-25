from nuodb_test_class import *


class TestNuoDB(NuoDBTelegrafTestClass):
    database_name = "nuodb"
    host = 'localhost'

    def test_measurements_ActualVersion(self):
        client = InfluxDBClient(self.host, 8086)
        client.switch_database(self.database_name)
        assert_await(lambda: self.assertMeasurementPresent(client, "ActualVersion"), timeout=120, interval=10)

    def test_measurements_ArchiveQueue(self):
        client = InfluxDBClient(self.host, 8086)
        client.switch_database(self.database_name)
        assert_await(lambda: self.assertMeasurementPresent(client, "ArchiveQueue"), timeout=120, interval=10)

    def test_measurements_ChairmanMigration(self):
        client = InfluxDBClient(self.host, 8086)
        client.switch_database(self.database_name)
        assert_await(lambda: self.assertMeasurementPresent(client, "ChairmanMigration"), timeout=120, interval=10)

    def test_measurements_CurrentActiveTransactions(self):
        client = InfluxDBClient(self.host, 8086)
        client.switch_database(self.database_name)
        assert_await(lambda: self.assertMeasurementPresent(client, "CurrentActiveTransactions"), timeout=120, interval=10)

    def test_measurements_CurrentCommittedTransactions(self):
        client = InfluxDBClient(self.host, 8086)
        client.switch_database(self.database_name)
        assert_await(lambda: self.assertMeasurementPresent(client, "CurrentCommittedTransactions"), timeout=120, interval=10)

    def test_measurements_NodeId(self):
        client = InfluxDBClient(self.host, 8086)
        client.switch_database(self.database_name)
        assert_await(lambda: self.assertMeasurementPresent(client, "NodeId"), timeout=120, interval=10)

    def test_measurements_CurrentActiveTransactions_count(self):
        client = InfluxDBClient(self.host, 8086)
        client.switch_database(self.database_name)
        assert_await(lambda: self.assertMeasurementCountGt0(client, "CurrentActiveTransactions"), timeout=120, interval=10)