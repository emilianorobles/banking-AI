"""Resolving an alert -- the human checkpoint, and the learning loop's entry point.

This lived inside `web/routes/admin.py::resolve` and had exactly one caller. It now has
two: that route, and the analyst assistant's `resolve_alert` tool. A second copy was never
an option -- this function writes the case that goes back into FAISS, which is the climax
of the demo, and two implementations of it would drift in the worst possible place.

Import-safe and UI-free, like everything else in `core/`: it returns a result dict and
lets the caller decide whether that becomes a flash message or a chat reply.
"""

from __future__ import annotations

from typing import Any

from . import db, notifications as notif, rag

VALID_OUTCOMES = ("confirmed_fraud", "false_positive")

DEFAULT_NOTES = {
    "confirmed_fraud": "Confirmed fraudulent by analyst review.",
    "false_positive": "Cleared by analyst; legitimate customer activity.",
}


def resolve_alert(alert_id: str, outcome: str, note: str = "",
                  actor: str = "analyst:ops") -> dict[str, Any]:
    """Resolve one pending alert.

    Returns `{"ok": bool, "error": str, ...}` rather than raising, because both callers
    want to report the failure rather than 500 -- a route mid-demo and a chat turn that
    must still produce a sentence.

    Order matters and is deliberate: index the case FIRST, then unfreeze, then resolve.
    Indexing is the only step that can fail for an interesting reason (the embedding
    endpoint), and it is better to have an unindexed resolution than a card left frozen
    because the index was down.
    """
    outcome = (outcome or "").strip().lower()
    if outcome not in VALID_OUTCOMES:
        return {"ok": False,
                "error": f"Outcome must be one of {', '.join(VALID_OUTCOMES)}."}

    alert = db.get_alert(alert_id)
    if alert is None:
        return {"ok": False, "error": f"No alert found with ID {alert_id}."}
    if alert.status != "PENDING":
        return {"ok": False,
                "error": f"{alert_id} has already been resolved ({alert.status})."}

    txn = db.get_transaction(alert.txn_id)
    note = (note or "").strip()
    narrative = note or DEFAULT_NOTES[outcome]

    case_id = None
    index_error = None
    if txn is not None:
        try:
            case = rag.learn_from_alert(alert, txn, outcome, narrative, actor)
            case_id = case.case_id
        except Exception as exc:            # noqa: BLE001 -- reported, never fatal
            index_error = str(exc)

    if outcome == "false_positive":
        db.set_card_frozen(alert.customer_id, False)

    db.resolve_alert(alert_id, outcome, resolved_by=actor, note=note,
                     learned_case_id=case_id)
    db.audit(
        actor=actor, event_type="APPROVAL", subject_id=alert_id,
        detail=(f"{outcome}; "
                f"{'freeze upheld' if outcome == 'confirmed_fraud' else 'card unfrozen'}"
                f"; learned {case_id}"),
    )

    _notify(alert, outcome, note)

    return {
        "ok": True,
        "alert_id": alert_id,
        "txn_id": alert.txn_id,
        "customer_id": alert.customer_id,
        # So `tools._notify_customer` alerts the customer the ALERT belongs to, not
        # whichever account the analyst happened to be viewing.
        "notify_customer_id": alert.customer_id,
        "outcome": outcome,
        "learned_case_id": case_id,
        "index_error": index_error,
        "card_unfrozen": outcome == "false_positive",
        "message": (
            ("Confirmed as fraud; the freeze stands."
             if outcome == "confirmed_fraud" else
             "Cleared as a false positive; the card has been unfrozen.")
            + (f" Indexed as {case_id}, retrievable immediately." if case_id else "")
        ),
    }


def _notify(alert, outcome: str, note: str) -> None:
    """The customer hears about it either way.

    An analyst clearing a freeze without telling anyone leaves the customer still
    believing their card is dead. Never raises: a notification failure must not undo a
    resolution that has already been written.
    """
    try:
        if outcome == "confirmed_fraud":
            notif.notify(
                alert.customer_id, kind="fraud_alert", severity="danger",
                subject="Fraud confirmed on your card",
                body="Our fraud team reviewed the transaction we stopped and confirmed "
                     "it was not you. Your card stays frozen and a replacement is on "
                     "its way.",
                detail=note, related_id=alert.alert_id)
        else:
            notif.notify(
                alert.customer_id, kind="fraud_alert", severity="ok",
                subject="Your card is active again",
                body="Our fraud team reviewed the transaction we flagged and confirmed "
                     "it was genuine. Your card has been unfrozen.",
                detail=note, related_id=alert.alert_id)
    except Exception:                       # noqa: BLE001
        pass
