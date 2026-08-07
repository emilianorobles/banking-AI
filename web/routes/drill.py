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


def _txn(key: str, cid: str) -> dict[str, Any]:
    """One transaction: the rules that fired, the score, and the agent's reasoning."""
    txn = db.get_transaction(key)
    if txn is None or txn.customer_id != cid:
        return {}
    d = db.get_decision(key)
    if d is None:
        return {"label": txn.merchant, "subtitle": key,
                "what": "This transaction has not been scored yet.",
                "inputs": {"amount": f"{txn.amount:,.2f} {txn.currency}",
                           "merchant": txn.merchant,
                           "where": f"{txn.city}, {txn.country}"}}

    hits = d.get("rule_hits") or []
    rule_rows = [{"rule": h.get("rule_id"), "points": h.get("weight"),
                  "why it fired": h.get("reason")} for h in hits]

    formula = "\n".join(f"{h.get('weight'):>3}  {h.get('rule_id')}" for h in hits) or \
              "  0  no rule fired"
    formula += f"\n{'-' * 34}\n{d['rule_score']:>3}  rule score"
    if d.get("llm_used"):
        confidence = d.get("confidence")
        formula += (f"\n\nAI analyst reviewed it against "
                    f"{len(d.get('retrieved_case_ids') or [])} similar historical cases"
                    + (f" ({confidence:.0%} confident)" if confidence else "") + ".")
    else:
        formula += ("\n\nThe rule score was decisive on its own, so no model was called "
                    "and this decision cost nothing.")
    formula += f"\n\nfinal risk {d['risk_score']}  →  {d['action']}"

    return {
        "label": f"{txn.merchant} · {txn.amount:,.0f} {txn.currency}",
        "subtitle": f"{txn.txn_id} — {d['action'].replace('_', ' ').title()}",
        "headline": f"Risk {d['risk_score']}/100",
        "headline_class": _risk_class(d["risk_score"]),
        "grade": d["risk_level"],
        "what": "Deterministic rules score first. Only when the result is genuinely "
                "ambiguous does an AI analyst look at it, and when it does it has to cite "
                "the historical cases it reasoned from.",
        "inputs": {
            "when": txn.timestamp[:16].replace("T", " "),
            "amount": f"{txn.amount:,.2f} {txn.currency}",
            "where": f"{txn.city}, {txn.country}",
            "channel": txn.channel,
            "category": txn.merchant_category,
            "rule_score": d["rule_score"],
            "final_risk": d["risk_score"],
            "ai_analyst_consulted": "yes" if d.get("llm_used") else "no — rules were decisive",
            "travel_notice_applied": "yes" if d.get("suppressed_by_travel") else "no",
            "decided_in_ms": d.get("latency_ms") or 0,
        },
        "formula": formula,
        "evidence": rule_rows,
        "remediation": d.get("reasoning") or "",
        "note": ("Cited cases: " + ", ".join(d.get("cited_case_ids") or [])
                 if d.get("cited_case_ids") else
                 "Decided by rules alone — no model call, and therefore no cost."),
        "passed": d["action"] == "ALLOW",
    }


def _alert(key: str, _cid: str) -> dict[str, Any]:
    """An alert as the analyst sees it. Staff-only — gated below."""
    alert = db.get_alert(key)
    if alert is None:
        return {}
    txn = db.get_transaction(alert.txn_id)
    d = db.get_decision(alert.txn_id) or {}
    hits = d.get("rule_hits") or []

    return {
        "label": f"{alert.alert_id} · {alert.priority}",
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
            "priority": alert.priority,
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


# Presentation metadata for the rules. The rule logic lives in core/rules.py; this is
# only the plain-English gloss the UI shows, which is a UI concern.
RULE_CATALOGUE: dict[str, tuple[str, int, str]] = {
    "CARD_ALREADY_FROZEN": ("The card is already frozen, so nothing should authorise on it.",
                            60, "Unfreeze the card once you have confirmed recent activity."),
    "AMOUNT_EXTREME": ("The amount is 10× or more the largest transaction this customer has "
                       "ever made.", 35, ""),
    "AMOUNT_ANOMALY": ("The amount is 3× or more the customer's usual maximum.", 22, ""),
    "GEO_FOREIGN": ("The transaction is outside the customer's home country.", 14,
                    "File a travel notice before you go and this stops firing."),
    "GEO_NEW_COUNTRY": ("The customer has never transacted in this country before.", 16,
                        "Covered by a travel notice for the destination."),
    "IMPOSSIBLE_TRAVEL": ("Two transactions too far apart to be reached in the time between "
                          "them.", 30, ""),
    "VELOCITY": ("An unusual burst of transactions in a short window.", 20, ""),
    "CARD_TESTING": ("Several small transactions in quick succession — the pattern used to "
                     "test whether a stolen card still works.", 28, ""),
    "CNP_HIGH_VALUE": ("A high-value card-not-present transaction, where no physical card "
                       "was checked.", 18, ""),
    "ODD_HOUR": ("A transaction at an hour this customer never normally transacts.", 8, ""),
    "MERCHANT_RISK": ("A merchant category with a historically elevated fraud rate.", 12, ""),
}


def _rule(key: str, _cid: str) -> dict[str, Any]:
    entry = RULE_CATALOGUE.get(key.upper())
    if entry is None:
        return {}
    what, weight, fix = entry
    return {
        "label": key.upper(),
        "subtitle": f"Adds {weight} points to the risk score",
        "headline": f"+{weight} points",
        "headline_class": "pill-warn" if weight < 30 else "pill-danger",
        "what": what,
        "inputs": {"rule_id": key.upper(), "weight": weight,
                   "rules_in_engine": len(rules.RULES)},
        "formula": "Rules are additive. The rule score is the sum of every rule that fired, "
                   "capped at 100.\n\nUnder 30 → allow with no model call.\n"
                   "Over 90 → freeze with no model call.\n"
                   "Between → the AI analyst is consulted.",
        "remediation": fix,
        "note": "Rules are deterministic and auditable: the same transaction always "
                "produces the same rule score, which is why they can be trusted to decide "
                "without a model.",
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
