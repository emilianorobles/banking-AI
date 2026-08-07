"""The drill-down API — every number on the page can be asked to explain itself.

`GET /api/drill/<kind>/<key>` returns the working behind one figure: what it measures,
the values that went in, the arithmetic, the actual records, and what would change it.
`renderDrill()` in `static/js/app.js` renders whatever this returns, so a new kind needs
no front-end work at all — add a handler to DISPATCH and give a card a `data-drill`.

A score a customer cannot interrogate is decoration. This is the endpoint that makes it
evidence instead.

Scoping: customer-bound kinds read `auth.active_customer_id()` from the session and never
the key from the URL. Passing someone else's id in the path gets you your own data, not
theirs.
"""

from __future__ import annotations

from typing import Any, Callable

from flask import Blueprint, jsonify

from core import db, insights, rules

from .. import auth

bp = Blueprint("drill", __name__, url_prefix="/api/drill")


def _fail(message: str, code: int = 404):
    return jsonify({"label": "Not available", "note": message}), code


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def _health(key: str, cid: str) -> dict[str, Any]:
    """One slice of the account health score."""
    health = insights.account_health(cid)

    if key == "_total":
        return {
            "label": "Account health score",
            "subtitle": f"{health.score} / 100 — grade {health.grade}",
            "headline": f"{health.score}/100",
            "headline_class": _band(health.score, 70, 45),
            "grade": f"Grade {health.grade}",
            "what": "Four independent measures of how your account is doing, added together. "
                    "Each one is listed below with the points it contributed — click any "
                    "individual card to see how that number was reached.",
            "inputs": {c.label: f"{c.earned} / {c.max} points" for c in health.components},
            "formula": "  " + "\n+ ".join(
                f"{c.earned:>3} / {c.max:<3}  {c.label}" for c in health.components
            ) + f"\n{'=' * 34}\n  {health.score:>3} / 100  overall  →  grade {health.grade}",
            "evidence": [{"component": c.label, "earned": c.earned, "out_of": c.max,
                          "share": f"{c.pct:.0f}%", "basis": c.detail}
                         for c in health.components],
            "remediation": health.summary,
            "note": "Nothing here is a black box: every point is attributable to a named "
                    "component, and every component shows its arithmetic.",
        }

    c = health.component(key)
    if c is None:
        return {}
    return {
        "label": c.label,
        "subtitle": f"{c.earned} of {c.max} points ({c.pct:.0f}%)",
        "headline": f"{c.earned}/{c.max} points",
        "headline_class": _band(c.pct, 75, 45),
        "what": c.what,
        "inputs": c.inputs,
        "formula": c.formula,
        "evidence": c.evidence,
        "remediation": c.remediation,
        "passed": c.pct >= 60,
    }


def _security(key: str, cid: str) -> dict[str, Any]:
    """One security check, and why it passed or failed."""
    posture = insights.security_posture(cid)

    if key == "_total":
        passed = [c for c in posture.checks if c.passed]
        earned = sum(c.weight for c in passed)
        total = sum(c.weight for c in posture.checks)
        return {
            "label": "Security score",
            "subtitle": f"{posture.score}/100 — grade {posture.grade}",
            "headline": f"{len(passed)} of {len(posture.checks)} checks passing",
            "headline_class": _band(posture.score, 80, 55),
            "grade": f"Grade {posture.grade}",
            "what": "Every check below is run against your real account state — none of it "
                    "is a self-assessment questionnaire. The score is the weighted share "
                    "of checks that pass.",
            "inputs": {"checks_passing": f"{len(passed)} of {len(posture.checks)}",
                       "weighted_points": f"{earned} of {total}",
                       "password_age_days": posture.password_age_days},
            "formula": f"{earned} weighted points passing ÷ {total} available "
                       f"× 100 = {posture.score}",
            "evidence": [{"check": c.label, "result": "PASS" if c.passed else "ACTION NEEDED",
                          "weight": c.weight, "basis": c.detail} for c in posture.checks],
            "remediation": " ".join(posture.recommendations) or
                           "Nothing outstanding — every check is passing.",
            "passed": posture.score >= 80,
        }

    c = posture.check(key)
    if c is None:
        return {}
    return {
        "label": c.label,
        "subtitle": "Passing" if c.passed else "Needs your attention",
        "headline": "PASS" if c.passed else "ACTION NEEDED",
        "headline_class": "pill-ok" if c.passed else "pill-warn",
        "what": c.what,
        "inputs": c.inputs,
        "evidence": c.evidence,
        "remediation": c.remediation,
        "passed": c.passed,
        "note": f"Weighted {c.weight}× in the overall security score.",
    }


def _protection(key: str, cid: str) -> dict[str, Any]:
    """What the bank actually did for this customer. Every figure is a row count."""
    p = insights.protection_stats(cid)

    metrics = {
        "screened": ("Transactions screened", p.transactions_screened,
                     "Every card transaction on your account that went through the fraud "
                     "pipeline. Not a sample — all of them.",
                     "count of transactions with a stored decision"),
        "blocked": ("Fraud stopped", p.fraud_blocked,
                    "Transactions frozen and escalated to a human analyst before the money "
                    "moved.",
                    "count of decisions with action = FREEZE_AND_ESCALATE or QUARANTINE"),
        "challenged": ("Verification requested", p.challenges_issued,
                       "Transactions we were unsure about, so we asked you to confirm "
                       "rather than guessing.",
                       "count of decisions with action = CHALLENGE"),
        "suppressed": ("False alarms avoided", p.false_positives_prevented,
                       "Transactions that would have been flagged on geography alone, but "
                       "were cleared because you had told us you were travelling.",
                       "count of decisions where a travel notice suppressed the geo rules"),
        "tokenized": ("PII fields tokenised", p.pii_fields_tokenized,
                      "Card numbers, names and account details replaced with opaque tokens "
                      "before any text reached the language model. The model reasons about "
                      "\"the same card\" without ever seeing the number.",
                      "sum of pii_tokenized counts recorded in guardrail notes"),
        "injection": ("Prompt injections blocked", p.injection_blocked,
                      "Attempts to smuggle instructions to the AI through merchant names "
                      "or transaction text.",
                      "count of decisions with injection_detected = true"),
        "amount": ("Value protected", round(p.amount_protected),
                   "The total value of the transactions that were stopped.",
                   "sum of amount over transactions that were frozen or quarantined"),
    }

    if key not in metrics:
        return {}
    label, value, what, formula = metrics[key]

    rows = []
    for t in db.recent_transactions(cid, limit=200):
        d = db.get_decision(t.txn_id)
        if not d:
            continue
        keep = (
            (key == "screened") or
            (key == "blocked" and d["action"] in ("FREEZE_AND_ESCALATE", "QUARANTINE")) or
            (key == "challenged" and d["action"] == "CHALLENGE") or
            (key == "suppressed" and d.get("suppressed_by_travel")) or
            (key == "injection" and d.get("injection_detected")) or
            (key == "amount" and d["action"] in ("FREEZE_AND_ESCALATE", "QUARANTINE")) or
            (key == "tokenized" and any(str(n).startswith("pii_tokenized:")
                                        for n in d.get("guardrail_notes") or []))
        )
        if keep:
            rows.append({"when": t.timestamp[:16].replace("T", " "), "merchant": t.merchant,
                         "where": f"{t.city}, {t.country}",
                         "amount": f"{t.amount:,.0f} {t.currency}",
                         "risk": d["risk_score"], "outcome": d["action"]})
        if len(rows) >= 25:
            break

    return {
        "label": label,
        "subtitle": f"{value:,}" + (f" {p.currency}" if key == "amount" else ""),
        "headline": f"{value:,}",
        "headline_class": "pill-danger" if key in ("blocked", "injection") and value else "pill-info",
        "what": what,
        "inputs": {"value": f"{value:,}", "out_of_screened": f"{p.transactions_screened:,}",
                   "last_event": (p.last_event or "—")[:16].replace("T", " ")},
        "formula": formula,
        "evidence": rows,
        "note": "Counted from stored decisions, not estimated. Click any transaction in "
                "your activity list to see the rules that fired on it.",
    }


def _spend(key: str, cid: str) -> dict[str, Any]:
    """A spending category, and the transactions inside it."""
    txns = db.recent_transactions(cid, limit=1000)
    s = insights.spend_analytics(cid, txns)

    if key == "_total":
        proj = insights.monthly_projection(cid, txns)
        return {
            "label": "Spending this month",
            "subtitle": f"{proj.spent_so_far:,.0f} {proj.currency} so far",
            "headline": f"{proj.projected_total:,.0f} {proj.currency} projected",
            "headline_class": "pill-ok" if proj.on_track else "pill-warn",
            "what": "A straight-line run rate: what you have spent so far, divided by the "
                    "days gone, times the days in the month. Deliberately simple so you "
                    "can check it in your head.",
            "inputs": {"spent_so_far": f"{proj.spent_so_far:,.2f} {proj.currency}",
                       "days_elapsed": proj.days_elapsed,
                       "days_in_month": proj.days_in_month,
                       "daily_rate": f"{proj.daily_rate:,.2f} {proj.currency}",
                       "previous_month": f"{proj.previous_month:,.2f} {proj.currency}"},
            "formula": (f"{proj.spent_so_far:,.2f} ÷ {proj.days_elapsed} days"
                        f" = {proj.daily_rate:,.2f} per day\n"
                        f"{proj.daily_rate:,.2f} × {proj.days_in_month} days"
                        f" = {proj.projected_total:,.2f} projected\n"
                        f"vs {proj.previous_month:,.2f} last month"
                        f"  →  {proj.vs_previous_pct:+.1f}%"),
            "evidence": [{"category": c, "so_far": f"{sf:,.0f}", "projected": f"{pr:,.0f}"}
                         for c, sf, pr in proj.by_category_projected],
            "remediation": ("On track against last month."
                            if proj.on_track else
                            "Running ahead of last month. The categories above are sorted by "
                            "projected spend if you want to know where it is going."),
            "passed": proj.on_track,
        }

    total = sum(v for _, v in s.by_category)
    match = next((v for c, v in s.by_category if c == key), None)
    if match is None:
        return {}

    rows = [{"when": t.timestamp[:16].replace("T", " "), "merchant": t.merchant,
             "where": f"{t.city}, {t.country}", "channel": t.channel,
             "amount": f"{t.amount:,.2f} {t.currency}"}
            for t in txns if t.merchant_category == key][:30]

    return {
        "label": f"{key} spending",
        "subtitle": f"{match:,.0f} {s.currency} over 90 days",
        "headline": f"{(match / total * 100 if total else 0):.1f}% of your spending",
        "headline_class": "pill-info",
        "what": f"Every {key} transaction in the last 90 days, and what they add up to.",
        "inputs": {"category_total_90d": f"{match:,.2f} {s.currency}",
                   "all_categories_90d": f"{total:,.2f} {s.currency}",
                   "transactions": len(rows)},
        "formula": f"{match:,.2f} ÷ {total:,.2f} × 100 = "
                   f"{(match / total * 100 if total else 0):.1f}% of 90-day spending",
        "evidence": rows,
    }


# What each outcome means, said the way a person would say it. No enum ever reaches the
# customer: "FREEZE_AND_ESCALATE" is a database value, not an explanation.
VERDICTS: dict[str, tuple[str, str, str]] = {
    "ALLOW": ("We let this through", "ok",
              "Nothing about this payment looked wrong, so it went through normally."),
    "CHALLENGE": ("We asked you to confirm it", "warn",
                  "A few things about this payment were unusual — not enough to stop it, "
                  "but enough that we wanted to hear from you before it went through."),
    "FREEZE_AND_ESCALATE": ("We blocked this", "danger",
                            "This looked enough like fraud that we stopped it and sent it "
                            "to a person to review, rather than deciding on our own."),
    "QUARANTINE": ("We held this back", "danger",
                   "Something in this payment's details tried to interfere with our "
                   "checks, so we set it aside for a person to look at."),
}


def _txn(key: str, cid: str) -> dict[str, Any]:
    """One transaction, explained to the person it happened to.

    This is the answer to "why was this flagged?" and it has two halves that matter
    equally. The first is what fired. The second — which did not exist at all until now —
    is what *didn't*: the checks that were evaluated and found not to apply, and the ones
    a travel notice suppressed. A screen that only ever lists reasons to be suspicious
    reads like an accusation; the reassurance half is what makes it an explanation.
    """
    txn = db.get_transaction(key)
    if txn is None:
        return {}
    # An analyst working the ops console has to be able to open any transaction; a
    # customer only ever their own. Scoping to `cid` unconditionally meant "Full detail"
    # in the alert queue could never open anything.
    user = auth.current_user() or {}
    if txn.customer_id != cid and user.get("role") not in ("analyst", "admin"):
        return {}
    staff = user.get("role") in ("analyst", "admin")

    d = db.get_decision(key)
    if d is None:
        return {
            "label": txn.merchant,
            "subtitle": f"{txn.amount:,.2f} {txn.currency} · {txn.timestamp[:16].replace('T', ' ')}",
            "sections": [
                {"kind": "verdict", "headline": "Not checked yet", "tone": "info",
                 "plain": "This payment has not been through our fraud checks yet."},
                {"kind": "kv", "title": "The payment",
                 "items": {"amount": f"{txn.amount:,.2f} {txn.currency}",
                           "merchant": txn.merchant,
                           "where": f"{txn.city}, {txn.country}"}},
            ],
        }

    facts = insights.parse_guardrail_notes(d.get("guardrail_notes"))
    hits = d.get("rule_hits") or []
    fired_ids = [str(h.get("rule_id")) for h in hits]
    rule_score, risk = int(d["rule_score"]), int(d["risk_score"])

    sections: list[dict[str, Any]] = []

    # --- 1. the verdict, in a sentence -------------------------------------
    headline, tone, plain = VERDICTS.get(
        d["action"], ("We reviewed this", "info", "This payment was checked."))
    sections.append({"kind": "verdict", "headline": headline, "tone": tone, "plain": plain})

    # --- 2. how the score was built ----------------------------------------
    # One bar per everyday AREA rather than per rule. Two reasons: stackedBars elides its
    # row label past 15 characters, and every rule title is longer than that; and a
    # customer reads five areas far more easily than twelve individual checks. The
    # individual checks are still visible -- they are the segments, and each carries its
    # own SVG <title> tooltip.
    if hits:
        area_hits: dict[str, list[dict[str, Any]]] = {}
        for h in hits:
            area_hits.setdefault(rules.meta(h.get("rule_id")).category, []).append(h)
        area_rows = sorted(
            area_hits.items(),
            key=lambda kv: rules.CATEGORIES.index(kv[0]) if kv[0] in rules.CATEGORIES else 99)
        height = 60 + 34 * len(area_rows)
        sections.append({
            "kind": "chart", "title": "What added up to the score",
            "chart_type": "stackedBars", "height": height,
            "data": [{"label": area,
                      "total": sum(int(h.get("weight") or 0) for h in group),
                      "parts": [{"label": rules.meta(h.get("rule_id")).title,
                                 "value": int(h.get("weight") or 0)} for h in group]}
                     for area, group in area_rows],
            "opts": {"height": height},
            "caption": f"Each check that applied adds its own points. Together they came "
                       f"to {rule_score} out of 100.",
        })

    # --- 3. rule score -> final risk ---------------------------------------
    # The model's raw score is NOT persisted and `_blend`'s rounding makes any attempt to
    # invert it approximate. Showing an inferred number as if it were a recorded fact is
    # not something a bank UI gets to do, so state the policy instead -- which is the
    # better line anyway, because the asymmetry is the point.
    if not d.get("llm_used"):
        policy = ("The checks were clear enough on their own, so no AI was involved at "
                  "all and this decision cost nothing to make.")
    elif risk < rule_score:
        policy = ("Our AI reviewer argued this was safer than the checks assumed. When it "
                  "argues in your favour we give it 65% of the weight.")
    elif risk > rule_score:
        policy = ("Our AI reviewer thought this was riskier. When it argues against you "
                  "we give it only 25% — the checks are already good at spotting risk, "
                  "and we would rather not interrupt you on a hunch.")
    elif rule_score >= 90:
        policy = ("The score from the checks was high enough that no opinion could talk "
                  "past it.")
    else:
        policy = "Our AI reviewer agreed with the checks, so the score did not move."

    sections.append({
        "kind": "chart", "title": "From the checks to the final score",
        "chart_type": "bars", "height": 160,
        "data": [{"label": "Checks", "value": rule_score},
                 {"label": "Final", "value": risk}],
        "opts": {"height": 160},
        "caption": policy,
    })

    # --- 4. the three-state checklist, grouped by everyday area ------------
    # Derived by SET ARITHMETIC over what was stored, never by re-running rules.evaluate().
    # Four rules read `history`, which has grown since this decision was made, and
    # rule_frozen_card reads the customer's LIVE card_frozen flag -- so after the fraud
    # injection beat freezes the hero card, a re-run would re-score every past transaction
    # to ~100 and contradict the number printed two sections above.
    suppressed_ids = set(facts.travel_suppressed)
    fired = set(fired_ids)
    checked_ids = [rid for rid in rules.RULE_META if rid not in fired | suppressed_ids]

    if hits:
        sections.append({
            "kind": "checklist", "title": "Why this was flagged",
            "items": [{"label": rules.meta(h.get("rule_id")).title, "state": "fired",
                       "detail": f"{rules.meta(h.get('rule_id')).plain}  "
                                 f"(+{h.get('weight')} points)"}
                      for h in hits],
        })

    if suppressed_ids:
        sections.append({
            "kind": "checklist",
            "title": "We would have flagged this, but you told us you were travelling",
            "subtitle": "Your travel notice cancelled these out before they counted.",
            "items": [{"label": rules.meta(rid).title, "state": "suppressed",
                       "detail": f"{rules.meta(rid).plain}  "
                                 f"(would have added {rules.meta(rid).points} points)"}
                      for rid in sorted(suppressed_ids)],
        })

    if checked_ids:
        by_area: dict[str, list[str]] = {}
        for rid in checked_ids:
            by_area.setdefault(rules.meta(rid).category, []).append(rules.meta(rid).title)
        sections.append({
            "kind": "checklist", "title": "Other things we check on every payment",
            # Honest wording. These were evaluated and did not apply -- that is not the
            # same as "verified", and a neutral dot is not a green tick for a reason.
            "subtitle": "We looked at each of these and they did not apply here.",
            "items": [{"label": area, "state": "clear", "detail": ", ".join(titles)}
                      for area, titles in
                      sorted(by_area.items(), key=lambda kv: rules.CATEGORIES.index(kv[0])
                             if kv[0] in rules.CATEGORIES else 99)],
        })

    # --- 5. the detail table ------------------------------------------------
    if hits:
        sections.append({
            "kind": "table", "title": "The detail",
            "columns": [{"key": "check", "label": "What we checked"},
                        {"key": "meaning", "label": "What it means"},
                        {"key": "points", "label": "Points", "num": True}],
            "rows": [{"check": rules.meta(h.get("rule_id")).title,
                      "meaning": rules.meta(h.get("rule_id")).plain,
                      "points": f"+{h.get('weight')}"} for h in hits],
            "note": f"Total {rule_score} of 100. Under 30 goes through, over 90 is "
                    f"stopped, and anything in between gets an AI review.",
        })
        if staff:
            # The raw rule text is precise and full of jargon. It belongs to the analyst,
            # not to the customer whose payment this was.
            sections.append({
                "kind": "table", "title": "Technical detail (fraud operations)",
                "columns": [{"key": "rule", "label": "Rule"},
                            {"key": "reason", "label": "Recorded reason"},
                            {"key": "points", "label": "Weight", "num": True}],
                "rows": [{"rule": h.get("rule_id"), "reason": h.get("reason"),
                          "points": h.get("weight")} for h in hits],
            })

    # --- 6. what the AI weighed --------------------------------------------
    if d.get("llm_used"):
        if facts.key_factors:
            sections.append({
                "kind": "checklist", "title": "What our AI reviewer weighed",
                "items": [{"label": f, "state": "clear"} for f in facts.key_factors],
            })
        if d.get("reasoning"):
            sections.append({"kind": "note", "tone": "info", "body": d["reasoning"]})
        if facts.counterfactual:
            sections.append({"kind": "text", "title": "What would have changed its mind",
                             "body": facts.counterfactual})
        cited = d.get("cited_case_ids") or []
        if cited:
            sections.append({
                "kind": "pills", "title": "Past cases it reasoned from",
                "items": [{"label": c, "tone": "violet"} for c in cited],
            })
            sections.append({
                "kind": "text",
                "body": "Our AI is not allowed to invent a precedent. Every case listed "
                        "above is a real one it was shown, and any it cannot point to is "
                        "dropped before you ever see this.",
            })
        elif facts.llm_unavailable:
            sections.append({
                "kind": "note", "tone": "warn",
                "body": "Our AI reviewer could not be reached for this one, so the "
                        "decision was made by the checks alone.",
            })

    # Rule pills stay clickable, but only for staff -- a customer gets the plain-English
    # names above and has no use for a rule ID. Works inside the modal because openModal()
    # re-runs wireDrilldowns() over its own content.
    if staff and fired_ids:
        sections.append({
            "kind": "pills", "title": "Open a rule",
            "items": [{"label": rid, "tone": "warn", "drill": f"rule:{rid}"}
                      for rid in fired_ids],
        })

    # --- the facts, last, for anyone who wants them ------------------------
    kv: dict[str, Any] = {
        "when": txn.timestamp[:16].replace("T", " "),
        "amount": f"{txn.amount:,.2f} {txn.currency}",
        "where": f"{txn.city}, {txn.country}",
        "how it was paid": txn.channel.replace("_", " "),
        "kind of business": txn.merchant_category.replace("_", " "),
        "score from the checks": f"{rule_score} of 100",
        "final score": f"{risk} of 100",
        "AI reviewer involved": "yes" if d.get("llm_used") else "no — the checks were clear",
        "decided in": f"{d.get('latency_ms') or 0} ms",
    }
    if facts.pii_tokenized:
        kv["personal details hidden from the AI"] = (
            f"{facts.pii_tokenized} ({', '.join(facts.pii_kinds)})"
            if facts.pii_kinds else str(facts.pii_tokenized))
    sections.append({"kind": "kv", "title": "The payment", "items": kv})

    return {
        "label": f"{txn.merchant} · {txn.amount:,.0f} {txn.currency}",
        "subtitle": f"{txn.timestamp[:16].replace('T', ' ')} · {txn.city}, {txn.country}",
        "sections": sections,
    }


def _alert(key: str, cid: str) -> dict[str, Any]:
    """An alert as the analyst sees it. Staff-only — gated below.

    Delegates the body to `_txn`, which is where the explanation lives. Duplicating it
    here is how the two views drifted apart in the first place: this one still rendered
    raw rule IDs after the transaction view had stopped.
    """
    alert = db.get_alert(key)
    if alert is None:
        return {}
    txn = db.get_transaction(alert.txn_id)
    d = db.get_decision(alert.txn_id) or {}

    detail = _txn(alert.txn_id, cid) if txn is not None else {}
    if detail.get("sections"):
        head: list[dict[str, Any]] = [{
            "kind": "kv", "title": "Case",
            "items": {"alert": alert.alert_id,
                      "customer": alert.customer_id,
                      "transaction": alert.txn_id,
                      "region": alert.region or "—",
                      "status": alert.status,
                      "raised": alert.created_at[:16].replace("T", " ")},
        }]
        if alert.status != "PENDING":
            head.append({
                "kind": "note",
                "tone": "danger" if alert.outcome == "confirmed_fraud" else "ok",
                "body": f"{(alert.outcome or '').replace('_', ' ') or 'Resolved'} by "
                        f"{alert.resolved_by or 'an analyst'}"
                        + (f" — “{alert.analyst_note}”" if alert.analyst_note else "")
                        + (f"  Written into the knowledge store as {alert.learned_case_id}, "
                           f"retrievable from now on."
                           if alert.learned_case_id else ""),
            })
        return {
            "label": f"{alert.alert_id} · {alert.region or 'unknown region'}",
            "subtitle": alert.summary,
            "sections": head + detail["sections"],
        }

    # The transaction behind the alert is missing — degrade to what the alert itself knows.
    hits = d.get("rule_hits") or []
    return {
        "label": f"{alert.alert_id} · {alert.region or 'unknown region'}",
        "subtitle": alert.summary,
        "headline": f"Risk {alert.risk_score}/100",
        "headline_class": _risk_class(alert.risk_score),
        "grade": alert.status,
        "what": "Why this reached the queue, and what the agent had in front of it.",
        "inputs": {
            "customer": alert.customer_id,
            "transaction": alert.txn_id,
            "amount": f"{txn.amount:,.2f} {txn.currency}" if txn else "—",
            "where": f"{txn.city}, {txn.country}" if txn else "—",
            "region": alert.region or "—",
            "status": alert.status,
            "raised": alert.created_at[:16].replace("T", " "),
            "ai_analyst_consulted": "yes" if d.get("llm_used") else "no",
        },
        "formula": "\n".join(f"{h.get('weight'):>3}  {h.get('rule_id')}"
                             for h in hits) or "no rule fired",
        "evidence": [{"rule": h.get("rule_id"), "points": h.get("weight"),
                      "why it fired": h.get("reason")} for h in hits],
        "remediation": d.get("reasoning") or "",
        "note": ("Cited precedents: " + ", ".join(d.get("cited_case_ids") or [])
                 if d.get("cited_case_ids") else "No precedents cited."),
    }


def _cost(key: str, _cid: str) -> dict[str, Any]:
    """The cost-effectiveness argument, with its arithmetic exposed."""
    c = db.cost_summary()

    if key == "avoided":
        return {
            "label": "Transactions decided without a model",
            "subtitle": f"{c['avoided_pct']:.1f}% of all scored volume",
            "headline": f"{c['avoided']:,} of {c['total_transactions']:,}",
            "headline_class": "pill-ok",
            "what": "Deterministic rules run before the model. When the rule score is "
                    "clearly low or clearly high, the decision is already made and no "
                    "model call happens at all. This is the entire cost argument, and it "
                    "is measured here rather than asserted.",
            "inputs": {"total_scored": f"{c['total_transactions']:,}",
                       "needed_a_model": f"{c['llm_transactions']:,}",
                       "decided_by_rules_alone": f"{c['avoided']:,}"},
            "formula": (f"{c['total_transactions']:,} scored"
                        f" − {c['llm_transactions']:,} model-scored"
                        f" = {c['avoided']:,} decided by rules alone\n"
                        f"{c['avoided']:,} ÷ {c['total_transactions']:,} × 100"
                        f" = {c['avoided_pct']:.1f}%"),
            "remediation": "The cheap path takes about 9 ms. A model call takes about 3.4 s.",
            "passed": c["avoided_pct"] >= 90,
        }

    if key == "saving":
        return {
            "label": "Cost saving vs sending everything to the model",
            "subtitle": f"${c['saved_usd']:,.2f} saved on this volume",
            "headline": f"${c['actual_cost_usd']:,.2f} vs ${c['naive_cost_usd']:,.2f}",
            "headline_class": "pill-ok",
            "what": "What this volume actually cost, against what it would have cost if "
                    "every transaction were sent to the model. Measured from recorded "
                    "token counts, not estimated from a price list.",
            "inputs": {"model_calls": f"{c['llm_calls']:,}",
                       "prompt_tokens": f"{c['prompt_tokens']:,}",
                       "completion_tokens": f"{c['completion_tokens']:,}",
                       "cost_per_model_scored_txn": f"${c['cost_per_llm_txn_usd']:.5f}",
                       "actual_spend": f"${c['spent_so_far_usd']:.4f}"},
            "formula": (f"actual    {c['total_transactions']:,} txns"
                        f" → ${c['actual_cost_usd']:,.2f}\n"
                        f"all-model {c['total_transactions']:,} txns"
                        f" × ${c['cost_per_llm_txn_usd']:.5f}"
                        f" = ${c['naive_cost_usd']:,.2f}\n"
                        f"{'-' * 44}\n"
                        f"saved     ${c['saved_usd']:,.2f}"),
            "remediation": "Cost per model-scored transaction is derived from real measured "
                           "token counts at published gpt-4.1 rates.",
            "passed": True,
        }

    if key == "latency":
        return {
            "label": "Decision latency",
            "subtitle": f"{c['avg_latency_ms']:,.0f} ms average on model calls",
            "headline": f"{c['avg_latency_ms']:,.0f} ms",
            "headline_class": "pill-info",
            "what": "How long a decision takes. The two paths are wildly different, which "
                    "is why the split matters operationally and not just financially.",
            "inputs": {"avg_model_call_ms": f"{c['avg_latency_ms']:,.0f}",
                       "rules_only_path_ms": "~9",
                       "model_calls_made": f"{c['llm_calls']:,}"},
            "formula": "mean latency_ms over recorded model telemetry",
        }
    return {}


def _rule(key: str, _cid: str) -> dict[str, Any]:
    """One check, explained. Metadata comes from `core.rules.RULE_META`.

    This used to keep its own RULE_CATALOGUE copy of the weights and wording, which drifted
    from the engine: five weights were wrong and four of its IDs -- IMPOSSIBLE_TRAVEL,
    VELOCITY, MERCHANT_RISK and a missing VELOCITY_ELEVATED -- were not rule IDs the engine
    ever emits, so those pills 404'd on click. Reading the engine's own table is what makes
    a second copy impossible rather than merely discouraged.
    """
    rule_id = key.upper()
    if rule_id not in rules.RULE_META:
        return {}
    m = rules.RULE_META[rule_id]

    same_area = [o.title for o in rules.RULE_META.values()
                 if o.category == m.category and o.rule_id != rule_id]

    return {
        "label": m.title,
        "subtitle": f"Adds {m.points} points to the risk score",
        "headline": f"+{m.points} points",
        "headline_class": "pill-warn" if m.points < 30 else "pill-danger",
        "what": m.plain,
        "inputs": {
            "area": m.category,
            "points_added": m.points,
            "other_checks_in_this_area": ", ".join(same_area) or "none",
            "checks_in_total": len(rules.RULE_META),
        },
        "formula": "Every check that applies adds its points together, capped at 100.\n\n"
                   "Under 30  →  allowed, with no AI involved and no cost.\n"
                   "Over 90   →  blocked, with no AI involved and no cost.\n"
                   "In between →  an AI reviewer looks at it against similar past cases.",
        "remediation": m.fix,
        "note": (f"Technical detail: {m.technical}\n\n" if m.technical else "") +
                f"Reference {rule_id}. These checks are deterministic: the same "
                f"transaction always produces the same score, which is why they can be "
                f"trusted to decide without an AI.",
    }


# --------------------------------------------------------------------------- #

DISPATCH: dict[str, Callable[[str, str], dict[str, Any]]] = {
    "health": _health,
    "security": _security,
    "protection": _protection,
    "spend": _spend,
    "txn": _txn,
    "alert": _alert,
    "cost": _cost,
    "rule": _rule,
}

# Kinds that expose data beyond the signed-in customer's own account.
STAFF_ONLY = {"alert", "cost"}


def _band(value: float, good: float, ok: float) -> str:
    return "pill-ok" if value >= good else "pill-warn" if value >= ok else "pill-danger"


def _risk_class(score: int) -> str:
    return "pill-ok" if score < 30 else "pill-warn" if score < 70 else "pill-danger"


@bp.get("/<kind>/<path:key>")
@auth.login_required
def drill(kind: str, key: str):
    handler = DISPATCH.get(kind)
    if handler is None:
        return _fail(f"No drill-down of kind '{kind}'.")

    user = auth.current_user() or {}
    if kind in STAFF_ONLY and user.get("role") not in ("analyst", "admin"):
        db.audit(actor=f"{user.get('role')}:{user.get('username')}", event_type="GUARDRAIL",
                 subject_id=f"drill:{kind}:{key}",
                 detail=f"Blocked drill-down '{kind}' for role '{user.get('role')}'")
        return _fail("That detail is only available to fraud operations staff.", 403)

    # Always the session's customer, never the key. Otherwise this endpoint is an IDOR.
    cid = auth.active_customer_id()

    try:
        payload = handler(key, cid)
    except Exception as exc:                      # a broken card must not break the page
        return _fail(f"Could not assemble that detail: {exc}", 500)

    if not payload:
        return _fail("No detail recorded for that item.")
    return jsonify(payload)
