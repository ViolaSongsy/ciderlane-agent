import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.storage import connect_database, initialize_database


# Freeze the pre-conversation schema so migration tests exercise a real upgrade.
LEGACY_SCHEMA = """
CREATE TABLE support_cases (
    case_id TEXT PRIMARY KEY,
    escalation_reason TEXT NOT NULL CHECK (
        escalation_reason IN (
            'return_exception', 'final_sale_dispute',
            'customer_remains_angry', 'verification_failed'
        )
    ),
    order_id TEXT,
    item_id TEXT,
    eligibility_decision TEXT CHECK (
        eligibility_decision IS NULL
        OR eligibility_decision IN ('blocked', 'exception_flow')
    ),
    summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX unique_open_support_case
ON support_cases (
    escalation_reason,
    COALESCE(order_id, ''),
    COALESCE(item_id, ''),
    COALESCE(eligibility_decision, '')
)
WHERE status = 'open';
"""


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
                "conversation_id",
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

    def test_pending_label_requests_enforce_pair_and_key_uniqueness(self):
        initialize_database(self.database_path)
        insert = ('INSERT INTO return_label_requests (order_id, item_id, idempotency_key) '
                  'VALUES (?, ?, ?)')
        with connect_database(self.database_path) as connection:
            connection.execute(insert, ('CL1002', 'it00004', 'key-1'))
            for values in (
                ('CL1002', 'it00004', 'key-2'),  # Same item, different key.
                ('CL1002', 'it00005', 'key-1'),  # Different item, reused key.
                ('CL1002', 'it00005', None),
                (None, 'it00005', 'key-2'),
                ('CL1002', None, 'key-2'),
            ):
                with self.subTest(values=values), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(insert, values)
            connection.execute(insert, ('CL1002', 'it00005', 'key-2'))
            before = [dict(row) for row in connection.execute(
                'SELECT * FROM return_label_requests ORDER BY item_id')]
        initialize_database(self.database_path)
        with connect_database(self.database_path) as connection:
            after = [dict(row) for row in connection.execute(
                'SELECT * FROM return_label_requests ORDER BY item_id')]
        self.assertEqual(after, before)
        self.assertEqual(len(after), 2)

    def test_label_request_schema_upgrade_preserves_completed_returns(self):
        with connect_database(self.database_path) as connection:
            connection.execute('CREATE TABLE returns ('
                               'return_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, '
                               'item_id TEXT NOT NULL, label_url TEXT NOT NULL, '
                               'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, '
                               'UNIQUE(order_id, item_id))')
            connection.execute('INSERT INTO returns (return_id, order_id, item_id, label_url) '
                               "VALUES ('RET-OLD', 'CL1002', 'it00004', 'https://example.test/label')")
            before = dict(connection.execute('SELECT * FROM returns').fetchone())
            schema_before = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'returns'").fetchone()[0]
        initialize_database(self.database_path)
        with connect_database(self.database_path) as connection:
            after = dict(connection.execute('SELECT * FROM returns').fetchone())
            schema_after = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'returns'").fetchone()[0]
            pending = connection.execute('SELECT * FROM return_label_requests').fetchall()
        self.assertEqual(after, before)
        self.assertEqual(schema_after, schema_before)
        self.assertEqual(pending, [])

    def test_migration_preserves_cases_and_assigns_stable_legacy_identity(self):
        with connect_database(self.database_path) as connection:
            connection.executescript(LEGACY_SCHEMA)
            for status in ("open", "closed"):
                connection.execute(
                    """
                    INSERT INTO support_cases (
                        case_id, escalation_reason, summary, status
                    ) VALUES (?, 'verification_failed', 'Cannot verify the order.', ?)
                    """,
                    (f"CASE-{status}", status),
                )
            old_cases = [dict(row) for row in connection.execute(
                "SELECT * FROM support_cases ORDER BY case_id"
            )]

        initialize_database(self.database_path)
        initialize_database(self.database_path)

        with connect_database(self.database_path) as connection:
            migrated_cases = [dict(row) for row in connection.execute(
                "SELECT * FROM support_cases ORDER BY case_id"
            )]
            columns = connection.execute("PRAGMA table_info(support_cases)").fetchall()

        self.assertEqual(migrated_cases, [
            {**case, "conversation_id": "legacy"} for case in old_cases
        ])
        conversation_column = next(
            column for column in columns if column["name"] == "conversation_id"
        )
        self.assertEqual(conversation_column["notnull"], 1)

    def test_scoped_index_on_fresh_and_migrated_databases(self):
        insert = """
            INSERT INTO support_cases (
                case_id, conversation_id, escalation_reason,
                order_id, item_id, eligibility_decision, summary
            ) VALUES (?, ?, ?, ?, ?, ?, 'Review required.')
        """
        for migrate in (False, True):
            database_path = self.database_path.with_name(f"scoped-{migrate}.db")
            with self.subTest(migrate=migrate):
                if migrate:
                    with connect_database(database_path) as connection:
                        connection.executescript(LEGACY_SCHEMA)
                initialize_database(database_path)
                initialize_database(database_path)

                with connect_database(database_path) as connection:
                    for fields in (
                        ("return_exception", "CL1002", "it00002", "exception_flow"),
                        ("verification_failed", None, None, None),
                    ):
                        with self.subTest(fields=fields):
                            first_id = f"CASE-{fields[0]}-first"
                            connection.execute(insert, (first_id, "first", *fields))
                            connection.execute(
                                insert, (f"CASE-{fields[0]}-second", "second", *fields),
                            )
                            with self.assertRaises(sqlite3.IntegrityError):
                                connection.execute(
                                    insert, ("CASE-duplicate", "first", *fields),
                                )
                            with self.assertRaises(sqlite3.IntegrityError):
                                connection.execute(insert, ("CASE-null", None, *fields))
                            connection.execute(
                                "UPDATE support_cases SET status = 'closed' WHERE case_id = ?",
                                (first_id,),
                            )
                            connection.execute(
                                insert, (f"CASE-{fields[0]}-reopened", "first", *fields),
                            )
