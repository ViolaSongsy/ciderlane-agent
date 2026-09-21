import os
import sqlite3
from pathlib import Path


DATABASE_PATH = Path(__file__).parents[1] / "data" / "ciderlane.db"
DATABASE_PATH_ENV_VAR = "CIDERLANE_DATABASE_PATH"

CREATE_SUPPORT_CASES_TABLE = """
CREATE TABLE IF NOT EXISTS support_cases (
    case_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    escalation_reason TEXT NOT NULL CHECK (
        escalation_reason IN (
            'return_exception',
            'final_sale_dispute',
            'customer_remains_angry',
            'verification_failed'
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
)
"""

CREATE_OPEN_CASE_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS unique_open_support_case
ON support_cases (
    conversation_id,
    escalation_reason,
    COALESCE(order_id, ''),
    COALESCE(item_id, ''),
    COALESCE(eligibility_decision, '')
)
WHERE status = 'open'
"""


def resolve_database_path(database_path=None):
    if database_path is not None:
        return Path(database_path)

    configured_path = os.getenv(DATABASE_PATH_ENV_VAR)
    if configured_path:
        return Path(configured_path)

    return DATABASE_PATH


def connect_database(database_path=None):
    path = resolve_database_path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database(database_path=None):
    with connect_database(database_path) as connection:
        # Keep the schema check and migration atomic, including concurrent startup.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(CREATE_SUPPORT_CASES_TABLE)
        columns = connection.execute("PRAGMA table_info(support_cases)").fetchall()
        if "conversation_id" not in {column["name"] for column in columns}:
            # Historical cases have no recoverable conversation identity.
            connection.execute(
                "ALTER TABLE support_cases "
                "ADD COLUMN conversation_id TEXT NOT NULL DEFAULT 'legacy'"
            )
            connection.execute("DROP INDEX IF EXISTS unique_open_support_case")
        connection.execute(CREATE_OPEN_CASE_UNIQUE_INDEX)
