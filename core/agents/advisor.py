"""The Advisor agent -- personalised recommendations for the customer dashboard.

Grounded in the deterministic analysis from core/insights.py. The agent does not compute
figures; it is handed real ones and asked to decide what is worth telling this particular
customer and how to say it. That split matters: a model that invents "you spent 4,200 on
groceries" is worse than useless in a banking product, so it never gets the chance.

Falls back to deterministic recommendations when the model is unavailable, so the
dashboard is never empty.

Recommendations are advice about account security and spending patterns. They are
deliberately NOT investment or financial product advice -- the system prompt forbids it
and the fallback path cannot produce it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .. import db, insights, llm, security

SYSTEM_PROMPT = """You are a retail banking assistant writing the personalised insights \
panel on a customer's account dashboard.

You are given REAL, already-computed figures about this customer's account. Use only \
those figures. Never invent a number, a merchant, a date or a transaction. If you want to \
reference a figure that was not given to you, leave it out.

Write 3 to 5 recommendations. Each must be:
  - Specific to what you were given, not generic banking advice.
  - Actionable -- the customer can do something about it today.
  - Honest about severity. Do not manufacture alarm, and do not reassure past a real problem.
  - Short: a title under 60 characters, and one or two sentences of body.

Prioritise in this order: security issues that need action, then avoidable friction \
(such as a missing travel notice), then spending patterns worth knowing, then positive \
confirmation when things are genuinely fine.

Do NOT give investment advice, recommend financial products, or suggest how to invest, \
borrow or move money. You are not a licensed adviser. Stick to account security, card \
usage, spending awareness and features the customer already has.

{injection_rule}

Respond with ONLY a JSON array, no markdown fences and no prose:
[
  {{
    "category": "security" | "protection" | "spending" | "feature" | "positive",
    "severity": "action" | "attention" | "info" | "good",
    "title": "<under 60 chars>",
    "body": "<1-2 sentences>",
    "action": "<a short button label, or empty string>"
  }}
]"""


@dataclass
class Recommendation:
    category: str
    severity: str
    title: str
    body: str
    action: str = ""

    @property
    def icon(self) -> str:
        return {
            "security": "🔐", "protection": "🛡️", "spending": "📊",
            "feature": "✨", "positive": "✅",
        }.get(self.category, "•")

    @property
    def colour(self) -> str:
        return {
            "action": "#b91c1c", "attention": "#b45309",
            "info": "#2563eb", "good": "#15803d",
        }.get(self.severity, "#64748b")


@dataclass
class AdvisorResult:
    recommendations: list[Recommendation] = field(default_factory=list)
    generated_by: str = "rules"     # "model" | "rules"
    latency_ms: int = 0
    error: str | None = None


def _facts(customer_id: str) -> dict[str, Any]:
    """Assemble the grounded figures the agent is allowed to talk about."""
    customer = db.get_customer(customer_id)
    health = insights.account_health(customer_id)
    sec = insights.security_posture(customer_id)
    prot = insights.protection_stats(customer_id)
    spend = insights.spend_analytics(customer_id)
    proj = insights.monthly_projection(customer_id)
    actions = insights.upcoming_actions(customer_id)

    return {
        "card_frozen": bool(customer and customer.card_frozen),
        "health_score": health.score,
        "health_grade": health.grade,
        "security_score": sec.score,
        "security_failing_checks": [lbl for lbl, ok, _ in sec.checks if not ok],
        "password_age_days": sec.password_age_days,
        "transactions_screened": prot.transactions_screened,
        "fraud_blocked": prot.fraud_blocked,
        "attacks_blocked": prot.injection_blocked,
        "false_declines_avoided": prot.false_positives_prevented,
        "pii_fields_tokenized": prot.pii_fields_tokenized,
        "currency": spend.currency,
        "spend_last_30d": round(spend.total_30d, 2),
        "spend_change_pct": round(spend.change_pct, 1),
        "transactions_last_30d": spend.transaction_count_30d,
        "top_categories": [{"category": c, "amount": round(a, 2)}
                           for c, a in spend.by_category[:4]],
        "top_merchants": [{"merchant": m, "visits": n, "amount": round(a, 2)}
                          for m, n, a in spend.top_merchants[:4]],
        "month_spent_so_far": round(proj.spent_so_far, 2),
        "month_projected_total": round(proj.projected_total, 2),
        "previous_month_total": round(proj.previous_month, 2),
        "projection_change_pct": round(proj.vs_previous_pct, 1),
        "open_actions": [a.title for a in actions],
    }


def _fallback(customer_id: str) -> list[Recommendation]:
    """Deterministic recommendations. The dashboard must never be blank."""
    f = _facts(customer_id)
    recs: list[Recommendation] = []

    if f["card_frozen"]:
        recs.append(Recommendation(
            "security", "action", "Your card is frozen",
            "We stopped a transaction that didn't match your usual pattern. "
            "Confirm whether it was you and we'll restore the card immediately.",
            "Confirm with the assistant"))

    for check in f["security_failing_checks"]:
        if "Travel" in check:
            recs.append(Recommendation(
                "feature", "info", "Add a travel notice before your next trip",
                "Tell us where you're going and we won't flag your card for being "
                "abroad. It takes about ten seconds.", "Add travel notice"))
        elif "Password" in check:
            recs.append(Recommendation(
                "security", "attention", "Your password is getting old",
                f"It was last changed {f['password_age_days']} days ago. "
                "A long passphrase is easier to remember and much harder to crack.",
                "Check password strength"))
        elif "alert" in check.lower():
            recs.append(Recommendation(
                "security", "action", "You have alerts awaiting a response",
                "Confirming or rejecting them helps us protect you more accurately.",
                "Review alerts"))

    if f["fraud_blocked"]:
        recs.append(Recommendation(
            "protection", "good", f"We blocked {f['fraud_blocked']} suspicious transaction(s)",
            f"Across {f['transactions_screened']} transactions screened on your account. "
            f"{f['pii_fields_tokenized']} personal data fields were tokenized before any "
            "AI processing.", ""))

    if f["previous_month_total"] and abs(f["projection_change_pct"]) > 15:
        direction = "above" if f["projection_change_pct"] > 0 else "below"
        recs.append(Recommendation(
            "spending", "info" if f["projection_change_pct"] > 0 else "good",
            f"Spending tracking {abs(f['projection_change_pct']):.0f}% {direction} last month",
            f"You're on course for about {f['month_projected_total']:,.0f} "
            f"{f['currency']} this month, against {f['previous_month_total']:,.0f} last month.",
            "See projection"))

    if f["top_categories"]:
        top = f["top_categories"][0]
        recs.append(Recommendation(
            "spending", "info", f"{top['category'].replace('_', ' ').title()} is your largest category",
            f"{top['amount']:,.0f} {f['currency']} over the last 90 days.", ""))

    if not recs:
        recs.append(Recommendation(
            "positive", "good", "Your account is in good shape",
            f"Health score {f['health_score']}/100. Nothing needs your attention today.", ""))

    return recs[:5]


def recommend(customer_id: str, *, use_model: bool = True) -> AdvisorResult:
    """Generate recommendations. Never raises."""
    import time
    started = time.perf_counter()
    facts = _facts(customer_id)

    if not use_model:
        return AdvisorResult(_fallback(customer_id), "rules",
                             int((time.perf_counter() - started) * 1000))

    try:
        from .fraud_analyst import extract_json
        system = SYSTEM_PROMPT.format(injection_rule=security.INJECTION_SYSTEM_RULE)
        user = (
            "Account figures for this customer (all verified, all real):\n"
            f"{json.dumps(facts, indent=2)}\n\n"
            "Write the personalised insights panel as a JSON array."
        )
        # Stable cache key: the same account picture yields the same advice, which also
        # keeps the dashboard from re-rendering different text on every Streamlit rerun.
        import hashlib
        key = "adv-" + hashlib.sha256(
            json.dumps(facts, sort_keys=True).encode()).hexdigest()[:20]

        text, tel = llm.chat(system, user, agent="advisor", cache_key=key)
        raw = extract_json(f'{{"items": {text.strip()}}}') if text.strip().startswith("[") \
            else extract_json(text)
        items = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise ValueError("advisor did not return a list")

        recs: list[Recommendation] = []
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            body = security.scan_outbound(str(item.get("body", ""))).safe_text
            recs.append(Recommendation(
                category=str(item.get("category", "info")).lower(),
                severity=str(item.get("severity", "info")).lower(),
                title=str(item.get("title", ""))[:80],
                body=body,
                action=str(item.get("action", ""))[:40],
            ))
        recs = [r for r in recs if r.title and r.body]
        if not recs:
            raise ValueError("advisor returned no usable recommendations")

        return AdvisorResult(recs, "model", tel.latency_ms)

    except Exception as exc:
        return AdvisorResult(_fallback(customer_id), "rules",
                             int((time.perf_counter() - started) * 1000), str(exc)[:200])
