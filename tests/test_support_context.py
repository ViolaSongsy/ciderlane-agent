import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from agents import RunContextWrapper
from agents.tool_context import ToolContext

from src.agent_tools import create_support_case_tool, get_order_tool
from src.context import ReturnContext
from src.storage import connect_database


class TestSupportCaseContext(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database_path = Path(directory.name) / "test.db"
        environment = patch.dict(
            os.environ, {"CIDERLANE_DATABASE_PATH": str(self.database_path)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.context = ReturnContext()
        self.arguments = {
            "escalation_reason": "return_exception",
            "summary": "The swimsuit arrived damaged.",
            "order_id": "CL1002",
            "item_id": "it00002",
            "eligibility_decision": "exception_flow",
        }

    async def invoke(self, context, arguments, tool=create_support_case_tool):
        raw_arguments = json.dumps(arguments)
        tool_context = ToolContext.from_agent_context(
            RunContextWrapper(context=context),
            tool_name=tool.name,
            tool_call_id="test-support-call",
            tool_arguments=raw_arguments,
        )
        return await tool.on_invoke_tool(tool_context, raw_arguments)

    def test_new_context_gets_unique_conversation_id(self):
        other_context = ReturnContext()

        self.assertIsInstance(self.context.conversation_id, str)
        self.assertEqual(UUID(self.context.conversation_id).version, 4)
        self.assertNotEqual(self.context.conversation_id, other_context.conversation_id)

    async def test_same_context_keeps_case_and_identity_across_turns(self):
        conversation_id = self.context.conversation_id
        first = await self.invoke(self.context, self.arguments)
        # Both successful verification and failed reverification preserve identity.
        for email in ("maya@example.com", "wrong@example.com"):
            await self.invoke(
                self.context,
                {"order_id": "CL1002", "email": email},
                tool=get_order_tool,
            )
            result = await self.invoke(
                self.context,
                {**self.arguments, "summary": "The customer asked for an update."},
            )
            self.assertEqual(self.context.conversation_id, conversation_id)
            self.assertEqual(result["status"], "case_already_exists")
            self.assertEqual(result["case_id"], first["case_id"])

    async def test_identical_cases_are_independent_between_conversations(self):
        for arguments in (
            self.arguments,
            {
                "escalation_reason": "verification_failed",
                "summary": "The customer cannot provide verification details.",
                "order_id": None,
                "item_id": None,
                "eligibility_decision": None,
            },
        ):
            with self.subTest(reason=arguments["escalation_reason"]):
                other_context = ReturnContext()
                first = await self.invoke(self.context, arguments)
                second = await self.invoke(other_context, arguments)

                self.assertEqual(first["status"], "case_created")
                self.assertEqual(second["status"], "case_created")
                self.assertNotEqual(first["case_id"], second["case_id"])
                for context, expected in ((self.context, first), (other_context, second)):
                    repeated = await self.invoke(context, arguments)
                    self.assertEqual(repeated["status"], "case_already_exists")
                    self.assertEqual(repeated["case_id"], expected["case_id"])

    async def test_model_cannot_override_conversation_id(self):
        first = await self.invoke(self.context, self.arguments)
        other_context = ReturnContext()
        second = await self.invoke(other_context, {
            **self.arguments,
            "conversation_id": self.context.conversation_id,
            "ctx": {"context": {"conversation_id": self.context.conversation_id}},
        })

        self.assertEqual(second["status"], "case_created")
        self.assertNotEqual(first["case_id"], second["case_id"])
        self.assertNotIn("conversation_id", second)
        with connect_database(self.database_path) as connection:
            saved_case = connection.execute(
                "SELECT conversation_id FROM support_cases WHERE case_id = ?",
                (second["case_id"],),
            ).fetchone()
        self.assertEqual(saved_case["conversation_id"], other_context.conversation_id)

    def test_tool_schema_does_not_expose_conversation_id(self):
        schema = create_support_case_tool.params_json_schema

        self.assertEqual(set(schema["properties"]), {
            "escalation_reason", "summary", "order_id", "item_id", "eligibility_decision",
        })
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("conversation_id", json.dumps(schema))
