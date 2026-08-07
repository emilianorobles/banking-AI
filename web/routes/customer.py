"""The customer portal.

Six pages. Every figure on them is clickable, because the whole argument of this build is
that a customer should be able to ask "on what basis?" of anything the bank tells them and
get the arithmetic back. The routes assemble data; `core/insights.py` computes it and
`routes/drill.py` explains it.
"""

from __future__ import annotations

from datetime import date, timedelta

from flask import (Blueprint, Response, flash, redirect, render_template,
                   request, url_for)

from core import db, insights, notifications as notif, rag, travel
from core.contracts import CaseOutcome

from .. import auth

bp = Blueprint("customer", __name__)


def _ctx(cid: str) -> dict:
    """The handful of things every customer page needs in its header."""
    return {
        "customer": db.get_customer(cid),
        "unread": db.unread_notification_count(cid),
    }


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

@bp.get("/dashboard")
@auth.login_required
def dashboard():
    cid = auth.active_customer_id()
    txns = db.recent_transactions(cid, limit=1000)

    health = insights.account_health(cid)
    posture = insights.security_posture(cid)
    prot = insights.protection_stats(cid)
    proj = insights.monthly_projection(cid, txns)
    spend = insights.spend_analytics(cid, txns)

    recent = txns[:8]
    decisions = {t.txn_id: db.get_decision(t.txn_id) for t in recent}

    return render_template(
        "customer/dashboard.html", active="dashboard", **_ctx(cid),
        health=health, posture=posture, prot=prot, proj=proj, spend=spend,
        recent=recent, decisions=decisions,
        actions=insights.upcoming_actions(cid),
        notices=travel.active_notices(cid),
        pending_alerts=[a for a in db.list_alerts(status="PENDING", limit=200)
                        if a.customer_id == cid],
    )


@bp.get("/spending")
@auth.login_required
def spending():
    cid = auth.active_customer_id()
    txns = db.recent_transactions(cid, limit=1000)
    return render_template(
        "customer/spending.html", active="spending", **_ctx(cid),
        spend=insights.spend_analytics(cid, txns),
        proj=insights.monthly_projection(cid, txns),
        txns=txns[:40],
        # The template passed `{}` here, so every already-screened transaction on this page
        # rendered as "Pending". Only the 40 rows actually shown are looked up.
        decisions={t.txn_id: d for t in txns[:40]
                   if (d := db.get_decision(t.txn_id)) is not None},
        alerts=db.list_spending_alerts(cid),
    )


@bp.get("/security")
@auth.login_required
def security():
    cid = auth.active_customer_id()
    posture = insights.security_posture(cid)
    prot = insights.protection_stats(cid)

    # 12 weeks of screening activity, for the heat grid. A row per week, a cell per day.
    txns = db.recent_transactions(cid, limit=1000)
    today = date.today()
    counts: dict[str, int] = {}
    for t in txns:
        counts[t.timestamp[:10]] = counts.get(t.timestamp[:10], 0) + 1
    heat = [{"day": (d := (today - timedelta(days=i))).isoformat(),
             "value": counts.get(d.isoformat(), 0)}
            for i in range(83, -1, -1)]

    return render_template(
        "customer/security.html", active="security", **_ctx(cid),
        posture=posture, prot=prot, heat=heat,
        suggested=insights.suggest_passphrase(),
        notices=travel.active_notices(cid),
    )


@bp.get("/travel")
@auth.login_required
def travel_page():
    cid = auth.active_customer_id()
    return render_template(
        "customer/travel.html", active="travel", **_ctx(cid),
        notices=travel.active_notices(cid),
        all_notices=db.list_travel_notices(cid, active_only=False),
        today=date.today().isoformat(),
        default_end=(date.today() + timedelta(days=10)).isoformat(),
    )


@bp.get("/alerts")
@auth.login_required
def alerts():
    cid = auth.active_customer_id()
    rows = [a for a in db.list_alerts(limit=200) if a.customer_id == cid]
    txns = {a.txn_id: db.get_transaction(a.txn_id) for a in rows}
    decisions = {a.txn_id: db.get_decision(a.txn_id) for a in rows}
    return render_template(
        "customer/alerts.html", active="alerts", **_ctx(cid),
        alerts=rows, txns=txns, decisions=decisions,
        pending=[a for a in rows if a.status == "PENDING"],
    )


@bp.get("/statements")
@auth.login_required
def statements():
    cid = auth.active_customer_id()
    txns = db.recent_transactions(cid, limit=1000)
    return render_template(
        "customer/statements.html", active="statements", **_ctx(cid),
        statement=insights.build_statement(cid),
        spend=insights.spend_analytics(cid, txns),
        proj=insights.monthly_projection(cid, txns),
    )


@bp.get("/statements/download")
@auth.login_required
def statement_download():
    cid = auth.active_customer_id()
    body = insights.build_statement(cid)
    db.audit(actor=f"customer:{cid}", event_type="STATEMENT",
             subject_id=cid, detail="Statement downloaded")
    notif.notify(cid, kind="statement", severity="ok",
                 subject="Your statement is ready",
                 body="You downloaded an account statement. If this wasn't you, "
                      "freeze your card and contact us.")
    return Response(
        body, mimetype="text/plain",
        headers={"Content-Disposition":
                 f'attachment; filename="sentinelbank-{cid}-{date.today()}.txt"'},
    )


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #

@bp.post("/travel/file")
@auth.login_required
def file_notice():
    cid = auth.active_customer_id()
    countries = [c.strip() for c in (request.form.get("countries") or "").split(",") if c.strip()]
    start = request.form.get("start_date") or date.today().isoformat()
    end = request.form.get("end_date") or (date.today() + timedelta(days=10)).isoformat()

    if not countries:
        flash("Tell us at least one country.", "warn")
        return redirect(url_for("customer.travel_page"))

    try:
        notice = travel.create_notice(cid, countries, start, end, created_via="form")
    except Exception as exc:
        flash(f"Couldn't file that: {exc}", "danger")
        return redirect(url_for("customer.travel_page"))

    notif.notify(cid, kind="travel_notice", severity="ok",
                 subject=f"Travel notice filed for {', '.join(countries)}",
                 body=f"Your card will work in {', '.join(countries)} between {start} and "
                      f"{end} without the geography rules flagging it.",
                 detail=travel.describe(notice), related_id=notice.notice_id)
    flash(f"Travel notice filed for {', '.join(countries)}.", "ok")
    return redirect(url_for("customer.travel_page"))


@bp.post("/travel/<notice_id>/cancel")
@auth.login_required
def cancel_notice(notice_id: str):
    cid = auth.active_customer_id()
    owned = [n for n in db.list_travel_notices(cid, active_only=False)
             if n.notice_id == notice_id]
    if not owned:
        flash("That travel notice isn't on your account.", "danger")
        return redirect(url_for("customer.travel_page"))

    travel.cancel(notice_id, actor=f"customer:{cid}")
    notif.notify(cid, kind="travel_notice", severity="info",
                 subject="Travel notice cancelled",
                 body="Foreign transactions will be screened normally again.",
                 related_id=notice_id)
    flash("Travel notice cancelled.", "ok")
    return redirect(url_for("customer.travel_page"))


@bp.post("/alerts/<alert_id>/respond")
@auth.login_required
def respond_alert(alert_id: str):
    """The customer's own verdict on a flagged transaction.

    Confirming fraud freezes the card immediately -- waiting for an analyst while the
    customer is telling us it was fraud would be the wrong way round. Either answer
    becomes a retrievable precedent, so the next similar transaction is judged against it.
    """
    cid = auth.active_customer_id()
    alert = db.get_alert(alert_id)
    if alert is None or alert.customer_id != cid:
        flash("That alert isn't on your account.", "danger")
        return redirect(url_for("customer.alerts"))
    if alert.status != "PENDING":
        flash("That alert has already been resolved.", "warn")
        return redirect(url_for("customer.alerts"))

    verdict = request.form.get("verdict")
    txn = db.get_transaction(alert.txn_id)

    if verdict == "fraud":
        outcome, note = CaseOutcome.CONFIRMED_FRAUD.value, "Customer confirmed this was fraud."
        db.set_card_frozen(cid, True)
        severity, subject = "danger", "Card frozen — fraud confirmed"
        body = ("You confirmed this transaction was not you. Your card is frozen and the "
                "case is with our fraud team.")
    else:
        outcome, note = CaseOutcome.FALSE_POSITIVE.value, "Customer confirmed this was genuine."
        severity, subject = "ok", "Thanks — transaction confirmed as yours"
        body = ("You confirmed this transaction was genuine. We've recorded it so similar "
                "activity isn't flagged the same way again.")

    case_id = None
    if txn is not None:
        try:
            case = rag.learn_from_alert(alert, txn, outcome, note, analyst=f"customer:{cid}")
            case_id = case.case_id
        except Exception:
            # The learning loop is valuable, not load-bearing. A failure here must not
            # stop the customer from resolving their own alert.
            pass

    db.resolve_alert(alert_id, outcome, resolved_by=f"customer:{cid}", note=note,
                     learned_case_id=case_id)
    db.audit(actor=f"customer:{cid}", event_type="ALERT_RESOLVED", subject_id=alert_id,
             detail=f"{outcome} by customer", learned_case_id=case_id)

    notif.notify(cid, kind="fraud_alert", severity=severity, subject=subject, body=body,
                 detail=(f"Recorded as case {case_id}, which the system can now cite when "
                         f"judging similar transactions." if case_id else ""),
                 related_id=alert_id)

    flash(subject, "danger" if verdict == "fraud" else "ok")
    return redirect(url_for("customer.alerts"))


@bp.post("/card/<action>")
@auth.login_required
def card_control(action: str):
    """Freeze or unfreeze from the dashboard, without going through the agent."""
    cid = auth.active_customer_id()
    if action not in ("freeze", "unfreeze"):
        flash("Unknown card action.", "danger")
        return redirect(url_for("customer.dashboard"))

    frozen = action == "freeze"
    db.set_card_frozen(cid, frozen)
    db.audit(actor=f"customer:{cid}",
             event_type="CARD_FREEZE" if frozen else "CARD_UNFREEZE",
             subject_id=cid, detail=f"Card {action}d from the dashboard")

    notif.notify(
        cid, kind="card_frozen" if frozen else "card_unfrozen",
        severity="danger" if frozen else "ok",
        subject="Your card has been frozen" if frozen else "Your card is active again",
        body=("No further transactions will authorise on this card. Nothing you have "
              "already paid for is affected." if frozen else
              "Your card is working again and every transaction is being screened."),
    )
    flash("Card frozen." if frozen else "Card unfrozen.", "danger" if frozen else "ok")
    return redirect(request.referrer or url_for("customer.dashboard"))
