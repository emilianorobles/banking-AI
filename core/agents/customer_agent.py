"""The Customer Service agent -- a bounded tool-calling loop.

Deliberate choice: we drive tool selection with structured JSON rather than the
provider's native function-calling API. Two reasons, both defensible in the pitch:

  1. Portability. The lab endpoint is an OpenAI-compatible proxy and native tool-calling
     support through it is not guaranteed. Structured JSON works against any chat model,
     which is exactly the "avoid lock-in" requirement.
  2. Auditability. Every proposed call is a plain object we can log, scope-check, and
     gate on approval BEFORE anything executes.

The loop is bounded (MAX_STEPS) so a confused model cannot spin. Approval-gated tools
return a proposal that the UI turns into a confirm button -- the agent cannot freeze a
card on its own.
"""

from __future__ import annotations

import json
import time
from datetime import date
from typing import Any

from .. import db, llm, security
from ..contracts import AgentReply, ToolCall
from . import router, tools

MAX_STEPS = 3

SYSTEM_PROMPT = """You are the customer service assistant for SentinelBank. You are \
helping ONE authenticated customer with their own account.

Today's date is {today}. Resolve relative dates ("next week", "the 10th to the 20th") \
against it and always emit dates as YYYY-MM-DD.

{injection_rule}

You have tools. To use one, reply with ONLY this JSON object:
{{"action": "tool", "tool": "<name>", "arguments": {{...}}, "say": "<short line telling \
the customer what you're doing>"}}

To answer directly, reply with ONLY:
{{"action": "answer", "say": "<your reply to the customer>"}}

Available tools:
{tools}

Rules:
  - Never invent account data. If you do not have a number, call a tool to get it.
  - Never reveal or guess full card numbers, account numbers, or other identifiers. \
Values shown to you as tokens like <PAN_7f3a2b> must never be echoed back.
  - Tools marked REQUIRES CUSTOMER CONFIRMATION will pause for the customer to confirm. \
Propose them normally; the system handles the confirmation step.
  - For travel, you need destination country/countries AND both dates. If any are \
missing, ask for them rather than guessing.
  - Be concise and warm. Two or three sentences unless listing transactions.
  - If the customer asks something outside banking, say so briefly and redirect."""

FINALISE_PROMPT = """The tool returned the result below. Write the customer's reply.

Tool: {tool}
Result:
{result}

Reply with ONLY: {{"action": "answer", "say": "<your reply>"}}
Be specific and use the actual values from the result. Format any list of transactions \
as a short markdown table. Never show full card or account numbers."""


def _parse(text: str) -> dict[str, Any]:
    """Extract the agent's JSON decision, tolerating fences and stray prose."""
    from .fraud_analyst import extract_json
    try:
        return extract_json(text)
    except Exception:
        # The model answered in plain prose. Treat that as a direct answer rather
        # than failing the turn -- a slightly unstructured reply beats an error.
        return {"action": "answer", "say": text.strip()}


def _fmt_result(result: Any) -> str:
    if isinstance(result, (dict, list)):
        return json.dumps(result, indent=2, default=str)
    return str(result)


def respond(
    message: str,
    customer_id: str,
    history: list[dict[str, str]] | None = None,
    *,
    pending_approval: ToolCall | None = None,
) -> AgentReply:
    """Handle one customer turn.

    `pending_approval` carries a tool the customer has just confirmed in the UI; when
    present we execute it directly rather than re-asking the model.
    """
    started = time.perf_counter()
    notes: list[str] = []

    # --- guardrail: scan the customer's own message ---
    injection = security.detect_injection(message)
    if injection.detected:
        db.audit(actor=f"customer:{customer_id}", event_type="GUARDRAIL",
                 subject_id=customer_id,
                 detail=f"Prompt injection in chat input: {injection.summary}")
        return AgentReply(
            text=("I can help with your account, but I can't act on instructions that try "
                  "to change how I work. What would you like to do with your account?"),
            intent="general",
            guardrail_notes=[f"prompt_injection_blocked:{','.join(injection.categories)}"],
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # --- an approved tool executes immediately ---
    if pending_approval is not None:
        call = tools.execute(pending_approval.tool_name, customer_id,
                             pending_approval.arguments, approved=True)
        say = _finalise(call, customer_id) if call.error is None else (
            f"That didn't go through: {call.error}")
        return AgentReply(
            text=say, intent="card_control", tool_calls=[call],
            guardrail_notes=["human_approved_action"],
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # --- route ---
    routing = router.route(message, customer_id)
    intent = routing["intent"]
    allowed = set(routing["tools"])

    tool_docs = "\n".join(
        f"- {spec.name}: {spec.description}\n    parameters: "
        f"{', '.join(f'{k} ({v})' for k, v in spec.parameters.items()) or 'none'}"
        + ("  [REQUIRES CUSTOMER CONFIRMATION]" if spec.requires_approval else "")
        for name, spec in tools.REGISTRY.items() if name in allowed
    )
    system = SYSTEM_PROMPT.format(
        today=date.today().isoformat(),
        injection_rule=security.INJECTION_SYSTEM_RULE,
        tools=tool_docs,
    )

    convo = []
    for turn in (history or [])[-6:]:
        convo.append(f"{turn.get('role', 'user').upper()}: {turn.get('content', '')}")
    convo.append(f"USER: {message}")
    user_prompt = "\n".join(convo)

    executed: list[ToolCall] = []
    citations: list[str] = []

    for _ in range(MAX_STEPS):
        try:
            text, _tel = llm.chat(system, user_prompt, agent="customer_agent")
        except llm.LLMUnavailable as exc:
            return AgentReply(
                text=("I can't reach the assistant service right now. Your account and "
                      "transactions are still available in the dashboard below."),
                intent=intent, tool_calls=executed,
                guardrail_notes=[f"llm_unavailable:{str(exc)[:120]}"],
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        decision = _parse(text)

        if decision.get("action") != "tool":
            reply = str(decision.get("say", "")).strip() or "Could you rephrase that?"
            dlp = security.scan_outbound(reply)
            if dlp.blocked:
                notes.append(f"dlp_egress_redacted:{','.join(dlp.violations)}")
            return AgentReply(
                text=dlp.safe_text, intent=intent, tool_calls=executed,
                citations=citations, guardrail_notes=notes,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        tool_name = str(decision.get("tool", ""))
        arguments = decision.get("arguments") or {}

        # Scope check: the router decided which tools this intent may reach.
        if tool_name not in allowed:
            db.audit(actor=f"agent:customer", event_type="GUARDRAIL", subject_id=customer_id,
                     detail=f"Blocked out-of-scope tool '{tool_name}' for intent '{intent}'")
            notes.append(f"tool_out_of_scope_blocked:{tool_name}")
            user_prompt += (
                f"\n\nSYSTEM: '{tool_name}' is not available for this request. "
                "Use an available tool or answer directly."
            )
            continue

        call = tools.execute(tool_name, customer_id, arguments)
        executed.append(call)

        if call.requires_approval and call.approved is None:
            # Stop and hand the proposal to the UI for confirmation.
            say = str(decision.get("say", "")).strip() or (
                f"I can do that — please confirm and I'll {tool_name.replace('_', ' ')}.")
            return AgentReply(
                text=say, intent=intent, tool_calls=executed,
                guardrail_notes=notes + ["awaiting_human_approval"],
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        if tool_name == "search_fraud_precedents" and isinstance(call.result, list):
            citations.extend(c.get("case_id", "") for c in call.result if c.get("case_id"))

        say = _finalise(call, customer_id)
        dlp = security.scan_outbound(say)
        if dlp.blocked:
            notes.append(f"dlp_egress_redacted:{','.join(dlp.violations)}")
        return AgentReply(
            text=dlp.safe_text, intent=intent, tool_calls=executed,
            citations=[c for c in citations if c], guardrail_notes=notes,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    return AgentReply(
        text="I wasn't able to complete that. Could you try rephrasing?",
        intent=intent, tool_calls=executed, guardrail_notes=notes + ["max_steps_reached"],
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def _finalise(call: ToolCall, customer_id: str) -> str:
    """Turn a tool result into customer-facing prose."""
    if call.error:
        return f"I couldn't complete that: {call.error}"
    try:
        text, _ = llm.chat(
            "You write short, warm, accurate replies for a retail bank customer. "
            "Reply with ONLY the JSON object requested.",
            FINALISE_PROMPT.format(tool=call.tool_name, result=_fmt_result(call.result)),
            agent="customer_agent_finalise",
        )
        parsed = _parse(text)
        return str(parsed.get("say", "")).strip() or _fmt_result(call.result)
    except Exception:
        # Deterministic fallback so a model failure still yields a usable answer.
        return _readable_fallback(call)


def _readable_fallback(call: ToolCall) -> str:
    result = call.result
    if isinstance(result, dict) and result.get("confirmed"):
        if call.tool_name == "set_travel_notice":
            return (f"Travel notice saved for {', '.join(result.get('countries', []))} "
                    f"from {result.get('from')} to {result.get('to')}. "
                    "Your card won't be flagged for being abroad during those dates.")
        if call.tool_name == "freeze_card":
            return f"Your card ending {result.get('card_last4')} is now frozen."
        if call.tool_name == "raise_dispute":
            return (f"Dispute opened on {result.get('txn_id')} for "
                    f"{result.get('amount')}. Provisional credit applies while we investigate.")
    if isinstance(result, list) and result and "amount" in result[0]:
        rows = "\n".join(
            f"- {r['when']} · {r['amount']} · {r['merchant_category']} · {r['location']}"
            for r in result[:10]
        )
        return f"Here are your most recent transactions:\n{rows}"
    return _fmt_result(result)
