"""Moving money -- and screening it before it moves.

The whole point of this module is the ordering. A transfer is built, scored by the fraud
pipeline, and only then settled. The assistant cannot move money that the bank's own fraud
engine has not passed, which is what makes "the AI can send money" a safe sentence to say
on stage rather than a reckless one.

    build Transaction -> pipeline.score_transaction -> settle | hold | block

`score_transaction` is the same function the card switch calls through
`POST /api/transactions`. Nothing here re-implements a rule, a threshold or an alert: a
transfer is screened by exactly the machinery every other transaction is screened by, and
that is the claim worth making.

**First-time beneficiary is expressed as a merchant category, not a new rule.** The bank's
risk taxonomy already declares `wire_transfer` and `prepaid_reload` high-risk
(`rules.HIGH_RISK_CATEGORIES`), and a first payment to a brand-new payee is precisely what
that category exists to describe. Adding a rule would mean RULE_META/EMITS/selfcheck churn
and would put a new number into published evaluation figures, for a signal the engine can
already express.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import db, money, notifications, pipeline
from .contracts import Channel, Transaction, new_id

# What a payment of each kind looks like to the fraud rules.
#
# The pair on the left is (kind, is_first_time). "First time" means a payee this customer
# has never successfully paid before -- the classic authorised-push-payment signal, and
# the one the seeded RAG corpus already has a narrative for.
CATEGORY: dict[tuple[str, bool], str] = {
    ("transfer", True): "wire_transfer",     # high-risk: new beneficiary
    ("transfer", False): "transfer",         # benign: someone they pay regularly
    ("topup", True): "prepaid_reload",       # high-risk: classic cash-out route
    ("topup", False): "telecom",
    ("tax", True): "government",             # benign either way: the payee is the state
    ("tax", False): "government",
}

KIND_LABEL = {"transfer": "Transfer", "topup": "Phone top-up", "tax": "Tax payment"}

# Terminal states, and what the customer is told for each.
SETTLED, HELD, BLOCKED = "settled", "pending", "blocked"


def _currency_for(customer_id: str) -> str:
    recent = db.recent_transactions(customer_id, limit=1)
    return recent[0].currency if recent else "INR"


def submit(ctx, *, kind: str, amount: Any, destination: str,
           payee: dict[str, Any] | None = None, reference: str = "") -> dict[str, Any]:
    """Screen and settle one money movement.

    `ctx` is an `AgentContext`; taken untyped to keep this module importable from the
    pipeline side without a circular import back into `core.agents`.
    """
    customer_id = ctx.customer_id
    customer = db.get_customer(customer_id)
    if customer is None:
        return {"error": "Account not found."}

    try:
        value = round(float(amount), 2)
    except (TypeError, ValueError):
        return {"error": "How much would you like to send?"}
    if value <= 0:
        return {"error": "The amount needs to be more than zero."}

    currency = _currency_for(customer_id)

    # A frozen card means the account is already under suspicion. Refusing here rather
    # than letting the pipeline score it keeps the reason honest: the customer is told
    # their card is frozen, not handed a risk score for a payment that was never going
    # to leave.
    if customer.card_frozen:
        return {"error": ("Your card is frozen, so I can't move money right now. "
                          "Unfreeze it first, or confirm the alert on your account.")}

    if value > customer.balance:
        return {"error": (f"That's more than your balance of "
                          f"{money.fmt(customer.balance, currency)}.")}

    first_time = not (payee and int(payee.get("transfer_count") or 0) > 0)
    category = CATEGORY[(kind, first_time)]

    txn = Transaction(
        txn_id=new_id("TXN"),
        customer_id=customer_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        amount=value,
        currency=currency,
        # Customer-supplied text. It is stored and shown, never handed to a model
        # unwrapped -- `pipeline` already treats `merchant` as untrusted data.
        merchant=str(destination)[:80],
        merchant_category=category,
        country=customer.home_country,
        city=customer.home_city,
        region=customer.region,
        channel=Channel.TRANSFER.value,
        card_last4=customer.card_number[-4:],
    )

    decision = pipeline.score_transaction(txn, persist=True)

    transfer_id = new_id("XFER")
    status = {"ALLOW": SETTLED}.get(decision.action,
                                    HELD if decision.action == "CHALLENGE" else BLOCKED)

    db.save_transfer(
        transfer_id=transfer_id, txn_id=txn.txn_id, customer_id=customer_id,
        payee_id=(payee or {}).get("payee_id"), amount=value, currency=currency,
        kind=kind, reference=reference, destination=str(destination)[:80],
        status=status, risk_score=decision.risk_score, action=decision.action,
    )

    if status == SETTLED:
        _settle(transfer_id, customer_id, payee, value, currency)

    db.audit(actor=ctx.actor, event_type="MONEY_MOVEMENT", subject_id=txn.txn_id,
             detail=(f"{KIND_LABEL.get(kind, kind)} of {value:,.2f} {currency} to "
                     f"{destination}: {decision.action} at risk {decision.risk_score}"),
             kind=kind, status=status, first_time_payee=first_time,
             transfer_id=transfer_id)

    return {
        "confirmed": status == SETTLED,
        "held": status == HELD,
        "blocked": status == BLOCKED,
        "transfer_id": transfer_id,
        "txn_id": txn.txn_id,
        "kind": kind,
        "destination": str(destination)[:80],
        "amount_value": value,
        "currency": currency,
        # ASCII, because this string is prompt text -- see the note on
        # `list_recent_transactions`. Customer-facing paths format from amount_value.
        "amount": f"{value:,.2f} {currency}",
        "reference": reference,
        "first_time_payee": first_time,
        "screening": {
            "action": decision.action,
            "risk_score": decision.risk_score,
            "risk_level": decision.risk_level,
            "rules_fired": [h.reason if hasattr(h, "reason") else str(h)
                            for h in decision.rule_hits],
            "checked_by_model": decision.llm_used,
        },
        "new_balance": (db.get_customer(customer_id).balance
                        if status == SETTLED else customer.balance),
        "message": _outcome_message(status, kind, value, currency,
                                    str(destination), decision),
    }


def _settle(transfer_id: str, customer_id: str, payee: dict[str, Any] | None,
            value: float, currency: str) -> float:
    """Debit the sender, credit an internal payee, and mark the payee used.

    Only ever reached once per transfer: `settle_if_held` checks the status first, so a
    customer confirming the same alert twice cannot double-debit.
    """
    new_balance = db.adjust_balance(customer_id, -value)

    if payee and payee.get("kind") == "internal" and payee.get("internal_customer_id"):
        # An internal transfer is visibly real: the money arrives in another seeded
        # account rather than evaporating.
        try:
            db.adjust_balance(str(payee["internal_customer_id"]), value)
        except ValueError:
            pass
    if payee and payee.get("payee_id"):
        db.mark_payee_used(str(payee["payee_id"]))

    db.set_transfer_status(transfer_id, SETTLED, settled=True)
    return new_balance


def settle_if_held(txn_id: str, actor: str = "system") -> dict[str, Any] | None:
    """Release money the fraud engine held, once a human has cleared it.

    Called from both resolution paths -- the customer answering "yes, that was me" on
    their own alert, and an analyst clearing it as a false positive. That is the loop
    closing: the engine holds it, a person clears it, the money moves, and the resolution
    is embedded into FAISS by the same action.

    Returns None when there is nothing held for this transaction, which is the common
    case -- most alerts are about card transactions, not transfers.
    """
    transfer = db.get_transfer_for_txn(txn_id)
    if transfer is None or transfer.get("status") != HELD:
        return None

    payee = db.get_payee(transfer["payee_id"]) if transfer.get("payee_id") else None
    customer_id = transfer["customer_id"]
    value = float(transfer["amount"] or 0.0)
    currency = transfer.get("currency") or "INR"

    customer = db.get_customer(customer_id)
    if customer is None or customer.balance < value:
        db.set_transfer_status(transfer["transfer_id"], BLOCKED)
        db.audit(actor=actor, event_type="MONEY_MOVEMENT", subject_id=txn_id,
                 detail="Held transfer could not settle: insufficient balance")
        return {"settled": False, "reason": "insufficient_balance",
                "transfer_id": transfer["transfer_id"]}

    new_balance = _settle(transfer["transfer_id"], customer_id, payee, value, currency)
    db.audit(actor=actor, event_type="MONEY_MOVEMENT", subject_id=txn_id,
             detail=(f"Held {transfer.get('kind')} of {value:,.2f} {currency} to "
                     f"{transfer.get('destination')} settled after human confirmation"),
             transfer_id=transfer["transfer_id"])

    try:
        notifications.notify(
            customer_id, kind="transfer_settled", severity="ok",
            subject=f"{KIND_LABEL.get(transfer.get('kind'), 'Payment')} sent",
            body=(f"You confirmed the {money.fmt(value, currency)} payment to "
                  f"{transfer.get('destination')}, so we've released it. Your balance is "
                  f"now {money.fmt(new_balance, currency)}."),
            related_id=transfer["transfer_id"])
    except Exception:                       # noqa: BLE001 -- never undo a settlement
        pass

    return {"settled": True, "transfer_id": transfer["transfer_id"],
            "amount_value": value, "currency": currency, "new_balance": new_balance,
            "destination": transfer.get("destination")}


def cancel_if_held(txn_id: str, actor: str = "system") -> dict[str, Any] | None:
    """Drop a held transfer when the customer or an analyst says it was fraud."""
    transfer = db.get_transfer_for_txn(txn_id)
    if transfer is None or transfer.get("status") != HELD:
        return None
    db.set_transfer_status(transfer["transfer_id"], BLOCKED)
    db.audit(actor=actor, event_type="MONEY_MOVEMENT", subject_id=txn_id,
             detail="Held transfer cancelled after fraud confirmation",
             transfer_id=transfer["transfer_id"])
    return {"settled": False, "cancelled": True,
            "transfer_id": transfer["transfer_id"]}


def _outcome_message(status: str, kind: str, value: float, currency: str,
                     destination: str, decision) -> str:
    label = KIND_LABEL.get(kind, "Payment")
    amount = money.fmt(value, currency)
    if status == SETTLED:
        return f"{label} of {amount} to {destination} has gone through."
    if status == HELD:
        return (f"{label} of {amount} to {destination} scored "
                f"{decision.risk_score}/100 and is on hold. The money has NOT left your "
                f"account. Confirm it on your Alerts page and I'll release it.")
    return (f"{label} of {amount} to {destination} scored {decision.risk_score}/100 and "
            f"was stopped. Nothing has left your account and your card is frozen while "
            f"we check with you.")
