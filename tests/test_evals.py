import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.items import ToolCallItem, ToolCallOutputItem

from evals.run_evals import (
    ApprovalObservation, CaseRun, REQUIREMENT_IDS, ResponseGrade, ToolTrace,
    collect_run_items, grade_case, grade_requirements, load_eval_suite,
    matching_arguments, parse_tool_arguments, run_case,
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


class TestEvalHarness(unittest.TestCase):
    def test_approval_observation_and_case_database_isolation(self):
        approval = SimpleNamespace(tool_name="start_return")
        state = SimpleNamespace(approve=lambda _: None)
        def first_result(*_args, **_kwargs):
            return SimpleNamespace(interruptions=[approval], to_state=lambda: state)
        def resumed_result(*_args, **_kwargs):
            with connect_database() as connection:
                connection.execute("INSERT INTO returns(return_id,order_id,item_id,label_url) VALUES ('R','O','I','U')")
            return SimpleNamespace(interruptions=[], new_items=[], final_output="done",
                                   to_input_list=lambda: [])
        responses = iter((first_result, resumed_result))
        with patch("evals.run_evals.Runner.run_sync",
                   side_effect=lambda *args, **kwargs: next(responses)(*args, **kwargs)):
            first = run_case({"messages": ["start"]})
        self.assertEqual(first.approval_observations, [ApprovalObservation(0, 0)])
        self.assertEqual(len(first.returns_rows), 1)
        with patch("evals.run_evals.Runner.run_sync", return_value=SimpleNamespace(
                interruptions=[], new_items=[], final_output="done", to_input_list=lambda: [])):
            second = run_case({"messages": ["hello"]})
        self.assertEqual(second.returns_rows, [])

    def test_proposal_write_stops_before_approval(self):
        approval = SimpleNamespace(tool_name="start_return")
        state = SimpleNamespace(approve=lambda _: self.fail("approval must not happen"))
        def dirty_result(*_args, **_kwargs):
            with connect_database() as connection:
                connection.execute("INSERT INTO returns(return_id,order_id,item_id,label_url) VALUES ('R','O','I','U')")
            return SimpleNamespace(interruptions=[approval], to_state=lambda: state)
        with patch("evals.run_evals.Runner.run_sync", side_effect=dirty_result):
            with self.assertRaisesRegex(AssertionError, "before approval"):
                run_case({"messages": ["start"]})

    def test_suite_references_known_requirements_and_tools(self):
        names = {tool.name for tool in ciderlane_agent.tools}
        suite = load_eval_suite()
        covered = set()
        for item in suite["cases"]:
            expected = item["expected"]
            required = set(expected["required_tool_calls"])
            forbidden = set(expected["forbidden_tool_calls"])
            self.assertLessEqual(required | forbidden | set(expected["tool_sequence"]), names)
            self.assertFalse(required & forbidden)
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
