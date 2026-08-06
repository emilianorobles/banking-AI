"""Demo Control -- preset transaction injection.

Everything on stage is a button. Nothing is typed live: a typo in front of judges costs
forty seconds and all your momentum. The scenarios come from data/demo_injections.json
so the same sequence is reproducible across every rehearsal.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import streamlit as st

from core import config, db, pipeline, travel
from core.contracts import Transaction, new_id

from . import components


def _load_demo() -> dict:
    if not config.DEMO_INJECTIONS_PATH.exists():
        return {}
    return json.loads(config.DEMO_INJECTIONS_PATH.read_text(encoding="utf-8"))


def _inject(scenario: dict) -> tuple[Transaction, object]:
    """Score an injected transaction, narrating each pipeline stage as it runs.

    Retrieval and inference take a few seconds. Showing the stage names turns that wait
    into a live walkthrough of the architecture instead of a spinner — the judges watch
    the pipeline work rather than watching you wait.
    """
    payload = dict(scenario["txn"])
    payload["txn_id"] = new_id("TXN")
    payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    txn = Transaction(**payload)

    with st.status("Scoring transaction…", expanded=True) as status:
        def on_step(label: str) -> None:
            status.write(f"→ {label}")

        decision = pipeline.score_transaction(txn, persist=True, on_step=on_step)
        status.update(
            label=f"{decision.action} · risk {decision.risk_score} · {decision.latency_ms} ms",
            state="complete", expanded=False,
        )
    return txn, decision


def render() -> None:
    st.subheader("Demo Control")
    components.health_badge()

    demo = _load_demo()
    if not demo:
        st.error("No demo scenarios found. Run: `python -m data.generate`")
        return

    hero_id = demo["hero_customer_id"]
    st.caption(
        f"Scripted scenarios for **{demo['hero_customer_name']}** (`{hero_id}`). "
        "Switch to the Customer portal as this customer to see the effect."
    )

    # ---------------- setup ----------------
    with st.expander("Demo setup", expanded=True):
        c1, c2, c3 = st.columns(3)

        notices = travel.active_notices(hero_id)
        if c1.button(
            f"✈️ File travel notice (Spain){'  ✓' if notices else ''}",
            use_container_width=True,
            help="Run this BEFORE the 'legitimate in Spain' scenario.",
        ):
            tn = demo["travel_notice"]
            travel.create_notice(tn["customer_id"], tn["countries"],
                                 tn["start_date"], tn["end_date"], created_via="form")
            st.success("Travel notice filed for Spain.")
            st.rerun()

        if c2.button("🔓 Unfreeze hero card", use_container_width=True,
                     help="Reset the card between rehearsals."):
            db.set_card_frozen(hero_id, False)
            st.success("Card unfrozen.")
            st.rerun()

        if c3.button("♻️ Full reset (reseed)", use_container_width=True,
                     help="Wipes transactions, alerts and learned cases. Rebuilds seed state."):
            from core import seed
            with st.spinner("Reseeding…"):
                seed.seed(reset=True, build_index=False)
            for key in list(st.session_state.keys()):
                if str(key).startswith(("chat::", "pending_approval::", "eval_results")):
                    del st.session_state[key]
            st.success("Reset complete. Re-file the travel notice before running the demo.")
            st.rerun()

    st.divider()

    # ---------------- scenarios ----------------
    st.markdown("#### Inject a transaction")
    for scenario in demo["scenarios"]:
        key = scenario["key"]
        is_attack = key.startswith("attack")
        is_fraud = key.startswith("fraud")
        icon = "🛡️" if is_attack else ("🚨" if is_fraud else "✅")

        with st.container():
            c1, c2 = st.columns([3, 1])
            c1.markdown(
                f"**{icon} {scenario['label']}**  \n"
                f"<span class='sb-sub'>{scenario['expect']}</span>",
                unsafe_allow_html=True,
            )
            if c2.button("Inject", key=f"inject_{key}", use_container_width=True):
                with st.spinner("Scoring…"):
                    txn, decision = _inject(scenario)
                st.session_state["last_injection"] = txn.txn_id
                st.rerun()

    # ---------------- result ----------------
    last = st.session_state.get("last_injection")
    if last:
        txn = db.get_transaction(last)
        decision = db.get_decision(last)
        if txn and decision:
            st.divider()
            st.markdown("#### Result")
            components.decision_card(txn, decision)

    st.divider()
    with st.expander("Inject a custom transaction"):
        st.caption("For Q&A — judges often ask to try their own scenario.")
        customers = db.list_customers(limit=25)
        options = {f"{c.name} ({c.customer_id})": c for c in customers}
        chosen = st.selectbox("Customer", list(options))
        customer = options[chosen]

        c1, c2, c3 = st.columns(3)
        amount = c1.number_input("Amount", min_value=1.0,
                                 value=float(round(customer.baseline_avg_amount, 2)))
        country = c2.text_input("Country (ISO-2)", value=customer.home_country)
        city = c3.text_input("City", value=customer.home_city)

        c1, c2, c3 = st.columns(3)
        merchant = c1.text_input("Merchant", value="FreshMart")
        category = c2.selectbox("Category", [
            "groceries", "restaurants", "fuel", "travel", "electronics", "apparel",
            "crypto_exchange", "gift_cards", "wire_transfer", "online_gambling",
        ])
        channel = c3.selectbox("Channel", [
            "card_present", "card_not_present", "online", "atm", "transfer",
        ])
        hours_ago = st.slider("Hours ago", 0, 24, 0)

        if st.button("Inject custom transaction", type="primary"):
            txn = Transaction(
                txn_id=new_id("TXN"),
                customer_id=customer.customer_id,
                timestamp=(datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(),
                amount=float(amount),
                currency="INR" if customer.home_country == "IN" else "USD",
                merchant=merchant,
                merchant_category=category,
                country=country.upper()[:2],
                city=city,
                region=customer.region,
                channel=channel,
                card_last4=customer.card_number[-4:],
                device_id="dev-custom",
                ip_address="10.0.0.1",
            )
            with st.spinner("Scoring…"):
                pipeline.score_transaction(txn, persist=True)
            st.session_state["last_injection"] = txn.txn_id
            st.rerun()

    st.divider()
    st.caption(
        "The same scoring path is available over HTTP for external feeds:  \n"
        "`uvicorn api.main:app --port 8000` then "
        "`POST /api/transactions` — see the API tab in the docs."
    )
