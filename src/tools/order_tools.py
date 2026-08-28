import json
from pathlib import Path


ORDERS_PATH = Path(__file__).parents[2] / "data" / "orders.json"


def load_orders():
    with ORDERS_PATH.open("r") as file:
        return json.load(file)
    
def get_order(order_id, email):
    orders = load_orders()
    matched_order = None

    for order in orders:
        if order["order_id"] == order_id:
            matched_order = order
            break

    if matched_order is None:
        return {
            "verified": False,
            "error": "order_not_found",
        }

    if matched_order["email"] != email:
        return {
            "verified": False,
            "error": "email_mismatch",
        }

    return {
        "verified": True,
        "order_id": matched_order["order_id"],
        "delivered_date": matched_order["delivered_date"],
        "items": matched_order["items"],
    }