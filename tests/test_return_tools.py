import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from src.context import ReturnContext
from src.tools.return_tools import (
    check_return_eligibility,
    evaluate_return_policy,
)


POLICY_DATE = date(2026, 8, 31)


def delivered_days_ago(days):
    return (POLICY_DATE - timedelta(days=days)).isoformat()


def verified_context(order_id):
    return ReturnContext(verified_order_id=order_id)


class TestReturnPolicy(unittest.TestCase):
    def test_reference_date_override(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        with patch.dict(
            os.environ,
            {"CIDERLANE_REFERENCE_DATE": "2026-08-31",
             "CIDERLANE_DATABASE_PATH": str(Path(directory) / "policy.db")},
        ):
            result = check_return_eligibility(
                "it00004",
                context=verified_context("CL1002"),
            )

        self.assertEqual(result["decision"], "standard_return")
        self.assertEqual(result["policy_codes"], [])

    def test_standard_return_eligibility(self):
        result = evaluate_return_policy(
            delivered_days_ago(1),
            "dress",
            False,
            today=POLICY_DATE,
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
        result = evaluate_return_policy(
            delivered_days_ago(4),
            "swimwear",
            True,
            today=POLICY_DATE,
        )

        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(
            result["reasons"],
            ["Swimwear cannot be returned unless it arrived damaged or wrong."],
        )
        self.assertEqual(
            result["policy_codes"],
            ["restricted_swimwear"],
        )

    def test_final_sale_eligibility(self):
        result = evaluate_return_policy(
            delivered_days_ago(4),
            "pants",
            True,
            today=POLICY_DATE,
        )

        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(
            result["reasons"],
            ["Final sale items cannot be returned unless they arrived damaged or wrong."],
        )
        self.assertEqual(result["policy_codes"], ["final_sale"])

    def test_final_sale_damaged_eligibility(self):
        result = evaluate_return_policy(
            delivered_days_ago(4),
            "pants",
            True,
            "damaged",
            today=POLICY_DATE,
        )

        self.assertEqual(result["decision"], "exception_flow")
        self.assertEqual(
            result["reasons"],
            ["Damaged or wrong items can enter exception flow even if normally restricted."],
        )
        self.assertEqual(
            result["policy_codes"],
            ["final_sale", "damaged_item_exception"],
        )

    def test_swimwear_wrong_item_eligibility(self):
        result = evaluate_return_policy(
            delivered_days_ago(4),
            "swimwear",
            True,
            "wrong_item",
            today=POLICY_DATE,
        )

        self.assertEqual(result["decision"], "exception_flow")
        self.assertEqual(
            result["reasons"],
            ["Damaged or wrong items can enter exception flow even if normally restricted."],
        )
        self.assertEqual(
            result["policy_codes"],
            ["restricted_swimwear", "wrong_item_exception"],
        )

    def test_regular_damaged_item_uses_standard_return(self):
        result = evaluate_return_policy(
            delivered_days_ago(4),
            "dress",
            False,
            "damaged",
            today=POLICY_DATE,
        )

        self.assertEqual(result["decision"], "standard_return")
        self.assertNotIn("escalation", " ".join(result["reasons"]).lower())

    def test_return_on_day_30_is_eligible(self):
        result = evaluate_return_policy(
            delivered_days_ago(30),
            "dress",
            False,
            today=POLICY_DATE,
        )

        self.assertEqual(result["decision"], "standard_return")

    def test_return_after_day_30_is_blocked(self):
        result = evaluate_return_policy(
            delivered_days_ago(31),
            "dress",
            False,
            today=POLICY_DATE,
        )

        self.assertEqual(result["decision"], "blocked")

    def test_expired_damaged_item_is_blocked(self):
        result = evaluate_return_policy(
            delivered_days_ago(31),
            "pants",
            True,
            "damaged",
            today=POLICY_DATE,
        )

        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(
            result["reasons"],
            ["The item is outside the 30-day return window."],
        )
        self.assertEqual(result["policy_codes"], ["outside_return_window"])
