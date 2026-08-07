"""The fraud operations console.

Analyst-and-above only, and the gate is on the route rather than on the link -- a customer
who types /ops/queue gets a 403 and a line in the audit log, not a hidden page.

The alert queue is the important one. It is where a human confirms or overrules the
system, and where a resolved case is written back into the knowledge store so the next
similar transaction is judged against a decision a person actually made.
"""

from __future__ import annotations

import threading
from typing import Any

from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, url_for)

from core import (casework, config, db, evaluation, llm, money, money_ops,
                  notifications as notif, rag, rules)

from .. import auth

bp = Blueprint("admin", __name__, url_prefix="/ops")


@bp.before_request
@auth.role_required("analyst")
def _gate():
    """Applied to every route in this blueprint, so a new page cannot be added ungated."""
    return None


def _region() -> str:
    return request.args.get("region", "ALL")


@bp.get("/")
@bp.get("/overview")
def overview():
    region = _region()
    alerts = db.list_alerts(status="PENDING", region=region, limit=500)
    txns = {a.txn_id: db.get_transaction(a.txn_id) for a in alerts[:15]}
    return render_template(
        "admin/overview.html", active="ops", region=region, regions=config.REGIONS,
        stats=db.region_stats(None if region == "ALL" else region),
        pending=alerts, txns=txns,
        cost=db.cost_summary(),
        recent=db.recent_transactions(limit=15),
        decisions={t.txn_id: db.get_decision(t.txn_id)
                   for t in db.recent_transactions(limit=15)},
        learned=rag.learned_case_count(), index_size=rag.index_size(),
    )


@bp.get("/queue")
def alert_queue():
    region = _region()
    status = request.args.get("status", "PENDING")
    alerts = db.list_alerts(status=status, region=region, limit=50)

    rows = []
    for a in alerts:
        txn = db.get_transaction(a.txn_id)
        decision = db.get_decision(a.txn_id)
        if txn is None or decision is None:
            continue
        cases = [db.get_fraud_case(c) for c in (decision.get("cited_case_ids") or [])]
        rows.append({"alert": a, "txn": txn, "decision": decision,
                     "customer": db.get_customer(a.customer_id),
                     "cases": [c for c in cases if c]})

    return render_template("admin/queue.html", active="queue", region=region,
                           regions=config.REGIONS, status=status, rows=rows)


@bp.post("/queue/<alert_id>/resolve")
def resolve(alert_id: str):
    """The human checkpoint, and the learning loop's entry point.

    The work is in `core.casework.resolve_alert`, because the analyst assistant's
    `resolve_alert` tool calls the same function. Two implementations of the learning
    loop would drift, and they would drift in the demo's climax.
    """
    user = auth.current_user() or {}
    actor = f"{user.get('role', 'analyst')}:{user.get('username', 'ops')}"

    result = casework.resolve_alert(
        alert_id,
        request.form.get("outcome") or "",
        (request.form.get("note") or "").strip(),
        actor=actor,
    )

    if not result.get("ok"):
        flash(result.get("error", "That alert could not be resolved."), "danger")
        return redirect(url_for("admin.alert_queue", region=_region()))

    if result.get("index_error"):
        flash(f"Resolved, but the case could not be indexed: {result['index_error']}",
              "warn")

    # Money the engine was holding behind this alert now settles or drops.
    held = (money_ops.settle_if_held(result["txn_id"], actor=actor)
            if result["outcome"] == "false_positive"
            else money_ops.cancel_if_held(result["txn_id"], actor=actor))

    message = result["message"]
    if held and held.get("settled"):
        message += (f" The held payment of {money.fmt(held['amount_value'], held['currency'])}"
                    f" to {held['destination']} has been released.")
    elif held and held.get("cancelled"):
        message += " The held payment has been cancelled."

    flash(message, "danger" if result["outcome"] == "confirmed_fraud" else "ok")
    return redirect(url_for("admin.alert_queue", region=_region()))


@bp.get("/cost")
def cost():
    return render_template("admin/cost.html", active="cost", cost=db.cost_summary(),
                           mode=config.DEMO_MODE, model=config.CHAT_MODEL)


@bp.get("/business-case")
def business_case():
    """The commercial argument, computed live rather than pasted into a slide.

    A slide goes stale the moment anyone reseeds; this page reads `db.cost_summary()` and
    the decision table on every request, so the numbers a judge sees are the numbers this
    installation actually produced. It is also clickable during the demo, which a slide
    is not.

    Where a figure comes from a measured run rather than from the live database -- the
    A/B precision numbers -- it is labelled as such. Presenting a benchmark result as a
    live reading would be the exact dishonesty this page exists to avoid.
    """
    c = db.cost_summary()

    with db.connect() as conn:
        actions = {r["action"]: r["n"] for r in conn.execute(
            "SELECT action, COUNT(*) n FROM decisions GROUP BY action").fetchall()}
        suppressed = conn.execute(
            "SELECT COUNT(*) n FROM decisions WHERE suppressed_by_travel=1").fetchone()["n"]
        injections = conn.execute(
            "SELECT COUNT(*) n FROM decisions WHERE injection_detected=1").fetchone()["n"]
    learned = rag.learned_case_count()

    scored = sum(actions.values())
    blocked = actions.get("FREEZE_AND_ESCALATE", 0) + actions.get("QUARANTINE", 0)

    return render_template(
        "admin/business_case.html", active="business_case",
        cost=c, actions=actions, scored=scored, blocked=blocked,
        challenged=actions.get("CHALLENGE", 0), allowed=actions.get("ALLOW", 0),
        suppressed=suppressed, injections=injections,
        learned=learned, index_size=rag.index_size(),
        rules_count=len(rules.RULE_META), model=config.CHAT_MODEL,
    )


# --------------------------------------------------------------------------- #
# Evaluation — runs in a thread so a 30-case sweep does not hold the request open
# --------------------------------------------------------------------------- #

_EVAL: dict[str, Any] = {"state": "idle", "done": 0, "total": 0,
                         "results": [], "summary": None, "comparison": None, "error": None}
_EVAL_LOCK = threading.Lock()


def _run_eval(allow_llm: bool) -> None:
    """Score the eval set, reporting progress as it goes.

    Two arms when the agent is enabled: rules only, then the full pipeline. We score each
    arm once and hand both to `evaluation.compare_results`. Calling `evaluation.compare`
    instead would re-score every case from scratch -- it takes the raw eval rows, not
    results -- which is both a third pass of model calls and, if you feed it results,
    a crash, because a result dict carries a `label` key that `Transaction` has no
    field for.
    """
    try:
        rows = evaluation.load_eval_set()
        arms = 2 if allow_llm else 1
        with _EVAL_LOCK:
            _EVAL.update(state="running", done=0, total=len(rows) * arms, results=[],
                         summary=None, comparison=None, error=None)

        done = 0

        def score(arm_allow_llm: bool) -> list[dict]:
            nonlocal done
            out = []
            for row in rows:
                out.append(evaluation.run_case(row, allow_llm=arm_allow_llm))
                done += 1
                with _EVAL_LOCK:
                    _EVAL["done"] = done
                    # Show the agentic arm's rows; the baseline is only there for the A/B.
                    if arm_allow_llm or not allow_llm:
                        _EVAL["results"] = out
            return out

        if allow_llm:
            # Rules first: it is the fast arm, so the progress bar moves immediately
            # rather than sitting still through the first model call.
            baseline = score(False)
            full = score(True)
            comparison = evaluation.compare_results(baseline, full)
        else:
            full = score(False)
            comparison = None

        with _EVAL_LOCK:
            _EVAL.update(state="done", results=full,
                         summary=evaluation.summarise(full), comparison=comparison)
    except Exception as exc:
        with _EVAL_LOCK:
            _EVAL.update(state="error", error=f"{type(exc).__name__}: {exc}")


@bp.get("/evaluation")
def evaluation_page():
    with _EVAL_LOCK:
        snapshot = dict(_EVAL)
    return render_template("admin/evaluation.html", active="eval", eval=snapshot,
                           has_key=config.has_api_key())


@bp.post("/evaluation/run")
def evaluation_run():
    with _EVAL_LOCK:
        if _EVAL["state"] == "running":
            return jsonify({"error": "Already running."}), 409
        _EVAL.update(state="running", done=0, total=0)
    allow_llm = (request.get_json(silent=True) or {}).get("allow_llm", True)
    threading.Thread(target=_run_eval, args=(bool(allow_llm),), daemon=True).start()
    db.audit(actor=f"analyst:{(auth.current_user() or {}).get('username')}",
             event_type="EVALUATION", subject_id="eval_set",
             detail=f"Evaluation started (allow_llm={allow_llm})")
    return jsonify({"ok": True})


@bp.get("/evaluation/status")
def evaluation_status():
    with _EVAL_LOCK:
        snapshot = dict(_EVAL)
    snapshot["results"] = snapshot["results"][-60:]
    return jsonify(snapshot)


# --------------------------------------------------------------------------- #

@bp.get("/knowledge")
def knowledge():
    cases = db.list_fraud_cases()
    query = (request.args.get("q") or "").strip()
    hits = rag.search(query, k=6) if query else []
    return render_template(
        "admin/knowledge.html", active="knowledge",
        cases=cases, learned=[c for c in cases if c.source == "learned"],
        index_size=rag.index_size(), ready=rag.index_ready(),
        consistency=rag.consistency_check(), query=query, hits=hits,
    )


@bp.get("/audit")
def audit():
    subject = (request.args.get("subject") or "").strip() or None
    event = (request.args.get("event") or "").strip()
    rows = db.list_audit(subject_id=subject, limit=400)
    if event:
        rows = [r for r in rows if r.get("event_type") == event]
    events = sorted({r.get("event_type", "") for r in db.list_audit(limit=400)})
    return render_template("admin/audit.html", active="audit", rows=rows,
                           events=events, subject=subject or "", event=event,
                           provider=llm.health_check())
