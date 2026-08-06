"""Customer portal.

A monitoring dashboard rather than a chat window with extras. When someone logs into
their bank they want to know, in this order: is anything wrong, is my money safe, what
is waiting on me, and where is my spending going. The layout follows that order.

The assistant is a button that opens a dedicated chat window, not a tab competing with
the dashboard -- it is a way to act on what you have just been shown.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core import db, insights, travel
from core.agents import advisor, customer_agent
from core.contracts import ToolCall

from . import components

PLOT_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    margin=dict(l=0, r=0, t=28, b=0),
    legend=dict(orientation="h", yanchor="bottom", y=-0.25, x=0),
    font=dict(size=12),
)
CAT_COLOURS = px.colors.qualitative.Set2


def _pending_key(customer_id: str) -> str:
    return f"pending_approval::{customer_id}"


def _history_key(customer_id: str) -> str:
    return f"chat::{customer_id}"


# --------------------------------------------------------------------------- #
# Assistant, in its own window
# --------------------------------------------------------------------------- #

OPEN_KEY = "assistant_open"


@st.dialog("SentinelBank Assistant", width="large")
def _assistant_dialog(customer_id: str) -> None:
    """The assistant, in its own window.

    Streamlit dismisses a dialog on st.rerun(), and sending a chat message requires one
    to show the reply -- so a naive implementation closes the window on the first thing
    the customer types. We keep an explicit open flag in session state and re-invoke the
    dialog on every rerun while it is set, which makes the window persist across the
    whole conversation.
    """
    history_key = _history_key(customer_id)
    pending_key = _pending_key(customer_id)

    if history_key not in st.session_state:
        st.session_state[history_key] = [{
            "role": "assistant",
            "content": (
                "Hi — I can check recent activity, explain why something was flagged, "
                "register travel so your card isn't declined abroad, raise a dispute, or "
                "freeze your card. What can I help with?"
            ),
        }]

    head = st.columns([5, 1])
    head[0].caption(
        "Your card and account numbers are tokenized before anything reaches the AI. "
        "Actions that change your account always ask you to confirm."
    )
    if head[1].button("Close", key="dlg_close", use_container_width=True):
        st.session_state[OPEN_KEY] = False
        st.rerun()

    quick = st.columns(3)
    presets = [
        ("🔎 Recent activity", "Any suspicious activity on my account?"),
        ("✈️ I'm travelling", "I'm travelling to Spain from the 10th to the 20th"),
        ("❓ Why flagged?", "Why was my card frozen?"),
    ]
    preset_clicked = None
    for col, (label, prompt) in zip(quick, presets):
        if col.button(label, use_container_width=True, key=f"preset_{label}"):
            preset_clicked = prompt

    box = st.container(height=340, border=False)
    with box:
        for msg in st.session_state[history_key]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                for note in msg.get("notes", []):
                    st.caption(note)

    pending: ToolCall | None = st.session_state.get(pending_key)
    if pending is not None:
        st.warning(
            f"**Confirmation required** — this will run `{pending.tool_name}`"
            + (f" with `{pending.arguments}`" if pending.arguments else "")
        )
        st.caption("Nothing that changes your account happens without you confirming it.")
        a, b = st.columns(2)
        if a.button("✅ Confirm", key="dlg_approve", use_container_width=True,
                    type="primary"):
            reply = customer_agent.respond("", customer_id, pending_approval=pending)
            st.session_state[pending_key] = None
            st.session_state[history_key].append({
                "role": "assistant", "content": reply.text,
                "notes": ["✅ confirmed by you — executed and written to the audit log"],
            })
            st.rerun()
        if b.button("✖ Cancel", key="dlg_reject", use_container_width=True):
            st.session_state[pending_key] = None
            st.session_state[history_key].append(
                {"role": "assistant", "content": "No problem — I haven't changed anything."})
            st.rerun()

    prompt = preset_clicked or st.chat_input("Ask about your account…")
    if prompt:
        st.session_state[history_key].append({"role": "user", "content": prompt})
        with st.spinner("Thinking…"):
            reply = customer_agent.respond(
                prompt, customer_id, history=st.session_state[history_key][:-1])

        notes = [f"intent **{reply.intent}** · {reply.latency_ms} ms"]
        for call in reply.tool_calls:
            if call.requires_approval and call.approved is None:
                notes.append(f"🔒 proposed `{call.tool_name}` — awaiting your confirmation")
            elif call.error:
                notes.append(f"⚠️ `{call.tool_name}` — {call.error}")
            else:
                notes.append(f"🔧 called `{call.tool_name}`")
        notes += [f"🛡️ {g}" for g in reply.guardrail_notes]
        if reply.citations:
            notes.append("📚 cited " + ", ".join(reply.citations))

        st.session_state[history_key].append(
            {"role": "assistant", "content": reply.text, "notes": notes})

        for call in reply.tool_calls:
            if call.requires_approval and call.approved is None:
                st.session_state[pending_key] = call
        st.rerun()


# --------------------------------------------------------------------------- #

def render(customer_id: str) -> None:
    customer = db.get_customer(customer_id)
    if customer is None:
        st.error(f"Customer {customer_id} not found. Run: `python -m core.seed --reset`")
        return

    txns = db.recent_transactions(customer_id, limit=1000)
    health = insights.account_health(customer_id)
    sec = insights.security_posture(customer_id)
    prot = insights.protection_stats(customer_id)
    spend = insights.spend_analytics(customer_id, txns)
    proj = insights.monthly_projection(customer_id, txns)
    actions = insights.upcoming_actions(customer_id)

    # ---------------- hero ----------------
    first = customer.name.split()[0]
    urgent = [a for a in actions if a.priority == "urgent"]
    if urgent:
        subtitle = ("One thing needs your attention" if len(urgent) == 1
                    else f"{len(urgent)} things need your attention")
    else:
        subtitle = health.summary
    components.hero(f"Good to see you, {first}", subtitle)

    top = st.columns([1, 1, 2])
    with top[0]:
        components.score_ring(health.score, "Account health", health.grade)
    with top[1]:
        components.score_ring(sec.score, "Security", sec.grade)
    with top[2]:
        st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)
        if st.button("💬  Talk to your assistant", type="primary",
                     use_container_width=True):
            st.session_state[OPEN_KEY] = True
            st.rerun()
        st.caption(
            "Ask about any transaction, register travel, raise a dispute or freeze "
            "your card. Opens in its own window."
        )

    # Re-invoked on every rerun while open, so the window survives sending a message.
    if st.session_state.get(OPEN_KEY):
        _assistant_dialog(customer_id)

    if customer.card_frozen:
        st.error(
            f"**Your card ending {customer.card_number[-4:]} is frozen.** We stopped a "
            "transaction that didn't match your pattern before it completed. Confirm "
            "whether it was you and we'll restore the card straight away — open the "
            "assistant above."
        )

    # Two rows of three. Six across one row collapses into unreadable slivers on a
    # projector or a narrower laptop, which is exactly where this gets shown.
    k = st.columns(3)
    with k[0]:
        components.stat("Available balance",
                        f"{customer.balance:,.0f} {spend.currency}",
                        f"card •••• {customer.card_number[-4:]} · "
                        + ("frozen — action needed" if customer.card_frozen
                           else "active & monitored"),
                        "#dc2626" if customer.card_frozen else None, "💳")
    with k[1]:
        components.stat("Spent, last 30 days",
                        f"{spend.total_30d:,.0f} {spend.currency}",
                        f"{spend.change_pct:+.1f}% vs previous 30 days · "
                        f"{spend.transaction_count_30d} transactions",
                        "#d97706" if spend.change_pct > 20 else None, "📈")
    with k[2]:
        components.stat("Transactions screened", f"{prot.transactions_screened:,}",
                        "every one, before it completes", None, "🔍")

    k = st.columns(3)
    with k[0]:
        threats = prot.fraud_blocked + prot.injection_blocked
        components.stat("Threats blocked", threats,
                        f"{prot.amount_protected:,.0f} {prot.currency} protected"
                        if threats else "nothing suspicious so far",
                        "#dc2626" if threats else "#16a34a", "🛡️")
    with k[1]:
        components.stat("False declines avoided", prot.false_positives_prevented,
                        "your travel notices did this" if prot.false_positives_prevented
                        else "add a travel notice before you fly", "#16a34a", "✈️")
    with k[2]:
        components.stat("Data fields protected", prot.pii_fields_tokenized,
                        "tokenized before any AI processing", "#2563eb", "🔐")

    tabs = st.tabs([
        "🏠 Overview", "📊 Spending", "🔐 Security & data",
        "✈️ Travel", "🔔 Alerts", "📄 Statements",
    ])

    with tabs[0]:
        _overview(customer_id, actions, txns, proj)
    with tabs[1]:
        _spending(spend, proj)
    with tabs[2]:
        _security(customer_id, sec, prot)
    with tabs[3]:
        _travel(customer_id)
    with tabs[4]:
        _alerts(customer_id)
    with tabs[5]:
        _statements(customer_id, customer)


# --------------------------------------------------------------------------- #

def _overview(customer_id, actions, txns, proj) -> None:
    left, right = st.columns([3, 2])

    with left:
        head = st.columns([4, 1])
        head[0].markdown("#### Recommended for you")

        # Cache per (customer, account picture). Streamlit reruns the whole script on
        # every interaction -- switching a tab, toggling a preference -- and the advisor
        # is a ~10s model call. Without this, the dashboard would stall on every click.
        # The key is a hash of the underlying figures, so the advice refreshes by itself
        # when something about the account actually changes.
        import hashlib
        import json as _json
        facts_key = hashlib.sha256(
            _json.dumps(advisor._facts(customer_id), sort_keys=True).encode()
        ).hexdigest()[:16]
        cache_key = f"advisor::{customer_id}::{facts_key}"

        if head[1].button("↻", help="Regenerate recommendations", key="adv_refresh"):
            st.session_state.pop(cache_key, None)

        if cache_key not in st.session_state:
            with st.spinner("Reviewing your account…"):
                st.session_state[cache_key] = advisor.recommend(customer_id)
        result = st.session_state[cache_key]

        for rec in result.recommendations:
            components.recommendation_card(rec)
        st.caption(
            ("Generated by the AI advisor from your real account figures — it is given "
             "the numbers rather than asked to recall them, so it cannot invent one."
             if result.generated_by == "model"
             else "Generated from your account figures (AI advisor unavailable, "
                  "showing deterministic guidance).")
            + f" · {result.latency_ms} ms"
        )

        st.markdown("#### Recent activity")
        components.transactions_table(txns[:8])

    with right:
        st.markdown("#### Needs your attention")
        if not actions:
            st.success("Nothing waiting on you.")
        for item in actions[:5]:
            components.action_item(item)

        st.markdown("#### This month")
        pct = min(1.0, proj.days_elapsed / proj.days_in_month)
        st.progress(pct, text=f"Day {proj.days_elapsed} of {proj.days_in_month}")
        m = st.columns(2)
        m[0].metric("Spent so far", f"{proj.spent_so_far:,.0f}",
                    help=f"{proj.currency}, from day 1 of this month")
        m[1].metric("Projected", f"{proj.projected_total:,.0f}",
                    f"{proj.vs_previous_pct:+.0f}% vs last month",
                    delta_color="inverse")
        st.caption(
            f"Straight-line projection: {proj.daily_rate:,.0f} {proj.currency}/day "
            f"× {proj.days_in_month} days. Simple on purpose — you can check it yourself."
        )


def _spending(spend, proj) -> None:
    if not spend.by_category:
        st.info("Not enough transaction history yet to chart.")
        return

    c1, c2 = st.columns(2)

    with c1:
        st.markdown("**Where your money went** · last 90 days")
        df = pd.DataFrame(spend.by_category, columns=["category", "amount"])
        fig = px.pie(df, names="category", values="amount", hole=0.6,
                     color_discrete_sequence=CAT_COLOURS)
        fig.update_traces(textposition="outside", textinfo="percent+label")
        fig.update_layout(**PLOT_LAYOUT, height=330, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with c2:
        st.markdown("**Month by month**")
        df = pd.DataFrame(spend.by_month, columns=["month", "amount"])
        fig = px.bar(df, x="month", y="amount", color_discrete_sequence=["#2563eb"])
        fig.update_layout(**PLOT_LAYOUT, height=330, xaxis_title="", yaxis_title="")
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("**This month, day by day** — with the projection to month end")
    if spend.daily_current_month:
        df = pd.DataFrame(spend.daily_current_month, columns=["day", "amount"])
        df["cumulative"] = df["amount"].cumsum()
        fig = go.Figure()
        fig.add_trace(go.Bar(x=df["day"], y=df["amount"], name="Daily",
                             marker_color="rgba(37,99,235,.45)"))
        fig.add_trace(go.Scatter(x=df["day"], y=df["cumulative"], name="Running total",
                                 mode="lines+markers", line=dict(color="#7c3aed", width=3)))
        fig.add_hline(y=proj.projected_total, line_dash="dot", line_color="#d97706",
                      annotation_text=f"projected month end {proj.projected_total:,.0f}",
                      annotation_position="top left")
        if proj.previous_month:
            fig.add_hline(y=proj.previous_month, line_dash="dash", line_color="#16a34a",
                          annotation_text=f"last month {proj.previous_month:,.0f}",
                          annotation_position="bottom left")
        fig.update_layout(**PLOT_LAYOUT, height=360, xaxis_title="", yaxis_title="")
        st.plotly_chart(fig, use_container_width=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Projected by category**")
        if proj.by_category_projected:
            df = pd.DataFrame(proj.by_category_projected,
                              columns=["category", "so far", "projected"])
            fig = go.Figure()
            fig.add_trace(go.Bar(y=df["category"], x=df["so far"], name="So far",
                                 orientation="h", marker_color="#2563eb"))
            fig.add_trace(go.Bar(y=df["category"], x=df["projected"] - df["so far"],
                                 name="Rest of month", orientation="h",
                                 marker_color="rgba(37,99,235,.28)"))
            fig.update_layout(**PLOT_LAYOUT, height=300, barmode="stack",
                              xaxis_title="", yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)
    with c2:
        st.markdown("**Where you shop most** · last 90 days")
        if spend.top_merchants:
            st.dataframe(
                pd.DataFrame(
                    [{"Merchant": m, "Visits": n, "Total": f"{a:,.0f} {spend.currency}"}
                     for m, n, a in spend.top_merchants]),
                use_container_width=True, hide_index=True,
            )
        s = st.columns(3)
        s[0].metric("Transactions", spend.transaction_count_30d, help="last 30 days")
        s[1].metric("Average", f"{spend.avg_transaction:,.0f}")
        s[2].metric("Largest", f"{spend.largest_30d:,.0f}")


def _security(customer_id, sec, prot) -> None:
    left, right = st.columns([3, 2])

    with left:
        st.markdown("#### Your security checklist")
        for label, passed, detail in sec.checks:
            components.security_check(label, passed, detail)

        if sec.recommendations:
            st.markdown("#### Suggested next steps")
            for rec in sec.recommendations:
                st.markdown(f"- {rec}")

        st.markdown("#### How your data is protected")
        st.markdown(
            "Your card number, account number, email and phone are replaced with "
            "**tokens** before anything is sent to the AI model. The model can tell that "
            "two transactions used the same card without ever seeing a digit of it."
        )
        st.code(
            "What we hold:      4532 0151 1283 0366\n"
            "What the AI sees:  <PAN_9b0893>\n"
            "\n"
            "Same card, same token, every time — so the AI can still spot patterns.\n"
            "Nothing is reversible outside your authenticated session.",
            language="text",
        )
        g = st.columns(3)
        g[0].metric("Fields tokenized", prot.pii_fields_tokenized)
        g[1].metric("Attacks blocked", prot.injection_blocked)
        g[2].metric("Screened", prot.transactions_screened)
        st.caption(
            "Every decision on your account is written to an audit log with a timestamp "
            "and an actor. Outgoing messages are scanned again to make sure no personal "
            "identifier leaks, even by accident."
        )

    with right:
        st.markdown("#### Password check")
        st.caption("Runs entirely in this page. Nothing is stored, logged or sent anywhere.")

        pwd = st.text_input("Test a password", type="password",
                            placeholder="type to check strength")
        score, verdict, tips = insights.password_strength(pwd)
        if pwd:
            colour = ("#16a34a" if score >= 70 else "#d97706" if score >= 50 else "#dc2626")
            st.progress(score / 100)
            st.markdown(
                f"<span style='color:{colour};font-weight:800;'>{verdict}</span> · {score}/100",
                unsafe_allow_html=True)
            for tip in tips:
                st.caption(f"· {tip}")

        st.divider()
        st.markdown("**Need a stronger one?**")
        if st.button("🎲 Suggest a passphrase", use_container_width=True):
            st.session_state["suggested_passphrase"] = insights.suggest_passphrase()
        if st.session_state.get("suggested_passphrase"):
            st.code(st.session_state["suggested_passphrase"], language="text")
            st.caption(
                "Four random words are far harder to crack than a short mangled password, "
                "and far easier to remember. Generated in your browser session and never "
                "stored — copy it into your password manager."
            )


def _travel(customer_id) -> None:
    st.markdown(
        "Tell us where you're going and we won't flag your card for being abroad. "
        "Amount, velocity and channel checks stay fully active — a travel notice is not "
        "a blank cheque, it just removes the geography false positive."
    )

    with st.form("travel_form"):
        c1, c2, c3 = st.columns([2, 1, 1])
        countries = c1.multiselect(
            "Destination(s)",
            ["Spain", "France", "Germany", "Italy", "United Kingdom", "United States",
             "India", "Singapore", "Japan", "UAE", "Thailand", "Australia",
             "Brazil", "Mexico", "Canada", "Netherlands"],
            default=["Spain"])
        start = c2.date_input("From", value=date.today())
        end = c3.date_input("To", value=date.today() + timedelta(days=10))
        if st.form_submit_button("Register travel notice", use_container_width=True,
                                 type="primary"):
            if not countries:
                st.error("Pick at least one destination.")
            else:
                try:
                    n = travel.create_notice(customer_id, countries, start, end, "form")
                    st.success(f"Registered **{n.notice_id}** for {', '.join(n.countries)}, "
                               f"{n.start_date} → {n.end_date}.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    st.divider()
    notices = travel.active_notices(customer_id)
    if not notices:
        st.info("No travel notices on file. Your card may be flagged if you travel.")
    for n in notices:
        c1, c2 = st.columns([5, 1])
        via = "via the assistant" if n.created_via == "chat_agent" else "via this form"
        c1.markdown(
            f"**{', '.join(n.countries)}** · {n.start_date} → {n.end_date}  \n"
            f"<span class='sb-sub'>{n.notice_id} · {via}</span>", unsafe_allow_html=True)
        if c2.button("Cancel", key=f"cancel_{n.notice_id}"):
            travel.cancel(n.notice_id, actor=f"customer:{customer_id}")
            st.rerun()


def _alerts(customer_id) -> None:
    alerts = [a for a in db.list_alerts(limit=200) if a.customer_id == customer_id]
    pending = [a for a in alerts if a.status == "PENDING"]
    resolved = [a for a in alerts if a.status != "PENDING"]

    c = st.columns(3)
    c[0].metric("Awaiting your response", len(pending))
    c[1].metric("Resolved", len(resolved))
    c[2].metric("Total", len(alerts))

    if not alerts:
        st.success("No alerts on your account. Nothing has needed your attention.")
        return

    st.markdown("#### Awaiting your response")
    if not pending:
        st.caption("Nothing outstanding.")
    for alert in pending[:6]:
        txn = db.get_transaction(alert.txn_id)
        decision = db.get_decision(alert.txn_id)
        if not (txn and decision):
            continue
        with st.expander(f"⚠️ [{alert.risk_score}] {alert.summary}",
                         expanded=(alert is pending[0])):
            components.decision_card(txn, decision, show_internals=False)
            st.caption("Open the assistant to confirm whether this was you.")

    if resolved:
        st.markdown("#### Resolved")
        st.dataframe(
            pd.DataFrame([{
                "When": a.created_at[:16].replace("T", " "),
                "Summary": a.summary,
                "Outcome": a.outcome or a.status,
                "Reviewed by": a.resolved_by or "—",
            } for a in resolved[:20]]),
            use_container_width=True, hide_index=True)


def _statements(customer_id, customer) -> None:
    left, right = st.columns([3, 2])

    with left:
        st.markdown("#### Monthly statement")
        st.caption(
            "Assembled from your real records — health score, spending breakdown, "
            "month-end projection, protection summary and security checks."
        )
        statement = insights.build_statement(customer_id)
        st.download_button(
            "⬇️  Download statement (.txt)",
            data=statement,
            file_name=f"sentinelbank-statement-{customer_id}-{date.today()}.txt",
            mime="text/plain",
            use_container_width=True,
            type="primary",
        )

        txns = db.recent_transactions(customer_id, limit=500)
        if txns:
            rows = []
            for t in txns:
                d = db.get_decision(t.txn_id) or {}
                rows.append({
                    "timestamp": t.timestamp, "amount": t.amount, "currency": t.currency,
                    "merchant": t.merchant, "category": t.merchant_category,
                    "city": t.city, "country": t.country, "channel": t.channel,
                    "risk_score": d.get("risk_score"), "decision": d.get("action"),
                })
            st.download_button(
                "⬇️  Download transactions (.csv)",
                data=pd.DataFrame(rows).to_csv(index=False),
                file_name=f"sentinelbank-transactions-{customer_id}-{date.today()}.csv",
                mime="text/csv",
                use_container_width=True,
            )

        with st.expander("Preview the statement"):
            st.code(statement, language="text")

    with right:
        st.markdown("#### How we reach you")
        st.caption("Where each kind of message should go.")

        prefs = st.session_state.setdefault(f"prefs::{customer_id}", {
            "fraud_push": True, "fraud_email": True, "fraud_sms": True,
            "statement_email": True, "spending_email": False, "marketing": False,
        })

        st.markdown("**Fraud alerts** — we always contact you somehow")
        prefs["fraud_push"] = st.toggle("Push notification", prefs["fraud_push"])
        prefs["fraud_sms"] = st.toggle("SMS", prefs["fraud_sms"])
        prefs["fraud_email"] = st.toggle("Email", prefs["fraud_email"])

        st.markdown("**Reports**")
        prefs["statement_email"] = st.toggle("Monthly statement by email",
                                             prefs["statement_email"])
        prefs["spending_email"] = st.toggle("Weekly spending summary",
                                            prefs["spending_email"])
        prefs["marketing"] = st.toggle("Product news", prefs["marketing"])

        if st.button("Save preferences", use_container_width=True):
            db.audit(actor=f"customer:{customer_id}", event_type="PREFERENCES",
                     subject_id=customer_id,
                     detail="Notification preferences updated", **prefs)
            st.success("Saved and written to your audit log.")

        st.caption(
            f"Delivery would go to **{customer.email}** and the phone ending "
            f"{customer.phone[-4:]}. This prototype records the preference and the audit "
            "entry; connecting a real mail or SMS gateway is a configuration change, not "
            "a product one, so we have not faked a send here."
        )
