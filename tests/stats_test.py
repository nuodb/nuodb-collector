from nuodb_test_class import *


class TestNuoDB(NuoDBTelegrafTestClass):
    database_name = "nuodb"

    def test_measurements(self):
        client = InfluxDBClient('localhost', 8086)
        client.switch_database(self.database_name)
        measurements = extract_names(client.get_list_measurements())

        # verify a random set of stats
        self.assertIn("ActualVersion", measurements)
        self.assertIn("ArchiveQueue", measurements)
        self.assertIn("ChairmanMigration", measurements)
        self.assertIn("CurrentActiveTransactions", measurements)
        self.assertIn("CurrentCommittedTransactions", measurements)
        self.assertIn("NodeId", measurements)

        # verify that data is being written
        rs = client.query("select count(*) from CurrentActiveTransactions")
        points = list(rs.get_points(measurement='CurrentActiveTransactions'))
        self.assertGreater(points[0]["count_raw"], 0)

