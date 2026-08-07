"""Transaction ingestion — the live-demo injection point.

Mirrors `POST /api/transactions` from `api/main.py` so the whole demo runs in one process:
POST from a phone on the same wifi and the analyst dashboard lights up. The FastAPI
service still exists and still works; it is the same `core.pipeline.score_transaction`
behind both, which is the point.

Unauthenticated, like the FastAPI one, because it stands in for a transaction switch
rather than a person. It cannot read anything back -- it scores what it is given.
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from core import config, db, llm, pipeline, rag
from core.contracts import Transaction, new_id

bp = Blueprint("ingest", __name__)


@bp.get("/health")
def health():
    """Check this before a demo. Everything that can be dark shows up here."""
    return jsonify({
        "status": "ok",
        "provider": llm.health_check(),
        "mode": config.DEMO_MODE,
        "index_cases": rag.index_size(),
        "counters": {
            "scored": db.get_counter("transactions_scored"),
            "llm_used": db.get_counter("llm_scored"),
            "llm_avoided": db.get_counter("llm_avoided"),
        },
    })


@bp.post("/api/transactions")
def ingest_transaction():
    payload = request.get_json(silent=True) or {}

    customer_id = str(payload.get("customer_id") or "").strip()
    customer = db.get_customer(customer_id)
    if customer is None:
        return jsonify({"error": f"Unknown customer {customer_id!r}"}), 404

    try:
        amount = float(payload.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400
    if amount <= 0:
        return jsonify({"error": "amount must be greater than zero"}), 400

    txn = Transaction(
        txn_id=new_id("TXN"),
        customer_id=customer_id,
        timestamp=payload.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        amount=amount,
        currency=payload.get("currency", "INR"),
        merchant=payload.get("merchant", "Unknown Merchant"),
        merchant_category=payload.get("merchant_category", "groceries"),
        country=str(payload.get("country", "IN")).upper()[:2],
        city=payload.get("city", "Mumbai"),
        region=payload.get("region", "INDIA"),
        channel=payload.get("channel", "card_present"),
        card_last4=customer.card_number[-4:],
        device_id=payload.get("device_id"),
        ip_address=payload.get("ip_address"),
    )

    decision = pipeline.score_transaction(txn, persist=True)
    db.audit(actor="api", event_type="INGEST", subject_id=txn.txn_id,
             detail=f"Scored via HTTP API: {decision.action} ({decision.risk_score})")

    return jsonify({
        "txn_id": decision.txn_id,
        "risk_score": decision.risk_score,
        "risk_level": decision.risk_level,
        "action": decision.action,
        "reasoning": decision.reasoning,
        "rule_hits": [h if isinstance(h, dict) else h.__dict__ for h in decision.rule_hits],
        "cited_case_ids": decision.cited_case_ids,
        "llm_used": decision.llm_used,
        "injection_detected": decision.injection_detected,
        "groundedness_ok": decision.groundedness_ok,
        "suppressed_by_travel": decision.suppressed_by_travel,
        "latency_ms": decision.latency_ms,
    })
