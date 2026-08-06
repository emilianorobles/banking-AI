"""The Router agent -- classifies a customer message and hands off to a specialist.

Same cost-conscious pattern as the fraud path: a cheap deterministic classifier handles
the unambiguous majority, and only genuinely ambiguous messages pay for an LLM call.
Consistency here is deliberate -- one architectural idea applied in two places is easier
to defend than two different ones.
"""

from __future__ import annotations

import re

from .. import db, llm
from ..contracts import Intent

# Ordered: the first pattern to match wins, so specific intents precede general ones.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (Intent.TRAVEL.value, re.compile(
        r"\b(travel|travell?ing|trip|holiday|vacation|abroad|overseas|flying to|"
        r"going to \w+ (next|this|on)|visit(ing)? \w+ (next|this)|business trip)\b",
        re.IGNORECASE)),
    (Intent.FRAUD_REPORT.value, re.compile(
        r"\b(fraud|scam|stolen|lost my card|unauthorised|unauthorized|didn'?t make|"
        r"did not make|someone else|hacked|compromised|suspicious)\b", re.IGNORECASE)),
    (Intent.DISPUTE.value, re.compile(
        r"\b(dispute|chargeback|refund|wrong(ly)? charged|double charged|"
        r"charged twice|incorrect charge)\b", re.IGNORECASE)),
    (Intent.CARD_CONTROL.value, re.compile(
        r"\b(freeze|block|lock|unfreeze|unblock|unlock|cancel) (my )?card\b",
        re.IGNORECASE)),
    (Intent.TRANSACTIONS.value, re.compile(
        r"\b(transaction|payment|charge|spend|spent|purchase|activity|statement|"
        r"recent|history)\b", re.IGNORECASE)),
    (Intent.BALANCE.value, re.compile(
        r"\b(balance|how much.*(have|left)|account summary|overview|my account)\b",
        re.IGNORECASE)),
]

CLASSIFIER_SYSTEM = """You classify a retail banking customer's message into exactly one \
intent. Reply with ONLY the intent word, nothing else.

Valid intents:
  balance        - account overview, card status, how much they have
  transactions   - recent activity, charges, statements
  dispute        - a specific charge they want reversed or investigated
  fraud_report   - reporting fraud, theft, or a compromised card
  travel         - telling us about upcoming or current travel
  card_control   - freeze, unfreeze, block or unblock a card
  general        - anything else"""


def classify(message: str, *, allow_llm: bool = True) -> tuple[str, str, bool]:
    """Return (intent, how_it_was_decided, used_llm).

    Deterministic first. If nothing matches and the message is substantive enough to be
    worth a call, ask the model.
    """
    text = (message or "").strip()
    if not text:
        return Intent.GENERAL.value, "empty message", False

    for intent, pattern in _PATTERNS:
        m = pattern.search(text)
        if m:
            return intent, f"matched '{m.group(0)}'", False

    if not allow_llm or len(text.split()) < 3:
        return Intent.GENERAL.value, "no pattern matched", False

    try:
        reply, _ = llm.chat(CLASSIFIER_SYSTEM, text, agent="router")
        candidate = reply.strip().lower().split()[0].strip(".,:\"'")
        valid = {i.value for i in Intent}
        if candidate in valid:
            return candidate, "classified by model", True
    except Exception:
        pass

    return Intent.GENERAL.value, "fallback", False


# Which tools each intent is allowed to reach. Narrowing the surface per intent means a
# message classified as "balance" cannot be talked into freezing a card.
INTENT_TOOLS: dict[str, list[str]] = {
    Intent.BALANCE.value: ["get_account_summary", "list_travel_notices"],
    Intent.TRANSACTIONS.value: ["list_recent_transactions", "get_account_summary"],
    Intent.DISPUTE.value: ["list_recent_transactions", "raise_dispute", "get_account_summary"],
    Intent.FRAUD_REPORT.value: ["list_recent_transactions", "freeze_card", "raise_dispute",
                                "search_fraud_precedents", "get_account_summary"],
    Intent.TRAVEL.value: ["set_travel_notice", "list_travel_notices", "get_account_summary"],
    Intent.CARD_CONTROL.value: ["freeze_card", "unfreeze_card", "get_account_summary"],
    Intent.GENERAL.value: ["get_account_summary", "list_recent_transactions",
                           "list_travel_notices", "search_fraud_precedents"],
}


def allowed_tools(intent: str) -> list[str]:
    return INTENT_TOOLS.get(intent, INTENT_TOOLS[Intent.GENERAL.value])


# The tool to fall back on when the model talks about acting instead of acting. Every
# entry is read-only and scoped to the caller, so running one unprompted is always safe.
PRIMARY_TOOL: dict[str, str] = {
    Intent.BALANCE.value: "get_account_summary",
    Intent.TRANSACTIONS.value: "list_recent_transactions",
    Intent.DISPUTE.value: "list_recent_transactions",
    Intent.FRAUD_REPORT.value: "list_recent_transactions",
    Intent.TRAVEL.value: "list_travel_notices",
    Intent.CARD_CONTROL.value: "get_account_summary",
    Intent.GENERAL.value: "get_account_summary",
}


def primary_tool(intent: str) -> str:
    return PRIMARY_TOOL.get(intent, "get_account_summary")


def route(message: str, customer_id: str, *, allow_llm: bool = True) -> dict:
    intent, why, used_llm = classify(message, allow_llm=allow_llm)
    db.audit(actor=f"agent:router", event_type="ROUTE", subject_id=customer_id,
             detail=f"intent={intent} ({why})", used_llm=used_llm)
    return {"intent": intent, "why": why, "used_llm": used_llm,
            "tools": allowed_tools(intent)}
