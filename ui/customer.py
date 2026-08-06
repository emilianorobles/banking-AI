"""Customer portal -- chat agent, travel notices, transactions, fraud alerts."""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from core import db, travel
from core.agents import customer_agent
from core.contracts import ToolCall

from . import components


def _pending_key(customer_id: str) -> str:
    return f"pending_approval::{customer_id}"


def render(customer_id: str) -> None:
    customer = db.get_customer(customer_id)
    if customer is None:
        st.error(f"Customer {customer_id} not found. Run: `python -m core.seed`")
        return

    st.subheader(f"Welcome back, {customer.name.split()[0]}")

    # ---------------- alert banner ----------------
    if customer.card_frozen:
        open_alerts = [
            a for a in db.list_alerts(status="PENDING")
            if a.customer_id == customer_id
        ]
        st.error(
            f"**Your card ending {customer.card_number[-4:]} has been frozen.**  \n"
            "We detected activity that doesn't match your usual pattern and stopped it "
            "before it completed. Review the alert below — if this was you, we'll "
            "unfreeze the card straight away."
        )
        for alert in open_alerts[:2]:
            txn = db.get_transaction(alert.txn_id)
            decision = db.get_decision(alert.txn_id)
            if txn and decision:
                components.decision_card(txn, decision, show_internals=False)

    # ---------------- account strip ----------------
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        components.kpi("Card", f"•••• {customer.card_number[-4:]}",
                       "frozen" if customer.card_frozen else "active",
                       "#b91c1c" if customer.card_frozen else "#15803d")
    with c2:
        components.kpi("Home", customer.home_country, customer.home_city)
    with c3:
        notices = travel.active_notices(customer_id)
        components.kpi("Travel notices", len(notices),
                       notices[0].countries[0] if notices else "none on file")
    with c4:
        txns = db.recent_transactions(customer_id, limit=200)
        components.kpi("Transactions", len(txns), "on record")

    tab_chat, tab_travel, tab_txns = st.tabs(
        ["💬 Assistant", "✈️ Travel notices", "📄 Transactions"]
    )

    # ================= CHAT =================
    with tab_chat:
        history_key = f"chat::{customer_id}"
        if history_key not in st.session_state:
            st.session_state[history_key] = [{
                "role": "assistant",
                "content": (
                    "Hi — I can check your recent activity, explain why something was "
                    "flagged, register travel so your card isn't declined abroad, or "
                    "raise a dispute. What can I help with?"
                ),
            }]

        for msg in st.session_state[history_key]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                for note in msg.get("notes", []):
                    st.caption(note)

        # --- pending approval gate ---
        pending: ToolCall | None = st.session_state.get(_pending_key(customer_id))
        if pending is not None:
            with st.chat_message("assistant"):
                st.warning(
                    f"**Confirmation required** — this will run "
                    f"`{pending.tool_name}`"
                    + (f" with {pending.arguments}" if pending.arguments else "")
                )
                st.caption(
                    "Actions that change your account never happen automatically. "
                    "A person confirms them — that's you, here."
                )
                a, b = st.columns(2)
                if a.button("✅ Confirm", key="approve", use_container_width=True):
                    reply = customer_agent.respond(
                        "", customer_id, pending_approval=pending
                    )
                    st.session_state[_pending_key(customer_id)] = None
                    st.session_state[history_key].append({
                        "role": "assistant", "content": reply.text,
                        "notes": ["✅ confirmed by you — action executed and audit-logged"],
                    })
                    st.rerun()
                if b.button("✖ Cancel", key="reject", use_container_width=True):
                    st.session_state[_pending_key(customer_id)] = None
                    st.session_state[history_key].append({
                        "role": "assistant",
                        "content": "No problem — I haven't made any changes.",
                    })
                    st.rerun()

        prompt = st.chat_input("Ask about your account, or tell me about a trip…")
        if prompt:
            st.session_state[history_key].append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.chat_message("assistant"):
                with st.spinner("Thinking…"):
                    reply = customer_agent.respond(
                        prompt, customer_id,
                        history=st.session_state[history_key][:-1],
                    )
                st.markdown(reply.text)

                notes = []
                notes.append(f"intent: **{reply.intent}** · {reply.latency_ms} ms")
                for call in reply.tool_calls:
                    if call.requires_approval and call.approved is None:
                        notes.append(f"🔒 proposed `{call.tool_name}` — awaiting your confirmation")
                    elif call.error:
                        notes.append(f"⚠️ `{call.tool_name}` — {call.error}")
                    else:
                        notes.append(f"🔧 called `{call.tool_name}`")
                for g in reply.guardrail_notes:
                    notes.append(f"🛡️ {g}")
                if reply.citations:
                    notes.append("📚 cited " + ", ".join(reply.citations))
                for n in notes:
                    st.caption(n)

            st.session_state[history_key].append(
                {"role": "assistant", "content": reply.text, "notes": notes}
            )

            for call in reply.tool_calls:
                if call.requires_approval and call.approved is None:
                    st.session_state[_pending_key(customer_id)] = call
                    st.rerun()

        with st.expander("Things you can try"):
            st.markdown(
                "- *Any suspicious activity on my account?*\n"
                "- *I'm travelling to Spain from the 10th to the 20th*\n"
                "- *Show me my last 5 transactions*\n"
                "- *Why was my card frozen?*\n"
                "- *Freeze my card* — note that this one asks you to confirm"
            )

    # ================= TRAVEL =================
    with tab_travel:
        st.markdown(
            "Tell us where you're going and we won't flag your card for being abroad. "
            "Amount, velocity and channel checks stay fully active — a travel notice "
            "isn't a blank cheque."
        )

        with st.form("travel_form"):
            col1, col2, col3 = st.columns([2, 1, 1])
            countries = col1.multiselect(
                "Destination(s)",
                ["Spain", "France", "Germany", "Italy", "United Kingdom", "United States",
                 "India", "Singapore", "Japan", "UAE", "Thailand", "Australia",
                 "Brazil", "Mexico", "Canada", "Netherlands"],
                default=["Spain"],
            )
            start = col2.date_input("From", value=date.today())
            end = col3.date_input("To", value=date.today() + timedelta(days=10))

            if st.form_submit_button("Register travel notice", use_container_width=True):
                if not countries:
                    st.error("Pick at least one destination.")
                else:
                    try:
                        notice = travel.create_notice(
                            customer_id, countries, start, end, created_via="form"
                        )
                        st.success(
                            f"Travel notice **{notice.notice_id}** registered for "
                            f"{', '.join(notice.countries)}, "
                            f"{notice.start_date} → {notice.end_date}."
                        )
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

        st.divider()
        notices = travel.active_notices(customer_id)
        if not notices:
            st.info("No travel notices on file.")
        for notice in notices:
            col1, col2 = st.columns([5, 1])
            via = "via chat assistant" if notice.created_via == "chat_agent" else "via this form"
            col1.markdown(
                f"**{', '.join(notice.countries)}** · {notice.start_date} → "
                f"{notice.end_date}  \n<span class='sb-sub'>{notice.notice_id} · {via}</span>",
                unsafe_allow_html=True,
            )
            if col2.button("Cancel", key=f"cancel_{notice.notice_id}"):
                travel.cancel(notice.notice_id, actor=f"customer:{customer_id}")
                st.rerun()

    # ================= TRANSACTIONS =================
    with tab_txns:
        txns = db.recent_transactions(customer_id, limit=25)
        components.transactions_table(txns)

        st.divider()
        st.markdown("**Inspect a decision**")
        scored = [t for t in txns if db.get_decision(t.txn_id)]
        if not scored:
            st.caption("None of these transactions have been scored yet. "
                       "Inject one from the Demo Control tab.")
        else:
            labels = {
                f"{t.timestamp[:16].replace('T', ' ')} · {t.amount:,.2f} {t.currency} · "
                f"{t.merchant_category} · {t.city}": t
                for t in scored[:15]
            }
            choice = st.selectbox("Transaction", list(labels))
            txn = labels[choice]
            components.decision_card(txn, db.get_decision(txn.txn_id), show_internals=False)
