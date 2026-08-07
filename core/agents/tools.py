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

from dataclasses import dataclass, field
from typing import Any, Callable

from .. import db, insights, money, notifications, rag, travel
from ..contracts import ToolCall, new_id
from .context import AgentContext

# Role sets, named so a declaration reads as a sentence.
ANY_ROLE = frozenset({"customer", "analyst", "admin"})
CUSTOMER_ONLY = frozenset({"customer"})
STAFF_ONLY = frozenset({"analyst", "admin"})
ADMIN_ONLY = frozenset({"admin"})


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, str]
    handler: Callable[..., Any]
    # Which roles may invoke this at all. Enforced in `execute`, not in the prompt.
    roles: frozenset[str] = field(default_factory=lambda: ANY_ROLE)
    requires_approval: bool = False
    reads_only: bool = True
    # How the customer hears about it. Only write tools carry one; see `execute`.
    notify_subject: str = ""
    notify_severity: str = "ok"
    # Whether the handler's first argument is the full AgentContext or a bare
    # customer_id. See the note above `execute`.
    takes_context: bool = False


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, parameters: dict[str, str], *,
         roles: frozenset[str],
         requires_approval: bool = False, reads_only: bool = True,
         notify_subject: str = "", notify_severity: str = "ok",
         takes_context: bool = False):
    """Register a tool.

    `roles` is a REQUIRED keyword with no default, and that is the point. Any default is
    wrong for half the registry, and the direction of the wrong answer matters: a
    forgotten `roles=` that defaulted permissive would expose a staff or ops tool to
    customers -- an access-control hole rather than a cosmetic miss. Same reasoning as
    `rules.selfcheck()`: a mistake that is invisible at runtime should be impossible to
    make at declaration time.
    """
    def decorator(fn):
        REGISTRY[name] = ToolSpec(name, description, parameters, fn, roles,
                                  requires_approval, reads_only,
                                  notify_subject, notify_severity, takes_context)
        return fn
    return decorator


# --------------------------------------------------------------------------- #
# Read-only tools
# --------------------------------------------------------------------------- #

@tool("get_account_summary",
      "Get the customer's account overview: card status, spending baseline, home country, "
      "and any active travel notices.",
      {}, roles=ANY_ROLE)
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
      {"limit": "how many to return, 1-20 (default 10)"}, roles=ANY_ROLE)
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
            # ASCII on purpose -- this string is PROMPT TEXT. `_fmt_result` serialises the
            # result with json.dumps at its default ensure_ascii=True, so a rupee sign
            # arrives at the model as the literal escape "₹" and the model, quite
            # reasonably, writes the amount without a symbol. Forcing ensure_ascii=False
            # would fix the escaping but push non-ASCII through the TCS proxy on every
            # call, which is the encoding risk we deliberately keep out of prompt text.
            # The symbol belongs on the customer-facing paths, which get it from
            # amount_value + currency below.
            "amount": f"{txn.amount:,.2f} {txn.currency}",
            # The number itself, so nothing downstream has to parse the string above --
            # a parser written against a display format breaks the week the format
            # changes. `merchant` is deliberately absent: attacker-controlled text that
            # no tool puts in front of the model.
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
      {}, roles=ANY_ROLE)
def list_travel_notices(customer_id: str) -> list[dict[str, Any]]:
    return [
        {"notice_id": n.notice_id, "countries": n.countries,
         "from": n.start_date, "to": n.end_date, "created_via": n.created_via}
        for n in travel.active_notices(customer_id)
    ]


@tool("search_fraud_precedents",
      "Search the bank's historical fraud case knowledge store. Use to explain WHY a "
      "transaction was flagged, or to answer general questions about fraud patterns.",
      # Widened to every role with no other change: it ignores `customer_id` entirely and
      # searches a corpus of anonymised historical cases. The clearest demonstration that
      # `roles` is a real declaration and not paperwork.
      {"query": "what to search for, in plain language"}, roles=ANY_ROLE)
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
      roles=CUSTOMER_ONLY, reads_only=False,
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
      roles=CUSTOMER_ONLY, requires_approval=True, reads_only=False,
      notify_subject="Dispute opened", notify_severity="warn", takes_context=True)
def raise_dispute(ctx: AgentContext, txn_id: str = "", reason: str = "") -> dict[str, Any]:
    customer_id = ctx.customer_id
    txn = db.get_transaction(str(txn_id))
    if txn is None:
        return {"error": f"No transaction found with ID {txn_id}."}
    if txn.customer_id != customer_id:
        # Scoping check. Reaching this branch means something tried to act across accounts.
        db.audit(actor=ctx.actor, event_type="GUARDRAIL",
                 subject_id=str(txn_id),
                 detail="Blocked cross-account dispute attempt")
        return {"error": "That transaction is not on your account."}

    db.audit(actor=ctx.actor, event_type="DISPUTE", subject_id=txn.txn_id,
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
      roles=CUSTOMER_ONLY, requires_approval=True, reads_only=False,
      notify_subject="Your card has been frozen", notify_severity="danger",
      takes_context=True)
def freeze_card(ctx: AgentContext, reason: str = "customer request") -> dict[str, Any]:
    customer_id = ctx.customer_id
    customer = db.get_customer(customer_id)
    if customer is None:
        return {"error": "Account not found."}
    db.set_card_frozen(customer_id, True)
    db.audit(actor=ctx.actor, event_type="CARD_FREEZE",
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
      roles=CUSTOMER_ONLY, requires_approval=True, reads_only=False,
      notify_subject="Your card is active again", notify_severity="ok",
      takes_context=True)
def unfreeze_card(ctx: AgentContext,
                  reason: str = "customer confirmed activity") -> dict[str, Any]:
    customer_id = ctx.customer_id
    customer = db.get_customer(customer_id)
    if customer is None:
        return {"error": "Account not found."}
    db.set_card_frozen(customer_id, False)
    db.audit(actor=ctx.actor, event_type="CARD_UNFREEZE",
             subject_id=customer_id, detail=f"Card unfrozen via agent: {reason}")
    return {"confirmed": True, "card_last4": customer.card_number[-4:], "status": "active"}


@tool("report_card_lost",
      "Report the card lost or stolen. Freezes it immediately and orders a replacement. "
      "Use when the customer says their card is lost, stolen, or missing.",
      {"reason": "what happened, in the customer's words"},
      roles=CUSTOMER_ONLY, requires_approval=True, reads_only=False,
      notify_subject="Card reported lost — replacement ordered", notify_severity="danger",
      takes_context=True)
def report_card_lost(ctx: AgentContext, reason: str = "reported lost") -> dict[str, Any]:
    customer_id = ctx.customer_id
    customer = db.get_customer(customer_id)
    if customer is None:
        return {"error": "Account not found."}
    db.set_card_frozen(customer_id, True)
    db.audit(actor=ctx.actor, event_type="CARD_LOST", subject_id=customer_id,
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
      # Customer-only despite reading nothing but their own data: it notifies the customer
      # that "you asked the assistant for a statement", which would be a false statement
      # if staff had pulled it.
      {}, roles=CUSTOMER_ONLY, takes_context=True)
def get_statement(ctx: AgentContext) -> dict[str, Any]:
    customer_id = ctx.customer_id
    body = insights.build_statement(customer_id)
    db.audit(actor=ctx.actor, event_type="STATEMENT", subject_id=customer_id,
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
       "days": "how many days the trip lasts"}, roles=ANY_ROLE)
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
      {}, roles=ANY_ROLE)
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
      roles=CUSTOMER_ONLY, requires_approval=True, reads_only=False,
      notify_subject="Spending alert set", notify_severity="ok", takes_context=True)
def set_spending_alert(ctx: AgentContext, threshold: Any = 0,
                       period: str = "month") -> dict[str, Any]:
    customer_id = ctx.customer_id
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
    db.audit(actor=ctx.actor, event_type="SPENDING_ALERT",
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
      {"limit": "how many to return (default 10)"}, roles=ANY_ROLE)
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
# Moving money
# --------------------------------------------------------------------------- #
#
# Every one of these goes through `core.money_ops.submit`, which screens the payment with
# `pipeline.score_transaction` BEFORE any balance changes. `money_ops` is imported inside
# the handlers rather than at module scope: it pulls in the pipeline, and `core/` modules
# are required to be import-safe with no side effects, so the one-line lazy import is
# cheaper than reasoning about the import graph every time either file moves.

@tool("list_payees",
      "List the customer's saved payees -- who they can send money to, and whether they "
      "have paid each one before. Use before any transfer to check the recipient exists.",
      {}, roles=CUSTOMER_ONLY)
def list_payees(customer_id: str) -> list[dict[str, Any]]:
    return [
        {"name": p["name"],
         "account_number": f"****{str(p['account_number'])[-4:]}",
         "kind": p["kind"],
         "paid_before": int(p.get("transfer_count") or 0) > 0,
         "times_paid": int(p.get("transfer_count") or 0),
         "last_paid": (p.get("last_used_at") or "")[:10] or "never"}
        for p in db.list_payees(customer_id)
    ]


@tool("add_payee",
      "Save a new payee so the customer can send money to them. Use when they name a "
      "recipient that is not already on their payee list.",
      {"name": "the payee's name", "account_number": "their account number"},
      roles=CUSTOMER_ONLY, requires_approval=True, reads_only=False,
      notify_subject="New payee added", notify_severity="warn", takes_context=True)
def add_payee(ctx: AgentContext, name: str = "",
              account_number: str = "") -> dict[str, Any]:
    customer_id = ctx.customer_id
    name = str(name).strip()
    account_number = str(account_number).strip().replace(" ", "")
    if not name:
        return {"error": "What name should I save the payee under?"}
    if not account_number.isdigit() or not 6 <= len(account_number) <= 20:
        return {"error": "That account number doesn't look right — it should be 6 to 20 digits."}

    existing = db.find_payee(customer_id, account_number)
    if existing:
        return {"error": f"That account is already saved as {existing['name']}."}

    # An account number that belongs to another BedRock Financial customer makes this an
    # internal transfer, which actually credits the other side.
    internal = db.customer_by_account_number(account_number)
    if internal is not None and internal.customer_id == customer_id:
        return {"error": "That's your own account."}

    payee_id = new_id("PAYEE")
    db.save_payee(payee_id, customer_id, name, account_number,
                  kind="internal" if internal else "external",
                  internal_customer_id=internal.customer_id if internal else None)
    db.audit(actor=ctx.actor, event_type="PAYEE_ADDED", subject_id=customer_id,
             detail=f"Payee '{name}' added ({'internal' if internal else 'external'})")
    return {
        "confirmed": True, "payee_id": payee_id, "name": name,
        "account_last4": account_number[-4:],
        "kind": "internal" if internal else "external",
        "message": (f"{name} is saved. Their first payment will be screened more "
                    f"closely than usual, which is normal for a new payee."),
    }


@tool("transfer_money",
      "Send money from the customer's account to one of their saved payees. Every "
      "transfer is screened for fraud before it settles. Use when they ask to send, "
      "transfer, or pay money to a person.",
      {"payee": "the payee's name or account number, as the customer said it",
       "amount": "how much to send, as a number",
       "note": "an optional reference for the payment"},
      roles=CUSTOMER_ONLY, requires_approval=True, reads_only=False,
      notify_subject="Money transfer", notify_severity="warn", takes_context=True)
def transfer_money(ctx: AgentContext, payee: str = "", amount: Any = 0,
                   note: str = "") -> dict[str, Any]:
    from .. import money_ops

    target = db.find_payee(ctx.customer_id, str(payee))
    if target is None:
        saved = [p["name"] for p in db.list_payees(ctx.customer_id)]
        return {"error": (
            f"I can't find a saved payee matching {payee!r}. "
            + (f"You can send to: {', '.join(saved)}. " if saved else
               "You have no saved payees yet. ")
            + "Give me a name and account number and I'll add them first.")}

    return money_ops.submit(ctx, kind="transfer", amount=amount,
                            destination=target["name"], payee=target,
                            reference=str(note)[:80])


@tool("buy_phone_credit",
      "Top up mobile credit. Screened for fraud before it settles. Use when the customer "
      "asks to top up, recharge, or buy airtime or phone credit.",
      {"phone_number": "the number to top up; leave blank for their own registered number",
       "amount": "how much credit to buy, as a number"},
      roles=CUSTOMER_ONLY, requires_approval=True, reads_only=False,
      notify_subject="Phone top-up", notify_severity="ok", takes_context=True)
def buy_phone_credit(ctx: AgentContext, phone_number: str = "",
                     amount: Any = 0) -> dict[str, Any]:
    from .. import money_ops

    customer = db.get_customer(ctx.customer_id)
    if customer is None:
        return {"error": "Account not found."}

    number = str(phone_number).strip() or customer.phone
    own_number = "".join(c for c in number if c.isdigit()) == \
                 "".join(c for c in (customer.phone or "") if c.isdigit())

    # Topping up your own number is routine; topping up someone else's is the classic
    # cash-out, and `money_ops` scores it as a first-time prepaid reload.
    result = money_ops.submit(
        ctx, kind="topup", amount=amount,
        destination=f"Mobile top-up {number[-4:].rjust(len(number[-4:]), '*')}",
        payee={"transfer_count": 1} if own_number else None,
        reference=f"topup:{number[-4:]}")
    result["own_number"] = own_number
    return result


@tool("pay_tax",
      "Make a tax payment to the revenue authority. Screened for fraud before it settles. "
      "Use when the customer asks to pay tax, income tax, GST or VAT.",
      {"tax_type": "what the payment is for, e.g. 'income tax', 'GST'",
       "reference": "the customer's tax reference or assessment number",
       "amount": "how much to pay, as a number"},
      roles=CUSTOMER_ONLY, requires_approval=True, reads_only=False,
      notify_subject="Tax payment", notify_severity="ok", takes_context=True)
def pay_tax(ctx: AgentContext, tax_type: str = "", reference: str = "",
            amount: Any = 0) -> dict[str, Any]:
    from .. import money_ops

    label = str(tax_type).strip() or "Tax"
    if not str(reference).strip():
        return {"error": "I need your tax reference or assessment number for that payment."}

    return money_ops.submit(ctx, kind="tax", amount=amount,
                            destination=f"{label.title()} — Revenue Authority",
                            reference=str(reference)[:40])


# --------------------------------------------------------------------------- #
# Account self-service
# --------------------------------------------------------------------------- #

@tool("update_contact_details",
      "Update the customer's email address or phone number on file. Use when they ask to "
      "change, update or correct their contact details.",
      {"email": "the new email address, or blank to leave it alone",
       "phone": "the new phone number, or blank to leave it alone"},
      roles=CUSTOMER_ONLY, requires_approval=True, reads_only=False,
      notify_subject="Your contact details were changed", notify_severity="warn",
      takes_context=True)
def update_contact_details(ctx: AgentContext, email: str = "",
                           phone: str = "") -> dict[str, Any]:
    customer_id = ctx.customer_id
    email = str(email).strip()
    phone = str(phone).strip()
    if not email and not phone:
        return {"error": "What would you like to change — your email or your phone number?"}
    if email and ("@" not in email or "." not in email.split("@")[-1] or len(email) < 6):
        return {"error": f"{email!r} doesn't look like a valid email address."}
    if phone and sum(c.isdigit() for c in phone) < 7:
        return {"error": f"{phone!r} doesn't look like a valid phone number."}

    try:
        changed = db.update_contact(customer_id, email=email or None, phone=phone or None)
    except ValueError as exc:
        return {"error": str(exc)}
    if not changed:
        return {"error": "Those are already your details on file — nothing to change."}

    db.audit(actor=ctx.actor, event_type="CONTACT_CHANGE", subject_id=customer_id,
             detail="Contact details updated via the assistant: "
                    + ", ".join(k for k in changed if not k.startswith("previous_")))

    # A contact-detail change is the first move in an account takeover, so the OLD
    # address is told as well as the new one. `_notify_customer` handles the new
    # address; this covers the one being replaced, which is the only warning that
    # reaches the real owner if the change was not theirs.
    _notify_previous_contact(customer_id, changed)

    return {
        "confirmed": True,
        "email": changed.get("email", ""),
        "phone": changed.get("phone", ""),
        "changed": [k for k in changed if not k.startswith("previous_")],
        "message": ("Updated. I've also sent a confirmation to your previous "
                    "details — if this wasn't you, freeze your card straight away."),
    }


def _notify_previous_contact(customer_id: str, changed: dict[str, str]) -> None:
    """Warn the address that was just replaced. Never raises."""
    try:
        if not (changed.get("previous_email") or changed.get("previous_phone")):
            return
        notifications.notify(
            customer_id, kind="contact_change", severity="warn",
            subject="Your contact details were changed",
            body=("The email address or phone number on your BedRock Financial account was "
                  "just changed. If you did not do this, freeze your card immediately "
                  "and call us — someone may be trying to take over your account."),
            detail=", ".join(f"{k.replace('previous_', 'was ')}: {v}"
                             for k, v in changed.items() if k.startswith("previous_")),
        )
    except Exception:                       # noqa: BLE001
        pass


@tool("start_password_change",
      "Open the secure form where the customer sets a new password. Use whenever they "
      "ask to change, reset or update their password. Never ask them for the password "
      "itself -- this tool opens a form they type into directly.",
      {}, roles=CUSTOMER_ONLY, takes_context=True)
def start_password_change(ctx: AgentContext) -> dict[str, Any]:
    """Opens a form. Deliberately changes nothing and receives nothing.

    The password never becomes a tool argument, which means it never enters a prompt, the
    conversation history, the offline response cache, or an audit row. The agent's entire
    role here is to open the right door; the customer walks through it themselves and the
    form posts to a plain Flask route.
    """
    creds = db.get_credentials(ctx.customer_id) or {}
    changed_at = str(creds.get("password_changed_at") or "")
    return {
        "ui_action": "password_form",
        "last_changed": changed_at[:10] or "never",
        "message": ("I've opened the secure password form for you. I never see what you "
                    "type there — it goes straight to the account system."),
    }


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


# Argument names the model may never supply, because each is an authority claim rather
# than a query. `customer_id` was always stripped; the rest arrived with roles and would
# otherwise let a prompt injection promote itself by writing `{"role": "admin"}`.
#
# Note the consequence for staff tools: one that legitimately queries *another* account
# must NOT call its parameter `customer_id`, or it would be silently deleted here and the
# handler would fall back to its default. They use `target_customer_id`. For a customer
# tool the id is an authority claim and must be server-injected; for a staff tool it is a
# query argument. Same word, two meanings -- so they get two names, and the security rule
# stays a one-liner.
_RESERVED_ARGS = frozenset({"customer_id", "ctx", "role", "username", "actor"})


def execute(name: str, customer_id: str | AgentContext, arguments: dict[str, Any],
            approved: bool | None = None, *,
            ctx: AgentContext | None = None) -> ToolCall:
    """Run a tool as the caller, on the caller's account.

    Identity is passed by us and cannot be overridden by the model -- anything in
    `arguments` that names it is discarded (see `_RESERVED_ARGS`).

    Accepts a bare `customer_id` in the second position as it always did, so the
    pre-role callers (`core/record_demo.py`, `ui/customer.py`, the eval harness) keep
    working with no edit. The second positional argument means what it always meant:
    the account in view.
    """
    if isinstance(customer_id, AgentContext):
        ctx, customer_id = customer_id, customer_id.customer_id
    if ctx is None:
        ctx = AgentContext.for_customer(customer_id)

    spec = REGISTRY.get(name)
    if spec is None:
        return ToolCall(name, arguments, error=f"Unknown tool '{name}'.")

    args = {k: v for k, v in (arguments or {}).items() if k not in _RESERVED_ARGS}

    # --- authority ---------------------------------------------------------
    #
    # The router only *offers* a role the tools its persona declares; this *enforces* it
    # regardless of what the model asked for. The prompt is never the security boundary.
    #
    # It sits here, inside the single funnel every call passes through, so it also covers
    # the approval-replay path in web/routes/agent.py -- a proposal stored while the
    # session held one role must not execute after the role changed.
    if ctx.role not in spec.roles:
        db.audit(actor=ctx.actor, event_type="GUARDRAIL", subject_id=name,
                 detail=(f"Blocked '{name}' for role '{ctx.role}' "
                         f"(allows {', '.join(sorted(spec.roles))})"))
        return ToolCall(name, args,
                        error=f"'{name}' is not available to your role.")

    if spec.requires_approval and approved is not True:
        # Return a proposal, not a result. The UI renders a confirm button.
        return ToolCall(name, args, result=None,
                        requires_approval=True, approved=None)

    try:
        # A handler takes either the whole context or just the account id. Tools that
        # write, audit, or act on something other than one account need the identity;
        # the read-only ones would gain nothing from it, and rewriting eleven working
        # handlers for no behaviour change is a large diff on a high-traffic file.
        subject: Any = ctx if spec.takes_context else customer_id
        result = spec.handler(subject, **args)
    except TypeError as exc:
        return ToolCall(name, args, error=f"Invalid parameters for {name}: {exc}")
    except Exception as exc:
        return ToolCall(name, args, error=f"{name} failed: {exc}")

    db.audit(actor=ctx.actor, event_type="TOOL_CALL", subject_id=customer_id,
             detail=f"{name}({', '.join(f'{k}={v}' for k, v in args.items())})",
             approved=approved, requires_approval=spec.requires_approval,
             role=ctx.role, tool=name)

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

    The subject is taken from the RESULT when it names one. That matters now that staff
    tools exist: an analyst resolving an alert belonging to CUST-0042 while viewing
    CUST-0001 would otherwise raise a fraud notification -- toast, notification centre and
    a real .eml -- on the wrong customer's account. That is a privacy incident, not a
    cosmetic bug. Customer tools never return `notify_customer_id`, so their behaviour is
    unchanged.
    """
    if spec.reads_only or not spec.notify_subject:
        return
    if isinstance(result, dict) and result.get("error"):
        return

    try:
        detail = ""
        subject_cid = customer_id
        if isinstance(result, dict):
            detail = str(result.get("message") or result.get("effect") or "")
            subject_cid = str(result.get("notify_customer_id") or customer_id)
        arg_text = ", ".join(f"{k}: {v}" for k, v in args.items() if v not in (None, ""))

        notifications.action_performed(
            subject_cid,
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
