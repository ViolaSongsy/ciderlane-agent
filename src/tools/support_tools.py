import sqlite3
from uuid import uuid4

from src.context import ReturnContext
from src.storage import connect_database, initialize_database


ALLOWED_ESCALATION_REASONS = {
    "return_exception",
    "final_sale_dispute",
    "customer_remains_angry",
    "verification_failed",
}

REQUIRED_ELIGIBILITY_DECISIONS = {
    "return_exception": "exception_flow",
    "final_sale_dispute": "blocked",
    "customer_remains_angry": "blocked",
}


def request_human_support(
    escalation_reason,
    summary,
    *,
    context: ReturnContext,
    database_path=None,
):
    """Authorize support using application-owned conversation provenance."""
    if escalation_reason not in ALLOWED_ESCALATION_REASONS:
        return {
            "status": "case_not_created",
            "error": "unsupported_escalation_reason",
        }

    if not isinstance(summary, str) or not summary.strip():
        return {
            "status": "case_not_created",
            "error": "summary_required",
        }

    if escalation_reason == "verification_failed":
        return create_support_case(
            conversation_id=context.conversation_id,
            escalation_reason=escalation_reason,
            summary=summary,
            database_path=database_path,
        )

    evaluation = context.current_evaluation
    if evaluation is None:
        return {
            "status": "case_not_created",
            "error": "trusted_evaluation_required",
        }

    if evaluation.order_id != context.verified_order_id:
        return {
            "status": "case_not_created",
            "error": "trusted_evaluation_order_mismatch",
        }

    required_decision = REQUIRED_ELIGIBILITY_DECISIONS[escalation_reason]
    if evaluation.decision != required_decision:
        return {
            "status": "case_not_created",
            "error": "eligibility_decision_not_allowed",
        }

    if (
        escalation_reason == "final_sale_dispute"
        and "final_sale" not in evaluation.policy_codes
    ):
        return {
            "status": "case_not_created",
            "error": "final_sale_policy_required",
        }

    return create_support_case(
        conversation_id=context.conversation_id,
        escalation_reason=escalation_reason,
        summary=summary,
        order_id=evaluation.order_id,
        item_id=evaluation.item_id,
        eligibility_decision=evaluation.decision,
        database_path=database_path,
    )


def create_support_case(
    escalation_reason,
    summary,
    order_id=None,
    item_id=None,
    eligibility_decision=None,
    database_path=None,
    *,
    conversation_id: str,
):
    if escalation_reason not in ALLOWED_ESCALATION_REASONS:
        return {
            "status": "case_not_created",
            "error": "unsupported_escalation_reason",
        }

    if not isinstance(summary, str) or not summary.strip():
        return {
            "status": "case_not_created",
            "error": "summary_required",
        }

    order_id = order_id.strip() if order_id else None
    item_id = item_id.strip() if item_id else None
    summary = summary.strip()

    if escalation_reason == "verification_failed":
        if item_id is not None or eligibility_decision is not None:
            return {
                "status": "case_not_created",
                "error": "unverified_order_details_not_allowed",
            }
    else:
        if order_id is None or item_id is None:
            return {
                "status": "case_not_created",
                "error": "verified_order_and_item_required",
            }

        required_decision = REQUIRED_ELIGIBILITY_DECISIONS[escalation_reason]
        if eligibility_decision != required_decision:
            return {
                "status": "case_not_created",
                "error": "eligibility_decision_not_allowed",
            }

    initialize_database(database_path)

    with connect_database(database_path) as connection:
        existing_case = connection.execute(
            """
            SELECT case_id, escalation_reason
            FROM support_cases
            WHERE conversation_id = ?
              AND escalation_reason = ?
              AND order_id IS ?
              AND item_id IS ?
              AND eligibility_decision IS ?
              AND status = 'open'
            """,
            (
                conversation_id,
                escalation_reason,
                order_id,
                item_id,
                eligibility_decision,
            ),
        ).fetchone()

        if existing_case is not None:
            return {
                "status": "case_already_exists",
                "case_id": existing_case["case_id"],
                "escalation_reason": existing_case["escalation_reason"],
            }

        case_id = f"CASE-{uuid4().hex[:12].upper()}"

        try:
            connection.execute(
                """
                INSERT INTO support_cases (
                    case_id,
                    conversation_id,
                    escalation_reason,
                    order_id,
                    item_id,
                    eligibility_decision,
                    summary
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    conversation_id,
                    escalation_reason,
                    order_id,
                    item_id,
                    eligibility_decision,
                    summary,
                ),
            )
        except sqlite3.IntegrityError:
            existing_case = connection.execute(
                """
                SELECT case_id, escalation_reason
                FROM support_cases
                WHERE conversation_id = ?
                  AND escalation_reason = ?
                  AND order_id IS ?
                  AND item_id IS ?
                  AND eligibility_decision IS ?
                  AND status = 'open'
                """,
                (
                    conversation_id,
                    escalation_reason,
                    order_id,
                    item_id,
                    eligibility_decision,
                ),
            ).fetchone()

            if existing_case is None:
                raise

            return {
                "status": "case_already_exists",
                "case_id": existing_case["case_id"],
                "escalation_reason": existing_case["escalation_reason"],
            }

    return {
        "status": "case_created",
        "case_id": case_id,
        "escalation_reason": escalation_reason,
    }
