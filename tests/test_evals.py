import unittest
from unittest.mock import patch

from evals.run_evals import CaseRun, ResponseGrade, grade_case, load_eval_suite
from src.agent import ciderlane_agent


class TestEvalBoundary(unittest.TestCase):
    def test_cases_reference_only_current_tools(self):
        names = {tool.name for tool in ciderlane_agent.tools}
        for case in load_eval_suite()['cases']:
            expected = case['expected']
            required = set(expected['required_tool_calls'])
            forbidden = set(expected['forbidden_tool_calls'])
            self.assertLessEqual(required | forbidden, names)
            self.assertFalse(required & forbidden)

    def grade(self, calls, outputs, decision, label):
        case = {'expected': {
            'required_tool_calls': [], 'forbidden_tool_calls': [],
            'decision': decision, 'label_created': label,
            'human_review_required': False, 'response_requirements': [],
        }}
        with patch('evals.run_evals.grade_response',
                   return_value=ResponseGrade(False, [])):
            failures, _ = grade_case(case, CaseRun(calls, outputs, 'response'))
        self.assertEqual(failures, [])

    def test_grades_direct_return_without_eligibility_call(self):
        self.grade(['verify_order', 'start_return'], {
            'start_return': [{'status': 'return_created', 'decision': 'standard_return'}],
        }, 'standard_return', True)

    def test_grades_fresh_write_decision_over_old_eligibility(self):
        self.grade(['check_return_eligibility', 'start_return'], {
            'check_return_eligibility': [{'decision': 'standard_return'}],
            'start_return': [{'status': 'return_not_created', 'decision': 'blocked'}],
        }, 'blocked', False)

    def test_grades_read_only_question(self):
        self.grade(['verify_order', 'check_return_eligibility'], {
            'check_return_eligibility': [{'decision': 'standard_return'}],
        }, 'standard_return', False)

    def test_grades_existing_return_without_claiming_current_eligibility(self):
        self.grade(['verify_order', 'start_return'], {
            'start_return': [{'status': 'return_already_exists'}],
        }, None, True)
