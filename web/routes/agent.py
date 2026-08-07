"""The conversational agent endpoint.

One POST per turn. The interesting part is the approval gate: when the agent proposes a
tool that changes account state, we store that exact proposal in the session and hand the
browser a Confirm button. Confirming re-posts, and we execute **the stored proposal** --
not whatever the browser sends back.

That distinction matters. If the client could name the tool and its arguments on the
confirm hop, the confirmation step would be decorative: anyone could POST straight to it
and skip the gate. Matching against what the agent actually proposed is what makes the
human checkpoint real rather than cosmetic.
"""

from __future__ import annotations

from typing import Any, Callable

from flask import Blueprint, jsonify, request, session

from core import db
from core.agents import customer_agent
from core.agents.context import AgentContext
from core.contracts import ToolCall

from .. import auth, mdlite

bp = Blueprint("agent", __name__, url_prefix="/api/agent")

HISTORY_KEY = "chat_history"
PENDING_KEY = "pending_approval"
MAX_HISTORY = 12

# Tools whose effects the page is showing, so the view needs to refresh after they run.
REFRESH_AFTER = {"freeze_card", "unfreeze_card", "set_travel_notice", "raise_dispute",
                 "report_card_lost", "set_spending_alert",
                 "transfer_money", "buy_phone_credit", "pay_tax",
                 "add_payee", "update_contact_details", "resolve_alert"}

# Tools allowed to ask the browser to open something. Allowlisted by name for the same
# reason `VISUALS` is: a generic passthrough of `result` would put account detail on the
# wire, which `_call_to_dict` exists to prevent.
UI_ACTIONS = {"start_password_change"}


def _ctx() -> AgentContext:
    """Identity for this turn, from the session and nowhere else.

    The persona is derived server-side from the signed cookie. If the browser could name
    it, it would be a role-escalation channel rather than a preference.
    """
    user = auth.current_user() or {}
    return AgentContext(
        customer_id=auth.active_customer_id(),
        role=user.get("role", "customer"),
        username=user.get("username", ""),
    )


def _ui_action(reply) -> str:
    """The one thing a tool result may ask the browser to do."""
    for call in reply.tool_calls:
        if call.tool_name in UI_ACTIONS and isinstance(call.result, dict):
            action = call.result.get("ui_action")
            if action:
                return str(action)
    return ""


def _call_to_dict(c: ToolCall) -> dict:
    return {
        "tool_name": c.tool_name,
        "arguments": c.arguments,
        "requires_approval": c.requires_approval,
        "approved": c.approved,
        "error": c.error,
    }


# --------------------------------------------------------------------------- #
# Tool result visuals
# --------------------------------------------------------------------------- #
#
# HAND-WRITTEN, ONE ENTRY PER TOOL, ON PURPOSE. The tempting version derives a chart
# generically from `ToolCall.result` -- and that is a DLP hole, not a shortcut.
# `get_statement` returns the entire statement body; `get_account_summary` returns
# balances and `card_last4`. `_call_to_dict` drops `result` for exactly that reason, and
# a generic serialiser would put it all back on the wire while contradicting the premise
# that raw account detail never leaves the server unasked.
#
# So: a tool with no entry here produces no visual, and every entry names the specific
# fields it is allowed to read.

def _v_travel_budget(r: dict) -> list[dict]:
    rows = [c for c in (r.get("by_category") or []) if (c.get("amount") or 0) > 0]
    if not rows:
        return []
    ccy = r.get("currency") or ""
    total = r.get("recommended_total")
    return [{
        "type": "donut",
        "title": f"Where the money goes — {r.get('destination', 'your trip')}",
        "data": [{"label": c["category"], "value": round(float(c["amount"]), 2)}
                 for c in rows],
        "opts": {},
        "caption": (f"Recommended total {_money(total, ccy)} for {r.get('days')} days, "
                    f"including a 15% contingency." if total is not None else ""),
    }]


def _v_security_status(r: dict) -> list[dict]:
    out: list[dict] = []
    score = r.get("security_score")
    if isinstance(score, (int, float)):
        out.append({"type": "ring", "title": "Security score", "data": score,
                    "opts": {}, "caption": f"Grade {r.get('security_grade', '')}".strip()})

    comps = r.get("health_components") or []
    rows = []
    for c in comps:
        earned, out_of = c.get("earned"), c.get("out_of", c.get("max"))
        if not isinstance(earned, (int, float)) or not isinstance(out_of, (int, float)):
            continue
        rows.append({
            "label": str(c.get("component") or c.get("label") or ""),
            "total": out_of,
            # `earned` plus a dimmed remainder is exactly the shape stackedBars takes,
            # and it reads as "how much of this is filled in" without a second axis.
            "parts": [{"label": "earned", "value": earned},
                      {"label": "remaining", "value": max(0, out_of - earned), "dim": True}],
        })
    if rows:
        out.append({"type": "stackedBars", "title": "Account health, by component",
                    "data": rows, "opts": {"height": 40 + 34 * len(rows)},
                    "height": 40 + 34 * len(rows)})
    return out


def _v_account_summary(r: dict) -> list[dict]:
    """Credit utilisation only.

    A bar chart of three unrelated scalars (balance, limit, available) is decoration --
    the eye learns nothing from it that the numbers in the sentence above did not already
    say. Utilisation is one figure that means something on a dial.
    """
    limit, available = r.get("credit_limit"), r.get("available_credit")
    if not isinstance(limit, (int, float)) or not isinstance(available, (int, float)) or limit <= 0:
        return []
    used_pct = max(0.0, min(100.0, (limit - available) / limit * 100))
    ccy = r.get("currency") or ""
    return [{
        "type": "gauge", "title": "Credit used", "data": round(used_pct),
        "opts": {"max": 100, "sub": "of your limit"},
        "caption": f"{_money(limit - available, ccy)} of {_money(limit, ccy)} in use.",
    }]


def _v_recent_transactions(r: list) -> list[dict]:
    """Spend by kind of business.

    NOT by merchant: `list_recent_transactions` deliberately does not return merchant
    names -- they are attacker-controlled text and no tool puts them in front of the
    model. `merchant_category` is a controlled vocabulary and answers the same question.
    """
    if not isinstance(r, list) or not r:
        return []
    totals: dict[str, float] = {}
    ccy = ""
    for row in r:
        value = row.get("amount_value")
        if not isinstance(value, (int, float)):
            continue
        ccy = ccy or (row.get("currency") or "")
        key = str(row.get("merchant_category") or "other").replace("_", " ")
        totals[key] = totals.get(key, 0.0) + float(value)
    if not totals:
        return []
    top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:8]
    return [{
        "type": "bars", "title": "Spend by kind of business",
        "data": [{"label": k, "value": round(v, 2)} for k, v in top],
        "opts": {"height": 200}, "height": 200,
        "caption": f"Across the {len(r)} most recent payments"
                   + (f", in {ccy}." if ccy else "."),
    }]


def _money(value, currency: str) -> str:
    from core import money
    return money.fmt(value, currency, dp=0)


VISUALS: dict[str, Callable[[Any], list[dict]]] = {
    "plan_travel_budget": _v_travel_budget,
    "get_security_status": _v_security_status,
    "get_account_summary": _v_account_summary,
    "list_recent_transactions": _v_recent_transactions,
    # search_fraud_precedents: the table the model writes is the useful part; a bar chart
    # of similarity scores is noise dressed as rigour.
    # get_statement: no visual -- a download link is what that tool is for.
}


def visuals_for(call: ToolCall) -> list[dict]:
    """Chart specs for one tool result, or [] for any tool without an entry."""
    builder = VISUALS.get(call.tool_name)
    if builder is None or call.error or call.result is None:
        return []
    try:
        return builder(call.result) or []
    except Exception:
        # A malformed result must cost a chart, never the reply that goes with it.
        return []


@bp.post("/chat")
@auth.login_required
def chat():
    ctx = _ctx()
    cid = ctx.customer_id
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "").strip()
    approve_tool = payload.get("approve_tool")

    history = session.get(HISTORY_KEY, [])
    pending = session.get(PENDING_KEY)

    # ---- confirming a proposal -------------------------------------------------
    if approve_tool:
        if not pending or pending.get("tool_name") != approve_tool:
            # Either the session lost the proposal or the browser invented one.
            db.audit(actor=ctx.actor, event_type="GUARDRAIL", subject_id=cid,
                     detail=f"Approval for '{approve_tool}' with no matching proposal")
            return jsonify({
                "text": "That confirmation has expired. Ask me again and I'll re-propose it.",
                "tool_calls": [], "guardrail_notes": ["approval_without_proposal"],
            })

        # Execute the stored proposal — the browser's arguments are not consulted.
        call = ToolCall(pending["tool_name"], pending.get("arguments") or {},
                        requires_approval=True)
        reply = customer_agent.respond("", ctx, history=history, pending_approval=call)
        session[PENDING_KEY] = None
        refresh = pending["tool_name"] in REFRESH_AFTER

    # ---- an ordinary turn ------------------------------------------------------
    else:
        if not message:
            return jsonify({"error": "Say something first."}), 400

        reply = customer_agent.respond(message, ctx, history=history)
        history = (history + [{"role": "user", "content": message}])[-MAX_HISTORY:]

        proposal = next((c for c in reply.tool_calls
                         if c.requires_approval and c.approved is None), None)
        session[PENDING_KEY] = ({"tool_name": proposal.tool_name,
                                 "arguments": proposal.arguments} if proposal else None)
        refresh = any(c.tool_name in REFRESH_AFTER and c.approved is not False
                      and c.error is None and not (c.requires_approval and c.approved is None)
                      for c in reply.tool_calls)

    history = (history + [{"role": "assistant", "content": reply.text}])[-MAX_HISTORY:]
    session[HISTORY_KEY] = history
    session.modified = True

    # Only tools that actually ran contribute a visual: a proposal awaiting confirmation
    # has no result yet, and a rejected one never will.
    charts: list[dict] = []
    for c in reply.tool_calls:
        if c.requires_approval and c.approved is None:
            continue
        charts.extend(visuals_for(c))

    return jsonify({
        "text": reply.text,
        # `text` is kept alongside `html` because the two have different consumers: the
        # bubble renders the HTML, and voice reads the text (a spoken table is unbearable).
        "html": mdlite.render(reply.text),
        "intent": reply.intent,
        "tool_calls": [_call_to_dict(c) for c in reply.tool_calls],
        "charts": charts,
        "citations": reply.citations,
        "guardrail_notes": reply.guardrail_notes,
        "latency_ms": reply.latency_ms,
        "refresh": refresh,
        "ui_action": _ui_action(reply),
    })


@bp.post("/reset")
@auth.login_required
def reset():
    session[HISTORY_KEY] = []
    session[PENDING_KEY] = None
    return jsonify({"ok": True})
