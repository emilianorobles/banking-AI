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

from core import config, db, evaluation, llm, notifications as notif, rag

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
    """The human checkpoint, and the learning loop's entry point."""
    alert = db.get_alert(alert_id)
    if alert is None:
        flash("No such alert.", "danger")
        return redirect(url_for("admin.alert_queue"))
    if alert.status != "PENDING":
        flash("That alert has already been resolved.", "warn")
        return redirect(url_for("admin.alert_queue"))

    txn = db.get_transaction(alert.txn_id)
    outcome = request.form.get("outcome")
    note = (request.form.get("note") or "").strip()
    user = auth.current_user() or {}
    actor = f"analyst:{user.get('username', 'ops')}"

    if outcome not in ("confirmed_fraud", "false_positive"):
        flash("Unknown outcome.", "danger")
        return redirect(url_for("admin.alert_queue"))

    default_note = ("Confirmed fraudulent by analyst review."
                    if outcome == "confirmed_fraud" else
                    "Cleared by analyst; legitimate customer activity.")

    case_id = None
    if txn is not None:
        try:
            case = rag.learn_from_alert(alert, txn, outcome, note or default_note, actor)
            case_id = case.case_id
        except Exception as exc:
            flash(f"Resolved, but the case could not be indexed: {exc}", "warn")

    if outcome == "false_positive":
        db.set_card_frozen(alert.customer_id, False)

    db.resolve_alert(alert_id, outcome, resolved_by=actor, note=note,
                     learned_case_id=case_id)
    db.audit(actor=actor, event_type="APPROVAL", subject_id=alert_id,
             detail=(f"{outcome}; "
                     f"{'freeze upheld' if outcome == 'confirmed_fraud' else 'card unfrozen'}"
                     f"; learned {case_id}"))

    # The customer hears about it either way. An analyst clearing a freeze without
    # telling anyone leaves the customer still believing their card is dead.
    if outcome == "confirmed_fraud":
        notif.notify(alert.customer_id, kind="fraud_alert", severity="danger",
                     subject="Fraud confirmed on your card",
                     body="Our fraud team reviewed the transaction we stopped and confirmed "
                          "it was not you. Your card stays frozen and a replacement is on "
                          "its way.",
                     detail=note, related_id=alert_id)
    else:
        notif.notify(alert.customer_id, kind="fraud_alert", severity="ok",
                     subject="Your card is active again",
                     body="Our fraud team reviewed the transaction we flagged and confirmed "
                          "it was genuine. Your card has been unfrozen.",
                     detail=note, related_id=alert_id)

    flash(("Confirmed as fraud" if outcome == "confirmed_fraud" else
           "Cleared as a false positive")
          + (f" — indexed as {case_id}, retrievable immediately." if case_id else "."),
          "danger" if outcome == "confirmed_fraud" else "ok")
    return redirect(url_for("admin.alert_queue", region=_region()))


@bp.get("/cost")
def cost():
    return render_template("admin/cost.html", active="cost", cost=db.cost_summary(),
                           mode=config.DEMO_MODE, model=config.CHAT_MODEL)


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
