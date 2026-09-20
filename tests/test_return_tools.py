import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from src.context import ReturnContext
from src.tools.return_tools import (
    check_return_eligibility,
    create_return_label,
    evaluate_return_policy,
)


POLICY_DATE = date(2026, 8, 31)


def delivered_days_ago(days):
    return (POLICY_DATE - timedelta(days=days)).isoformat()


def verified_context(order_id):
    return ReturnContext(verified_order_id=order_id)


class TestReturnPolicy(unittest.TestCase):
    def test_reference_date_override(self):
        with patch.dict(
            os.environ,
            {"CIDERLANE_REFERENCE_DATE": "2026-08-31"},
        ):
            result = check_return_eligibility(
                "it00004",
                context=verified_context("CL1002"),
            )

        self.assertEqual(result["decision"], "standard_return")

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


class TestCreateReturnLabel(unittest.TestCase):
    def test_standard_return_creates_label(self):
        with patch.dict(
            os.environ,
            {"CIDERLANE_REFERENCE_DATE": "2026-08-31"},
        ):
            result = create_return_label(
                "CL1002",
                "it00004",
                "standard_return",
                context=verified_context("CL1002"),
            )

        self.assertEqual(result["status"], "label_created")
        self.assertEqual(result["return_id"], "RET-CL1002-IT00004")
        self.assertEqual(
            result["label_url"],
            "https://mock.ciderlane.com/returns/RET-CL1002-IT00004/label",
        )

    def test_unknown_order_does_not_create_label(self):
        result = create_return_label(
            "NO_SUCH_ORDER",
            "NO_SUCH_ITEM",
            "standard_return",
            context=verified_context("NO_SUCH_ORDER"),
        )

        self.assertEqual(result["status"], "label_not_created")
        self.assertEqual(result["error"], "order_or_item_not_found")
        self.assertNotIn("label_url", result)

    def test_unknown_item_does_not_create_label(self):
        result = create_return_label(
            "CL1002",
            "NO_SUCH_ITEM",
            "standard_return",
            context=verified_context("CL1002"),
        )

        self.assertEqual(result["status"], "label_not_created")
        self.assertEqual(result["error"], "order_or_item_not_found")
        self.assertNotIn("label_url", result)

    def test_asserted_standard_return_cannot_override_policy(self):
        with patch.dict(
            os.environ,
            {"CIDERLANE_REFERENCE_DATE": "2026-08-31"},
        ):
            result = create_return_label(
                "CL1002",
                "it00003",
                "standard_return",
                context=verified_context("CL1002"),
            )

        self.assertEqual(result["status"], "label_not_created")
        self.assertEqual(result["error"], "item_not_eligible")
        self.assertNotIn("label_url", result)

    def test_asserted_standard_return_cannot_override_expired_window(self):
        with patch.dict(
            os.environ,
            {"CIDERLANE_REFERENCE_DATE": "2026-08-31"},
        ):
            result = create_return_label(
                "CL1001",
                "it00001",
                "standard_return",
                context=verified_context("CL1001"),
            )

        self.assertEqual(result["status"], "label_not_created")
        self.assertEqual(result["error"], "item_not_eligible")
        self.assertNotIn("label_url", result)

    def test_blocked_return_does_not_create_label(self):
        result = create_return_label(
            "CL1001",
            "it00001",
            "blocked",
            context=verified_context("CL1001"),
        )

        self.assertEqual(result["status"], "label_not_created")
        self.assertEqual(result["error"], "eligibility_decision_not_allowed")
        self.assertNotIn("label_url", result)

    def test_exception_flow_does_not_create_label(self):
        result = create_return_label(
            "CL1002",
            "it00002",
            "exception_flow",
            context=verified_context("CL1002"),
        )

        self.assertEqual(result["status"], "label_not_created")
        self.assertEqual(result["error"], "eligibility_decision_not_allowed")
        self.assertNotIn("label_url", result)
