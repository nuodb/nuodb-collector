from .nuodb_test_class import *

class TestNuoDBInternal(NuoDBTelegrafTestClass):
    bucket_name = "nuodb_internal"
    host = 'localhost'

    def test_measurements_nuodb_msgtrace(self):
        client = getClient()
        assert_await(lambda: self.assertMeasurementPresent(client, self.bucket_name, "nuodb_msgtrace"), timeout=120, interval=10)

    def test_measurements_nuodb_synctrace(self):
        client = getClient()
        assert_await(lambda: self.assertMeasurementPresent(client, self.bucket_name, "nuodb_synctrace"), timeout=120, interval=10)

    def test_msgtrace_TRANSACTION_START(self):
        client = getClient()
        assert_await(lambda: self.assertMeasurementPresent(client, self.bucket_name, "nuodb_msgtrace"), timeout=120, interval=10)
        assert_await(lambda: self.assertTraceInMeasurement(client, self.bucket_name, "nuodb_msgtrace", "TRANSACTION_START"), timeout=120, interval=10)

    def test_msgtrace_TRANSACTION_STATE(self):
        client = getClient()
        assert_await(lambda: self.assertMeasurementPresent(client, self.bucket_name, "nuodb_msgtrace"), timeout=120, interval=10)
        assert_await(lambda: self.assertTraceInMeasurement(client, self.bucket_name, "nuodb_msgtrace", "TRANSACTION_STATE"), timeout=120, interval=10)

    def test_synctrace_not_empty(self):
        client = getClient()
        assert_await(lambda: self.assertMeasurementPresent(client, self.bucket_name, "nuodb_synctrace"), timeout=120, interval=10)
        assert_await(lambda: self.assertMeasurementCountGt0(client, self.bucket_name, "nuodb_synctrace"), timeout=120, interval=10)
