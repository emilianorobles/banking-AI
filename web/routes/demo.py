"""Demo control — the presenter's remote.

Everything on stage is a button. Nothing is typed live: a typo in front of judges costs
forty seconds and all your momentum. Scenarios come from `data/demo_injections.json` so
the sequence is identical across every rehearsal.

The two setup buttons are not conveniences, they are the fix for two stateful traps:
injecting fraud freezes the hero's card (making every later beat look like fraud, because
CARD_ALREADY_FROZEN is +60), and a reseed wipes the travel notice (so the Spain beat stops
testing suppression at all).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   session, url_for)

from core import config, db, llm, pipeline, rag, rules, seed, travel
from core.contracts import Transaction, new_id

from .. import auth

bp = Blueprint("demo", __name__, url_prefix="/demo")


@bp.before_request
@auth.role_required("admin")
def _gate():
    return None


def _load() -> dict:
    if not config.DEMO_INJECTIONS_PATH.exists():
        return {}
    return json.loads(config.DEMO_INJECTIONS_PATH.read_text(encoding="utf-8"))


@bp.get("/")
def control():
    demo = _load()
    hero = demo.get("hero_customer_id")
    customer = db.get_customer(hero) if hero else None
    return render_template(
        "admin/demo.html", active="demo", demo=demo, customer=customer,
        notices=travel.active_notices(hero) if hero else [],
        last=session.get("last_injection"),
        index_size=rag.index_size(), learned=rag.learned_case_count(),
        mode=config.DEMO_MODE, has_key=config.has_api_key(),
        pending=len(db.list_alerts(status="PENDING", limit=500)),
    )


@bp.post("/inject/<int:index>")
def inject(index: int):
    """Score a scripted transaction as if it had arrived from the card network."""
    demo = _load()
    scenarios = demo.get("scenarios", [])
    if index < 0 or index >= len(scenarios):
        return jsonify({"error": "No such scenario."}), 404

    scenario = scenarios[index]
    payload = dict(scenario["txn"])
    payload["txn_id"] = new_id("TXN")
    payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    txn = Transaction(**payload)

    steps: list[str] = []
    decision = pipeline.score_transaction(txn, persist=True, on_step=steps.append)

    db.audit(actor=f"admin:{(auth.current_user() or {}).get('username')}",
             event_type="DEMO_INJECT", subject_id=txn.txn_id,
             detail=f"{scenario.get('label', 'scenario')} → {decision.action} "
                    f"({decision.risk_score})")

    result = {
        "label": scenario.get("label", f"Scenario {index + 1}"),
        "txn_id": txn.txn_id,
        "merchant": txn.merchant,
        "amount": f"{txn.amount:,.2f} {txn.currency}",
        "where": f"{txn.city}, {txn.country}",
        "risk_score": decision.risk_score,
        "risk_level": decision.risk_level,
        "action": decision.action,
        "reasoning": decision.reasoning,
        "rule_hits": [h if isinstance(h, dict) else h.__dict__ for h in decision.rule_hits],
        "cited_case_ids": decision.cited_case_ids,
        "llm_used": decision.llm_used,
        "suppressed_by_travel": decision.suppressed_by_travel,
        "injection_detected": decision.injection_detected,
        "latency_ms": decision.latency_ms,
        "steps": steps,
    }
    session["last_injection"] = result
    return jsonify(result)


@bp.post("/travel-notice")
def file_travel_notice():
    """Run this BEFORE the 'legitimate in Spain' beat, or it isn't testing anything."""
    demo = _load()
    tn = demo.get("travel_notice")
    if not tn:
        flash("No travel notice defined in data/demo_injections.json.", "danger")
        return redirect(url_for("demo.control"))

    travel.create_notice(tn["customer_id"], tn["countries"], tn["start_date"],
                         tn["end_date"], created_via="form")
    flash(f"Travel notice filed for {', '.join(tn['countries'])}.", "ok")
    return redirect(url_for("demo.control"))


@bp.post("/unfreeze")
def unfreeze_hero():
    """Reset the card between rehearsals. Skip this and every later beat scores ~100."""
    demo = _load()
    hero = demo.get("hero_customer_id")
    if not hero:
        flash("No hero customer defined.", "danger")
        return redirect(url_for("demo.control"))
    db.set_card_frozen(hero, False)
    flash("Hero card unfrozen.", "ok")
    return redirect(url_for("demo.control"))


@bp.post("/reset")
def reset():
    """Full reseed. Takes ~40s and needs the API.

    `build_index=True` is required, not optional: the reset deletes the index so learned
    cases cannot outlive their SQLite mirror, and skipping the rebuild would leave the
    demo with retrieval that returns nothing and therefore no citations.
    """
    try:
        summary = seed.seed(reset=True, build_index=True)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    session.pop("last_injection", None)
    session.pop("chat_history", None)
    session.pop("pending_approval", None)

    return jsonify({
        "ok": True,
        "scored": summary.get("scored", 0),
        "cases": summary.get("cases", 0),
        "warning": "Re-file the travel notice before running the demo — the reset wiped it.",
    })


@bp.post("/preflight")
def preflight():
    """One click that puts the demo in a known-good state and says what it changed."""
    demo = _load()
    hero = demo.get("hero_customer_id")
    changes = []

    if hero:
        customer = db.get_customer(hero)
        if customer and customer.card_frozen:
            db.set_card_frozen(hero, False)
            changes.append("unfroze the hero card")

        if not travel.active_notices(hero):
            tn = demo.get("travel_notice")
            if tn:
                travel.create_notice(tn["customer_id"], tn["countries"], tn["start_date"],
                                     tn["end_date"], created_via="form")
                changes.append(f"filed the {', '.join(tn['countries'])} travel notice")

    # Give the primary a clean slate, then probe it. If it is up, the circuit closes and
    # we run normally. If it is down, the probe trips the breaker here rather than during
    # the first beat on stage -- pre-flight absorbs the timeout so the demo does not.
    llm.reset_circuit()
    health = llm.health_check()

    # Keyed on who actually answered, not on `llm_ok` -- which is true when the fallback
    # served the call, and would have reported "primary responding" while it was down.
    served = health.get("chat_served_by")
    changes.append(
        f"failover budget: primary gets {health.get('primary_timeout_s')}s, then "
        f"cache, then {health.get('fallback_provider') or 'no secondary'} "
        f"({health.get('fallback_timeout_s')}s), then the database"
    )
    if served == "primary":
        changes.append("primary provider responding")
    elif served == "nothing — all providers failed":
        changes.append("NO provider is reachable — the demo will run on the recorded "
                       "cache and the rules engine")
    else:
        changes.append(f"primary provider is DOWN — chat is being served by {served}")

    if not health.get("embeddings_ok"):
        changes.append("embeddings are unreachable — retrieval is running from the "
                       "cached query vectors, so citations work for the scripted beats "
                       "but not for improvised ones")

    # A rule whose ID has drifted from RULE_META is disabled silently -- evaluate()
    # swallows the KeyError -- so the scores on stage would be quietly wrong. Nothing to
    # fix automatically, but this is the moment to find out.
    rule_problems = rules.selfcheck()
    if rule_problems:
        changes.append(f"RULE METADATA HAS DRIFTED ({len(rule_problems)} problem(s)) — "
                       f"affected rules are silently disabled: "
                       + "; ".join(rule_problems))

    return jsonify({
        "ok": True,
        "changes": changes or ["nothing to fix — already in a good state"],
        "rules_ok": not rule_problems,
        "index_size": rag.index_size(),
        "pending_alerts": len(db.list_alerts(status="PENDING", limit=500)),
        "mode": config.DEMO_MODE,
        "has_key": config.has_api_key(),
        "primary_ok": health.get("primary_ok"),
        "chat_served_by": served,
        "embeddings_ok": health.get("embeddings_ok"),
        "fallback": health.get("fallback_provider"),
        "circuit": health.get("circuit"),
    })
