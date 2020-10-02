from nuodb_test_class import *


class TestNuoDBInternal(NuoDBTelegrafTestClass):
    database_name = "nuodb_internal"

    def test_measurements(self):
        client = InfluxDBClient('localhost', 8086)
        client.switch_database(self.database_name)
        measurements = extract_names(client.get_list_measurements())

        # verify a random set of stats
        self.assertIn("nuodb_msgtrace", measurements)
        self.assertIn("nuodb_synctrace", measurements)

    def test_msgtrace(self):
        client = InfluxDBClient('localhost', 8086)
        client.switch_database(self.database_name)

        rs = client.query("select * from nuodb_msgtrace")
        points = list(rs.get_points(measurement='nuodb_msgtrace'))
        traces = extract_traces(points)

        # check that a few msg_traces are being written
        self.assertIn("TABLE_INDEX_DELETED", traces)
        self.assertIn("TRANSACTION_START", traces)
        self.assertIn("TRANSACTION_STATE", traces)

    def test_synctrace(self):
        client = InfluxDBClient('localhost', 8086)
        client.switch_database(self.database_name)

        rs = client.query("select * from nuodb_msgtrace")
        points = list(rs.get_points(measurement='nuodb_msgtrace'))
        self.assertGreater(len(points), 0)
