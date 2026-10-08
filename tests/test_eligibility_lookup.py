import json
import os
import tempfile
import unittest
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from agents import RunContextWrapper
from agents.tool_context import ToolContext

from src.agent_tools import check_return_eligibility_tool, verify_order_tool
from src.context import ReturnContext, ReturnEvaluation
from src.tools.order_tools import load_orders
from src.tools.return_tools import check_return_eligibility
from src.storage import connect_database, initialize_database, reserve_return_label_request
from src.tools.return_tools import start_return


class TestTrustedEligibilityLookup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        date_patch = patch.dict(
            os.environ,
            {"CIDERLANE_REFERENCE_DATE": "2026-08-31",
             "CIDERLANE_DATABASE_PATH": str(Path(directory) / "eligibility.db")},
        )
        date_patch.start()
        self.addCleanup(date_patch.stop)
        self.ctx = RunContextWrapper(context=ReturnContext())

    async def invoke(self, tool, arguments):
        raw_arguments = json.dumps(arguments)
        tool_context = ToolContext.from_agent_context(
            self.ctx,
            tool_name=tool.name,
            tool_call_id="test-eligibility-call",
            tool_arguments=raw_arguments,
        )
        return await tool.on_invoke_tool(tool_context, raw_arguments)

    async def verify(self, order_id="CL1002", email="maya@example.com"):
        return await self.invoke(
            verify_order_tool,
            {"order_id": order_id, "email": email},
        )

    def assert_not_checked(self, result, error):
        self.assertEqual(
            result,
            {"status": "eligibility_not_checked", "error": error},
        )

    def test_internal_lookup_fails_closed_without_verification(self):
        for context in (None, ReturnContext(), ReturnContext(verified_order_id="")):
            with self.subTest(context=context):
                with patch("src.tools.return_tools.load_orders") as storage:
                    result = check_return_eligibility("it00004", context=context)

                self.assert_not_checked(result, "order_not_verified")
                storage.assert_not_called()

    async def test_unverified_tool_call_cannot_forge_verification(self):
        with patch("src.tools.return_tools.load_orders") as storage:
            result = await self.invoke(
                check_return_eligibility_tool,
                {
                    "item_id": "it00004",
                    "reason": None,
                    "order_id": "CL1002",
                    "verified_order_id": "CL1002",
                    "context": {"verified_order_id": "CL1002"},
                    "ctx": {"context": {"verified_order_id": "CL1002"}},
                    "delivered_date": "2026-08-30",
                    "category": "dress",
                    "final_sale": False,
                },
            )

        self.assert_not_checked(result, "order_not_verified")
        self.assertIsNone(self.ctx.context.verified_order_id)
        storage.assert_not_called()

    async def test_verified_eligible_item_uses_standard_return(self):
        self.assertTrue((await self.verify())["verified"])
        for arguments in (
            {"item_id": "it00004"},
            *({"item_id": "it00004", "reason": reason}
              for reason in (None, "damaged", "wrong_item", "other")),
        ):
            with self.subTest(arguments=arguments):
                result = await self.invoke(check_return_eligibility_tool, arguments)
                self.assertEqual(result["decision"], "standard_return")

        self.assertEqual(
            self.ctx.context.current_evaluation,
            ReturnEvaluation(
                order_id="CL1002",
                item_id="it00004",
                decision="standard_return",
                policy_codes=(),
            ),
        )

    async def test_later_successful_evaluation_replaces_previous_evaluation(self):
        await self.verify()
        await self.invoke(
            check_return_eligibility_tool,
            {"item_id": "it00003", "reason": None},
        )
        result = await self.invoke(
            check_return_eligibility_tool,
            {"item_id": "it00002", "reason": "damaged"},
        )

        self.assertEqual(result["decision"], "exception_flow")
        self.assertEqual(
            self.ctx.context.current_evaluation,
            ReturnEvaluation(
                order_id="CL1002",
                item_id="it00002",
                decision="exception_flow",
                policy_codes=(
                    "restricted_swimwear",
                    "damaged_item_exception",
                ),
            ),
        )

    async def test_failed_lookup_clears_previous_evaluation(self):
        await self.verify()
        await self.invoke(
            check_return_eligibility_tool,
            {"item_id": "it00003", "reason": None},
        )

        result = await self.invoke(
            check_return_eligibility_tool,
            {"item_id": "NO_SUCH_ITEM", "reason": None},
        )

        self.assert_not_checked(result, "order_or_item_not_found")
        self.assertIsNone(self.ctx.context.current_evaluation)

    async def test_evaluation_does_not_leak_between_conversations(self):
        await self.verify()
        await self.invoke(
            check_return_eligibility_tool,
            {"item_id": "it00003", "reason": None},
        )

        other_context = ReturnContext()
        self.assertIsNotNone(self.ctx.context.current_evaluation)
        self.assertIsNone(other_context.current_evaluation)

    async def test_stored_evaluation_is_immutable(self):
        await self.verify()
        await self.invoke(
            check_return_eligibility_tool,
            {"item_id": "it00003", "reason": None},
        )

        with self.assertRaises(FrozenInstanceError):
            self.ctx.context.current_evaluation.decision = "standard_return"

    async def test_stored_facts_cannot_be_overridden(self):
        cases = (
            ("CL1002", "it00003",
             "Final sale items cannot be returned unless they arrived damaged or wrong."),
            ("CL1002", "it00002",
             "Swimwear cannot be returned unless it arrived damaged or wrong."),
            ("CL1001", "it00001",
             "The item is outside the 30-day return window."),
        )
        for order_id, item_id, expected_reason in cases:
            with self.subTest(order_id=order_id, item_id=item_id):
                await self.verify(order_id)
                result = await self.invoke(
                    check_return_eligibility_tool,
                    {
                        "item_id": item_id,
                        "reason": "other",
                        "order_id": "FORGED_ORDER",
                        "verified_order_id": "FORGED_ORDER",
                        "delivered_date": "2026-08-30",
                        "category": "dress",
                        "final_sale": False,
                    },
                )

                self.assertEqual(result["decision"], "blocked")
                self.assertEqual(result["reasons"], [expected_reason])
                self.assertEqual(self.ctx.context.verified_order_id, order_id)

    async def test_restricted_items_use_exception_flow_for_damage_or_wrong_item(self):
        await self.verify()
        for item_id in ("it00002", "it00003"):
            for reason in ("damaged", "wrong_item"):
                with self.subTest(item_id=item_id, reason=reason):
                    result = await self.invoke(
                        check_return_eligibility_tool,
                        {"item_id": item_id, "reason": reason},
                    )

                    self.assertEqual(result["decision"], "exception_flow")
                    self.assertEqual(result["reasons"], [
                        "Damaged or wrong items can enter exception flow even if normally restricted."
                    ])

    async def test_restricted_items_without_exception_reason_are_blocked(self):
        await self.verify()
        for item_id in ("it00002", "it00003"):
            for reason in (None, "other"):
                with self.subTest(item_id=item_id, reason=reason):
                    result = await self.invoke(
                        check_return_eligibility_tool,
                        {"item_id": item_id, "reason": reason},
                    )
                    self.assertEqual(result["decision"], "blocked")

    async def test_item_from_another_order_is_rejected(self):
        await self.verify("CL1001")
        with patch("src.tools.return_tools.evaluate_return_policy") as policy:
            result = await self.invoke(
                check_return_eligibility_tool,
                {
                    "item_id": "it00004",
                    "reason": None,
                    "order_id": "CL1002",
                    "verified_order_id": "CL1002",
                },
            )

        self.assert_not_checked(result, "order_or_item_not_found")
        self.assertEqual(self.ctx.context.verified_order_id, "CL1001")
        policy.assert_not_called()

    def test_unknown_order_or_item_is_rejected_before_policy_runs(self):
        for order_id, item_id in (
            ("NO_SUCH_ORDER", "it00004"),
            ("CL1002", "NO_SUCH_ITEM"),
        ):
            with self.subTest(order_id=order_id, item_id=item_id):
                result_context = ReturnContext(verified_order_id=order_id)
                with patch("src.tools.return_tools.evaluate_return_policy") as policy:
                    result = check_return_eligibility(
                        item_id, context=result_context,
                    )

                self.assert_not_checked(result, "order_or_item_not_found")
                self.assertIsNone(result_context.current_evaluation)
                policy.assert_not_called()

    async def test_failed_verification_prevents_eligibility(self):
        for previously_verified in (False, True):
            with self.subTest(previously_verified=previously_verified):
                if previously_verified:
                    await self.verify()
                await self.verify(email="wrong@example.com")
                result = await self.invoke(
                    check_return_eligibility_tool,
                    {"item_id": "it00004", "reason": None},
                )
                self.assert_not_checked(result, "order_not_verified")

    async def test_lookup_reloads_stored_facts_after_verification(self):
        await self.verify()
        stored_orders = load_orders()
        order = next(order for order in stored_orders if order["order_id"] == "CL1002")
        item = next(item for item in order["items"] if item["item_id"] == "it00004")
        item["final_sale"] = True

        with patch("src.tools.return_tools.load_orders", return_value=stored_orders):
            result = await self.invoke(
                check_return_eligibility_tool,
                {"item_id": "it00004", "reason": None},
            )

        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["reasons"], [
            "Final sale items cannot be returned unless they arrived damaged or wrong."
        ])

    def test_internal_lookup_rejects_noncanonical_reasons(self):
        for reason in ("DAMAGED", "wrong item", "", False, [], {}):
            with self.subTest(reason=reason):
                with patch("src.tools.return_tools.load_orders") as storage:
                    result = check_return_eligibility(
                        "it00004", reason, context=ReturnContext(verified_order_id="CL1002"),
                    )

                self.assert_not_checked(result, "invalid_reason")
                storage.assert_not_called()

    async def test_tool_rejects_noncanonical_reasons(self):
        await self.verify()
        for reason in ("DAMAGED", "wrong item", "", False, [], {}):
            with self.subTest(reason=reason):
                with patch("src.agent_tools.check_return_eligibility") as lookup:
                    result = await self.invoke(
                        check_return_eligibility_tool,
                        {"item_id": "it00004", "reason": reason},
                    )

                self.assertIsInstance(result, str)
                self.assertIn("Invalid JSON input", result)
                lookup.assert_not_called()

    def test_tool_schema_exposes_only_item_id_and_canonical_reason(self):
        schema = check_return_eligibility_tool.params_json_schema

        self.assertEqual(set(schema["properties"]), {"item_id", "reason"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["item_id"]["type"], "string")
        self.assertEqual(schema["properties"]["reason"]["anyOf"], [
            {"enum": ["damaged", "wrong_item", "other"], "type": "string"},
            {"type": "null"},
        ])
        # Strict SDK schemas require both keys; reason remains nullable and
        # defaults to None when omitted from a direct invocation.
        self.assertEqual(set(schema["required"]), {"item_id", "reason"})

    def create_return(self):
        with patch("src.tools.return_tools.get_return_label_provider") as provider:
            provider.return_value.create_label.return_value = "https://carrier.example.test/original.pdf"
            created = start_return("it00004", context=ReturnContext(verified_order_id="CL1002"))
        self.assertEqual(created["status"], "return_created")
        return created

    async def test_existing_return_is_read_from_storage_without_policy_or_writes(self):
        created = self.create_return()
        await self.verify()
        self.ctx.context.current_evaluation = ReturnEvaluation("CL1002", "it00002", "blocked", ())
        path = Path(os.environ["CIDERLANE_DATABASE_PATH"])
        before = path.read_bytes()
        # The saved return remains authoritative even if the window has since expired.
        with patch.dict(os.environ, {"CIDERLANE_REFERENCE_DATE": "2027-01-01"}), \
                patch("src.tools.return_tools.evaluate_return_policy") as policy, \
                patch("src.tools.return_tools.get_return_label_provider") as provider:
            result = await self.invoke(check_return_eligibility_tool, {"item_id": "it00004"})
        self.assertEqual(result, {"status": "return_already_exists", **{
            key: created[key] for key in ("return_id", "order_id", "item_id", "label_url", "created_at")}})
        self.assertNotIn("decision", result)
        self.assertIsNone(self.ctx.context.current_evaluation)
        self.assertEqual(path.read_bytes(), before)
        policy.assert_not_called()
        provider.assert_not_called()

    async def test_fresh_database_query_does_not_create_a_file(self):
        await self.verify()
        path = Path(os.environ["CIDERLANE_DATABASE_PATH"])
        self.assertFalse(path.exists())
        result = await self.invoke(check_return_eligibility_tool, {"item_id": "it00004"})
        self.assertEqual(result["decision"], "standard_return")
        self.assertFalse(path.exists())

    async def test_empty_mktemp_database_query_does_not_initialize_schema(self):
        await self.verify()
        path = Path(os.environ["CIDERLANE_DATABASE_PATH"])
        path.touch()
        result = await self.invoke(check_return_eligibility_tool, {"item_id": "it00004"})
        self.assertEqual(result["decision"], "standard_return")
        self.assertEqual(path.read_bytes(), b"")

    async def test_other_item_does_not_reuse_an_existing_return(self):
        self.create_return()
        await self.verify()
        result = await self.invoke(check_return_eligibility_tool, {"item_id": "it00005"})
        self.assertEqual(result["decision"], "standard_return")
        self.assertNotIn("label_url", result)
        self.assertEqual(self.ctx.context.current_evaluation.item_id, "it00005")

    async def test_existing_return_is_not_disclosed_without_matching_verification(self):
        self.create_return()
        for order_id, error in ((None, "order_not_verified"), ("CL1001", "order_or_item_not_found")):
            with self.subTest(order_id=order_id):
                self.ctx.context = ReturnContext(verified_order_id=order_id)
                result = await self.invoke(check_return_eligibility_tool, {"item_id": "it00004"})
                self.assert_not_checked(result, error)
                self.assertNotIn("label_url", result)

    async def test_corrupt_return_storage_does_not_report_eligibility(self):
        await self.verify()
        Path(os.environ["CIDERLANE_DATABASE_PATH"]).write_bytes(b"not a SQLite database")
        self.ctx.context.current_evaluation = ReturnEvaluation("CL1002", "it00004", "standard_return", ())
        with patch("src.tools.return_tools.evaluate_return_policy") as policy:
            result = await self.invoke(check_return_eligibility_tool, {"item_id": "it00004"})
        self.assert_not_checked(result, "return_storage_unavailable")
        self.assertIsNone(self.ctx.context.current_evaluation)
        policy.assert_not_called()

    async def test_pending_label_request_is_not_reported_as_a_created_return(self):
        initialize_database()
        reserve_return_label_request("CL1002", "it00004")
        await self.verify()
        with closing(connect_database()) as connection:
            pending = [dict(row) for row in connection.execute("SELECT * FROM return_label_requests")]
        result = await self.invoke(check_return_eligibility_tool, {"item_id": "it00004"})
        self.assertEqual(result["decision"], "standard_return")
        self.assertNotIn("label_url", result)
        with closing(connect_database()) as connection:
            self.assertEqual([dict(row) for row in connection.execute("SELECT * FROM return_label_requests")], pending)
            self.assertEqual(connection.execute("SELECT count(*) FROM returns").fetchone()[0], 0)
