import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from agents import RunContextWrapper
from agents.tool_context import ToolContext

from src.agent_tools import (
    check_return_eligibility_tool,
    get_order_tool,
    request_human_support_tool,
)
from src.context import ReturnContext, ReturnEvaluation
from src.storage import connect_database


class TestSupportCaseContext(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database_path = Path(directory.name) / "test.db"
        environment = patch.dict(
            os.environ,
            {
                "CIDERLANE_DATABASE_PATH": str(self.database_path),
                "CIDERLANE_REFERENCE_DATE": "2026-08-31",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.context = ReturnContext()

    async def invoke(self, context, arguments, tool=request_human_support_tool):
        raw_arguments = json.dumps(arguments)
        tool_context = ToolContext.from_agent_context(
            RunContextWrapper(context=context),
            tool_name=tool.name,
            tool_call_id="test-support-call",
            tool_arguments=raw_arguments,
        )
        return await tool.on_invoke_tool(tool_context, raw_arguments)

    async def evaluate(self, context, item_id, reason=None):
        verified = await self.invoke(
            context,
            {"order_id": "CL1002", "email": "maya@example.com"},
            tool=get_order_tool,
        )
        self.assertTrue(verified["verified"])
        return await self.invoke(
            context,
            {"item_id": item_id, "reason": reason},
            tool=check_return_eligibility_tool,
        )

    def test_new_context_gets_unique_conversation_id(self):
        other_context = ReturnContext()

        self.assertIsInstance(self.context.conversation_id, str)
        self.assertEqual(UUID(self.context.conversation_id).version, 4)
        self.assertNotEqual(self.context.conversation_id, other_context.conversation_id)

    async def test_support_fails_without_required_trusted_evaluation(self):
        result = await self.invoke(self.context, {
            "escalation_reason": "return_exception",
            "summary": "The swimsuit arrived damaged.",
        })

        self.assertEqual(result, {
            "status": "case_not_created",
            "error": "trusted_evaluation_required",
        })
        self.assertFalse(self.database_path.exists())

    async def test_mismatched_evaluation_order_is_rejected(self):
        self.context.verified_order_id = "CL1002"
        self.context.current_evaluation = ReturnEvaluation(
            order_id="CL1001",
            item_id="it00001",
            decision="blocked",
            policy_codes=("outside_return_window",),
        )

        result = await self.invoke(self.context, {
            "escalation_reason": "customer_remains_angry",
            "summary": "The customer remains angry.",
        })

        self.assertEqual(result, {
            "status": "case_not_created",
            "error": "trusted_evaluation_order_mismatch",
        })
        self.assertFalse(self.database_path.exists())

    async def test_non_final_sale_block_cannot_authorize_final_sale_dispute(self):
        await self.invoke(
            self.context,
            {"order_id": "CL1001", "email": "maya@example.com"},
            tool=get_order_tool,
        )
        result = await self.invoke(
            self.context,
            {"item_id": "it00001", "reason": None},
            tool=check_return_eligibility_tool,
        )
        self.assertEqual(result["policy_codes"], ["outside_return_window"])

        support = await self.invoke(self.context, {
            "escalation_reason": "final_sale_dispute",
            "summary": "The customer disputes the policy.",
        })

        self.assertEqual(support, {
            "status": "case_not_created",
            "error": "final_sale_policy_required",
        })

    async def test_swimwear_branch_cannot_authorize_final_sale_dispute(self):
        result = await self.evaluate(self.context, "it00002")
        self.assertEqual(result["policy_codes"], ["restricted_swimwear"])

        support = await self.invoke(self.context, {
            "escalation_reason": "final_sale_dispute",
            "summary": "The customer disputes the policy.",
        })

        self.assertEqual(support, {
            "status": "case_not_created",
            "error": "final_sale_policy_required",
        })

    async def test_valid_final_sale_escalation_uses_trusted_evaluation(self):
        await self.evaluate(self.context, "it00003")

        result = await self.invoke(self.context, {
            "escalation_reason": "final_sale_dispute",
            "summary": "The customer disputes the final-sale policy.",
        })

        self.assertEqual(result["status"], "case_created")
        with connect_database(self.database_path) as connection:
            saved_case = connection.execute(
                "SELECT * FROM support_cases WHERE case_id = ?",
                (result["case_id"],),
            ).fetchone()
        self.assertEqual(saved_case["conversation_id"], self.context.conversation_id)
        self.assertEqual(saved_case["order_id"], "CL1002")
        self.assertEqual(saved_case["item_id"], "it00003")
        self.assertEqual(saved_case["eligibility_decision"], "blocked")

    async def test_valid_exception_escalation_and_idempotency(self):
        await self.evaluate(self.context, "it00002", "damaged")
        arguments = {
            "escalation_reason": "return_exception",
            "summary": "The swimsuit arrived damaged.",
        }

        first = await self.invoke(self.context, arguments)
        second = await self.invoke(
            self.context,
            {**arguments, "summary": "The customer asked for an update."},
        )

        self.assertEqual(first["status"], "case_created")
        self.assertEqual(second["status"], "case_already_exists")
        self.assertEqual(second["case_id"], first["case_id"])

    async def test_angry_customer_requires_current_blocked_evaluation(self):
        arguments = {
            "escalation_reason": "customer_remains_angry",
            "summary": "The customer remains angry after the policy explanation.",
        }

        missing = await self.invoke(self.context, arguments)
        self.assertEqual(missing["error"], "trusted_evaluation_required")

        await self.evaluate(self.context, "it00004")
        standard = await self.invoke(self.context, arguments)
        self.assertEqual(standard["error"], "eligibility_decision_not_allowed")

        await self.evaluate(self.context, "it00002", "damaged")
        exception = await self.invoke(self.context, arguments)
        self.assertEqual(exception["error"], "eligibility_decision_not_allowed")

        await self.evaluate(self.context, "it00003")
        blocked = await self.invoke(self.context, arguments)
        self.assertEqual(blocked["status"], "case_created")

    async def test_identical_cases_are_independent_between_conversations(self):
        other_context = ReturnContext()
        await self.evaluate(self.context, "it00002", "damaged")
        await self.evaluate(other_context, "it00002", "damaged")
        arguments = {
            "escalation_reason": "return_exception",
            "summary": "The swimsuit arrived damaged.",
        }

        first = await self.invoke(self.context, arguments)
        second = await self.invoke(other_context, arguments)

        self.assertEqual(first["status"], "case_created")
        self.assertEqual(second["status"], "case_created")
        self.assertNotEqual(first["case_id"], second["case_id"])

    async def test_verification_failure_does_not_require_evaluation(self):
        result = await self.invoke(self.context, {
            "escalation_reason": "verification_failed",
            "summary": "The customer could not verify the order.",
        })

        self.assertEqual(result["status"], "case_created")
        with connect_database(self.database_path) as connection:
            saved_case = connection.execute(
                "SELECT * FROM support_cases WHERE case_id = ?",
                (result["case_id"],),
            ).fetchone()
        self.assertIsNone(saved_case["order_id"])
        self.assertIsNone(saved_case["item_id"])
        self.assertIsNone(saved_case["eligibility_decision"])

    async def test_verification_failure_is_rejected_for_verified_order(self):
        await self.evaluate(self.context, "it00004")

        result = await self.invoke(self.context, {
            "escalation_reason": "verification_failed",
            "summary": "The customer requested verification support.",
        })

        self.assertEqual(result, {
            "status": "case_not_created",
            "error": "verification_failure_not_allowed",
        })

    def test_tool_schema_exposes_only_reason_and_summary(self):
        schema = request_human_support_tool.params_json_schema

        self.assertEqual(
            set(schema["properties"]),
            {"escalation_reason", "summary"},
        )
        self.assertEqual(set(schema["required"]), {"escalation_reason", "summary"})
        self.assertFalse(schema["additionalProperties"])
        serialized_schema = json.dumps(schema)
        for hidden_field in (
            "conversation_id",
            "order_id",
            "item_id",
            "eligibility_decision",
        ):
            self.assertNotIn(hidden_field, serialized_schema)
