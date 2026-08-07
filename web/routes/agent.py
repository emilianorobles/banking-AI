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

import html as html_lib
import re

from flask import Blueprint, jsonify, request, session

from core import db
from core.agents import customer_agent
from core.contracts import ToolCall

from .. import auth

bp = Blueprint("agent", __name__, url_prefix="/api/agent")

HISTORY_KEY = "chat_history"
PENDING_KEY = "pending_approval"
MAX_HISTORY = 12

# Tools whose effects the page is showing, so the view needs to refresh after they run.
REFRESH_AFTER = {"freeze_card", "unfreeze_card", "set_travel_notice", "raise_dispute",
                 "report_card_lost", "set_spending_alert"}


def _render(text: str) -> str:
    """Escape first, then re-introduce the few marks the agent actually uses.

    Escaping before formatting rather than after is the whole point -- the agent's output
    includes merchant names and free text that came from outside, and this is the last
    place it can turn into markup.
    """
    out = html_lib.escape(text or "")
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`(.+?)`", r'<code class="mono">\1</code>', out)
    out = re.sub(r"^[-*]\s+(.+)$", r"• \1", out, flags=re.MULTILINE)
    return out.replace("\n", "<br>")


def _call_to_dict(c: ToolCall) -> dict:
    return {
        "tool_name": c.tool_name,
        "arguments": c.arguments,
        "requires_approval": c.requires_approval,
        "approved": c.approved,
        "error": c.error,
    }


@bp.post("/chat")
@auth.login_required
def chat():
    cid = auth.active_customer_id()
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "").strip()
    approve_tool = payload.get("approve_tool")

    history = session.get(HISTORY_KEY, [])
    pending = session.get(PENDING_KEY)

    # ---- confirming a proposal -------------------------------------------------
    if approve_tool:
        if not pending or pending.get("tool_name") != approve_tool:
            # Either the session lost the proposal or the browser invented one.
            db.audit(actor=f"customer:{cid}", event_type="GUARDRAIL", subject_id=cid,
                     detail=f"Approval for '{approve_tool}' with no matching proposal")
            return jsonify({
                "text": "That confirmation has expired. Ask me again and I'll re-propose it.",
                "tool_calls": [], "guardrail_notes": ["approval_without_proposal"],
            })

        # Execute the stored proposal — the browser's arguments are not consulted.
        call = ToolCall(pending["tool_name"], pending.get("arguments") or {},
                        requires_approval=True)
        reply = customer_agent.respond("", cid, history=history, pending_approval=call)
        session[PENDING_KEY] = None
        refresh = pending["tool_name"] in REFRESH_AFTER

    # ---- an ordinary turn ------------------------------------------------------
    else:
        if not message:
            return jsonify({"error": "Say something first."}), 400

        reply = customer_agent.respond(message, cid, history=history)
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

    return jsonify({
        "text": reply.text,
        "html": _render(reply.text),
        "intent": reply.intent,
        "tool_calls": [_call_to_dict(c) for c in reply.tool_calls],
        "citations": reply.citations,
        "guardrail_notes": reply.guardrail_notes,
        "latency_ms": reply.latency_ms,
        "refresh": refresh,
    })


@bp.post("/reset")
@auth.login_required
def reset():
    session[HISTORY_KEY] = []
    session[PENDING_KEY] = None
    return jsonify({"ok": True})
