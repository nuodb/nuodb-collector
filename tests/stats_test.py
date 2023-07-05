from .nuodb_test_class import *


class TestNuoDB(NuoDBTelegrafTestClass):
    bucket_name = "nuodb"
    host = 'localhost'

    def test_measurements_ActualVersion(self):
        client = getClient()
        assert_await(lambda: self.assertMeasurementPresent(client, self.bucket_name, "ActualVersion"), timeout=120, interval=10)

    def test_measurements_ArchiveQueue(self):
        client = getClient()
        assert_await(lambda: self.assertMeasurementPresent(client, self.bucket_name, "ArchiveQueue"), timeout=120, interval=10)

    def test_measurements_ChairmanMigration(self):
        client = getClient()
        assert_await(lambda: self.assertMeasurementPresent(client,  self.bucket_name, "ChairmanMigration"), timeout=120, interval=10)

    def test_measurements_CurrentActiveTransactions(self):
        client = getClient()
        assert_await(lambda: self.assertMeasurementPresent(client,  self.bucket_name, "CurrentActiveTransactions"), timeout=120, interval=10)

    def test_measurements_CurrentCommittedTransactions(self):
        client = getClient()
        assert_await(lambda: self.assertMeasurementPresent(client,  self.bucket_name, "CurrentCommittedTransactions"), timeout=120, interval=10)

    def test_measurements_NodeId(self):
        client = getClient()
        assert_await(lambda: self.assertMeasurementPresent(client,  self.bucket_name, "NodeId"), timeout=120, interval=10)

    def test_measurements_CurrentActiveTransactions_count(self):
        client = getClient()
        assert_await(lambda: self.assertMeasurementCountGt0(client, self.bucket_name, "CurrentActiveTransactions"), timeout=120, interval=10)