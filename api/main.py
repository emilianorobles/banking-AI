"""FastAPI ingestion service -- the "real-time fraud detection API" from the use case.

Deliberately thin. Every route translates HTTP to a `core` call and back; there is no
business logic here. That separation is the point: the same scoring path serves the
Streamlit UI, this API, and the evaluation harness, so what you see demonstrated is
what a bank's transaction switch would actually call.

Run:  uvicorn api.main:app --port 8000 --reload
Docs: http://localhost:8000/docs

Demo use -- inject a transaction from a phone or another laptop and watch the dashboard:

    curl -X POST http://localhost:8000/api/transactions \\
      -H "Content-Type: application/json" \\
      -d '{"customer_id":"CUST-0001","amount":9500,"currency":"INR",
           "merchant":"CoinBridge","merchant_category":"crypto_exchange",
           "country":"NG","city":"Lagos","region":"EMEA","channel":"card_not_present"}'
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from core import db, llm, rag
from core import pipeline as core_pipeline
from core.contracts import Transaction, new_id

app = FastAPI(
    title="BedRock Financial Fraud Detection API",
    description="Real-time transaction scoring with RAG-grounded explanations.",
    version="1.0.0",
)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class TransactionIn(BaseModel):
    customer_id: str = Field(..., examples=["CUST-0001"])
    amount: float = Field(..., gt=0)
    currency: str = "INR"
    merchant: str = "Unknown Merchant"
    merchant_category: str = "groceries"
    country: str = Field("IN", max_length=2)
    city: str = "Mumbai"
    region: str = "INDIA"
    channel: str = "card_present"
    timestamp: str | None = None
    device_id: str | None = None
    ip_address: str | None = None


class DecisionOut(BaseModel):
    txn_id: str
    risk_score: int
    risk_level: str
    action: str
    reasoning: str
    rule_hits: list[dict[str, Any]]
    cited_case_ids: list[str]
    llm_used: bool
    injection_detected: bool
    groundedness_ok: bool
    suppressed_by_travel: bool
    latency_ms: int


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness plus provider and index status -- check this before a demo."""
    return {
        "status": "ok",
        "provider": llm.health_check(),
        "index_cases": rag.index_size(),
        "counters": {
            "scored": db.get_counter("transactions_scored"),
            "llm_used": db.get_counter("llm_scored"),
            "llm_avoided": db.get_counter("llm_avoided"),
        },
    }


@app.post("/api/transactions", response_model=DecisionOut)
def ingest_transaction(payload: TransactionIn) -> DecisionOut:
    """Score a single transaction. This is the endpoint a transaction switch would call."""
    customer = db.get_customer(payload.customer_id)
    if customer is None:
        raise HTTPException(404, f"Unknown customer {payload.customer_id}")

    txn = Transaction(
        txn_id=new_id("TXN"),
        customer_id=payload.customer_id,
        timestamp=payload.timestamp or datetime.now(timezone.utc).isoformat(),
        amount=payload.amount,
        currency=payload.currency,
        merchant=payload.merchant,
        merchant_category=payload.merchant_category,
        country=payload.country.upper(),
        city=payload.city,
        region=payload.region,
        channel=payload.channel,
        card_last4=customer.card_number[-4:],
        device_id=payload.device_id,
        ip_address=payload.ip_address,
    )

    decision = core_pipeline.score_transaction(txn, persist=True)
    db.audit(actor="api", event_type="INGEST", subject_id=txn.txn_id,
             detail=f"Scored via HTTP API: {decision.action} ({decision.risk_score})")

    return DecisionOut(
        txn_id=decision.txn_id,
        risk_score=decision.risk_score,
        risk_level=decision.risk_level,
        action=decision.action,
        reasoning=decision.reasoning,
        rule_hits=[h.__dict__ if not isinstance(h, dict) else h for h in decision.rule_hits],
        cited_case_ids=decision.cited_case_ids,
        llm_used=decision.llm_used,
        injection_detected=decision.injection_detected,
        groundedness_ok=decision.groundedness_ok,
        suppressed_by_travel=decision.suppressed_by_travel,
        latency_ms=decision.latency_ms,
    )


@app.get("/api/alerts")
def list_alerts(status: str | None = None, region: str | None = None, limit: int = 25):
    return [a.to_dict() for a in db.list_alerts(status=status, region=region, limit=limit)]


@app.get("/api/transactions/{txn_id}")
def get_transaction(txn_id: str):
    txn = db.get_transaction(txn_id)
    if txn is None:
        raise HTTPException(404, "Transaction not found")
    return {"transaction": txn.to_dict(), "decision": db.get_decision(txn_id)}


@app.get("/api/stats")
def stats(region: str | None = None):
    return {"cost": db.cost_summary(), "regions": db.region_stats(region)}


@app.get("/api/audit")
def audit(subject_id: str | None = None, limit: int = 100):
    return db.list_audit(subject_id=subject_id, limit=limit)
