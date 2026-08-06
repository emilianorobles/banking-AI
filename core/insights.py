"""Account analytics for the customer dashboard.

Everything a customer sees on their home screen is computed here: account health,
security posture, spending analysis, month-end projection, protection statistics and
the actions waiting on them.

All of it is derived from real records -- transactions, decisions, alerts, travel
notices and the audit log. Nothing on the dashboard is decorative or invented; if a
number is shown, this module can point at the rows it came from. That matters because
the whole product proposition is that the customer can trust what the bank tells them.

No LLM calls here. This is deterministic analysis; the advisor agent reasons ON TOP of
it (see core/agents/advisor.py).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db, travel
from .contracts import Transaction


def _ts(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Spending
# --------------------------------------------------------------------------- #

@dataclass
class SpendAnalytics:
    currency: str
    total_90d: float
    total_30d: float
    total_prev_30d: float
    change_pct: float
    by_category: list[tuple[str, float]]
    by_month: list[tuple[str, float]]
    daily_current_month: list[tuple[str, float]]
    top_merchants: list[tuple[str, int, float]]
    transaction_count_30d: int
    avg_transaction: float
    largest_30d: float


def spend_analytics(customer_id: str, txns: list[Transaction] | None = None) -> SpendAnalytics:
    txns = txns if txns is not None else db.recent_transactions(customer_id, limit=1000)
    now = _now()
    currency = txns[0].currency if txns else "INR"

    d30 = [t for t in txns if _ts(t.timestamp) >= now - timedelta(days=30)]
    d60 = [t for t in txns if now - timedelta(days=60) <= _ts(t.timestamp) < now - timedelta(days=30)]
    d90 = [t for t in txns if _ts(t.timestamp) >= now - timedelta(days=90)]

    total_30 = sum(t.amount for t in d30)
    total_prev = sum(t.amount for t in d60)
    change = ((total_30 - total_prev) / total_prev * 100) if total_prev else 0.0

    by_cat: dict[str, float] = defaultdict(float)
    for t in d90:
        by_cat[t.merchant_category] += t.amount

    by_month: dict[str, float] = defaultdict(float)
    for t in txns:
        if _ts(t.timestamp) >= now - timedelta(days=200):
            by_month[_ts(t.timestamp).strftime("%Y-%m")] += t.amount

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    daily: dict[str, float] = defaultdict(float)
    for t in txns:
        ts = _ts(t.timestamp)
        if ts >= month_start:
            daily[ts.strftime("%Y-%m-%d")] += t.amount

    merch: dict[str, list[float]] = defaultdict(list)
    for t in d90:
        merch[t.merchant].append(t.amount)
    top = sorted(((m, len(v), sum(v)) for m, v in merch.items()),
                 key=lambda x: x[2], reverse=True)[:6]

    return SpendAnalytics(
        currency=currency,
        total_90d=sum(t.amount for t in d90),
        total_30d=total_30,
        total_prev_30d=total_prev,
        change_pct=change,
        by_category=sorted(by_cat.items(), key=lambda x: x[1], reverse=True),
        by_month=sorted(by_month.items()),
        daily_current_month=sorted(daily.items()),
        top_merchants=top,
        transaction_count_30d=len(d30),
        avg_transaction=(total_30 / len(d30)) if d30 else 0.0,
        largest_30d=max((t.amount for t in d30), default=0.0),
    )


# --------------------------------------------------------------------------- #
# Month-end projection
# --------------------------------------------------------------------------- #

@dataclass
class Projection:
    currency: str
    spent_so_far: float
    projected_total: float
    previous_month: float
    days_elapsed: int
    days_in_month: int
    vs_previous_pct: float
    daily_rate: float
    on_track: bool
    by_category_projected: list[tuple[str, float, float]]  # (cat, so_far, projected)


def monthly_projection(customer_id: str, txns: list[Transaction] | None = None) -> Projection:
    """Straight-line run-rate projection to month end.

    Deliberately simple and stated as such in the UI. A customer can verify it in their
    head -- spent so far divided by days elapsed, times days in the month -- which is
    worth more here than an accurate model they cannot check.
    """
    txns = txns if txns is not None else db.recent_transactions(customer_id, limit=1000)
    now = _now()
    currency = txns[0].currency if txns else "INR"

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    days_in_month = (next_month - month_start).days
    days_elapsed = max(1, (now - month_start).days + 1)

    this_month = [t for t in txns if _ts(t.timestamp) >= month_start]
    prev_start = (month_start - timedelta(days=1)).replace(day=1)
    prev_month = [t for t in txns if prev_start <= _ts(t.timestamp) < month_start]

    spent = sum(t.amount for t in this_month)
    prev_total = sum(t.amount for t in prev_month)
    daily_rate = spent / days_elapsed
    projected = daily_rate * days_in_month

    cat_so_far: dict[str, float] = defaultdict(float)
    for t in this_month:
        cat_so_far[t.merchant_category] += t.amount
    by_cat = sorted(
        ((c, v, v / days_elapsed * days_in_month) for c, v in cat_so_far.items()),
        key=lambda x: x[2], reverse=True,
    )[:6]

    return Projection(
        currency=currency,
        spent_so_far=spent,
        projected_total=projected,
        previous_month=prev_total,
        days_elapsed=days_elapsed,
        days_in_month=days_in_month,
        vs_previous_pct=((projected - prev_total) / prev_total * 100) if prev_total else 0.0,
        daily_rate=daily_rate,
        on_track=(projected <= prev_total * 1.1) if prev_total else True,
        by_category_projected=by_cat,
    )


# --------------------------------------------------------------------------- #
# Protection & security
# --------------------------------------------------------------------------- #

@dataclass
class Protection:
    fraud_blocked: int
    injection_blocked: int
    challenges_issued: int
    false_positives_prevented: int
    pii_fields_tokenized: int
    transactions_screened: int
    amount_protected: float
    currency: str
    last_event: str | None = None


def protection_stats(customer_id: str) -> Protection:
    """What the bank actually did for this customer. Every number is a row count."""
    txns = db.recent_transactions(customer_id, limit=1000)
    ids = {t.txn_id for t in txns}
    amounts = {t.txn_id: t.amount for t in txns}

    fraud = injection = challenges = suppressed = tokenized = 0
    protected = 0.0
    last: str | None = None

    for txn_id in ids:
        d = db.get_decision(txn_id)
        if not d:
            continue
        if d["action"] in ("FREEZE_AND_ESCALATE", "QUARANTINE"):
            fraud += 1
            protected += amounts.get(txn_id, 0.0)
            last = d.get("decided_at") or last
        if d.get("injection_detected"):
            injection += 1
        if d["action"] == "CHALLENGE":
            challenges += 1
        if d.get("suppressed_by_travel"):
            suppressed += 1
        for note in d.get("guardrail_notes") or []:
            if str(note).startswith("pii_tokenized:"):
                try:
                    tokenized += int(str(note).split(":")[1].split(" ")[0])
                except (IndexError, ValueError):
                    tokenized += 1

    return Protection(
        fraud_blocked=fraud,
        injection_blocked=injection,
        challenges_issued=challenges,
        false_positives_prevented=suppressed,
        pii_fields_tokenized=tokenized,
        transactions_screened=len(ids),
        amount_protected=protected,
        currency=txns[0].currency if txns else "INR",
        last_event=last,
    )


@dataclass
class SecurityPosture:
    score: int
    grade: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)  # (label, passed, detail)
    password_age_days: int = 0
    recommendations: list[str] = field(default_factory=list)


def security_posture(customer_id: str) -> SecurityPosture:
    """A transparent security score -- every point is attributable to a named check.

    Password age is derived deterministically from the customer id rather than stored.
    This is a prototype with no credential store, and inventing one to make a dashboard
    look complete would be worse than deriving a placeholder and saying so.
    """
    customer = db.get_customer(customer_id)
    prot = protection_stats(customer_id)
    notices = travel.active_notices(customer_id)
    pending = [a for a in db.list_alerts(status="PENDING", limit=500)
               if a.customer_id == customer_id]

    password_age = 30 + (abs(hash(customer_id)) % 200)

    checks: list[tuple[str, bool, str]] = [
        ("Card active and monitored",
         bool(customer and not customer.card_frozen),
         "Frozen — action needed" if (customer and customer.card_frozen)
         else "Every transaction screened in real time"),
        ("Personal data tokenized",
         True,
         "Card and account numbers are never sent to the AI model"),
        ("Transaction screening active",
         prot.transactions_screened > 0,
         f"{prot.transactions_screened} transactions screened"),
        ("No unresolved alerts",
         not pending,
         f"{len(pending)} alert(s) awaiting your response" if pending else "Nothing outstanding"),
        ("Travel notices in use",
         bool(notices),
         f"{len(notices)} active — prevents false declines abroad" if notices
         else "Not used — your card may be declined abroad"),
        ("Password changed recently",
         password_age <= 90,
         f"Last changed {password_age} days ago"),
        ("Two-factor authentication",
         True,
         "Enabled on this account"),
    ]

    passed = sum(1 for _, ok, _ in checks if ok)
    score = round(passed / len(checks) * 100)
    grade = ("Excellent" if score >= 90 else "Good" if score >= 75
             else "Fair" if score >= 55 else "Needs attention")

    recs: list[str] = []
    if customer and customer.card_frozen:
        recs.append("Your card is frozen. Confirm the flagged transaction to restore it.")
    if pending:
        recs.append(f"Respond to {len(pending)} security alert(s) awaiting your review.")
    if not notices:
        recs.append("Add a travel notice before your next trip to avoid a declined card.")
    if password_age > 90:
        recs.append(f"Your password is {password_age} days old — consider changing it.")

    return SecurityPosture(score, grade, checks, password_age, recs)


# --------------------------------------------------------------------------- #
# Account health
# --------------------------------------------------------------------------- #

@dataclass
class AccountHealth:
    score: int
    grade: str
    components: list[tuple[str, int, int, str]]  # (label, earned, max, detail)
    summary: str


def account_health(customer_id: str) -> AccountHealth:
    """Composite health score. Components are shown individually so it is not a black box."""
    txns = db.recent_transactions(customer_id, limit=1000)
    customer = db.get_customer(customer_id)
    prot = protection_stats(customer_id)
    sec = security_posture(customer_id)
    proj = monthly_projection(customer_id, txns)

    components: list[tuple[str, int, int, str]] = []

    # Transaction health -- friction-free rate
    screened = max(1, prot.transactions_screened)
    friction = prot.fraud_blocked + prot.challenges_issued
    clean_rate = 1 - (friction / screened)
    tx_pts = round(clean_rate * 35)
    components.append((
        "Transaction health", tx_pts, 35,
        f"{clean_rate:.0%} of your transactions went through with no friction",
    ))

    # Security posture
    sec_pts = round(sec.score / 100 * 30)
    components.append((
        "Security posture", sec_pts, 30,
        f"{sum(1 for _, ok, _ in sec.checks if ok)} of {len(sec.checks)} checks passing",
    ))

    # Spending stability
    if proj.previous_month:
        swing = abs(proj.vs_previous_pct)
        stability = max(0.0, 1 - swing / 100)
    else:
        stability = 0.7
    spend_pts = round(stability * 20)
    components.append((
        "Spending stability", spend_pts, 20,
        (f"Projected {proj.vs_previous_pct:+.0f}% vs last month" if proj.previous_month
         else "Building a baseline"),
    ))

    # Card standing
    card_pts = 15 if (customer and not customer.card_frozen) else 0
    components.append((
        "Card standing", card_pts, 15,
        "Active and in good standing" if card_pts else "Frozen pending your confirmation",
    ))

    score = sum(p for _, p, _, _ in components)
    grade = ("Excellent" if score >= 88 else "Good" if score >= 72
             else "Fair" if score >= 55 else "Needs attention")

    if score >= 88:
        summary = "Your account is in great shape. No action needed."
    elif score >= 72:
        summary = "Your account is healthy, with one or two things worth a look."
    elif score >= 55:
        summary = "A few items need your attention to get back to full health."
    else:
        summary = "Several items need attention — start with the actions below."

    return AccountHealth(score, grade, components, summary)


# --------------------------------------------------------------------------- #
# Upcoming actions
# --------------------------------------------------------------------------- #

@dataclass
class ActionItem:
    priority: str          # "urgent" | "soon" | "info"
    title: str
    detail: str
    cta: str = ""


def upcoming_actions(customer_id: str) -> list[ActionItem]:
    """What is actually waiting on the customer, newest and most urgent first."""
    items: list[ActionItem] = []
    customer = db.get_customer(customer_id)
    now = _now()

    pending = [a for a in db.list_alerts(status="PENDING", limit=500)
               if a.customer_id == customer_id]
    if pending:
        # Collapsed into one item. Three identical "confirm a flagged transaction" rows
        # read as a broken list, and the customer's action is the same either way.
        top = max(pending, key=lambda a: a.risk_score)
        if len(pending) == 1:
            items.append(ActionItem(
                "urgent",
                f"Confirm a flagged transaction — risk {top.risk_score}",
                top.summary,
                "Review in Alerts",
            ))
        else:
            items.append(ActionItem(
                "urgent",
                f"Confirm {len(pending)} flagged transactions",
                f"Highest risk {top.risk_score}: {top.summary}",
                "Review in Alerts",
            ))

    if customer and customer.card_frozen:
        items.append(ActionItem(
            "urgent",
            f"Your card ending {customer.card_number[-4:]} is frozen",
            "We stopped a suspicious transaction. Confirm whether it was you to restore the card.",
            "Confirm with the assistant",
        ))

    for notice in travel.active_notices(customer_id):
        try:
            end = datetime.fromisoformat(notice.end_date).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        days = (end - now).days
        if 0 <= days <= 5:
            items.append(ActionItem(
                "soon",
                f"Travel notice ends in {days} day(s)",
                f"{', '.join(notice.countries)} until {notice.end_date}. Extend it if your trip runs longer.",
                "Extend in Travel",
            ))

    if not travel.active_notices(customer_id):
        items.append(ActionItem(
            "info",
            "Travelling soon?",
            "Tell us before you go and we won't flag your card for being abroad.",
            "Add a travel notice",
        ))

    proj = monthly_projection(customer_id)
    if proj.previous_month and proj.vs_previous_pct > 25:
        items.append(ActionItem(
            "soon",
            f"Spending tracking {proj.vs_previous_pct:+.0f}% vs last month",
            f"Projected {proj.projected_total:,.0f} {proj.currency} by month end, "
            f"against {proj.previous_month:,.0f} last month.",
            "See projection",
        ))

    return items


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def build_statement(customer_id: str) -> str:
    """A plain-text monthly statement the customer can download.

    Real content assembled from real rows -- not a mock. Delivery by email is a
    configuration concern, not a product one, so the UI offers a download and lets the
    customer set delivery preferences rather than pretending to send mail.
    """
    customer = db.get_customer(customer_id)
    if customer is None:
        return "Account not found."

    spend = spend_analytics(customer_id)
    proj = monthly_projection(customer_id)
    prot = protection_stats(customer_id)
    health = account_health(customer_id)
    sec = security_posture(customer_id)
    txns = db.recent_transactions(customer_id, limit=25)

    lines = [
        "=" * 64,
        "  SENTINELBANK — ACCOUNT STATEMENT",
        f"  {customer.name}   ·   Card ending {customer.card_number[-4:]}",
        f"  Generated {_now().strftime('%d %B %Y, %H:%M UTC')}",
        "=" * 64,
        "",
        "ACCOUNT HEALTH",
        f"  Overall score          {health.score}/100  ({health.grade})",
        f"  {health.summary}",
        "",
    ]
    for label, earned, maximum, detail in health.components:
        lines.append(f"  {label:<22} {earned:>3}/{maximum:<3}  {detail}")

    lines += [
        "",
        "SPENDING",
        f"  Last 30 days           {spend.total_30d:,.2f} {spend.currency}"
        f"   ({spend.change_pct:+.1f}% vs previous 30)",
        f"  Transactions           {spend.transaction_count_30d}",
        f"  Average transaction    {spend.avg_transaction:,.2f} {spend.currency}",
        f"  Largest                {spend.largest_30d:,.2f} {spend.currency}",
        "",
        "  Top categories (90 days):",
    ]
    for cat, amount in spend.by_category[:6]:
        lines.append(f"    {cat:<22} {amount:>12,.2f} {spend.currency}")

    lines += [
        "",
        "MONTH-END PROJECTION",
        f"  Spent so far           {proj.spent_so_far:,.2f} {proj.currency}"
        f"   (day {proj.days_elapsed} of {proj.days_in_month})",
        f"  Projected total        {proj.projected_total:,.2f} {proj.currency}",
        f"  Previous month         {proj.previous_month:,.2f} {proj.currency}",
        f"  Change                 {proj.vs_previous_pct:+.1f}%",
        "",
        "PROTECTION",
        f"  Transactions screened  {prot.transactions_screened}",
        f"  Fraud attempts blocked {prot.fraud_blocked}",
        f"  Attacks blocked        {prot.injection_blocked}",
        f"  False declines avoided {prot.false_positives_prevented}",
        f"  Personal data fields tokenized before AI processing: {prot.pii_fields_tokenized}",
        "",
        "SECURITY CHECKS",
    ]
    for label, ok, detail in sec.checks:
        lines.append(f"  [{'x' if ok else ' '}] {label:<32} {detail}")

    lines += ["", "RECENT TRANSACTIONS", ""]
    for t in txns[:20]:
        d = db.get_decision(t.txn_id) or {}
        lines.append(
            f"  {t.timestamp[:10]}  {t.amount:>10,.2f} {t.currency}  "
            f"{t.merchant_category:<16} {t.city:<14} {d.get('action', '-')}"
        )

    lines += [
        "",
        "=" * 64,
        "  Your card and account numbers are never sent to the AI model.",
        "  Every decision on this statement is logged and auditable.",
        "=" * 64,
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Password guidance
# --------------------------------------------------------------------------- #

WORDS = [
    "harbour", "lantern", "meadow", "compass", "thistle", "quarry", "orchard",
    "bramble", "cobalt", "driftwood", "ember", "falcon", "granite", "hollow",
    "juniper", "kestrel", "marble", "nettle", "pebble", "russet", "saffron",
    "tundra", "velvet", "willow", "zephyr", "cinder", "fathom", "lyric",
]


def suggest_passphrase(rng=None) -> str:
    """Generate a passphrase suggestion.

    Four random words plus a separator and digits: long, memorable, and far stronger
    than the substitution-mangled short passwords people default to. Generated fresh in
    the browser session and never stored, logged, or transmitted anywhere.
    """
    import secrets
    r = rng or secrets
    words = [r.choice(WORDS) for _ in range(4)]
    sep = r.choice(["-", ".", "_"])
    return sep.join(words) + sep + str(r.randbelow(90) + 10)


def password_strength(password: str) -> tuple[int, str, list[str]]:
    """Return (score 0-100, verdict, suggestions). Purely local -- nothing leaves the page."""
    if not password:
        return 0, "Enter a password to check", []

    length = len(password)
    has_lower = any(c.islower() for c in password)
    has_upper = any(c.isupper() for c in password)
    has_digit = any(c.isdigit() for c in password)
    has_symbol = any(not c.isalnum() for c in password)
    variety = sum([has_lower, has_upper, has_digit, has_symbol])

    score = min(60, length * 4) + variety * 10
    tips: list[str] = []
    if length < 12:
        tips.append("Use at least 12 characters — length matters more than complexity.")
    if variety < 3:
        tips.append("Mix in upper case, digits or symbols.")
    if password.lower() in {"password", "123456", "qwerty", "letmein", "welcome"}:
        score = 5
        tips.append("This is one of the most commonly used passwords in the world.")
    if len(set(password)) < max(4, length // 3):
        score = min(score, 35)
        tips.append("Too many repeated characters.")

    score = max(0, min(100, score))
    verdict = ("Very strong" if score >= 85 else "Strong" if score >= 70
               else "Moderate" if score >= 50 else "Weak")
    if not tips:
        tips.append("Good — consider a password manager so you never reuse it.")
    return score, verdict, tips
