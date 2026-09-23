import os
import sqlite3
from datetime import date, datetime
from typing import Literal

from src.storage import connect_database, initialize_database
from src.context import ReturnContext, ReturnEvaluation
from src.tools.order_tools import load_orders


REFERENCE_DATE_ENV_VAR = "CIDERLANE_REFERENCE_DATE"
ReturnReason = Literal["damaged", "wrong_item", "other"]


def _current_date():
    reference_date = os.getenv(REFERENCE_DATE_ENV_VAR)

    if reference_date:
        return datetime.strptime(reference_date, "%Y-%m-%d").date()

    return datetime.now().date()


def check_return_eligibility(
    item_id: str,
    reason: ReturnReason | None = None,
    *,
    context: ReturnContext | None = None,
) -> dict:
    """Check an item using only the verified order's stored eligibility facts."""
    if context is not None:
        context.current_evaluation = None

    if context is None or not context.verified_order_id:
        return {
            "status": "eligibility_not_checked",
            "error": "order_not_verified",
        }

    if reason not in (None, "damaged", "wrong_item", "other"):
        return {
            "status": "eligibility_not_checked",
            "error": "invalid_reason",
        }

    matched_order = next(
        (order for order in load_orders()
         if order["order_id"] == context.verified_order_id),
        None,
    )
    if matched_order is None:
        return {
            "status": "eligibility_not_checked",
            "error": "order_or_item_not_found",
        }

    matched_item = next(
        (item for item in matched_order["items"] if item["item_id"] == item_id),
        None,
    )
    if matched_item is None:
        return {
            "status": "eligibility_not_checked",
            "error": "order_or_item_not_found",
        }

    result = evaluate_return_policy(
        matched_order["delivered_date"],
        matched_item["category"],
        matched_item["final_sale"],
        reason,
        today=_current_date(),
    )
    context.current_evaluation = ReturnEvaluation(
        order_id=matched_order["order_id"],
        item_id=matched_item["item_id"],
        decision=result["decision"],
        policy_codes=tuple(result["policy_codes"]),
    )
    return result


def evaluate_return_policy(
    delivered_date,
    category,
    final_sale,
    reason: ReturnReason | None = None,
    *,
    today: date,
):
    """Apply deterministic policy to trusted facts without reading state."""
    delivery_date = datetime.strptime(delivered_date, "%Y-%m-%d").date()

    difference = today - delivery_date

    days_difference = difference.days

    response_reasons = []
    policy_codes = []
    decision = ''

    if days_difference > 30:
        decision = "blocked"
        response_reasons.append("The item is outside the 30-day return window.")
        policy_codes.append("outside_return_window")

        return {
            "decision": decision,
            "reasons": response_reasons,
            "policy_codes": policy_codes,
        }

    if (category == "swimwear" or final_sale) and reason in {
        "damaged",
        "wrong_item",
    }:
        decision = 'exception_flow'
        response_reasons.append("Damaged or wrong items can enter exception flow even if normally restricted.")
        if category == "swimwear":
            policy_codes.append("restricted_swimwear")
        else:
            policy_codes.append("final_sale")
        if reason == "damaged":
            policy_codes.append("damaged_item_exception")
        else:
            policy_codes.append("wrong_item_exception")

    elif category == 'swimwear':
        decision = 'blocked'
        response_reasons.append("Swimwear cannot be returned unless it arrived damaged or wrong.")
        policy_codes.append("restricted_swimwear")

    elif final_sale and category != "swimwear":
        decision = 'blocked'
        response_reasons.append("Final sale items cannot be returned unless they arrived damaged or wrong.")
        policy_codes.append("final_sale")

    if not final_sale and category != "swimwear" and days_difference<=30:
        decision = 'standard_return'
        response_reasons.append("The item is within the 30-day return window.")
        response_reasons.append("The item is not final sale.")
        response_reasons.append("The item is not restricted swimwear.")

    return {
        "decision": decision,
        "reasons": response_reasons,
        "policy_codes": policy_codes,
    }


def start_return(
    item_id: str,
    reason: ReturnReason | None = None,
    *,
    context: ReturnContext | None = None,
    database_path=None,
) -> dict:
    """Independently authorize a new return, or retrieve a persisted return."""
    if context is not None:
        context.current_evaluation = None

    if context is None or not context.verified_order_id:
        return {"status": "return_not_created", "error": "order_not_verified"}
    if reason not in (None, "damaged", "wrong_item", "other"):
        return {"status": "return_not_created", "error": "invalid_reason"}

    order_id = context.verified_order_id
    order = next((order for order in load_orders()
                  if order["order_id"] == order_id), None)
    item = next((item for item in order["items"]
                 if item["item_id"] == item_id), None) if order else None
    if item is None:
        return {"status": "return_not_created", "error": "order_or_item_not_found"}

    initialize_database(database_path)
    lookup = "SELECT * FROM returns WHERE order_id = ? AND item_id = ?"
    with connect_database(database_path) as connection:
        existing = connection.execute(lookup, (order_id, item_id)).fetchone()
        if existing is not None:
            return {"status": "return_already_exists", **dict(existing)}

        result = evaluate_return_policy(
            order["delivered_date"], item["category"], item["final_sale"],
            reason, today=_current_date(),
        )
        context.current_evaluation = ReturnEvaluation(
            order_id=order_id,
            item_id=item_id,
            decision=result["decision"],
            policy_codes=tuple(result["policy_codes"]),
        )
        if result["decision"] != "standard_return":
            return {"status": "return_not_created", **result}

        return_id = f"RET-{order_id}-{item_id}".upper()
        label_url = f"https://mock.ciderlane.com/returns/{return_id}/label"
        try:
            connection.execute(
                "INSERT INTO returns (return_id, order_id, item_id, label_url) "
                "VALUES (?, ?, ?, ?)",
                (return_id, order_id, item_id, label_url),
            )
        except sqlite3.IntegrityError:
            existing = connection.execute(lookup, (order_id, item_id)).fetchone()
            if existing is None:
                raise
            return {"status": "return_already_exists", **dict(existing)}

        saved = connection.execute(lookup, (order_id, item_id)).fetchone()
    return {"status": "return_created", **result, **dict(saved)}
