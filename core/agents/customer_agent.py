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

import hashlib
import json
import re
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
{{"action": "tool", "tool": "<name>", "arguments": {{...}}, "say": ""}}

To answer directly, reply with ONLY:
{{"action": "answer", "say": "<your reply to the customer>"}}

Available tools:
{tools}

Worked examples — follow these exactly:

  Customer: "What's my balance?"
  CORRECT:  {{"action": "tool", "tool": "get_account_summary", "arguments": {{}}, "say": ""}}
  WRONG:    {{"action": "answer", "say": "Retrieving your account summary now."}}

  Customer: "Any suspicious activity?"
  CORRECT:  {{"action": "tool", "tool": "list_recent_transactions", \
"arguments": {{"limit": 10}}, "say": ""}}
  WRONG:    {{"action": "answer", "say": "Let me check your recent transactions."}}

  Customer: "Thanks, that's all"
  CORRECT:  {{"action": "answer", "say": "Anytime — I'm here if anything looks off."}}

The WRONG replies are wrong because the customer reads them and nothing happens. You get \
one turn: use it to fetch the data, and you will be asked again afterwards to write the \
reply using the real values.

Rules:
  - NEVER announce that you are about to do something. Replies like "let me check that",
    "I'll look into your recent transactions" or "one moment while I retrieve that" are
    failures: the customer reads them and nothing happens. If the answer needs data,
    return the tool action NOW. Speak only once you have the result.
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
        return _normalise(extract_json(text))
    except Exception:
        # The model answered in plain prose. Treat that as a direct answer rather
        # than failing the turn -- a slightly unstructured reply beats an error.
        return {"action": "answer", "say": text.strip()}


def _chat_cache_key(message: str, step: int) -> str:
    """A stable key for the offline replay cache.

    The default key hashes the whole prompt, which for this agent is fatal to the offline
    fallback: the system prompt embeds `date.today()`, so a cache recorded on Thursday
    misses on Friday, and the user prompt carries the conversation history, so the same
    question keyed differently depending on what was asked before it. Both mean the
    recorded chat beats silently fail to replay on the one occasion they exist for.

    The key is therefore the normalised question plus which step of the tool loop we are
    on -- and deliberately NOTHING ELSE.

    In particular it must not include the routed intent, which an earlier version did.
    `router.classify` falls back to the model when no pattern matches, so the intent for
    "Why was my card frozen?" was `card_control` while the API was up and `general` once
    it went down. That put the recorded response behind a key the lookup could no longer
    compute: the cache missed precisely when the provider was unreachable, which is the
    only situation it exists for. **Never derive a fallback's cache key from anything that
    depends on the thing being fallen back from.**
    """
    normalised = " ".join((message or "").lower().split())
    digest = hashlib.sha256(f"{normalised}\x00{step}".encode()).hexdigest()
    return "chat-" + digest[:20]


def _normalise(decision: dict[str, Any]) -> dict[str, Any]:
    """Repair the two ways the model reliably gets the envelope wrong.

    It routinely returns `{"action": "freeze_card", "tool": "freeze_card", ...}` instead of
    the literal `{"action": "tool", "tool": "freeze_card", ...}` the prompt asks for, and
    occasionally drops `tool` and leaves only the name in `action`. Both are unambiguous --
    the intent is a named, registered tool -- so we repair rather than fail.

    Getting this wrong is not cosmetic: the turn falls through to the prose branch with an
    empty `say`, the customer is told "could you rephrase that", and a request to freeze a
    stolen card silently does nothing. Same reasoning as the JSON repair-retry on the fraud
    path -- structured output through this proxy drifts, so validate the shape rather than
    trusting it.
    """
    if not isinstance(decision, dict):
        return {"action": "answer", "say": str(decision)}

    action = str(decision.get("action", "")).strip()
    named = str(decision.get("tool", "")).strip()

    if action == "tool":
        return decision
    if action in tools.REGISTRY:
        return {**decision, "action": "tool", "tool": named or action}
    if named in tools.REGISTRY and action in ("", "call", "call_tool", "use_tool", "function"):
        return {**decision, "action": "tool", "tool": named}
    return decision


_STALL_RE = re.compile(
    r"\b(let me|i'?ll|i will|allow me to|give me a moment|one moment|hold on|"
    r"i'?m going to|let'?s (check|take a look)|"
    # Gerund-form announcements: "Retrieving your account summary...", "Checking your
    # recent transactions...". These read as progress but nothing has happened.
    r"(check|retriev|fetch|pull|gather|access|review|look|verif|prepar|load)ing)\b",
    re.IGNORECASE,
)


def _is_stall(text: str) -> bool:
    """A reply that promises to do something instead of doing it.

    Short and forward-looking with no actual data in it. We only treat it as a stall
    when nothing has been retrieved yet -- "I'll cancel that for you" after a successful
    tool call is a perfectly good sentence.
    """
    stripped = text.strip()
    return bool(_STALL_RE.search(stripped)) and len(stripped) < 220


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
    stall_retries = 0

    for step in range(MAX_STEPS):
        try:
            text, _tel = llm.chat(system, user_prompt, agent="customer_agent",
                                  cache_key=_chat_cache_key(message, step))
        except llm.LLMUnavailable as exc:
            # The provider is down and this exact question was never recorded. Rather than
            # a dead end, run the read-only tool this intent is about and answer from the
            # database -- which is where the answer lives anyway. `primary_tool` is
            # read-only and scoped to the caller for exactly this purpose, so running one
            # unprompted is always safe.
            #
            # It degrades honestly: the customer gets their real balance or their real
            # transactions, and is told the assistant is limited rather than being shown a
            # confident wrong answer. Only worth doing on the first step; mid-loop we
            # already have a tool result to fall back on.
            fallback = router.primary_tool(intent)
            if not executed and fallback in allowed:
                call = tools.execute(fallback, customer_id, {})
                if call.error is None:
                    executed.append(call)
                    say = _readable_fallback(call)
                    dlp = security.scan_outbound(say)
                    return AgentReply(
                        text=(dlp.safe_text + "\n\n_(The assistant service is unreachable, "
                              "so this is straight from your account records. Everything "
                              "in the dashboard is live and unaffected.)_"),
                        intent=intent, tool_calls=executed,
                        guardrail_notes=[f"llm_unavailable:{str(exc)[:120]}",
                                         f"answered_from_records:{fallback}"],
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )

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

            # The "let me check that" failure mode: the model promises to act instead of
            # acting, so the customer reads a reply and nothing happens. Re-prompting
            # alone does not fix it -- the model repeats itself and we exhaust the loop,
            # which is worse. Ask once, then just run the obvious read-only tool for this
            # intent and answer from real data.
            if not executed and _is_stall(reply) and allowed:
                if stall_retries == 0:
                    stall_retries += 1
                    notes.append("stall_reply_retried")
                    user_prompt += (
                        "\n\nSYSTEM: You replied that you would check something but "
                        "called no tool, so nothing happened and the customer is still "
                        "waiting. Return the tool action now, or answer directly."
                    )
                    continue

                fallback = router.primary_tool(intent)
                if fallback in allowed:
                    notes.append(f"stall_fallback:{fallback}")
                    call = tools.execute(fallback, customer_id, {})
                    executed.append(call)
                    say = _finalise(call, customer_id)
                    dlp = security.scan_outbound(say)
                    return AgentReply(
                        text=dlp.safe_text, intent=intent, tool_calls=executed,
                        citations=[c for c in citations if c], guardrail_notes=notes,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )

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
    """Turn a tool result into prose without the model.

    Reached whenever the phrasing call fails -- which, when the provider is down, is every
    single turn. It has to cover **every** tool, because the last resort is dumping raw
    JSON at a customer, and a chat bubble full of `{"security_score": 86, ...}` is worse
    than any wording problem it might have avoided.

    Deliberately formats the *real* result rather than replaying recorded prose. Stale
    figures presented as current are a worse failure in a banking app than plain phrasing,
    so this stays accurate even when nothing else can reach the network.
    """
    result = call.result
    name = call.tool_name

    if isinstance(result, dict) and result.get("error"):
        return str(result["error"])

    # ---- confirmations -----------------------------------------------------
    if isinstance(result, dict) and result.get("confirmed"):
        if name == "set_travel_notice":
            return (f"Travel notice saved for {', '.join(result.get('countries', []))} "
                    f"from {result.get('from')} to {result.get('to')}. "
                    "Your card won't be flagged for being abroad during those dates.")
        if name == "freeze_card":
            return (f"Your card ending {result.get('card_last4')} is now frozen. "
                    "Nothing further will authorise on it.")
        if name == "unfreeze_card":
            return (f"Your card ending {result.get('card_last4')} is active again, and "
                    "every transaction is being screened as normal.")
        if name == "report_card_lost":
            return (f"Card ending {result.get('card_last4')} is frozen and a replacement "
                    f"is on its way — usually {result.get('delivery_days', '5-7')} days. "
                    "Anything that lands on the old card from now on is declined.")
        if name == "raise_dispute":
            return (f"Dispute opened on {result.get('txn_id')} for "
                    f"{result.get('amount')}. Provisional credit applies while we investigate.")
        if name == "set_spending_alert":
            over = ("  You're already projected to pass it this month."
                    if result.get("already_over") else "")
            return (f"Done — I'll tell you if you go over "
                    f"{result.get('threshold'):,.0f} in a {result.get('period')}.{over}")

    # ---- dict results ------------------------------------------------------
    if isinstance(result, dict):
        if name == "get_account_summary":
            notices = result.get("active_travel_notices") or []
            return (
                f"Your balance is {result.get('balance', 0):,.2f} "
                f"{result.get('currency', '')}, with "
                f"{result.get('available_credit', 0):,.2f} of credit available. "
                f"Card ending {result.get('card_last4')} is "
                f"{result.get('card_status', 'active')}."
                + (f" Travel notice on file: {'; '.join(notices)}." if notices else "")
            )

        if name == "get_security_status":
            # Use each failing check's `basis`, not its label. The labels are written as
            # the passing state ("No unresolved alerts"), so listing them under "worth your
            # attention" says the opposite of what is true. The basis describes what was
            # actually found.
            failing = [c for c in (result.get("checks") or []) if c.get("result") != "pass"]
            lines = [
                f"Your security score is {result.get('security_score')}/100 "
                f"({result.get('security_grade')}), and account health is "
                f"{result.get('health_score')}/100 ({result.get('health_grade')}).",
            ]
            if failing:
                lines.append(
                    f"{len(failing)} of {len(result.get('checks') or [])} checks need "
                    "attention:\n"
                    + "\n".join(f"- {c.get('check')}: {c.get('basis')}" for c in failing)
                )
            else:
                lines.append("Every security check is passing.")
            prot = result.get("protection") or {}
            lines.append(
                f"We've screened {prot.get('transactions_screened', 0)} transactions on "
                f"your account, stopped {prot.get('fraud_blocked', 0)}, and kept "
                f"{prot.get('pii_fields_tokenized', 0)} pieces of your personal data "
                f"away from the AI entirely."
            )
            return "\n\n".join(lines)

        if name == "plan_travel_budget":
            rows = "\n".join(
                f"- {c['category']}: {c['amount']:,.0f}"
                for c in (result.get("by_category") or [])
            )
            return (
                f"For {result.get('days')} days in {result.get('destination')} I'd budget "
                f"about **{result.get('recommended_total', 0):,.0f} "
                f"{result.get('currency', '')}** — that's "
                f"{result.get('daily_estimate', 0):,.0f} a day plus 15% contingency.\n"
                + (f"\n{rows}\n" if rows else "")
                + "\n" + str(result.get("next_step", ""))
            )

        if name == "get_statement":
            return (f"Your statement is ready — {result.get('lines')} lines covering your "
                    f"balance, spending, projections and security checks. You can download "
                    f"it from the Statements page, and I've sent you a notification "
                    f"confirming it was issued.")

    # ---- list results ------------------------------------------------------
    if isinstance(result, list):
        if not result:
            return {
                "list_travel_notices": "You have no travel notices on file.",
                "list_my_notifications": "You have no recent notifications.",
                "search_fraud_precedents": "I couldn't find a similar past case.",
            }.get(name, "There's nothing to show for that.")

        first = result[0]
        if "amount" in first and "when" in first:
            rows = "\n".join(
                f"- {r['when']} · {r['amount']} · {r.get('merchant_category', '')} · "
                f"{r.get('location', '')}" for r in result[:10]
            )
            return f"Here are your most recent transactions:\n{rows}"

        if name == "list_travel_notices":
            rows = "\n".join(
                f"- {', '.join(r.get('countries', []))}: {r.get('from')} to {r.get('to')}"
                for r in result
            )
            return f"Travel notices on file:\n{rows}"

        if name == "list_my_notifications":
            rows = "\n".join(
                f"- {r.get('when')} · {r.get('subject')}"
                + ("" if r.get("read") else "  (unread)") for r in result[:10]
            )
            return f"Here's what's happened on your account recently:\n{rows}"

        if name == "search_fraud_precedents":
            rows = "\n".join(
                f"- {r.get('case_id')}: {r.get('title')} ({r.get('outcome', '')})"
                for r in result[:5]
            )
            return f"Similar cases we've seen before:\n{rows}"

    return _fmt_result(result)
