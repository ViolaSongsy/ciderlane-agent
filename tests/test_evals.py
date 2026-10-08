import unittest
import json
import tempfile
import io
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.items import ToolCallItem, ToolCallOutputItem
from agents import trace
from test_api import ScriptedModel, VERIFY, START_RETURN

from evals.run_evals import (
    ApprovalObservation, CaseRun, REQUIREMENT_IDS, ResponseGrade, ToolTrace,
    collect_run_items, grade_case, grade_requirements, load_eval_suite,
    matching_arguments, parse_tool_arguments, run_case, run_suite, TurnRun,
)
from src.agent import ciderlane_agent
from src.storage import connect_database


def call(name, call_id, arguments):
    return ToolCallItem(ciderlane_agent, {
        "type": "function_call", "name": name, "call_id": call_id,
        "arguments": arguments,
    })


def output(call_id, value):
    import json
    return ToolCallOutputItem(ciderlane_agent, {"call_id": call_id}, json.dumps(value))


def run(traces, rows=None, support=None, approvals=None):
    return CaseRun(traces, "response", rows or [], support or [], approvals or [])


def case(requirements=(), **expected):
    return {"requirements": list(requirements), "expected": {
        "response_requirements": [], "human_review_required": False,
        **expected,
    }}


class TestEvalTrace(unittest.TestCase):
    def test_extracts_model_arguments_and_rejects_non_objects(self):
        self.assertEqual(parse_tool_arguments({"arguments": '{"item_id":"it00004","reason":null}'}),
                         {"item_id": "it00004", "reason": None})
        with self.assertRaises(ValueError):
            parse_tool_arguments({"arguments": "[]"})

    def test_pairs_outputs_by_call_id_and_preserves_call_order(self):
        result = SimpleNamespace(new_items=[
            call("verify_order", "a", '{"order_id":"CL1002","email":"maya@example.com"}'),
            call("check_return_eligibility", "b", '{"item_id":"it00004"}'),
            output("b", {"decision": "standard_return"}),
            output("a", {"verified": True}),
        ])
        traces, by_id = [], {}
        collect_run_items(result, traces, by_id)
        self.assertEqual([t.name for t in traces], ["verify_order", "check_return_eligibility"])
        self.assertEqual(traces[0].arguments["order_id"], "CL1002")
        self.assertEqual(traces[0].output, {"verified": True})
        self.assertEqual(traces[1].output, {"decision": "standard_return"})

    def test_unknown_output_and_duplicate_call_ids_fail(self):
        with self.assertRaises(ValueError):
            collect_run_items(SimpleNamespace(new_items=[output("missing", {})]), [], {})
        traces, by_id = [], {}
        with self.assertRaises(ValueError):
            collect_run_items(SimpleNamespace(new_items=[call("verify_order", "x", "{}"),
                                                        call("verify_order", "x", "{}")]), traces, by_id)

    def test_sdk_resume_replays_are_deduplicated_but_conflicts_still_fail(self):
        result = SimpleNamespace(new_items=[call("verify_order", "a", "{}"), output("a", {"verified": True})])
        traces, by_id = [], {}
        for _ in range(2):
            collect_run_items(result, traces, by_id, turn_index=0, allow_replays=True)
        self.assertEqual(len(traces), 1)
        with self.assertRaises(ValueError):
            collect_run_items(SimpleNamespace(new_items=[output("a", {"verified": False})]),
                              traces, by_id, turn_index=0, allow_replays=True)
        with self.assertRaises(ValueError):
            collect_run_items(result, traces, by_id, turn_index=1, allow_replays=True)

    def test_optional_null_equivalence_without_weakening_key_boundary(self):
        self.assertTrue(matching_arguments({"item_id": "it00004"},
                                           {"item_id": "it00004", "reason": None}))
        self.assertFalse(matching_arguments({"item_id": "it00004", "order_id": "CL1002"},
                                            {"item_id": "it00004", "reason": None, "order_id": None}))
        traces = [ToolTrace("verify_order", {}, {"verified": True}),
                  ToolTrace("check_return_eligibility", {"item_id": "it00004", "order_id": "CL1002"},
                            {"decision": "standard_return"})]
        self.assertFalse(grade_requirements(case(["POLICY-01"]), run(traces))["POLICY-01"])
        traces[1] = ToolTrace("start_return", {"item_id": "it00004", "policy_codes": []},
                              {"status": "return_not_created", "decision": "blocked"})
        self.assertFalse(grade_requirements(case(["WRITE-01"]),
                         run(traces, approvals=[ApprovalObservation(0, 0)]))["WRITE-01"])


class TestEvalRequirements(unittest.TestCase):
    def test_auth_requires_successful_verification_before_each_action(self):
        traces = [ToolTrace("verify_order", {}, {"verified": False}),
                  ToolTrace("check_return_eligibility", {"item_id": "it00004"}, {"decision": "standard_return"})]
        self.assertFalse(grade_requirements(case(["AUTH-01"]), run(traces))["AUTH-01"])
        traces[0].output = {"verified": True}
        self.assertTrue(grade_requirements(case(["AUTH-01"]), run(traces))["AUTH-01"])

    def test_failed_reverification_revokes_previous_authorization(self):
        verification = ToolTrace("verify_order", {}, {"verified": True, "order_id": "CL1002"})
        failed_verification = ToolTrace("verify_order", {}, {"verified": False})
        inquiry = ToolTrace("check_return_eligibility", {"item_id": "it00004"},
                            {"status": "eligibility_not_checked", "error": "order_not_verified"})
        fixture = case(["AUTH-01"])
        self.assertFalse(grade_requirements(fixture, run([verification, failed_verification, inquiry]))["AUTH-01"])
        self.assertTrue(grade_requirements(fixture, run([verification, failed_verification, verification, inquiry]))["AUTH-01"])

    def test_reverification_revokes_previous_support_evaluation(self):
        verification = ToolTrace("verify_order", {}, {"verified": True, "order_id": "CL1002"})
        evaluation = ToolTrace("check_return_eligibility", {"item_id": "it00003"},
                               {"decision": "blocked", "policy_codes": ["final_sale"]})
        new_verification = ToolTrace("verify_order", {}, {"verified": True, "order_id": "CL1001"})
        support = ToolTrace("request_human_support", {"escalation_reason": "final_sale_dispute",
                            "summary": "Review requested."}, {"status": "case_created", "case_id": "CASE"})
        row = {"case_id": "CASE", "escalation_reason": "final_sale_dispute", "order_id": "CL1002",
               "item_id": "it00003", "eligibility_decision": "blocked", "summary": "Review requested."}
        fixture = case(["ESC-02"])
        self.assertTrue(grade_requirements(fixture, run([verification, evaluation, support], support=[row]))["ESC-02"])
        self.assertFalse(grade_requirements(fixture, run([verification, evaluation, new_verification, support], support=[row]))["ESC-02"])

    def test_new_order_reset_revokes_authorization_and_support_evaluation(self):
        verification = ToolTrace("verify_order", {}, {"verified": True, "order_id": "CL1002"})
        evaluation = ToolTrace("check_return_eligibility", {"item_id": "it00003"},
                               {"decision": "blocked", "policy_codes": ["final_sale"]})
        reset = ToolTrace("begin_new_order_request", {}, {"status": "new_order_request_started"})
        support = ToolTrace("request_human_support", {
            "escalation_reason": "final_sale_dispute", "summary": "Review requested.",
        }, {"status": "case_created", "case_id": "CASE"})
        row = {"case_id": "CASE", "escalation_reason": "final_sale_dispute", "order_id": "CL1002",
               "item_id": "it00003", "eligibility_decision": "blocked", "summary": "Review requested."}
        self.assertFalse(grade_requirements(case(["AUTH-01"]),
                         run([verification, reset, evaluation]))["AUTH-01"])
        self.assertTrue(grade_requirements(case(["AUTH-01"]),
                        run([verification, reset, verification, evaluation]))["AUTH-01"])
        self.assertFalse(grade_requirements(case(["ESC-02"]),
                         run([verification, evaluation, reset, support], support=[row]))["ESC-02"])
        self.assertFalse(grade_requirements(case(["ESC-02"]),
                         run([verification, evaluation, reset, verification, support], support=[row]))["ESC-02"])
        self.assertTrue(grade_requirements(case(["ESC-02"]),
                        run([verification, evaluation, reset, verification, evaluation, support],
                            support=[row]))["ESC-02"])

    def test_approval_zero_delta_even_with_existing_row(self):
        traces = [ToolTrace("verify_order", {}, {"verified": True}),
                  ToolTrace("start_return", {"item_id": "it00004"}, {"status": "return_already_exists",
                    "order_id": "CL1002", "item_id": "it00004"})]
        self.assertTrue(grade_requirements(case(["WRITE-01"]),
                        run(traces, approvals=[ApprovalObservation(1, 1)]))["WRITE-01"])
        self.assertFalse(grade_requirements(case(["WRITE-01"]),
                         run(traces, approvals=[ApprovalObservation(1, 2)]))["WRITE-01"])
        self.assertFalse(grade_requirements(case(["WRITE-01"]), run(traces))["WRITE-01"])

    def test_created_row_must_match_output_and_no_extra_rows(self):
        row = {"return_id": "RET-CL1002-IT00004", "order_id": "CL1002",
               "item_id": "it00004", "label_url": "https://example.test/label"}
        traces = [ToolTrace("verify_order", {}, {"verified": True}),
                  ToolTrace("start_return", {"item_id": "it00004"},
                            {"status": "return_created", "decision": "standard_return", **row})]
        self.assertTrue(grade_requirements(case(["WRITE-02"]), run(traces, rows=[row]))["WRITE-02"])
        bad = {**row, "label_url": "wrong"}
        self.assertFalse(grade_requirements(case(["WRITE-02"]), run(traces, rows=[bad]))["WRITE-02"])
        self.assertFalse(grade_requirements(case(["WRITE-02"]), run(traces))["WRITE-02"])

    def test_blocked_and_exception_outcomes_leave_no_return_row(self):
        for decision in ("blocked", "exception_flow"):
            traces = [ToolTrace("verify_order", {}, {"verified": True}),
                      ToolTrace("start_return", {"item_id": "it00002"},
                                {"status": "return_not_created", "decision": decision})]
            self.assertTrue(grade_requirements(case(["WRITE-02"]), run(traces))["WRITE-02"])
            self.assertFalse(grade_requirements(case(["WRITE-02"]),
                             run(traces, rows=[{"order_id": "CL1002", "item_id": "it00002"}]))["WRITE-02"])

    def test_duplicate_persistence_requires_one_created_and_one_existing(self):
        row = {"return_id": "RET-CL1002-IT00004", "order_id": "CL1002",
               "item_id": "it00004", "label_url": "https://example.test/label"}
        traces = [ToolTrace("verify_order", {}, {"verified": True}),
                  ToolTrace("start_return", {"item_id": "it00004"},
                            {"status": "return_created", "decision": "standard_return", **row}),
                  ToolTrace("start_return", {"item_id": "it00004"},
                            {"status": "return_already_exists", **row})]
        self.assertTrue(grade_requirements(case(["WRITE-02"]), run(traces, rows=[row]))["WRITE-02"])
        traces[2].output = {"status": "return_already_exists", **row, "label_url": "wrong"}
        self.assertFalse(grade_requirements(case(["WRITE-02"]), run(traces, rows=[row]))["WRITE-02"])
        traces[2].output = {"status": "return_created", "decision": "standard_return", **row}
        self.assertFalse(grade_requirements(case(["WRITE-02"]), run(traces, rows=[row]))["WRITE-02"])

    def test_escalation_requires_matching_prior_evaluation_and_narrow_arguments(self):
        support = ToolTrace("request_human_support",
                            {"escalation_reason": "final_sale_dispute", "summary": "Disputes final sale"},
                            {"status": "case_created", "case_id": "CASE-1"})
        support_row = {"case_id": "CASE-1", "escalation_reason": "final_sale_dispute",
                       "order_id": "CL1002", "item_id": "it00003",
                       "eligibility_decision": "blocked"}
        blocked = ToolTrace("start_return", {"item_id": "it00003"},
                            {"status": "return_not_created", "decision": "blocked", "policy_codes": ["final_sale"]})
        traces = [ToolTrace("verify_order", {}, {"verified": True, "order_id": "CL1002"}),
                  blocked, support]
        self.assertEqual(grade_requirements(case(["ESC-01", "ESC-02"]),
                                            run(traces, support=[support_row])),
                         {"ESC-01": True, "ESC-02": True})
        self.assertFalse(grade_requirements(case(["ESC-02"]), run([traces[0], support]))["ESC-02"])
        support.arguments["order_id"] = "CL1002"
        self.assertFalse(grade_requirements(case(["ESC-01"]), run(traces, support=[support_row]))["ESC-01"])
        support.arguments = {"escalation_reason": "verification_failed", "summary": "Cannot verify"}
        self.assertTrue(grade_requirements(case(["ESC-02"]), run([support]))["ESC-02"])

    def test_escalation_rejects_row_for_another_item_or_decision(self):
        evaluation = ToolTrace("check_return_eligibility", {"item_id": "it00003"},
                               {"decision": "blocked", "policy_codes": ["final_sale"]})
        support = ToolTrace("request_human_support",
                            {"escalation_reason": "final_sale_dispute", "summary": "Disputed"},
                            {"status": "case_created", "case_id": "CASE-1"})
        traces = [ToolTrace("verify_order", {}, {"verified": True, "order_id": "CL1002"}),
                  evaluation, support]
        row = {"case_id": "CASE-1", "escalation_reason": "final_sale_dispute",
               "order_id": "CL1002", "item_id": "it00003", "eligibility_decision": "blocked"}
        requirement_case = case(["ESC-02"])
        self.assertTrue(grade_requirements(requirement_case, run(traces, support=[row]))["ESC-02"])
        for changed in ({"item_id": "it00002"}, {"eligibility_decision": "exception_flow"},
                        {"case_id": "CASE-2"}):
            with self.subTest(changed=changed):
                self.assertFalse(grade_requirements(requirement_case,
                                 run(traces, support=[{**row, **changed}]))["ESC-02"])
        support.output = {"status": "case_not_created", "case_id": "CASE-1"}
        self.assertFalse(grade_requirements(requirement_case,
                         run(traces, support=[row]))["ESC-02"])
        support.output = {"status": "case_created", "case_id": "CASE-1"}
        evaluation.output = {"decision": "blocked", "policy_codes": []}
        self.assertFalse(grade_requirements(requirement_case,
                         run(traces, support=[row]))["ESC-02"])

    def test_escalation_rejects_support_row_for_different_verified_order(self):
        traces = [ToolTrace("verify_order", {}, {"verified": True, "order_id": "CL1002"}),
                  ToolTrace("start_return", {"item_id": "it00003"},
                            {"status": "return_not_created", "decision": "blocked",
                             "policy_codes": ["final_sale"]}),
                  ToolTrace("request_human_support",
                            {"escalation_reason": "final_sale_dispute", "summary": "Disputed"},
                            {"status": "case_created", "case_id": "CASE-1"})]
        row = {"case_id": "CASE-1", "escalation_reason": "final_sale_dispute",
               "order_id": "CL1002", "item_id": "it00003",
               "eligibility_decision": "blocked"}
        requirement_case = case(["ESC-02"])
        self.assertTrue(grade_requirements(requirement_case,
                        run(traces, support=[row]))["ESC-02"])
        self.assertFalse(grade_requirements(requirement_case,
                         run(traces, support=[{**row, "order_id": "CL1001"}]))["ESC-02"])

    def test_escalation_uses_latest_evaluation_item(self):
        traces = [ToolTrace("verify_order", {}, {"verified": True, "order_id": "CL1002"}),
                  ToolTrace("check_return_eligibility", {"item_id": "it00003"},
                            {"decision": "blocked", "policy_codes": ["final_sale"]}),
                  ToolTrace("check_return_eligibility", {"item_id": "it00002"},
                            {"decision": "blocked", "policy_codes": ["final_sale"]}),
                  ToolTrace("request_human_support",
                            {"escalation_reason": "final_sale_dispute", "summary": "Disputed"},
                            {"status": "case_created", "case_id": "CASE-1"})]
        row = {"case_id": "CASE-1", "escalation_reason": "final_sale_dispute",
               "order_id": "CL1002", "item_id": "it00003",
               "eligibility_decision": "blocked"}
        self.assertFalse(grade_requirements(case(["ESC-02"]),
                         run(traces, support=[row]))["ESC-02"])

    def test_pure_inquiry_requires_no_write(self):
        traces = [ToolTrace("verify_order", {}, {"verified": True}),
                  ToolTrace("check_return_eligibility", {"item_id": "it00004"},
                            {"decision": "standard_return"})]
        self.assertTrue(grade_requirements(case(["POLICY-01", "INTENT-01"]), run(traces))["INTENT-01"])
        row = {"order_id": "CL1002", "item_id": "it00004"}
        grades = grade_requirements(case(["POLICY-01", "INTENT-01"]), run(traces, rows=[row]))
        self.assertEqual(grades, {"POLICY-01": True, "INTENT-01": False})
        traces.append(ToolTrace("start_return", {"item_id": "it00004"}, {"status": "return_not_created"}))
        self.assertFalse(grade_requirements(case(["INTENT-01"]), run(traces))["INTENT-01"])

    def test_duplicate_case_accepts_one_or_two_successful_verifications(self):
        duplicate_case = next(item for item in load_eval_suite()["cases"]
                              if item["id"] == "duplicate_return_request_is_idempotent")
        expected = duplicate_case["expected"]
        self.assertIn("verify_order", expected["required_tool_calls"])
        self.assertNotIn("verify_order", expected["tool_expectations"])
        self.assertEqual(len(expected["tool_expectations"]["start_return"]), 2)
        row = {"return_id": "RET-CL1002-IT00004", "order_id": "CL1002",
               "item_id": "it00004", "label_url": "https://example.test/label"}
        verification = ToolTrace("verify_order",
                                 {"order_id": "CL1002", "email": "maya@example.com"},
                                 {"verified": True, "order_id": "CL1002"})
        first = ToolTrace("start_return", {"item_id": "it00004", "reason": None},
                          {"status": "return_created", "decision": "standard_return", **row})
        second = ToolTrace("start_return", {"item_id": "it00004", "reason": None},
                           {"status": "return_already_exists", **row})
        approvals = [ApprovalObservation(0, 0), ApprovalObservation(1, 1)]
        with patch("evals.run_evals.grade_response", return_value=ResponseGrade(False, [])):
            for traces in ([verification, first, second],
                           [verification, first, verification, second]):
                with self.subTest(verification_count=len(traces) - 2):
                    failures, grades = grade_case(duplicate_case,
                                                  run(traces, rows=[row], approvals=approvals))
                    self.assertEqual(failures, [])
                    self.assertEqual(grades, {key: True for key in duplicate_case["requirements"]})

    def test_case_grader_checks_order_outputs_arguments_and_support_row(self):
        fixture = case(["ESC-01"], tool_sequence=["verify_order", "request_human_support"],
                       tool_expectations={"request_human_support": [{
                           "arguments": {"escalation_reason": "verification_failed"},
                           "output": {"status": "case_created"}}]},
                       support_rows=1, support_row={"escalation_reason": "verification_failed"})
        traces = [ToolTrace("request_human_support", {"escalation_reason": "wrong", "summary": "x"},
                            {"status": "case_not_created"})]
        with patch("evals.run_evals.grade_response", return_value=ResponseGrade(False, [])):
            failures, _ = grade_case(fixture, run(traces, support=[{"case_id": "C", "escalation_reason": "wrong"}]))
        self.assertTrue(any("tool order" in failure for failure in failures))
        self.assertTrue(any("arguments" in failure for failure in failures))
        self.assertTrue(any("status" in failure for failure in failures))
        self.assertTrue(any("support row" in failure for failure in failures))

    def test_first_business_tool_skips_only_order_reset(self):
        from evals.run_evals import grade_observation

        reset = ToolTrace("begin_new_order_request", {}, {"status": "new_order_request_started"})
        verification = ToolTrace("verify_order", {}, {"verified": True, "order_id": "CL1002"})
        inquiry = ToolTrace("check_return_eligibility", {"item_id": "it00004"},
                            {"decision": "standard_return"})
        expected = {"first_business_tool_call": "verify_order"}
        self.assertEqual(grade_observation(expected, [reset, verification, inquiry], [], []), [])
        self.assertTrue(grade_observation(expected, [reset, inquiry, verification], [], []))
        self.assertTrue(grade_observation(expected, [reset], [], []))
        self.assertTrue(grade_observation({"first_tool_call": "verify_order"},
                                        [reset, verification], [], []))

    def test_existing_support_case_keeps_original_summary_without_hiding_creation_mismatch(self):
        from evals.run_evals import grade_observation

        row = {
            "case_id": "CASE-ORIGINAL", "escalation_reason": "verification_failed",
            "summary": "Customer could not locate the order ID.",
            "order_id": None, "item_id": None, "eligibility_decision": None,
        }
        arguments = {
            "escalation_reason": "verification_failed",
            "summary": "Customer still cannot provide their order number.",
        }
        existing = ToolTrace("request_human_support", arguments,
                             {"status": "case_already_exists", **row})
        self.assertEqual(grade_observation({"support_rows": 1}, [existing], [], [row]), [])

        created = ToolTrace("request_human_support", arguments,
                            {"status": "case_created", **row})
        self.assertIn("support case output does not match persistent row",
                      grade_observation({"support_rows": 1}, [created], [], [row]))

    def test_existing_support_case_still_requires_matching_case_id_and_escalation_reason(self):
        from evals.run_evals import grade_observation

        row = {
            "case_id": "CASE-ORIGINAL", "escalation_reason": "verification_failed",
            "summary": "Original description.",
        }
        arguments = {"escalation_reason": "verification_failed", "summary": "A paraphrase."}
        for output_changes in ({"case_id": "CASE-WRONG"}, {}):
            with self.subTest(output_changes=output_changes):
                output = {"status": "case_already_exists", **row, **output_changes}
                actual_arguments = (arguments if output_changes else
                                    {**arguments, "escalation_reason": "return_exception"})
                existing = ToolTrace("request_human_support", actual_arguments, output)
                self.assertIn("support case output does not match persistent row",
                              grade_observation({}, [existing], [], [row]))

    def test_unavailable_verification_cases_reject_empty_verification_calls(self):
        case_ids = {
            "missing_verification_information_creates_support_case",
            "missing_order_id_creates_support_case",
            "missing_email_creates_support_case",
        }
        fixtures = [item for item in load_eval_suite()["cases"] if item["id"] in case_ids]
        self.assertEqual({item["id"] for item in fixtures}, case_ids)
        support_row = {
            "case_id": "C", "escalation_reason": "verification_failed",
            "summary": "Customer cannot provide verification information.",
            "order_id": None, "item_id": None, "eligibility_decision": None,
        }
        support = ToolTrace("request_human_support", {
            "escalation_reason": support_row["escalation_reason"],
            "summary": support_row["summary"],
        }, {"status": "case_created", **support_row})
        empty_verification = ToolTrace("verify_order", {"order_id": "", "email": ""},
                                       {"verified": False, "error": "order_not_found"})
        with patch("evals.run_evals.grade_response", return_value=ResponseGrade(True, [])):
            for fixture in fixtures:
                with self.subTest(case_id=fixture["id"]):
                    failures, _ = grade_case(fixture, run([support], support=[support_row]))
                    self.assertEqual(failures, [])
                    failures, _ = grade_case(fixture, run(
                        [empty_verification, support], support=[support_row]))
                    self.assertIn("forbidden tool call: verify_order", failures)


class TestEvalResponseGrader(unittest.TestCase):
    def setUp(self):
        self.enterContext(trace("Response grader tests", disabled=True))

    def test_choice_confirmation_questions_are_distinct_from_customer_context(self):
        from evals.run_evals import grade_target_response, response_grader

        requirement = (
            "Ask the customer to confirm whether the floral short dress belongs "
            "to the already verified order CL1002."
        )
        context = {"conversation": [
            {"role": "assistant", "content": "The white short dress is eligible."},
            {"role": "user", "content": (
                "I want to return the floral short dress, but I cannot find my order ID."
            )},
        ], "tool_results": [{"name": "verify_order", "output": {
            "verified": True, "order_id": "CL1002",
        }}]}
        questions = [
            "Does the floral short dress belong to order CL1002, or a different order?",
            "Does the floral short dress belong to the verified order CL1002 or a different order?",
        ]
        for question in questions:
            with self.subTest(question=question):
                # The fixture grade represents semantic acceptance. Exercise the
                # real SDK boundary so background and the scored reply retain
                # their roles instead of becoming one customer conversation.
                model = ScriptedModel([json.dumps({"response": {
                    "human_review_required": False, "unmet_requirements": [],
                    "target_response_text": question,
                }})])
                with patch("evals.run_evals.response_grader", response_grader.clone(model=model)):
                    failures = grade_target_response({
                        "response_requirements": [requirement], "response_context": True,
                        "human_review_required": False,
                    }, question, context)
                self.assertEqual(failures, [])
                self.assertEqual(len(model.inputs), 1)
                self.assertEqual(model.inputs[0][-1]["role"], "user")
                payload = json.loads(model.inputs[0][-1]["content"])
                self.assertEqual(payload["target_response"], {
                    "role": "assistant", "content": question,
                })
                self.assertEqual(payload["requirements"], [requirement])
                self.assertEqual(payload["reference_context"], context)
                self.assertEqual(list(payload)[-1], "target_response")

    def test_background_confirmation_cannot_override_unmet_target_grade(self):
        from evals.run_evals import grade_target_response, response_grader

        requirement = "Ask whether the floral short dress belongs to order CL1002."
        context = {"conversation": [
            {"role": "assistant", "content": (
                "Does the floral short dress belong to order CL1002, or a different order?"
            )},
            {"role": "user", "content": "I cannot find or provide my order ID."},
        ]}
        response = "Please provide the order ID before I can help."
        model = ScriptedModel([json.dumps({"response": {
            "human_review_required": False, "unmet_requirements": [requirement],
            "target_response_text": response,
        }})])
        with patch("evals.run_evals.response_grader", response_grader.clone(model=model)):
            failures = grade_target_response({
                "response_requirements": [requirement], "response_context": True,
            }, response, context)
        self.assertEqual(failures, [f"unmet response requirements: {[requirement]}"])
        payload = json.loads(model.inputs[0][-1]["content"])
        self.assertEqual(payload["target_response"], {"role": "assistant", "content": response})
        self.assertEqual(payload["reference_context"]["conversation"][0]["role"], "assistant")
        self.assertNotEqual(payload["reference_context"]["conversation"][0]["content"], response)

    def test_support_replies_parse_exact_target_and_independent_review_grade(self):
        from evals.run_evals import grade_target_response, response_grader

        fixtures = [
            (
                "I couldn’t verify your order without the order ID. "
                "Human review is required; case CASE-TEST has been created.",
                ["Confirm that human review is required and a support case was created."],
                True,
            ),
            (
                "You’re welcome. Human support will follow up regarding case CASE-TEST.",
                ["Do not claim that a new support case, return, or label was created."],
                False,
            ),
        ]
        context = {"conversation": [
            {"role": "assistant", "content": "Support case CASE-TEST was created."},
            {"role": "user", "content": "Thanks."},
        ], "tool_results": [{"name": "request_human_support", "output": {
            "status": "case_created", "case_id": "CASE-TEST",
        }}]}
        for response, requirements, review_required in fixtures:
            with self.subTest(response=response):
                # These supplied grades test SDK parsing and runner bookkeeping,
                # not whether the live grader understands the reply's meaning.
                model = ScriptedModel([json.dumps({"response": {
                    "target_response_text": response,
                    "human_review_required": review_required, "unmet_requirements": [],
                }})])
                with patch("evals.run_evals.response_grader", response_grader.clone(model=model)):
                    failures = grade_target_response({
                        "response_requirements": requirements, "response_context": True,
                        "human_review_required": review_required,
                    }, response, context)
                self.assertEqual(failures, [])
                self.assertIn(response, model.inputs[0][-1]["content"])
                payload = json.loads(model.inputs[0][-1]["content"])
                self.assertEqual(payload["target_response"]["content"], response)
                self.assertEqual(payload["reference_context"], context)

    def test_missing_or_different_echo_records_failure_and_preserves_model_failures(self):
        from evals.run_evals import grade_target_response, response_grader

        requirement = "Do not claim that a new support case was created."
        response = "A new support case CASE-NEW was created. Human review is required."
        for echo in (None, "You’re welcome. Human support will follow up."):
            with self.subTest(echo=echo):
                model = ScriptedModel([json.dumps({"response": {
                    "target_response_text": echo,
                    "human_review_required": False, "unmet_requirements": [requirement],
                }})])
                with patch("evals.run_evals.response_grader", response_grader.clone(model=model)):
                    failures = grade_target_response({
                        "response_requirements": [requirement], "human_review_required": True,
                    }, response, {})
                self.assertEqual(len(model.inputs), 1, "An invalid echo must not trigger a retry")
                self.assertIn("response grader did not reproduce the exact target response", failures)
                self.assertIn(f"unmet response requirements: {[requirement]}", failures)
                self.assertIn("human_review_required: expected True, got False", failures)

    def test_correct_echo_does_not_turn_a_prohibited_claim_into_a_pass(self):
        from evals.run_evals import grade_target_response, response_grader

        requirement = "Do not claim that a new support case was created."
        response = "A new support case CASE-NEW was created."
        model = ScriptedModel([json.dumps({"response": {
            "target_response_text": response,
            "human_review_required": False, "unmet_requirements": [requirement],
        }})])
        with patch("evals.run_evals.response_grader", response_grader.clone(model=model)):
            failures = grade_target_response({"response_requirements": [requirement]}, response, {})
        self.assertEqual(failures, [f"unmet response requirements: {[requirement]}"])


class TestEvalHarness(unittest.TestCase):
    def setUp(self):
        self.enterContext(trace("Eval harness tests", disabled=True))
        self.enterContext(patch.dict("os.environ", {"CIDERLANE_REFERENCE_DATE": "2026-08-31"}))
        self.provider = self.enterContext(patch("src.tools.return_tools.get_return_label_provider")).return_value
        self.provider.create_label.return_value = "https://carrier.example.test/label.pdf"

    def use_model(self, steps):
        self.model = ScriptedModel(steps)
        self.enterContext(patch("src.api.ciderlane_agent", ciderlane_agent.clone(model=self.model)))

    def test_approval_observation_and_case_database_isolation(self):
        self.use_model([VERIFY, START_RETURN, "done"])
        first = run_case({"messages": ["start"]})
        self.assertEqual(len(first.approval_observations), 1)
        observation = first.approval_observations[0]
        self.assertEqual((observation.before_count, observation.proposed_count, observation.after_count), (0, 0, 1))
        self.assertEqual(observation.call_id, first.tool_traces[-1].call_id)
        self.assertEqual(len(first.returns_rows), 1)
        self.use_model(["hello"])
        second = run_case({"messages": ["hello"]})
        self.assertEqual(second.returns_rows, [])

    def test_proposal_write_stops_before_approval(self):
        self.use_model([VERIFY, START_RETURN])
        original = self.model.get_response
        async def dirty_response(*args, **kwargs):
            response = await original(*args, **kwargs)
            if any(getattr(item, "name", None) == "start_return" for item in response.output):
                with connect_database() as connection:
                    connection.execute("INSERT INTO returns(return_id,order_id,item_id,label_url) VALUES ('R','O','I','U')")
            return response
        self.model.get_response = dirty_response
        with self.assertRaisesRegex(AssertionError, "before approval"):
            run_case({"messages": ["start"]})
        self.provider.create_label.assert_not_called()

    def test_reject_then_new_turn_approve_uses_real_api_rejection(self):
        self.use_model([VERIFY, START_RETURN, "Declined.", START_RETURN, "Created."])
        fixture = case(["AUTH-01", "WRITE-01", "WRITE-02"], return_rows=1)
        fixture["messages"] = [
            {"content": "start", "approvals": ["reject"], "expected": {"return_rows": 0}},
            {"content": "actually return it", "approvals": ["approve"], "expected": {"return_rows": 1}},
        ]
        result = run_case(fixture)
        self.assertEqual(result.workflow_failures, [])
        self.assertEqual([approval.decision for approval in result.approval_observations], ["reject", "approve"])
        self.assertEqual([approval.turn_index for approval in result.approval_observations], [0, 1])
        self.assertNotEqual(result.approval_observations[0].approval_id,
                            result.approval_observations[1].approval_id)
        self.assertEqual([len(turn.returns_rows) for turn in result.turns], [0, 1])
        self.assertIn("This return approval was rejected", str(self.model.inputs[2]))
        self.provider.create_label.assert_called_once()
        with patch("evals.run_evals.grade_response", return_value=ResponseGrade(False, [])):
            failures, grades = grade_case(fixture, result)
        self.assertEqual(failures, [])
        self.assertTrue(all(grades.values()))

    def test_same_turn_retry_cannot_consume_next_turn_approval(self):
        self.use_model([VERIFY, START_RETURN, START_RETURN, "Done."])
        fixture = {"messages": [
            {"content": "start", "approvals": ["reject"]},
            {"content": "try again", "approvals": ["approve"]},
        ]}
        result = run_case(fixture)
        self.assertTrue(any("unexpected approval" in failure for failure in result.workflow_failures))
        self.assertEqual(len(result.turns), 1)
        self.assertEqual(result.returns_rows, [])
        self.provider.create_label.assert_not_called()

    def test_unexpected_approval_on_inquiry_is_never_executed(self):
        self.use_model([VERIFY, START_RETURN])
        result = run_case({"messages": [{"content": "Can I return it?", "approvals": []}]})
        self.assertTrue(result.workflow_failures)
        self.assertEqual(result.approval_observations, [])
        self.assertEqual(result.returns_rows, [])
        self.provider.create_label.assert_not_called()
        self.assertTrue(grade_requirements(case(["WRITE-02"]), result)["WRITE-02"])

    def test_missing_fresh_approval_is_reported_even_without_extra_writes(self):
        self.use_model([VERIFY, START_RETURN, "Declined.", "Cannot resubmit."])
        result = run_case({"messages": [
            {"content": "start", "approvals": ["reject"]},
            {"content": "actually return it", "approvals": ["approve"]},
        ]})
        self.assertIn("turn 2: expected 1 approval decision(s), used 0", result.workflow_failures)
        self.assertEqual(result.returns_rows, [])

    def test_late_verification_cannot_hide_failure_in_the_switching_turn(self):
        fixture = case(required_tool_calls=["verify_order"])
        fixture["messages"] = [
            {"content": "different order CL9999", "approvals": [],
             "expected": {"first_tool_call": "verify_order", "required_tool_calls": ["verify_order"]}},
            {"content": "white short dress", "approvals": []},
        ]
        verification = ToolTrace("verify_order", {}, {"verified": False}, turn_index=1)
        result = run([verification])
        result.turns = [TurnRun("different order CL9999", "Which item?", [], [], 0, [], []),
                        TurnRun("white short dress", "Cannot verify.", [verification], [], 0, [], [])]
        with patch("evals.run_evals.grade_response", return_value=ResponseGrade(False, [])):
            failures, _ = grade_case(fixture, result)
        self.assertIn("turn 1: missing tool call: verify_order", failures)

    def test_response_grader_receives_context_and_target_separately(self):
        from evals.run_evals import grade_response
        context = {"conversation": [{"role": "user", "content": "Return the white short dress again."}]}
        with patch("evals.run_evals.Runner.run_sync", return_value=SimpleNamespace(
                final_output=ResponseGrade(False, [], target_response_text="An existing return is already on file."))) as grader:
            grade_response("An existing return is already on file.", ["Explain that a return exists."], context=context)
        payload = json.loads(grader.call_args.args[1])
        self.assertEqual(payload["reference_context"], context)
        self.assertEqual(payload["target_response"], {
            "role": "assistant", "content": "An existing return is already on file.",
        })

    def test_response_context_is_opt_in_so_background_is_not_graded_as_a_claim(self):
        from evals.run_evals import grade_target_response
        expected = {"response_requirements": ["Do not reveal unverified order details."]}
        context = {"conversation": [{"role": "user", "content": "Order CL1002."}]}
        with patch("evals.run_evals.grade_response", return_value=ResponseGrade(False, [])) as grader:
            grade_target_response(expected, "Please check your email.", context)
            self.assertIsNone(grader.call_args.kwargs["context"])
            grade_target_response({**expected, "response_context": True}, "Please check your email.", context)
            self.assertEqual(grader.call_args.kwargs["context"], context)

    def test_json_report_preserves_rejected_approval_and_turn_evidence(self):
        self.use_model([VERIFY, START_RETURN, "Declined."])
        fixture = case(["AUTH-01", "WRITE-01", "WRITE-02"])
        fixture.update(id="reject-report", messages=[{"content": "start", "approvals": ["reject"]}])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with patch("evals.run_evals.load_eval_suite", return_value={
                    "reference_date": "2026-08-31", "cases": [fixture]}), \
                    patch("evals.run_evals.grade_response", return_value=ResponseGrade(False, [])), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(run_suite(output_path=path), 0)
            report = json.loads(path.read_text())
        self.assertEqual((report["passed"], report["total"]), (1, 1))
        evidence = report["cases"][0]["run"]
        self.assertEqual(evidence["approval_observations"][0]["decision"], "reject")
        self.assertTrue(evidence["approval_observations"][0]["approval_id"])
        self.assertEqual(evidence["turns"][0]["response"], "Declined.")
        self.assertEqual(evidence["turns"][0]["returns_rows"], [])

    def test_rejection_instructions_are_evidence_but_not_grader_context(self):
        fixture = case(response_context=True)
        fixture["messages"] = [{"content": "start", "approvals": ["reject"],
                                "expected": {"response_context": True,
                                             "response_requirements": ["Explain cancellation."]}}]
        verification = ToolTrace("verify_order", {}, {"verified": True}, turn_index=0)
        rejection = ToolTrace("start_return", {}, "Do not retry this rejected tool call.",
                              call_id="rejected", turn_index=0)
        approval = ApprovalObservation(0, 0, 0, "reject", "rejected", 0, "approval")
        result = run([verification, rejection], approvals=[approval])
        result.turns = [TurnRun("start", "Cancelled.", result.tool_traces, [approval], 0, [], [])]
        with patch("evals.run_evals.grade_response", return_value=ResponseGrade(False, [])) as grader:
            grade_case(fixture, result)
        self.assertEqual(len(grader.call_args_list), 2)
        for invocation in grader.call_args_list:
            context = invocation.kwargs["context"]
            self.assertEqual([item["name"] for item in context["tool_results"]], ["verify_order"])
            self.assertEqual(context["approval_decisions"], ["reject"])
        self.assertEqual(result.tool_traces[1].output, "Do not retry this rejected tool call.")

    def test_suite_references_known_requirements_and_tools(self):
        names = {tool.name for tool in ciderlane_agent.tools}
        suite = load_eval_suite()
        covered = set()
        for item in suite["cases"]:
            expectations = [item["expected"]] + [
                turn.get("expected", {}) for turn in item["messages"] if isinstance(turn, dict)
            ]
            for expected in expectations:
                required = set(expected.get("required_tool_calls", []))
                forbidden = set(expected.get("forbidden_tool_calls", []))
                mentioned = (required | forbidden | set(expected.get("tool_sequence", []))
                             | set(expected.get("tool_expectations", {})))
                for field in ("first_tool_call", "first_business_tool_call"):
                    if expected.get(field):
                        mentioned.add(expected[field])
                self.assertLessEqual(mentioned, names, item["id"])
                self.assertFalse(required & forbidden, item["id"])
            self.assertLessEqual(set(item["requirements"]), REQUIREMENT_IDS)
            if item["id"] in {"repeated_verification_failure_creates_support_case",
                              "missing_verification_information_creates_support_case"}:
                self.assertNotIn("ESC-02", item["requirements"])
            covered.update(item["requirements"])
        self.assertEqual(covered, REQUIREMENT_IDS)

    def test_invalid_requirement_id_rejected_at_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps({"reference_date": "2026-08-31",
                                        "cases": [{"id": "bad", "requirements": ["UNKNOWN"]}]}))
            with patch("evals.run_evals.CASES_PATH", path):
                with self.assertRaisesRegex(ValueError, "invalid requirement IDs"):
                    load_eval_suite()

    def test_invalid_approval_script_rejected_instead_of_auto_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            for approvals in (["aprove"], "reject"):
                path.write_text(json.dumps({"reference_date": "2026-08-31", "cases": [{
                    "id": "bad-approval", "messages": [{"content": "start", "approvals": approvals}]}]}))
                with patch("evals.run_evals.CASES_PATH", path):
                    with self.assertRaisesRegex(ValueError, "invalid scripted turn"):
                        load_eval_suite()
