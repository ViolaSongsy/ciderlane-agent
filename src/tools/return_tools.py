from datetime import datetime


def check_return_eligibility(
    delivered_date,
    category,
    final_sale,
    reason=None,
):
    today = datetime.now().date()

    delivery_date = datetime.strptime(delivered_date, "%Y-%m-%d").date()

    difference = today - delivery_date

    days_difference = difference.days

    response_reasons = []
    decision = ''

    if days_difference > 30:
        decision = "blocked"
        response_reasons.append("The item is outside the 30-day return window.")

        return {"decision": decision, "reasons":response_reasons}

    if (category == "swimwear" or final_sale) and reason in {
        "damaged",
        "wrong_item",
    }:
        decision = 'exception_flow'
        response_reasons.append("Damaged or wrong items can enter exception flow even if normally restricted.")

    elif category == 'swimwear':
        decision = 'blocked'
        response_reasons.append("Swimwear cannot be returned unless it arrived damaged or wrong.")

    elif final_sale and category != "swimwear":
        decision = 'blocked'
        response_reasons.append("Final sale items cannot be returned unless they arrived damaged or wrong.")

    if not final_sale and category != "swimwear" and days_difference<=30:
        decision = 'standard_return'
        response_reasons.append("The item is within the 30-day return window.")
        response_reasons.append("The item is not final sale.")
        response_reasons.append("The item is not restricted swimwear.")

    return {"decision": decision, "reasons":response_reasons}


def create_return_label(
    order_id,
    item_id,
    eligibility_decision,
):
    if eligibility_decision != "standard_return":
        return {
            "status": "label_not_created",
            "error": "eligibility_decision_not_allowed",
        }

    return_id = f"RET-{order_id}-{item_id}".upper()

    return {
        "status": "label_created",
        "return_id": return_id,
        "label_url": f"https://mock.ciderlane.com/returns/{return_id}/label",
    }
