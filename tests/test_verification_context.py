import json
import os
import unittest
from unittest.mock import patch

from agents import RunContextWrapper
from agents.tool_context import ToolContext

from src.agent_tools import create_return_label_tool, get_order_tool
from src.context import ReturnContext
from src.tools.return_tools import create_return_label


class TestVerificationContext(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        date_patch = patch.dict(
            os.environ,
            {"CIDERLANE_REFERENCE_DATE": "2026-08-31"},
        )
        date_patch.start()
        self.addCleanup(date_patch.stop)
        self.ctx = RunContextWrapper(context=ReturnContext())

    async def invoke(self, tool, context_wrapper, arguments):
        raw_arguments = json.dumps(arguments)
        tool_context = ToolContext.from_agent_context(
            context_wrapper,
            tool_name=tool.name,
            tool_call_id="test-call",
            tool_arguments=raw_arguments,
        )
        return await tool.on_invoke_tool(tool_context, raw_arguments)

    async def verify(self, order_id="CL1002", email="maya@example.com"):
        return await self.invoke(
            get_order_tool,
            self.ctx,
            {"order_id": order_id, "email": email},
        )

    async def create_label(self, context_wrapper=None, order_id="CL1002"):
        return await self.invoke(
            create_return_label_tool,
            context_wrapper or self.ctx,
            {
                "order_id": order_id,
                "item_id": "it00004",
                "eligibility_decision": "standard_return",
            },
        )

    def assert_not_verified(self, result):
        self.assertEqual(result["status"], "label_not_created")
        self.assertEqual(result["error"], "order_not_verified")
        self.assertNotIn("label_url", result)

    async def test_unverified_conversation_is_rejected(self):
        self.assert_not_verified(await self.create_label())

    async def test_wrong_email_is_rejected(self):
        result = await self.verify(email="wrong@example.com")

        self.assertFalse(result["verified"])
        self.assert_not_verified(await self.create_label())

    async def test_verified_eligible_item_creates_label(self):
        result = await self.verify()

        self.assertTrue(result["verified"])
        self.assertEqual((await self.create_label())["status"], "label_created")

    async def test_verifying_one_order_does_not_authorize_another(self):
        result = await self.verify(order_id="CL1001")
        self.assertTrue(result["verified"])
        self.assert_not_verified(await self.create_label(order_id="CL1002"))

    async def test_verification_does_not_leak_to_new_conversation(self):
        await self.verify()
        other_context = RunContextWrapper(context=ReturnContext())

        self.assert_not_verified(await self.create_label(other_context))

    async def test_failed_reverification_clears_previous_verification(self):
        await self.verify()
        await self.verify(email="wrong@example.com")

        self.assertIsNone(self.ctx.context.verified_order_id)
        self.assert_not_verified(await self.create_label())

    async def test_same_conversation_keeps_verification(self):
        await self.verify()
        next_turn = RunContextWrapper(context=self.ctx.context)

        self.assertEqual(
            (await self.create_label(next_turn))["status"],
            "label_created",
        )

    def test_internal_label_function_fails_closed_without_context(self):
        self.assert_not_verified(
            create_return_label("CL1002", "it00004", "standard_return")
        )

    def test_model_cannot_supply_verification_state(self):
        self.assertEqual(
            set(get_order_tool.params_json_schema["properties"]),
            {"order_id", "email"},
        )
        self.assertEqual(
            set(create_return_label_tool.params_json_schema["properties"]),
            {"order_id", "item_id", "eligibility_decision"},
        )
