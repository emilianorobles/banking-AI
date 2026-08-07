"""The conversational agent -- a bounded tool-calling loop.

Despite the module name (kept so every existing caller and the recorded demo keep
working), this hosts the loop for ALL THREE personas: customer, fraud analyst and
operations admin. What differs between them -- the system prompt, the routing vocabulary,
the tool scope, the finalise voice, the offline cache namespace -- is DATA, declared in
`personas.py`. There is exactly one copy of the control flow.

That matters more than it looks. The loop below carries stall detection with a two-stage
recovery, the `_normalise` envelope repair, the LLM-unavailable degradation and the
approval pause. Every one of those is a fix for a bug that reached a browser. A second
copy of this function would start correct and diverge on the first fix that landed in
only one of them.

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

from .. import db, llm, money, security
from ..contracts import AgentReply, ToolCall
from . import personas, router, tools
from .context import AgentContext

MAX_STEPS = 3

# The prompts now live in `personas.py`, beside the routing tables and the widget strings
# they have to stay consistent with. Re-exported under their old names because they were
# public and one is quoted in the docs.
SYSTEM_PROMPT = personas.CUSTOMER_SYSTEM
FINALISE_PROMPT = personas.CUSTOMER_FINALISE



def _parse(text: str) -> dict[str, Any]:
    """Extract the agent's JSON decision, tolerating fences and stray prose."""
    from .fraud_analyst import extract_json
    try:
        return _normalise(extract_json(text))
    except Exception:
        # The model answered in plain prose. Treat that as a direct answer rather
        # than failing the turn -- a slightly unstructured reply beats an error.
        return {"action": "answer", "say": text.strip()}


def _chat_cache_key(message: str, step: int, prefix: str = "chat-") -> str:
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

    Role is separated by `prefix`, supplied by the persona, and is deliberately NOT hashed
    in. Two reasons, and they point in different directions so they are worth stating
    separately:

      * Hashing it would move every existing key and orphan all twelve recorded customer
        beats -- the exact failure this docstring already describes, self-inflicted.
      * Separation is nevertheless required: "show recent transactions" normalises
        identically whoever types it, and one shared key would replay a customer tool
        choice into the staff persona, where it is out of scope.

    Role passes the bug-12 test that intent failed -- it comes from the signed session
    cookie, so it is computable with the network dead and identical online and offline.
    """
    normalised = " ".join((message or "").lower().split())
    digest = hashlib.sha256(f"{normalised}\x00{step}".encode()).hexdigest()
    return prefix + digest[:20]


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
    customer_id: str | AgentContext,
    history: list[dict[str, str]] | None = None,
    *,
    pending_approval: ToolCall | None = None,
    ctx: AgentContext | None = None,
) -> AgentReply:
    """Handle one turn, for whichever persona the caller is.

    `pending_approval` carries a tool the user has just confirmed in the UI; when present
    we execute it directly rather than re-asking the model.

    The second positional argument means what it always meant -- the account in view --
    and may now also be a full `AgentContext`. Callers that pass a bare `customer_id`
    (`core/record_demo.py`, `ui/customer.py`, the eval harness) get the customer persona,
    exactly as before.
    """
    started = time.perf_counter()
    notes: list[str] = []

    if isinstance(customer_id, AgentContext):
        ctx, customer_id = customer_id, customer_id.customer_id
    if ctx is None:
        ctx = AgentContext.for_customer(customer_id)
    persona = personas.for_role(ctx.role)

    # --- guardrail: scan the user's own message ---
    injection = security.detect_injection(message)
    if injection.detected:
        db.audit(actor=ctx.actor, event_type="GUARDRAIL",
                 subject_id=customer_id,
                 detail=f"Prompt injection in chat input: {injection.summary}")
        return AgentReply(
            text=persona.injection_refusal,
            intent="general",
            guardrail_notes=[f"prompt_injection_blocked:{','.join(injection.categories)}"],
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # --- an approved tool executes immediately ---
    if pending_approval is not None:
        call = tools.execute(pending_approval.tool_name, customer_id,
                             pending_approval.arguments, approved=True, ctx=ctx)
        say = _finalise(call, persona) if call.error is None else (
            f"That didn't go through: {call.error}")
        return AgentReply(
            # The intent of an approved action is the action itself. It used to be
            # hardcoded "card_control", which was wrong the moment a second gated tool
            # existed and is now wrong for nine of them.
            text=say, intent=f"approved:{pending_approval.tool_name}", tool_calls=[call],
            guardrail_notes=["human_approved_action"],
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # --- route ---
    routing = router.route(message, ctx)
    intent = routing["intent"]
    # Intersect the persona's intent allowlist with what this role may actually invoke.
    # Belt and braces: `tools.execute` enforces the role regardless, but narrowing what
    # the model is even shown means it does not waste a step proposing the impossible.
    allowed = {name for name in routing["tools"]
               if name in tools.REGISTRY and ctx.role in tools.REGISTRY[name].roles}

    tool_docs = "\n".join(
        f"- {spec.name}: {spec.description}\n    parameters: "
        f"{', '.join(f'{k} ({v})' for k, v in spec.parameters.items()) or 'none'}"
        + ("  [REQUIRES CUSTOMER CONFIRMATION]" if spec.requires_approval else "")
        for name, spec in tools.REGISTRY.items() if name in allowed
    )
    system = persona.system_prompt.format(
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
            text, _tel = llm.chat(system, user_prompt, agent=f"{ctx.role}_agent",
                                  cache_key=_chat_cache_key(message, step,
                                                            persona.cache_prefix))
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
            fallback = router.primary_tool(intent, ctx.role)
            if not executed and fallback in allowed:
                call = tools.execute(fallback, customer_id, {}, ctx=ctx)
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

                fallback = router.primary_tool(intent, ctx.role)
                if fallback in allowed:
                    notes.append(f"stall_fallback:{fallback}")
                    call = tools.execute(fallback, customer_id, {}, ctx=ctx)
                    executed.append(call)
                    say = _finalise(call, persona)
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
            db.audit(actor=f"agent:{ctx.role}", event_type="GUARDRAIL",
                     subject_id=customer_id,
                     detail=f"Blocked out-of-scope tool '{tool_name}' for intent '{intent}'")
            notes.append(f"tool_out_of_scope_blocked:{tool_name}")
            user_prompt += (
                f"\n\nSYSTEM: '{tool_name}' is not available for this request. "
                "Use an available tool or answer directly."
            )
            continue

        call = tools.execute(tool_name, customer_id, arguments, ctx=ctx)
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
        if tool_name == "get_alert_detail" and isinstance(call.result, dict):
            citations.extend(c.get("case_id", "")
                             for c in (call.result.get("cited_cases") or []))

        say = _finalise(call, persona)
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


def _finalise(call: ToolCall, persona) -> str:
    """Turn a tool result into prose, in this persona's voice.

    The customer voice ("short, warm") would soften an alert summary into reassurance,
    which is the opposite of what an analyst working a queue needs -- so the voice is a
    persona property rather than a constant.
    """
    if call.error:
        return f"I couldn't complete that: {call.error}"
    try:
        text, _ = llm.chat(
            persona.finalise_system,
            persona.finalise_prompt.format(tool=call.tool_name,
                                           result=_fmt_result(call.result)),
            agent=f"{persona.role}_agent_finalise",
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

    # ---- money movement ----------------------------------------------------
    #
    # Handled before the generic `confirmed` branch because a held or blocked payment is
    # NOT confirmed and yet is the most important thing this assistant can tell someone.
    # Getting silence here would mean a customer believing money moved when it did not.
    if name in ("transfer_money", "buy_phone_credit", "pay_tax") and isinstance(result, dict):
        amount = money.fmt(result.get("amount_value", 0), result.get("currency", ""))
        where = result.get("destination", "the recipient")
        screening = result.get("screening") or {}
        lines = [str(result.get("message") or "")]
        if result.get("held") or result.get("blocked"):
            fired = screening.get("rules_fired") or []
            if fired:
                lines.append("What triggered it:\n"
                             + "\n".join(f"- {r}" for r in fired[:4]))
            lines.append("_Nothing has left your account._")
        else:
            lines.append(f"Your balance is now "
                         f"{money.fmt(result.get('new_balance', 0), result.get('currency', ''))}.")
            if result.get("first_time_payee"):
                lines.append(f"That was your first payment to {where}, so it was screened "
                             f"more closely than usual — it scored "
                             f"{screening.get('risk_score', 0)}/100 and passed.")
        return "\n\n".join(line for line in lines if line) or f"{amount} to {where}."

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
        if name == "add_payee":
            return (f"{result.get('name')} is saved as a payee "
                    f"(account ending {result.get('account_last4')}, "
                    f"{result.get('kind')}). Their first payment will be screened more "
                    f"closely than usual, which is normal for a new payee.")
        if name == "update_contact_details":
            changed = ", ".join(result.get("changed") or []) or "details"
            return (f"Your {changed} {'have' if len(result.get('changed') or []) > 1 else 'has'} "
                    f"been updated. I've also sent a confirmation to your previous "
                    f"details — if this wasn't you, freeze your card straight away.")
        if name == "resolve_alert":
            learned = result.get("learned_case_id")
            return (f"{result.get('alert_id')} resolved as "
                    f"{str(result.get('outcome', '')).replace('_', ' ')}. "
                    + (f"Indexed as {learned}, retrievable immediately. " if learned else "")
                    + ("The card has been unfrozen." if result.get("card_unfrozen")
                       else "The freeze stands."))

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

        if name == "start_password_change":
            return (f"I've opened the secure password form — I never see what you type "
                    f"there, it goes straight to the account system. Your password was "
                    f"last changed {result.get('last_changed', 'never')}.")

        # ---- staff tools ---------------------------------------------------
        if name == "list_open_alerts":
            rows = result.get("alerts") or []
            if not rows:
                return f"Nothing {str(result.get('status', 'PENDING')).lower()} in the queue."
            table = "\n".join(
                f"- **{r['risk_score']}** · {r['alert_id']} · {r['action']} · "
                f"{r.get('customer_name') or r['customer_id']} · {r.get('region', '')}"
                for r in rows)
            return (f"Showing {result.get('showing')} of {result.get('total_matching')} "
                    f"{str(result.get('status', '')).lower()} alerts, highest risk first:\n"
                    f"{table}")

        if name == "get_alert_detail":
            txn = result.get("transaction") or {}
            fired = result.get("rules_fired") or []
            lines = [
                f"**{result.get('alert_id')}** — {result.get('action')} at "
                f"{result.get('risk_score')}/100 ({result.get('risk_level')}), "
                f"status {result.get('status')}.",
                f"{txn.get('amount', '')} · {txn.get('merchant_category', '')} · "
                f"{txn.get('location', '')} · {txn.get('channel', '')}",
            ]
            if fired:
                lines.append("Rules fired:\n" + "\n".join(
                    f"- {r['rule_id']} (+{r['points']}): {r['reason']}" for r in fired))
            if result.get("model_reasoning"):
                lines.append(f"Model: {result['model_reasoning']}")
            cited = result.get("cited_cases") or []
            if cited:
                lines.append("Cited: " + ", ".join(
                    f"{c['case_id']} ({c['outcome']})" for c in cited))
            return "\n\n".join(lines)

        if name == "explain_decision":
            fired = result.get("rules_fired") or []
            head = (f"{result.get('txn_id')} scored {result.get('risk_score')}/100 "
                    f"({result.get('risk_level')}) → {result.get('action')}. "
                    f"Rule score {result.get('rule_score')}; model "
                    f"{'consulted' if result.get('model_used') else 'not needed'}.")
            body = ("\n".join(f"- {r['rule_id']} (+{r['points']}): {r['reason']}"
                              for r in fired) or "No rules fired.")
            tail = result.get("model_reasoning") or ""
            return "\n\n".join(x for x in (head, body, tail) if x)

        if name == "customer_360":
            return (
                f"**{result.get('name')}** ({result.get('customer_id')}) — "
                f"{result.get('home')}, {result.get('region')}.\n"
                f"Balance {result.get('balance'):,.2f}, limit "
                f"{result.get('credit_limit'):,.2f}, card ending "
                f"{result.get('card_last4')} is {result.get('card_status')}.\n"
                f"Security {result.get('security_score')}/100 "
                f"({result.get('security_grade')}); "
                f"{result.get('open_alerts')} open of {result.get('total_alerts')} alerts; "
                f"{result.get('transactions_screened')} transactions screened, "
                f"{result.get('fraud_blocked')} blocked."
            )

        if name == "queue_stats":
            regions = ", ".join(f"{k} {v}" for k, v in
                                (result.get("open_by_region") or {}).items()) or "none"
            return (
                f"{result.get('open')} alerts open of {result.get('total')} total. "
                f"Highest open risk {result.get('highest_open_risk')}, average "
                f"{result.get('average_open_risk')}.\n"
                f"Open by region: {regions}.\n"
                f"Resolved {result.get('resolved')} — {result.get('confirmed_fraud')} "
                f"confirmed fraud, {result.get('false_positives')} false positives. "
                f"The knowledge store holds {result.get('knowledge_store_size')} cases, "
                f"{result.get('cases_learned')} of them learned from resolutions."
            )

        if name == "cost_summary":
            return (
                f"{result.get('llm_calls', 0)} model calls on "
                f"{result.get('model')}, "
                f"{result.get('prompt_tokens', 0) + result.get('completion_tokens', 0):,} "
                f"tokens, ${float(result.get('actual_cost_usd') or 0):.4f} spent.\n"
                f"{result.get('total_transactions', 0):,} transactions scored and "
                f"{result.get('avoided_pct', 0)}% of them "
                f"({result.get('avoided', 0):,}) needed no model call at all — "
                f"${float(result.get('saved_usd') or 0):.2f} saved against "
                f"${float(result.get('naive_cost_usd') or 0):.2f} if every one had gone "
                f"to the model.\n"
                f"p95 latency {result.get('p95_latency_ms', 0)} ms."
            )

        if name == "system_health":
            breaker = ("OPEN — the primary is being skipped"
                       if result.get("primary_circuit_open") else "closed")
            return (
                f"Mode {result.get('mode')}, model {result.get('chat_model')}, API key "
                f"{'present' if result.get('api_key_present') else 'MISSING'}.\n"
                f"Primary circuit {breaker} "
                f"({result.get('consecutive_primary_failures')} consecutive failures). "
                f"Fallback "
                f"{result.get('fallback_provider') or 'not configured'}.\n"
                f"{result.get('cached_responses')} recorded responses, "
                f"{result.get('knowledge_store_size')} cases indexed. Rules "
                f"{'consistent' if result.get('rules_consistent') else 'INCONSISTENT'}."
            )

        if name == "search_audit_log":
            entries = result.get("entries") or []
            if not entries:
                return "No audit entries match that."
            rows = "\n".join(
                f"- {e['when']} · {e['actor']} · {e['event']} · {e['subject']} — {e['detail']}"
                for e in entries)
            return (f"Showing {result.get('showing')} of "
                    f"{result.get('total_matching')} matching entries:\n{rows}")

    # ---- list results ------------------------------------------------------
    if isinstance(result, list):
        if not result:
            return {
                "list_travel_notices": "You have no travel notices on file.",
                "list_my_notifications": "You have no recent notifications.",
                "search_fraud_precedents": "I couldn't find a similar past case.",
                "list_payees": ("You have no saved payees yet. Give me a name and an "
                                "account number and I'll add one."),
            }.get(name, "There's nothing to show for that.")

        first = result[0]
        if "amount" in first and "when" in first:
            # The customer sees the symbol here even though the model never does: the
            # tool's `amount` string is kept ASCII because it is prompt text, so this
            # path formats from amount_value + currency instead. This is the offline
            # branch -- when it runs, it is the only thing the customer will read.
            def _amount(r: dict[str, Any]) -> str:
                if r.get("amount_value") is not None:
                    return money.fmt(r["amount_value"], r.get("currency", ""))
                return str(r.get("amount", ""))

            rows = "\n".join(
                f"- {r['when']} · {_amount(r)} · {r.get('merchant_category', '')} · "
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

        if name == "list_payees":
            rows = "\n".join(
                f"- {r.get('name')} · {r.get('account_number')} · "
                + (f"paid {r.get('times_paid')}x, last {r.get('last_paid')}"
                   if r.get("paid_before") else "never paid — first payment gets extra checks")
                for r in result
            )
            return f"Your saved payees:\n{rows}"

    return _fmt_result(result)
