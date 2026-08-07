"""The Router agent -- classifies a message and narrows which tools can answer it.

Same cost-conscious pattern as the fraud path: a cheap deterministic classifier handles
the unambiguous majority, and only genuinely ambiguous messages pay for an LLM call.
Consistency here is deliberate -- one architectural idea applied in two places is easier
to defend than two different ones.

The tables themselves live in `personas.py`, one set per role. This module holds only the
mechanism, so a customer, an analyst and an admin are routed by the same code against
different vocabularies rather than by three copies of the same loop.
"""

from __future__ import annotations

from .. import db
from ..contracts import Intent
from . import personas
from .context import AgentContext

# Back-compatible re-exports. Nothing outside this package reads them today, but they were
# public and the customer tables have not changed, so they still mean what they meant.
INTENT_TOOLS = personas.CUSTOMER_INTENT_TOOLS
PRIMARY_TOOL = personas.CUSTOMER_PRIMARY_TOOL
_PATTERNS = personas.CUSTOMER_PATTERNS
CLASSIFIER_SYSTEM = personas.CUSTOMER_CLASSIFIER


def classify(message: str, *, allow_llm: bool = True,
             role: str = "customer") -> tuple[str, str, bool]:
    """Return (intent, how_it_was_decided, used_llm).

    Deterministic first. If nothing matches and the message is substantive enough to be
    worth a call, ask the model -- and validate its answer against this persona's own
    vocabulary, so a customer-shaped intent can never come back on a staff turn.
    """
    persona = personas.for_role(role)
    text = (message or "").strip()
    if not text:
        return persona.default_intent, "empty message", False

    for intent, pattern in persona.patterns:
        m = pattern.search(text)
        if m:
            return intent, f"matched '{m.group(0)}'", False

    if not allow_llm or len(text.split()) < 3:
        return persona.default_intent, "no pattern matched", False

    try:
        # Lazily imported so this module stays import-safe with no network stack loaded.
        from .. import llm
        reply, _ = llm.chat(persona.classifier_system, text, agent="router")
        candidate = reply.strip().lower().split()[0].strip(".,:\"'")
        if candidate in persona.intent_values:
            return candidate, "classified by model", True
    except Exception:
        pass

    return persona.default_intent, "fallback", False


def allowed_tools(intent: str, role: str = "customer") -> list[str]:
    return personas.for_role(role).allowed_tools(intent)


def primary_tool(intent: str, role: str = "customer") -> str:
    """The tool to fall back on when the model talks about acting instead of acting.

    Every entry in every persona's map is read-only, takes no arguments, and is scoped to
    the caller -- so running one unprompted is always safe.
    """
    return personas.for_role(role).fallback_tool(intent)


def route(message: str, ctx: AgentContext | str, *, allow_llm: bool = True) -> dict:
    """Classify one message and return the tool surface it may reach.

    Accepts a bare `customer_id` as well as a context, so the pre-role callers
    (`core/record_demo.py`, `ui/customer.py`, the eval harness) keep working unchanged.
    """
    if isinstance(ctx, str):
        ctx = AgentContext.for_customer(ctx)

    intent, why, used_llm = classify(message, allow_llm=allow_llm, role=ctx.role)
    db.audit(actor="agent:router", event_type="ROUTE", subject_id=ctx.customer_id,
             detail=f"intent={intent} ({why})", used_llm=used_llm, role=ctx.role)
    return {"intent": intent, "why": why, "used_llm": used_llm,
            "tools": allowed_tools(intent, ctx.role), "role": ctx.role}
