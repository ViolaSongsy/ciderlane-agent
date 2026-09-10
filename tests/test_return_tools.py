import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from src.tools.return_tools import check_return_eligibility, create_return_label


def delivered_days_ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


class TestCheckReturnEligibility(unittest.TestCase):
    def test_reference_date_override(self):
        with patch.dict(
            os.environ,
            {"CIDERLANE_REFERENCE_DATE": "2026-08-31"},
        ):
            result = check_return_eligibility(
                "2026-08-24",
                "dress",
                False,
            )

        self.assertEqual(result["decision"], "standard_return")

    def test_standard_return_eligibility(self):
        result = check_return_eligibility(
            delivered_days_ago(1),
            "dress",
            False,
        )

        self.assertEqual(result["decision"], "standard_return")
        self.assertEqual(
            result["reasons"],
            [
                "The item is within the 30-day return window.",
                "The item is not final sale.",
                "The item is not restricted swimwear.",
            ],
        )

    def test_swimwear_eligibility(self):
        result = check_return_eligibility(
            delivered_days_ago(4),
            "swimwear",
            True,
        )

        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(
            result["reasons"],
            ["Swimwear cannot be returned unless it arrived damaged or wrong."],
        )

    def test_final_sale_eligibility(self):
        result = check_return_eligibility(
            delivered_days_ago(4),
            "pants",
            True,
        )

        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(
            result["reasons"],
            ["Final sale items cannot be returned unless they arrived damaged or wrong."],
        )

    def test_final_sale_damaged_eligibility(self):
        result = check_return_eligibility(
            delivered_days_ago(4),
            "pants",
            True,
            "damaged",
        )

        self.assertEqual(result["decision"], "exception_flow")
        self.assertEqual(
            result["reasons"],
            ["Damaged or wrong items can enter exception flow even if normally restricted."],
        )

    def test_swimwear_wrong_item_eligibility(self):
        result = check_return_eligibility(
            delivered_days_ago(4),
            "swimwear",
            True,
            "wrong_item",
        )

        self.assertEqual(result["decision"], "exception_flow")
        self.assertEqual(
            result["reasons"],
            ["Damaged or wrong items can enter exception flow even if normally restricted."],
        )

    def test_regular_damaged_item_uses_standard_return(self):
        result = check_return_eligibility(
            delivered_days_ago(4),
            "dress",
            False,
            "damaged",
        )

        self.assertEqual(result["decision"], "standard_return")
        self.assertNotIn("escalation", " ".join(result["reasons"]).lower())

    def test_return_on_day_30_is_eligible(self):
        result = check_return_eligibility(
            delivered_days_ago(30),
            "dress",
            False,
        )

        self.assertEqual(result["decision"], "standard_return")

    def test_return_after_day_30_is_blocked(self):
        result = check_return_eligibility(
            delivered_days_ago(31),
            "dress",
            False,
        )

        self.assertEqual(result["decision"], "blocked")

    def test_expired_damaged_item_is_blocked(self):
        result = check_return_eligibility(
            delivered_days_ago(31),
            "pants",
            True,
            "damaged",
        )

        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(
            result["reasons"],
            ["The item is outside the 30-day return window."],
        )


class TestCreateReturnLabel(unittest.TestCase):
    def test_standard_return_creates_label(self):
        result = create_return_label(
            "CL1001",
            "it00001",
            "standard_return",
        )

        self.assertEqual(result["status"], "label_created")
        self.assertEqual(result["return_id"], "RET-CL1001-IT00001")
        self.assertEqual(
            result["label_url"],
            "https://mock.ciderlane.com/returns/RET-CL1001-IT00001/label",
        )

    def test_blocked_return_does_not_create_label(self):
        result = create_return_label(
            "CL1001",
            "it00001",
            "blocked",
        )

        self.assertEqual(result["status"], "label_not_created")
        self.assertEqual(result["error"], "eligibility_decision_not_allowed")
        self.assertNotIn("label_url", result)

    def test_exception_flow_does_not_create_label(self):
        result = create_return_label(
            "CL1002",
            "it00002",
            "exception_flow",
        )

        self.assertEqual(result["status"], "label_not_created")
        self.assertEqual(result["error"], "eligibility_decision_not_allowed")
        self.assertNotIn("label_url", result)
