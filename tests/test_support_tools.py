import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.context import ReturnContext
from src.storage import connect_database
from src.tools.support_tools import create_support_case


class TestCreateSupportCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "test.db"
        self.conversation_id = ReturnContext().conversation_id

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_return_exception_creates_persistent_case(self):
        result = create_support_case(
            conversation_id=self.conversation_id,
            escalation_reason="return_exception",
            summary="The verified swimsuit arrived damaged.",
            order_id="CL1002",
            item_id="it00002",
            eligibility_decision="exception_flow",
            database_path=self.database_path,
        )

        self.assertEqual(result["status"], "case_created")
        self.assertTrue(result["case_id"].startswith("CASE-"))

        with connect_database(self.database_path) as connection:
            saved_case = connection.execute(
                "SELECT * FROM support_cases WHERE case_id = ?",
                (result["case_id"],),
            ).fetchone()

        self.assertIsNotNone(saved_case)
        self.assertEqual(saved_case["conversation_id"], self.conversation_id)
        self.assertEqual(saved_case["order_id"], "CL1002")
        self.assertEqual(saved_case["item_id"], "it00002")
        self.assertEqual(saved_case["status"], "open")

    def test_duplicate_open_case_returns_existing_case(self):
        first_result = create_support_case(
            conversation_id=self.conversation_id,
            escalation_reason="return_exception",
            summary="The swimsuit arrived damaged.",
            order_id="CL1002",
            item_id="it00002",
            eligibility_decision="exception_flow",
            database_path=self.database_path,
        )
        second_result = create_support_case(
            conversation_id=self.conversation_id,
            escalation_reason="return_exception",
            summary="The customer asked about the damaged swimsuit again.",
            order_id="CL1002",
            item_id="it00002",
            eligibility_decision="exception_flow",
            database_path=self.database_path,
        )

        self.assertEqual(second_result["status"], "case_already_exists")
        self.assertEqual(second_result["case_id"], first_result["case_id"])

        with connect_database(self.database_path) as connection:
            case_count = connection.execute(
                "SELECT COUNT(*) FROM support_cases"
            ).fetchone()[0]

        self.assertEqual(case_count, 1)

    def test_insert_conflict_recovers_case_from_same_conversation(self):
        arguments = {
            "escalation_reason": "verification_failed",
            "summary": "The order could not be verified.",
            "database_path": self.database_path,
        }
        other_case = create_support_case(
            conversation_id=ReturnContext().conversation_id, **arguments,
        )
        expected_case = create_support_case(
            conversation_id=self.conversation_id, **arguments,
        )

        with connect_database(self.database_path) as connection:
            # Simulate a competing insert after the initial lookup missed the case.
            missing_case = Mock()
            missing_case.fetchone.return_value = None
            with patch("src.tools.support_tools.connect_database") as database:
                proxy = database.return_value.__enter__.return_value

                def execute(sql, parameters):
                    if proxy.execute.call_count == 1:
                        return missing_case
                    return connection.execute(sql, parameters)

                proxy.execute.side_effect = execute
                result = create_support_case(
                    conversation_id=self.conversation_id, **arguments,
                )

        self.assertEqual(result["status"], "case_already_exists")
        self.assertEqual(result["case_id"], expected_case["case_id"])
        self.assertNotEqual(result["case_id"], other_case["case_id"])

    def test_final_sale_dispute_requires_blocked_decision(self):
        result = create_support_case(
            conversation_id=self.conversation_id,
            escalation_reason="final_sale_dispute",
            summary="The customer disputes the final-sale policy.",
            order_id="CL1002",
            item_id="it00003",
            eligibility_decision="exception_flow",
            database_path=self.database_path,
        )

        self.assertEqual(result["status"], "case_not_created")
        self.assertEqual(result["error"], "eligibility_decision_not_allowed")
        self.assertFalse(self.database_path.exists())

    def test_final_sale_dispute_creates_case_after_blocked_decision(self):
        result = create_support_case(
            conversation_id=self.conversation_id,
            escalation_reason="final_sale_dispute",
            summary="The customer disputes the final-sale policy.",
            order_id="CL1002",
            item_id="it00003",
            eligibility_decision="blocked",
            database_path=self.database_path,
        )

        self.assertEqual(result["status"], "case_created")

    def test_verification_failure_rejects_item_details(self):
        result = create_support_case(
            conversation_id=self.conversation_id,
            escalation_reason="verification_failed",
            summary="The order could not be verified.",
            order_id="CL9999",
            item_id="it00001",
            database_path=self.database_path,
        )

        self.assertEqual(result["status"], "case_not_created")
        self.assertEqual(result["error"], "unverified_order_details_not_allowed")

    def test_verification_failure_creates_case_without_order_details(self):
        result = create_support_case(
            conversation_id=self.conversation_id,
            escalation_reason="verification_failed",
            summary="The customer confirmed the supplied details, but verification failed.",
            order_id="CL9999",
            database_path=self.database_path,
        )

        self.assertEqual(result["status"], "case_created")
        self.assertEqual(result["escalation_reason"], "verification_failed")

    def test_empty_summary_is_rejected(self):
        result = create_support_case(
            conversation_id=self.conversation_id,
            escalation_reason="return_exception",
            summary="   ",
            order_id="CL1002",
            item_id="it00002",
            eligibility_decision="exception_flow",
            database_path=self.database_path,
        )

        self.assertEqual(result["status"], "case_not_created")
        self.assertEqual(result["error"], "summary_required")
