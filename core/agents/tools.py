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

from .. import db, rag, travel
from ..contracts import ToolCall


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, str]
    handler: Callable[..., Any]
    requires_approval: bool = False
    reads_only: bool = True


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, parameters: dict[str, str],
         requires_approval: bool = False, reads_only: bool = True):
    def decorator(fn):
        REGISTRY[name] = ToolSpec(name, description, parameters, fn,
                                  requires_approval, reads_only)
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
            "amount": f"{txn.amount:,.2f} {txn.currency}",
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
      reads_only=False)
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
      requires_approval=True, reads_only=False)
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
        "amount": f"{txn.amount:,.2f} {txn.currency}",
        "provisional_credit": True,
        "sla_days": 10,
        "message": "Dispute opened. Provisional credit applies while we investigate.",
    }


@tool("freeze_card",
      "Freeze the customer's card immediately. Use when they report the card lost, stolen, "
      "or confirm fraudulent activity.",
      {"reason": "why the card is being frozen"},
      requires_approval=True, reads_only=False)
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
      requires_approval=True, reads_only=False)
def unfreeze_card(customer_id: str, reason: str = "customer confirmed activity") -> dict[str, Any]:
    customer = db.get_customer(customer_id)
    if customer is None:
        return {"error": "Account not found."}
    db.set_card_frozen(customer_id, False)
    db.audit(actor=f"customer:{customer_id}", event_type="CARD_UNFREEZE",
             subject_id=customer_id, detail=f"Card unfrozen via agent: {reason}")
    return {"confirmed": True, "card_last4": customer.card_number[-4:], "status": "active"}


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

    return ToolCall(name, args, result=result,
                    requires_approval=spec.requires_approval,
                    approved=True if spec.requires_approval else None)
