"""Tools for the fraud analyst and the operations admin.

Registered into the same `tools.REGISTRY` as the customer tools, so there is one registry,
one approval gate, one audit hook and one role check. What separates the personas is the
`roles` declaration on each tool and the allowlist in `personas.py` -- not a second
execution path.

Almost nothing here is new logic. These read `db`, `insights`, `rules` and `rag` exactly
as the ops pages already do; `resolve_alert` delegates to `core.casework`, which is the
same function `POST /ops/queue/<id>/resolve` calls. That is deliberate: the analyst
assistant must not be able to reach a second, subtly different implementation of the
learning loop.

**Naming rule, and it is load-bearing.** A staff tool that queries another account calls
its parameter `target_customer_id`, never `customer_id` -- `tools.execute` strips the
latter from model-supplied arguments because for a customer tool it is an authority claim.
For a staff tool it is a query argument. Same word, two meanings, so they get two names.
"""

from __future__ import annotations

from typing import Any

from .. import casework, config, db, insights, rag, rules, security
from .context import AgentContext
from .tools import ADMIN_ONLY, STAFF_ONLY, tool


def _txn_brief(txn) -> dict[str, Any]:
    """One transaction as staff need to see it.

    Includes `merchant`, which the customer-facing `list_recent_transactions`
    deliberately omits as attacker-controlled text. An analyst cannot work a case without
    the merchant name, so it goes in WRAPPED -- the `{injection_rule}` slot in the staff
    prompt exists to receive exactly this.
    """
    return {
        "txn_id": txn.txn_id,
        "when": txn.timestamp[:16].replace("T", " "),
        "amount_value": round(txn.amount, 2),
        "currency": txn.currency,
        "amount": f"{txn.amount:,.2f} {txn.currency}",
        "merchant": security.wrap_untrusted("merchant", txn.merchant),
        "merchant_category": txn.merchant_category,
        "location": f"{txn.city}, {txn.country}",
        "channel": txn.channel,
        "card_last4": txn.card_last4,
    }


# --------------------------------------------------------------------------- #
# Queue and triage
# --------------------------------------------------------------------------- #

@tool("list_open_alerts",
      "List alerts in the fraud queue, highest risk first. Use to see what is waiting, "
      "what the worst one is, or to find an alert to work on.",
      {"limit": "how many to return, 1-25 (default 10)",
       "status": "PENDING (default), APPROVED, REJECTED or AUTO_CLEARED",
       "region": "restrict to one region, or blank for all"},
      roles=STAFF_ONLY, takes_context=True)
def list_open_alerts(ctx: AgentContext, limit: Any = 10, status: str = "PENDING",
                     region: str = "") -> dict[str, Any]:
    try:
        count = max(1, min(int(limit), 25))
    except (TypeError, ValueError):
        count = 10
    status = (str(status).strip().upper() or "PENDING")
    region = str(region).strip().upper()

    alerts = db.list_alerts(status=status,
                            region=region if region and region != "ALL" else None,
                            limit=200)
    ranked = sorted(alerts, key=lambda a: a.risk_score, reverse=True)

    rows = []
    for a in ranked[:count]:
        customer = db.get_customer(a.customer_id)
        rows.append({
            "alert_id": a.alert_id, "txn_id": a.txn_id,
            "customer_id": a.customer_id,
            "customer_name": customer.name if customer else "",
            "risk_score": a.risk_score, "risk_level": a.risk_level,
            "action": a.action, "region": a.region,
            "raised": a.created_at[:16].replace("T", " "),
            "summary": a.summary,
        })
    return {"status": status, "showing": len(rows), "total_matching": len(alerts),
            "alerts": rows}


@tool("get_alert_detail",
      "Everything about one alert: the transaction, every rule that fired with its "
      "weight, the model's reasoning, and the precedents it cited.",
      {"alert_id": "the alert ID, e.g. ALERT-4f21a0"},
      roles=STAFF_ONLY, takes_context=True)
def get_alert_detail(ctx: AgentContext, alert_id: str = "") -> dict[str, Any]:
    alert = db.get_alert(str(alert_id).strip())
    if alert is None:
        return {"error": f"No alert found with ID {alert_id}."}

    txn = db.get_transaction(alert.txn_id)
    decision = db.get_decision(alert.txn_id) or {}
    customer = db.get_customer(alert.customer_id)
    cases = [db.get_fraud_case(c) for c in (decision.get("cited_case_ids") or [])]

    return {
        "alert_id": alert.alert_id, "status": alert.status,
        "risk_score": alert.risk_score, "risk_level": alert.risk_level,
        "action": alert.action, "summary": alert.summary,
        "raised": alert.created_at[:16].replace("T", " "),
        "customer": {"customer_id": alert.customer_id,
                     "name": customer.name if customer else "",
                     "home": f"{customer.home_city}, {customer.home_country}" if customer else "",
                     "card_frozen": bool(customer.card_frozen) if customer else None,
                     "typical_transaction": round(customer.baseline_avg_amount, 2)
                     if customer else None},
        "transaction": _txn_brief(txn) if txn else None,
        "rules_fired": _rule_rows(decision),
        "rule_score": decision.get("rule_score"),
        "suppressed_by_travel": bool(decision.get("suppressed_by_travel")),
        "model_used": bool(decision.get("llm_used")),
        "model_reasoning": decision.get("reasoning") or "",
        "cited_cases": [{"case_id": c.case_id, "title": c.title, "outcome": c.outcome}
                        for c in cases if c],
        "groundedness_ok": bool(decision.get("groundedness_ok", True)),
        "injection_detected": bool(decision.get("injection_detected")),
    }


def _rule_rows(decision: dict[str, Any]) -> list[dict[str, Any]]:
    """Rule hits with their weight and the analyst gloss, from RULE_META.

    Reads the single declaration site rather than carrying its own copy of the weights --
    the exact drift that broke the drill-down pills once already.
    """
    out = []
    for hit in decision.get("rule_hits") or []:
        rule_id = hit.get("rule_id") if isinstance(hit, dict) else getattr(hit, "rule_id", "")
        weight = hit.get("weight") if isinstance(hit, dict) else getattr(hit, "weight", 0)
        reason = hit.get("reason") if isinstance(hit, dict) else getattr(hit, "reason", "")
        meta = rules.RULE_META.get(str(rule_id).upper())
        out.append({
            "rule_id": rule_id, "points": weight, "reason": reason,
            "category": meta.category if meta else "",
            "technical": meta.technical if meta else "",
        })
    return sorted(out, key=lambda r: r["points"] or 0, reverse=True)


@tool("explain_decision",
      "Explain why one transaction scored what it did: every rule with its point "
      "contribution, whether the model was consulted, and what it concluded.",
      {"txn_id": "the transaction ID, e.g. TXN-a1b2c3"},
      roles=STAFF_ONLY, takes_context=True)
def explain_decision(ctx: AgentContext, txn_id: str = "") -> dict[str, Any]:
    txn = db.get_transaction(str(txn_id).strip())
    if txn is None:
        return {"error": f"No transaction found with ID {txn_id}."}
    decision = db.get_decision(txn.txn_id)
    if decision is None:
        return {"error": f"{txn.txn_id} has not been scored."}

    alert = db.get_alert_for_txn(txn.txn_id)
    return {
        "txn_id": txn.txn_id,
        "transaction": _txn_brief(txn),
        "risk_score": decision.get("risk_score"),
        "risk_level": decision.get("risk_level"),
        "action": decision.get("action"),
        "rule_score": decision.get("rule_score"),
        "rules_fired": _rule_rows(decision),
        "suppressed_by_travel": bool(decision.get("suppressed_by_travel")),
        "model_used": bool(decision.get("llm_used")),
        "model_reasoning": decision.get("reasoning") or "",
        "model_confidence": decision.get("confidence"),
        "cited_case_ids": decision.get("cited_case_ids") or [],
        "alert_id": alert.alert_id if alert else None,
        "alert_status": alert.status if alert else None,
        "latency_ms": decision.get("latency_ms"),
    }


@tool("customer_360",
      "The full picture for one customer: position, card state, security posture, recent "
      "activity and open alerts. Use when working a case that needs account context.",
      # `target_customer_id`, not `customer_id` -- see the naming rule in the module
      # docstring. Calling it `customer_id` would have it silently stripped by
      # `tools.execute` and the handler would fall back to its default.
      {"target_customer_id": "the customer ID to look up, e.g. CUST-0042; blank for the "
                             "customer currently in view"},
      roles=STAFF_ONLY, takes_context=True)
def customer_360(ctx: AgentContext, target_customer_id: str = "") -> dict[str, Any]:
    cid = str(target_customer_id).strip() or ctx.customer_id
    customer = db.get_customer(cid)
    if customer is None:
        return {"error": f"No customer found with ID {cid}."}

    # Staff reading another account is legitimate and logged. The audit row names both
    # the analyst and the subject, which is what makes it reviewable afterwards.
    db.audit(actor=ctx.actor, event_type="CUSTOMER_LOOKUP", subject_id=cid,
             detail=f"Full customer view opened via the assistant")

    posture = insights.security_posture(cid)
    prot = insights.protection_stats(cid)
    alerts = [a for a in db.list_alerts(limit=500) if a.customer_id == cid]
    recent = db.recent_transactions(cid, limit=8)

    return {
        "customer_id": cid,
        "name": customer.name,
        "home": f"{customer.home_city}, {customer.home_country}",
        "region": customer.region,
        # Last four only. The full PAN never leaves the vault, for staff either.
        "card_last4": customer.card_number[-4:],
        "card_status": "FROZEN" if customer.card_frozen else "active",
        "balance": round(customer.balance, 2),
        "credit_limit": round(customer.credit_limit, 2),
        "typical_transaction": round(customer.baseline_avg_amount, 2),
        "highest_historical": round(customer.baseline_max_amount, 2),
        "security_score": posture.score,
        "security_grade": posture.grade,
        "failing_checks": [c.label for c in posture.checks if not c.passed],
        "transactions_screened": prot.transactions_screened,
        "fraud_blocked": prot.fraud_blocked,
        "open_alerts": sum(1 for a in alerts if a.status == "PENDING"),
        "total_alerts": len(alerts),
        "recent_transactions": [_txn_brief(t) for t in recent],
    }


@tool("queue_stats",
      "Queue statistics: how many alerts are open, how they break down by status and "
      "region, and how many cases the system has learned.",
      {}, roles=STAFF_ONLY, takes_context=True)
def queue_stats(ctx: AgentContext) -> dict[str, Any]:
    everything = db.list_alerts(limit=2000)
    by_status: dict[str, int] = {}
    by_region: dict[str, int] = {}
    for a in everything:
        by_status[a.status] = by_status.get(a.status, 0) + 1
        if a.status == "PENDING":
            by_region[a.region or "UNKNOWN"] = by_region.get(a.region or "UNKNOWN", 0) + 1

    pending = [a for a in everything if a.status == "PENDING"]
    resolved = [a for a in everything if a.outcome]
    confirmed = sum(1 for a in resolved if a.outcome == "confirmed_fraud")

    return {
        "open": len(pending),
        "total": len(everything),
        "by_status": by_status,
        "open_by_region": by_region,
        "highest_open_risk": max((a.risk_score for a in pending), default=0),
        "average_open_risk": round(sum(a.risk_score for a in pending) / len(pending), 1)
        if pending else 0,
        "resolved": len(resolved),
        "confirmed_fraud": confirmed,
        "false_positives": len(resolved) - confirmed,
        "cases_learned": rag.learned_case_count(),
        "knowledge_store_size": rag.index_size(),
    }


@tool("resolve_alert",
      "Resolve an alert as confirmed fraud or a false positive. Writes the case into the "
      "knowledge store so the next similar transaction is judged against it, and unfreezes "
      "the card on a false positive.",
      {"alert_id": "the alert ID to resolve",
       "outcome": "exactly one of: confirmed_fraud, false_positive",
       "note": "the analyst's reasoning, in their words"},
      roles=STAFF_ONLY, requires_approval=True, reads_only=False,
      notify_subject="Fraud case resolved", notify_severity="info", takes_context=True)
def resolve_alert(ctx: AgentContext, alert_id: str = "", outcome: str = "",
                  note: str = "") -> dict[str, Any]:
    """Delegates to `core.casework`, which the ops route also calls.

    The result carries `notify_customer_id`, so `tools._notify_customer` alerts the
    customer the ALERT belongs to rather than whichever account the analyst was viewing.
    """
    result = casework.resolve_alert(str(alert_id).strip(), outcome, note, actor=ctx.actor)
    if not result.get("ok"):
        return {"error": result.get("error", "That alert could not be resolved.")}

    # A held transfer behind this alert now settles or drops, according to the verdict.
    from .. import money_ops
    if result.get("outcome") == "false_positive":
        released = money_ops.settle_if_held(result["txn_id"], actor=ctx.actor)
    else:
        released = money_ops.cancel_if_held(result["txn_id"], actor=ctx.actor)
    if released:
        result["held_transfer"] = released

    result["confirmed"] = True
    return result


# --------------------------------------------------------------------------- #
# Operations -- admin only
# --------------------------------------------------------------------------- #

@tool("cost_summary",
      "What the model has cost: calls, tokens, spend, and how many transactions were "
      "decided without touching it.",
      {}, roles=ADMIN_ONLY, takes_context=True)
def cost_summary(ctx: AgentContext) -> dict[str, Any]:
    """One source for the figures, on purpose.

    `db.cost_summary()` derives volume from the decisions table; the `transactions_scored`
    / `llm_avoided` counters measure something narrower (only transactions scored through
    a live pipeline run, not the seeded history). Returning both put two different
    "transactions scored" numbers in one answer and the model dutifully reported the
    contradiction. Only the decisions-derived figures ship. Same lesson as the inverted
    cost meter: a cost claim assembled from two differently-scoped sources is wrong even
    when both are individually correct.
    """
    summary = dict(db.cost_summary())
    summary.update({"model": config.CHAT_MODEL, "mode": config.DEMO_MODE})
    return summary


@tool("system_health",
      "System health: which provider is serving chat, whether the circuit breaker is "
      "open, the size of the knowledge store and the response cache, and whether the "
      "rule table is self-consistent.",
      {}, roles=ADMIN_ONLY, takes_context=True)
def system_health(ctx: AgentContext) -> dict[str, Any]:
    """Reads state. Deliberately does NOT call `llm.health_check()`.

    That function sends a live probe carrying a nonce so it can never be answered from
    the cache -- which is right for a pre-flight button and wrong here. Called from a
    chat turn with the primary dark, it costs up to PRIMARY_TIMEOUT (12s) of dead air, in
    the one persona whose entire purpose is knowing whether things are up. Everything
    below is already in memory.
    """
    from .. import llm

    breaker = llm.circuit_state()
    problems = rules.selfcheck()
    return {
        "mode": config.DEMO_MODE,
        "chat_model": config.CHAT_MODEL,
        "api_key_present": config.has_api_key(),
        "primary_circuit_open": breaker.get("primary_open"),
        "consecutive_primary_failures": breaker.get("consecutive_failures"),
        "circuit_reopens_in_seconds": breaker.get("reopens_in_seconds"),
        "fallback_configured": config.has_fallback(),
        "fallback_provider": (config.FALLBACK_PROVIDER_NAME
                              if config.has_fallback() else None),
        "primary_timeout_s": config.PRIMARY_TIMEOUT_SECONDS,
        "fallback_timeout_s": config.FALLBACK_TIMEOUT_SECONDS,
        "cached_responses": llm.cache_size(),
        "knowledge_store_size": rag.index_size(),
        "cases_learned": rag.learned_case_count(),
        "rules_consistent": not problems,
        "rule_problems": problems,
        "transactions_scored": db.get_counter("transactions_scored"),
        "note": ("Read from live state without probing the provider — a probe would cost "
                 "up to the primary timeout if it is down."),
    }


@tool("search_audit_log",
      "Search the audit log: who did what, to which subject, and when. Use to answer "
      "questions about actions taken on the system.",
      {"subject_id": "restrict to one subject (a customer, alert or transaction ID)",
       "event_type": "restrict to one event type, e.g. APPROVAL, GUARDRAIL, TOOL_CALL",
       "limit": "how many entries to return, 1-40 (default 20)"},
      roles=ADMIN_ONLY, takes_context=True)
def search_audit_log(ctx: AgentContext, subject_id: str = "", event_type: str = "",
                     limit: Any = 20) -> dict[str, Any]:
    try:
        count = max(1, min(int(limit), 40))
    except (TypeError, ValueError):
        count = 20

    rows = db.list_audit(str(subject_id).strip() or None, limit=400)
    wanted = str(event_type).strip().upper()
    if wanted:
        rows = [r for r in rows if str(r.get("event_type", "")).upper() == wanted]

    return {
        "showing": min(len(rows), count),
        "total_matching": len(rows),
        "filters": {"subject_id": subject_id or "any", "event_type": wanted or "any"},
        "entries": [
            {"when": str(r.get("timestamp", ""))[:19].replace("T", " "),
             "actor": r.get("actor"), "event": r.get("event_type"),
             "subject": r.get("subject_id"), "detail": r.get("detail")}
            for r in rows[:count]
        ],
    }
