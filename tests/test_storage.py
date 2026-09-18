import tempfile
import unittest
from pathlib import Path

from src.storage import connect_database, initialize_database


class TestInitializeDatabase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "test.db"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_creates_support_cases_table(self):
        initialize_database(self.database_path)

        with connect_database(self.database_path) as connection:
            columns = connection.execute(
                "PRAGMA table_info(support_cases)"
            ).fetchall()

        column_names = [column["name"] for column in columns]
        self.assertEqual(
            column_names,
            [
                "case_id",
                "escalation_reason",
                "order_id",
                "item_id",
                "eligibility_decision",
                "summary",
                "status",
                "created_at",
            ],
        )

    def test_initialization_can_run_more_than_once(self):
        initialize_database(self.database_path)
        initialize_database(self.database_path)

        self.assertTrue(self.database_path.exists())
