import unittest

from src.tools.order_tools import get_order


class TestGetOrder(unittest.TestCase):
    def test_verified_order(self):
        result = get_order("CL1001", "maya@example.com")

        self.assertTrue(result["verified"])
        self.assertEqual(result["order_id"], "CL1001")

    def test_mismatched_email(self):
        result = get_order("CL1001", "abc@example.com")

        self.assertFalse(result["verified"])
        self.assertEqual(result["error"], "email_mismatch")
        self.assertNotIn("items", result)

    def test_unverified_order(self):
        result = get_order("CL100001", "maya@example.com")

        self.assertFalse(result["verified"])
        self.assertEqual(result["error"], "order_not_found")
        self.assertNotIn("items", result)