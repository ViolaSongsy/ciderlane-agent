import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from agents import Model, ModelResponse, Usage, trace
from fastapi.testclient import TestClient
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from src import api
from src.storage import connect_database, initialize_database


VERIFY = ("verify_order", {"order_id": "CL1002", "email": "maya@example.com"})
START_RETURN = ("start_return", {"item_id": "it00004", "reason": None})


class ScriptedModel(Model):
    """Exercise real SDK runs and tools without calling a model provider."""

    def __init__(self, steps):
        self.steps = iter(steps)
        self.inputs = []

    async def get_response(self, *args, **kwargs):
        self.inputs.append(list(kwargs["input"]))
        step = next(self.steps, "Done.")
        if isinstance(step, Exception):
            raise step
        if isinstance(step, str):
            output = [ResponseOutputMessage(
                id=f"message-{len(self.inputs)}", role="assistant", status="completed",
                type="message", content=[ResponseOutputText(
                    annotations=[], text=step, type="output_text")],
            )]
        else:
            name, arguments = step
            output = [ResponseFunctionToolCall(
                arguments=json.dumps(arguments), call_id=f"call-{len(self.inputs)}",
                name=name, type="function_call",
            )]
        return ModelResponse(output=output, usage=Usage(), response_id=None)

    async def stream_response(self, *args, **kwargs):
        if False:
            yield


class TestAPI(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "CIDERLANE_DATABASE_PATH": str(Path(directory) / "http.db"),
            "CIDERLANE_REFERENCE_DATE": "2026-08-31",
        }))
        self.enterContext(trace("HTTP tests", disabled=True))
        self.enterContext(patch("src.api.load_dotenv"))
        initialize_database()
        self.provider_factory = self.enterContext(patch("src.tools.return_tools.get_return_label_provider"))
        self.provider = self.provider_factory.return_value
        self.provider.create_label.return_value = "https://carrier.example.test/label.pdf"
        api.sessions.clear()
        self.addCleanup(api.sessions.clear)
        self.client = self.enterContext(TestClient(api.app, raise_server_exceptions=False))
        self.session_id = self.new_session()
        self.session = api.sessions[self.session_id]

    def new_session(self):
        response = self.client.post("/sessions")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(response.json()), {"session_id"})
        return response.json()["session_id"]

    def use_model(self, steps):
        self.model = ScriptedModel(steps)
        self.enterContext(patch("src.api.ciderlane_agent", api.ciderlane_agent.clone(model=self.model)))

    def chat(self, session_id, message):
        response = self.client.post(f"/sessions/{session_id}/chat", json={"message": message})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def begin_return(self, steps=None):
        self.use_model(steps if steps is not None else [VERIFY, START_RETURN])
        pause = self.chat(self.session_id,
                          "Order CL1002, email maya@example.com. Please return the white short dress.")
        self.assertEqual(pause["status"], "approval_required")
        return pause

    def decide(self, pause, decision="approve"):
        return self.client.post(f"/sessions/{self.session_id}/approval", json={
            "approval_id": pause["approval_id"], "decision": decision,
        })

    def rows(self, table="returns"):
        with closing(connect_database()) as connection:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]

    def test_sessions_isolate_context_and_conversation(self):
        other_id = self.new_session()
        other = api.sessions[other_id]
        self.use_model([
            VERIFY, ("check_return_eligibility", {"item_id": "it00004", "reason": None}),
            "Eligible.", "Hello.",
        ])
        self.chat(self.session_id,
                  "Can I return the white short dress? Order CL1002, email maya@example.com.")
        self.assertEqual(self.chat(other_id, "Hello")["response"], "Hello.")
        self.assertEqual(self.session.context.verified_order_id, "CL1002")
        self.assertIsNotNone(self.session.context.current_evaluation)
        self.assertIsNone(other.context.verified_order_id)
        self.assertIsNone(other.context.current_evaluation)
        self.assertIsNot(self.session.context, other.context)
        self.assertIsNot(self.session.conversation, other.conversation)
        self.assertEqual(self.model.inputs[-1], [{"role": "user", "content": "Hello"}])
        self.assertEqual(len({self.session_id, other_id,
                              self.session.context.conversation_id, other.context.conversation_id}), 4)

    def test_multiple_requests_reuse_context_and_complete_sdk_history(self):
        self.use_model([VERIFY, "Verified.", "Remembered."])
        context = self.session.context
        conversation_id = context.conversation_id
        self.chat(self.session_id, "Order CL1002, email maya@example.com.")
        history = list(self.session.conversation)
        self.assertTrue(any(item.get("type") == "function_call_output" for item in history))
        response = self.chat(self.session_id, "What order did I verify?")
        self.assertEqual(response, {"status": "completed", "response": "Remembered."})
        self.assertEqual(self.model.inputs[-1], history + [
            {"role": "user", "content": "What order did I verify?"},
        ])
        self.assertIs(self.session.context, context)
        self.assertEqual(context.conversation_id, conversation_id)
        self.assertEqual(context.verified_order_id, "CL1002")
        self.assertEqual(len(self.session.conversation), len(history) + 2)

    def test_approval_resumes_across_requests_and_saves_completed_history(self):
        context = self.session.context
        conversation_id = context.conversation_id
        pause = self.begin_return()
        self.assertIn("white short dress (item ID it00004)", pause["approvals"][0])
        self.assertIsNotNone(self.session.paused_state)
        self.assertEqual(self.session.conversation, [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows("return_label_requests"), [])
        self.provider_factory.assert_not_called()
        self.assertEqual(self.client.post(f"/sessions/{self.session_id}/chat",
                                         json={"message": "Another message"}).status_code, 409)
        response = self.decide(pause)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "completed", "response": "Done."})
        self.assertIsNone(self.session.paused_state)
        self.assertIsNone(self.session.approval_id)
        self.assertIs(self.session.context, context)
        self.assertEqual(context.conversation_id, conversation_id)
        self.assertEqual([row["item_id"] for row in self.rows()], ["it00004"])
        self.chat(self.session_id, "Thanks")
        self.assertTrue(any(item.get("type") == "function_call_output" for item in self.model.inputs[-1]))

    def test_rejection_across_requests_does_not_execute_the_return_tool(self):
        pause = self.begin_return()
        with patch("src.agent_tools.start_return") as start:
            response = self.decide(pause, "reject")
        self.assertEqual(response.status_code, 200, response.text)
        start.assert_not_called()
        self.provider_factory.assert_not_called()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows("return_label_requests"), [])
        self.assertIsNone(self.session.paused_state)
        self.assertTrue(any("The customer declined this return" in str(item)
                            for item in self.session.conversation))

    def test_retrying_approval_does_not_duplicate_business_side_effects(self):
        pause = self.begin_return()
        self.assertEqual(self.decide(pause).status_code, 200)
        self.assertEqual(self.decide(pause).status_code, 409)
        self.assertEqual(len(self.rows()), 1)
        self.provider.create_label.assert_called_once()

    def test_stale_approval_cannot_approve_a_later_return(self):
        first = self.begin_return([
            VERIFY, START_RETURN, ("start_return", {"item_id": "it00005", "reason": None}),
        ])
        response = self.decide(first)
        self.assertEqual(response.status_code, 200, response.text)
        second = response.json()
        self.assertEqual(second["status"], "approval_required")
        self.assertNotEqual(first["approval_id"], second["approval_id"])
        self.assertEqual(self.decide(first).status_code, 409)
        self.assertEqual([row["item_id"] for row in self.rows()], ["it00004"])
        self.assertEqual(self.decide(second, "reject").status_code, 200)
        self.provider.create_label.assert_called_once()

    def test_retry_after_resume_failure_does_not_reexecute_a_completed_return(self):
        pause = self.begin_return([VERIFY, START_RETURN, RuntimeError("Injected model failure")])
        self.assertEqual(self.decide(pause).status_code, 500)
        self.assertEqual(self.decide(pause).status_code, 409)
        self.assertIsNone(self.session.paused_state)
        self.assertEqual(self.session.conversation, [])
        self.assertEqual(len(self.rows()), 1)
        self.provider.create_label.assert_called_once()
