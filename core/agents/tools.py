"""The tool registry -- what makes this an agent rather than a chatbot.

Every tool declares whether it needs human approval. Irreversible or
customer-visible actions (freezing a card, raising a dispute) never execute on the
model's say-so; they return a proposal that the UI turns into a confirmation step.
That is the "balance autonomy with human approval checkpoints" requirement, enforced
in code rather than in a prompt.

Tools are scoped to the authenticated customer. `customer_id` is injected by the caller
from the session, never taken from the model -- otherwise a prompt injection could read
another customer's account by simply asking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .. import db, insights, money, notifications, rag, travel
from ..contracts import ToolCall, new_id


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, str]
    handler: Callable[..., Any]
    requires_approval: bool = False
    reads_only: bool = True
    # How the customer hears about it. Only write tools carry one; see `execute`.
    notify_subject: str = ""
    notify_severity: str = "ok"


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, parameters: dict[str, str],
         requires_approval: bool = False, reads_only: bool = True,
         notify_subject: str = "", notify_severity: str = "ok"):
    def decorator(fn):
        REGISTRY[name] = ToolSpec(name, description, parameters, fn,
                                  requires_approval, reads_only,
                                  notify_subject, notify_severity)
        return fn
    return decorator


# --------------------------------------------------------------------------- #
# Read-only tools
# --------------------------------------------------------------------------- #

@tool("get_account_summary",
      "Get the customer's account overview: card status, spending baseline, home country, "
      "and any active travel notices.",
      {})
def get_account_summary(customer_id: str) -> dict[str, Any]:
    customer = db.get_customer(customer_id)
    if customer is None:
        return {"error": "Account not found."}
    notices = travel.active_notices(customer_id)
    recent = db.recent_transactions(customer_id, limit=100)
    currency = recent[0].currency if recent else "INR"
    return {
        "name": customer.name,
        "balance": round(customer.balance, 2),
        "credit_limit": round(customer.credit_limit, 2),
        "available_credit": round(max(0.0, customer.credit_limit - customer.balance), 2),
        "currency": currency,
        "card_last4": customer.card_number[-4:],
        "card_status": "FROZEN" if customer.card_frozen else "active",
        "home_country": customer.home_country,
        "home_city": customer.home_city,
        "typical_transaction": round(customer.baseline_avg_amount, 2),
        "highest_historical": round(customer.baseline_max_amount, 2),
        "transactions_on_record": len(recent),
        "active_travel_notices": [travel.describe(n) for n in notices],
    }


@tool("list_recent_transactions",
      "List the customer's most recent transactions, newest first. Use when they ask "
      "about recent activity, charges, or suspicious transactions.",
      {"limit": "how many to return, 1-20 (default 10)"})
def list_recent_transactions(customer_id: str, limit: int = 10) -> list[dict[str, Any]]:
    try:
        limit = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        limit = 10

    out = []
    for txn in db.recent_transactions(customer_id, limit=limit):
        decision = db.get_decision(txn.txn_id)
        out.append({
            "txn_id": txn.txn_id,
            "when": txn.timestamp[:16].replace("T", " "),
            "amount": money.fmt(txn.amount, txn.currency),
            # The display string above now carries a currency symbol, so anything that
            # wants the number has to be given the number. Parsing it back out would be a
            # parser written against a format that just changed -- and would break again
            # the next time it does. `merchant` is deliberately still absent: it is
            # attacker-controlled text and no tool puts it in front of the model.
            "amount_value": round(txn.amount, 2),
            "currency": txn.currency,
            "merchant_category": txn.merchant_category,
            "location": f"{txn.city}, {txn.country}",
            "channel": txn.channel,
            "status": (decision or {}).get("action", "not yet scored"),
            "risk_score": (decision or {}).get("risk_score"),
        })
    return out


@tool("list_travel_notices",
      "Show the travel notices currently on file for the customer.",
      {})
def list_travel_notices(customer_id: str) -> list[dict[str, Any]]:
    return [
        {"notice_id": n.notice_id, "countries": n.countries,
         "from": n.start_date, "to": n.end_date, "created_via": n.created_via}
        for n in travel.active_notices(customer_id)
    ]


@tool("search_fraud_precedents",
      "Search the bank's historical fraud case knowledge store. Use to explain WHY a "
      "transaction was flagged, or to answer general questions about fraud patterns.",
      {"query": "what to search for, in plain language"})
def search_fraud_precedents(customer_id: str, query: str = "") -> list[dict[str, Any]]:
    if not str(query).strip():
        return []
    return [
        {"case_id": c.get("case_id"), "title": c.get("title"),
         "outcome": c.get("outcome"), "similarity": c.get("similarity")}
        for c in rag.search(str(query), k=3)
    ]


# --------------------------------------------------------------------------- #
# Write tools
# --------------------------------------------------------------------------- #

@tool("set_travel_notice",
      "Register upcoming travel so the customer's card is not declined abroad. Use when "
      "they mention travelling, a trip, a holiday, or being in another country. "
      "Dates must be YYYY-MM-DD.",
      {"countries": "comma-separated country names or ISO-2 codes, e.g. 'Spain, France'",
       "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"},
      reads_only=False,
      notify_subject="Travel notice filed", notify_severity="ok")
def set_travel_notice(customer_id: str, countries: str = "",
                      start_date: str = "", end_date: str = "") -> dict[str, Any]:
    country_list = [c.strip() for c in str(countries).replace(";", ",").split(",") if c.strip()]
    if not country_list:
        return {"error": "Which country or countries are you travelling to?"}
    if not start_date or not end_date:
        return {"error": "I need both a start and an end date (YYYY-MM-DD)."}
    try:
        notice = travel.create_notice(
            customer_id, country_list, start_date, end_date, created_via="chat_agent"
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "confirmed": True,
        "notice_id": notice.notice_id,
        "countries": notice.countries,
        "from": notice.start_date,
        "to": notice.end_date,
        "effect": ("Geography-based fraud rules are suppressed for these countries and "
                   "dates. Amount, velocity and channel checks stay fully active."),
    }


@tool("raise_dispute",
      "Open a dispute on a specific transaction the customer says they did not make.",
      {"txn_id": "the transaction ID to dispute", "reason": "the customer's stated reason"},
      requires_approval=True, reads_only=False,
      notify_subject="Dispute opened", notify_severity="warn")
def raise_dispute(customer_id: str, txn_id: str = "", reason: str = "") -> dict[str, Any]:
    txn = db.get_transaction(str(txn_id))
    if txn is None:
        return {"error": f"No transaction found with ID {txn_id}."}
    if txn.customer_id != customer_id:
        # Scoping check. Reaching this branch means something tried to act across accounts.
        db.audit(actor=f"customer:{customer_id}", event_type="GUARDRAIL",
                 subject_id=str(txn_id),
                 detail="Blocked cross-account dispute attempt")
        return {"error": "That transaction is not on your account."}

    db.audit(actor=f"customer:{customer_id}", event_type="DISPUTE", subject_id=txn.txn_id,
             detail=f"Dispute opened: {reason}", amount=txn.amount, merchant=txn.merchant)
    return {
        "confirmed": True,
        "txn_id": txn.txn_id,
        "amount": money.fmt(txn.amount, txn.currency),
        "provisional_credit": True,
        "sla_days": 10,
        "message": "Dispute opened. Provisional credit applies while we investigate.",
    }


@tool("freeze_card",
      "Freeze the customer's card immediately. Use when they report the card lost, stolen, "
      "or confirm fraudulent activity.",
      {"reason": "why the card is being frozen"},
      requires_approval=True, reads_only=False,
      notify_subject="Your card has been frozen", notify_severity="danger")
def freeze_card(customer_id: str, reason: str = "customer request") -> dict[str, Any]:
    customer = db.get_customer(customer_id)
    if customer is None:
        return {"error": "Account not found."}
    db.set_card_frozen(customer_id, True)
    db.audit(actor=f"customer:{customer_id}", event_type="CARD_FREEZE",
             subject_id=customer_id, detail=f"Card frozen via agent: {reason}")
    return {
        "confirmed": True,
        "card_last4": customer.card_number[-4:],
        "status": "FROZEN",
        "message": "Card frozen. A replacement can be issued from the app or by calling support.",
    }


@tool("unfreeze_card",
      "Unfreeze a previously frozen card, once the customer confirms activity was genuine.",
      {"reason": "why the card is being unfrozen"},
      requires_approval=True, reads_only=False,
      notify_subject="Your card is active again", notify_severity="ok")
def unfreeze_card(customer_id: str, reason: str = "customer confirmed activity") -> dict[str, Any]:
    customer = db.get_customer(customer_id)
    if customer is None:
        return {"error": "Account not found."}
    db.set_card_frozen(customer_id, False)
    db.audit(actor=f"customer:{customer_id}", event_type="CARD_UNFREEZE",
             subject_id=customer_id, detail=f"Card unfrozen via agent: {reason}")
    return {"confirmed": True, "card_last4": customer.card_number[-4:], "status": "active"}


@tool("report_card_lost",
      "Report the card lost or stolen. Freezes it immediately and orders a replacement. "
      "Use when the customer says their card is lost, stolen, or missing.",
      {"reason": "what happened, in the customer's words"},
      requires_approval=True, reads_only=False,
      notify_subject="Card reported lost — replacement ordered", notify_severity="danger")
def report_card_lost(customer_id: str, reason: str = "reported lost") -> dict[str, Any]:
    customer = db.get_customer(customer_id)
    if customer is None:
        return {"error": "Account not found."}
    db.set_card_frozen(customer_id, True)
    db.audit(actor=f"customer:{customer_id}", event_type="CARD_LOST", subject_id=customer_id,
             detail=f"Card reported lost/stolen: {reason}")
    return {
        "confirmed": True,
        "card_last4": customer.card_number[-4:],
        "status": "FROZEN",
        "replacement": "ordered",
        "delivery_days": "5-7",
        "message": ("Card frozen and a replacement ordered to your registered address. "
                    "Any transaction that lands on the old card from now on is declined."),
    }


@tool("get_statement",
      "Produce an account statement covering balance, spending, projections, protection "
      "activity and security checks. Use when the customer asks for a statement, a summary, "
      "or their account in writing.",
      {})
def get_statement(customer_id: str) -> dict[str, Any]:
    body = insights.build_statement(customer_id)
    db.audit(actor=f"customer:{customer_id}", event_type="STATEMENT", subject_id=customer_id,
             detail="Statement generated via the assistant")
    # Reads only, but the customer should still hear about it -- a statement being pulled
    # is exactly the event you want a record of if the account is later compromised.
    notifications.notify(
        customer_id, kind="statement", severity="ok",
        subject="Your account statement is ready",
        body="You asked the assistant for a statement and it has been generated. "
             "If this wasn't you, freeze your card straight away.",
        detail=body[:1500],
    )
    return {
        "generated": True,
        "lines": len(body.splitlines()),
        "statement": body,
        "download_url": "/statements/download",
    }


@tool("plan_travel_budget",
      "Estimate a travel budget for a destination, worked out from the customer's own "
      "spending history rather than a generic average. Use when they mention going "
      "somewhere and ask about cost, budget, or how much to take.",
      {"destination": "country name or ISO-2 code, e.g. 'Spain' or 'ES'",
       "days": "how many days the trip lasts"})
def plan_travel_budget(customer_id: str, destination: str = "",
                       days: Any = 7) -> dict[str, Any]:
    try:
        day_count = int(float(days))
    except (TypeError, ValueError):
        day_count = 7

    budget = insights.travel_budget(customer_id, str(destination), day_count)
    if budget is None:
        return {"error": f"I don't have a cost profile for {destination!r}. Try a country "
                         f"name like 'Spain' or an ISO code like 'ES'."}

    return {
        "destination": budget.destination,
        "country_code": budget.country_code,
        "days": budget.days,
        "currency": budget.currency,
        "daily_estimate": round(budget.daily_estimate, 2),
        "trip_total": round(budget.total_estimate, 2),
        "contingency_15pct": round(budget.contingency, 2),
        "recommended_total": round(budget.grand_total, 2),
        "by_category": [{"category": c, "amount": round(v, 2)} for c, v in budget.by_category],
        "how_it_was_worked_out": budget.assumptions,
        "travel_notice_covers_destination": budget.notice_covers,
        "next_step": ("A travel notice is already in place for this destination."
                      if budget.notice_covers else
                      f"No travel notice covers {budget.destination}. Offer to file one — "
                      f"without it, card transactions there score higher and may be "
                      f"challenged."),
    }


@tool("get_security_status",
      "Run a full security review: the security score with every check, the account health "
      "score, and what the bank's fraud screening has actually done for this customer. Use "
      "when they ask if their account is safe, secure, or healthy.",
      {})
def get_security_status(customer_id: str) -> dict[str, Any]:
    posture = insights.security_posture(customer_id)
    health = insights.account_health(customer_id)
    prot = insights.protection_stats(customer_id)

    return {
        "security_score": posture.score,
        "security_grade": posture.grade,
        "checks": [{"check": c.label, "result": "pass" if c.passed else "action needed",
                    "basis": c.detail} for c in posture.checks],
        "failing": [c.label for c in posture.checks if not c.passed],
        "recommendations": posture.recommendations,
        "health_score": health.score,
        "health_grade": health.grade,
        "health_components": [{"component": c.label, "earned": c.earned, "out_of": c.max,
                               "basis": c.detail} for c in health.components],
        "protection": {
            "transactions_screened": prot.transactions_screened,
            "fraud_blocked": prot.fraud_blocked,
            "challenges_issued": prot.challenges_issued,
            "false_alarms_avoided": prot.false_positives_prevented,
            "pii_fields_tokenized": prot.pii_fields_tokenized,
            "injection_attempts_blocked": prot.injection_blocked,
        },
        "summary": health.summary,
    }


@tool("set_spending_alert",
      "Set a spending alert so the customer is told when they go over an amount. Use when "
      "they ask to be warned, notified, or alerted about spending.",
      {"threshold": "the amount to alert above",
       "period": "one of: day, week, month"},
      requires_approval=True, reads_only=False,
      notify_subject="Spending alert set", notify_severity="ok")
def set_spending_alert(customer_id: str, threshold: Any = 0,
                       period: str = "month") -> dict[str, Any]:
    try:
        amount = float(threshold)
    except (TypeError, ValueError):
        return {"error": "What amount should I alert you above?"}
    if amount <= 0:
        return {"error": "The alert threshold needs to be more than zero."}

    period = str(period).lower().strip()
    if period not in ("day", "week", "month"):
        period = "month"

    alert_id = new_id("SPALERT")
    db.save_spending_alert(alert_id, customer_id, amount, period)
    db.audit(actor=f"customer:{customer_id}", event_type="SPENDING_ALERT",
             subject_id=customer_id, detail=f"Alert set above {amount:,.0f} per {period}")

    proj = insights.monthly_projection(customer_id)
    return {
        "confirmed": True,
        "alert_id": alert_id,
        "threshold": amount,
        "period": period,
        "current_projection": round(proj.projected_total, 2),
        "already_over": proj.projected_total > amount if period == "month" else False,
        "message": f"I'll tell you if you go over {amount:,.0f} in a {period}.",
    }


@tool("list_my_notifications",
      "List the alerts and notifications recently raised on the customer's account. Use "
      "when they ask what has happened, what they have missed, or about recent alerts.",
      {"limit": "how many to return (default 10)"})
def list_my_notifications(customer_id: str, limit: Any = 10) -> list[dict[str, Any]]:
    try:
        count = max(1, min(int(limit), 25))
    except (TypeError, ValueError):
        count = 10
    return [
        {"when": r["created_at"][:16].replace("T", " "), "subject": r["subject"],
         "severity": r["severity"], "read": r["read"], "detail": r["body"]}
        for r in db.list_notifications(customer_id, limit=count)
    ]


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

def describe_tools() -> str:
    """Render the registry for the agent prompt."""
    lines = []
    for spec in REGISTRY.values():
        params = (", ".join(f"{k} ({v})" for k, v in spec.parameters.items())
                  or "no parameters")
        gate = "  [REQUIRES CUSTOMER CONFIRMATION]" if spec.requires_approval else ""
        lines.append(f"- {spec.name}: {spec.description}\n    parameters: {params}{gate}")
    return "\n".join(lines)


def execute(name: str, customer_id: str, arguments: dict[str, Any],
            approved: bool | None = None) -> ToolCall:
    """Run a tool with the caller's authenticated customer_id.

    `customer_id` is passed positionally by us and cannot be overridden by the model --
    any customer_id the model tries to supply in `arguments` is discarded.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return ToolCall(name, arguments, error=f"Unknown tool '{name}'.")

    args = {k: v for k, v in (arguments or {}).items() if k != "customer_id"}

    if spec.requires_approval and approved is not True:
        # Return a proposal, not a result. The UI renders a confirm button.
        return ToolCall(name, args, result=None,
                        requires_approval=True, approved=None)

    try:
        result = spec.handler(customer_id, **args)
    except TypeError as exc:
        return ToolCall(name, args, error=f"Invalid parameters for {name}: {exc}")
    except Exception as exc:
        return ToolCall(name, args, error=f"{name} failed: {exc}")

    db.audit(actor=f"agent:tools", event_type="TOOL_CALL", subject_id=customer_id,
             detail=f"{name}({', '.join(f'{k}={v}' for k, v in args.items())})",
             approved=approved, requires_approval=spec.requires_approval)

    _notify_customer(spec, customer_id, args, result)

    return ToolCall(name, args, result=result,
                    requires_approval=spec.requires_approval,
                    approved=True if spec.requires_approval else None)


def _notify_customer(spec: ToolSpec, customer_id: str, args: dict[str, Any],
                     result: Any) -> None:
    """Tell the customer what just happened to their account.

    Hooked here rather than inside each tool on purpose: a tool added next week gets the
    notification by declaring `notify_subject`, and cannot ship without one being
    considered. Per-tool calls are the kind of thing that gets forgotten exactly once, on
    the action you would most want a record of.

    Never raises. A notification that fails must not roll back an action that succeeded --
    the customer's card really is frozen either way, and pretending otherwise is worse.
    """
    if spec.reads_only or not spec.notify_subject:
        return
    if isinstance(result, dict) and result.get("error"):
        return

    try:
        detail = ""
        if isinstance(result, dict):
            detail = str(result.get("message") or result.get("effect") or "")
        arg_text = ", ".join(f"{k}: {v}" for k, v in args.items() if v not in (None, ""))

        notifications.action_performed(
            customer_id,
            action=spec.name,
            summary=spec.notify_subject,
            detail=detail or f"{spec.name} completed. {arg_text}".strip(),
            severity=spec.notify_severity,
            tool=spec.name,
            arguments=arg_text,
            via="assistant",
        )
    except Exception:
        pass
