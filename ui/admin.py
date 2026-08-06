"""Admin / supervisor portal.

Five things live here, in the order a fraud operations lead would actually use them:
KPIs, the region view, the alert queue with human approval, cost & performance, and the
evaluation harness. The audit log and knowledge store sit at the bottom as evidence.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from core import config, db, rag

from . import components


def render() -> None:
    st.subheader("Fraud Operations")
    components.health_badge()

    regions = ["ALL"] + config.REGIONS
    region = st.selectbox("Region", regions, index=0, key="admin_region")

    tabs = st.tabs([
        "📊 Overview", "🚨 Alert queue", "💰 Cost & performance",
        "🧪 Evaluation", "📚 Knowledge store", "🔍 Audit log",
    ])

    with tabs[0]:
        _overview(region)
    with tabs[1]:
        _alert_queue(region)
    with tabs[2]:
        _cost_panel()
    with tabs[3]:
        _evaluation_panel()
    with tabs[4]:
        _knowledge_panel()
    with tabs[5]:
        _audit_panel()


# --------------------------------------------------------------------------- #

def _overview(region: str) -> None:
    stats = db.region_stats(region)
    pending = db.list_alerts(status="PENDING", region=region, limit=500)
    cost = db.cost_summary()

    total_txns = sum(s["transactions"] for s in stats)
    total_frozen = sum(s["frozen"] for s in stats)
    total_suppressed = sum(s["travel_suppressed"] for s in stats)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        components.kpi("Transactions scored", f"{total_txns:,}",
                       f"region: {region.lower()}")
    with c2:
        components.kpi("Awaiting review", len(pending),
                       "human approval required",
                       "#b45309" if pending else "#15803d")
    with c3:
        components.kpi("Cards frozen", total_frozen, "auto-frozen, pending confirmation")
    with c4:
        components.kpi("False positives prevented", total_suppressed,
                       "via travel notices", "#15803d")

    if not stats:
        st.info("No scored transactions yet. Use the Demo Control tab to inject some.")
        return

    df = pd.DataFrame(stats)
    left, right = st.columns([3, 2])

    with left:
        st.markdown("**Escalations by region**")
        melted = df.melt(
            id_vars="region",
            value_vars=["frozen", "challenged", "travel_suppressed"],
            var_name="outcome", value_name="count",
        )
        fig = px.bar(
            melted, x="region", y="count", color="outcome", barmode="group",
            color_discrete_map={"frozen": "#b91c1c", "challenged": "#b45309",
                                "travel_suppressed": "#15803d"},
        )
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0),
                          legend_title_text="")
        st.plotly_chart(fig, use_container_width=True)

    with right:
        st.markdown("**Regional detail**")
        show = df[["region", "transactions", "frozen", "challenged", "avg_risk"]].copy()
        show["avg_risk"] = show["avg_risk"].round(1)
        st.dataframe(show, use_container_width=True, hide_index=True)

    st.caption(
        f"Model inference used on {cost['llm_transactions']} of "
        f"{cost['total_transactions']} scored transactions "
        f"({100 - cost['avoided_pct']:.1f}%)."
    )


# --------------------------------------------------------------------------- #

def _alert_queue(region: str) -> None:
    st.markdown(
        "Alerts are ordered by risk. **Nothing here is final** — a freeze stops the "
        "bleeding immediately, but an analyst confirms or reverses it. Confirming a "
        "case writes it back into the knowledge store, so the next similar transaction "
        "is caught faster."
    )

    status = st.radio("Show", ["PENDING", "APPROVED", "REJECTED"],
                      horizontal=True, key="alert_status")
    alerts = db.list_alerts(status=status, region=region, limit=50)

    if not alerts:
        st.success(f"No {status.lower()} alerts for {region.lower()}.")
        return

    for alert in alerts:
        txn = db.get_transaction(alert.txn_id)
        decision = db.get_decision(alert.txn_id)
        if txn is None or decision is None:
            continue

        customer = db.get_customer(alert.customer_id)
        header = (f"{components.RISK_COLOURS.get(alert.risk_level, '')and ''}"
                  f"[{alert.risk_score}] {alert.summary}")

        with st.expander(header, expanded=(status == "PENDING" and alert is alerts[0])):
            components.decision_card(txn, decision)

            if status != "PENDING":
                st.info(
                    f"Resolved by **{alert.resolved_by}** at {alert.resolved_at} — "
                    f"**{alert.outcome}**"
                    + (f"  \n_{alert.analyst_note}_" if alert.analyst_note else "")
                )
                if alert.learned_case_id:
                    st.success(
                        f"Written back into the knowledge store as "
                        f"**{alert.learned_case_id}** — retrievable immediately."
                    )
                continue

            st.divider()
            st.markdown("**Analyst decision**")
            note = st.text_input(
                "Case note (becomes part of the retrievable precedent)",
                key=f"note_{alert.alert_id}",
                placeholder="e.g. Customer confirmed card still in possession; PAN compromised.",
            )
            a, b, c = st.columns(3)

            if a.button("🔴 Confirm fraud", key=f"fraud_{alert.alert_id}", use_container_width=True):
                case = rag.learn_from_alert(
                    alert, txn, "confirmed_fraud",
                    note or "Confirmed fraudulent by analyst review.", "analyst:ops1",
                )
                db.resolve_alert(alert.alert_id, "confirmed_fraud", "analyst:ops1",
                                 note, case.case_id)
                db.audit(actor="analyst:ops1", event_type="APPROVAL",
                         subject_id=alert.alert_id,
                         detail=f"Confirmed fraud; freeze upheld; learned {case.case_id}")
                st.success(f"Confirmed. Indexed as **{case.case_id}** — the system now "
                           "recognises this pattern.")
                st.rerun()

            if b.button("🟢 False positive", key=f"fp_{alert.alert_id}", use_container_width=True):
                case = rag.learn_from_alert(
                    alert, txn, "false_positive",
                    note or "Cleared by analyst; legitimate customer activity.", "analyst:ops1",
                )
                db.resolve_alert(alert.alert_id, "false_positive", "analyst:ops1",
                                 note, case.case_id)
                if customer:
                    db.set_card_frozen(customer.customer_id, False)
                db.audit(actor="analyst:ops1", event_type="APPROVAL",
                         subject_id=alert.alert_id,
                         detail=f"Cleared as false positive; card unfrozen; learned {case.case_id}")
                st.success(f"Cleared and card unfrozen. Indexed as **{case.case_id}** so "
                           "similar activity is less likely to be flagged again.")
                st.rerun()

            c.caption("Both outcomes teach the system. A false positive is as valuable "
                      "a signal as a confirmed fraud.")


# --------------------------------------------------------------------------- #

def _cost_panel() -> None:
    cost = db.cost_summary()

    st.markdown(
        "#### Why this is deployable at bank scale\n"
        "Deterministic rules resolve the overwhelming majority of transactions with no "
        "model inference at all. Inference is spent only where it changes an outcome — "
        "on ambiguous cases and on every escalation a human has to action."
    )

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        components.kpi("Transactions scored", f"{cost['total_transactions']:,}")
    with c2:
        components.kpi("Resolved without a model", f"{cost['avoided']:,}",
                       f"{cost['avoided_pct']}% of volume", "#15803d")
    with c3:
        components.kpi("Actual spend", f"${cost['actual_cost_usd']:.4f}",
                       f"{cost['llm_calls']} model calls")
    with c4:
        components.kpi("If every txn used a model", f"${cost['naive_cost_usd']:.4f}",
                       f"saving ${cost['saved_usd']:.4f}", "#15803d")

    st.caption(
        "Cost is estimated from published gpt-4.1 rates against measured token usage. "
        "Latency and token counts are recorded per call, not sampled."
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        components.kpi("Avg latency", f"{cost['avg_latency_ms']} ms", "model calls only")
    with c2:
        components.kpi("p95 latency", f"{cost['p95_latency_ms']} ms", "tail performance")
    with c3:
        components.kpi("Tokens", f"{cost['prompt_tokens'] + cost['completion_tokens']:,}",
                       f"{cost['prompt_tokens']:,} in / {cost['completion_tokens']:,} out")

    if cost["total_transactions"]:
        share = pd.DataFrame({
            "path": ["Rules only (no model)", "Model inference"],
            "count": [cost["avoided"], cost["llm_transactions"]],
        })
        fig = px.pie(share, names="path", values="count", hole=0.55,
                     color="path",
                     color_discrete_map={"Rules only (no model)": "#15803d",
                                         "Model inference": "#2563eb"})
        fig.update_layout(height=280, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)


# --------------------------------------------------------------------------- #

def _evaluation_panel() -> None:
    from core import evaluation

    st.markdown(
        "#### Measured, not demonstrated\n"
        "A demo proves a system can work once. This runs the **real pipeline** over a "
        "held-out labelled set and reports what it actually does — including the metric "
        "most fraud demos avoid: how often it wrongly flags a legitimate customer."
    )

    col1, col2 = st.columns([1, 3])
    limit = col2.slider("Cases to run", 5, 30, 12, key="eval_limit",
                        help="Each case runs the full pipeline. Fewer = faster.")
    ab = col2.checkbox(
        "Also run a rules-only baseline (proves what the AI layer is worth)",
        value=True, key="eval_ab",
    )

    if col1.button("▶ Run evaluation", type="primary", use_container_width=True):
        rows = evaluation.load_eval_set()[:limit]
        progress = st.progress(0.0, text="Running…")
        results, baseline = [], []
        for i, row in enumerate(rows, 1):
            if ab:
                baseline.append(evaluation.run_case(row, allow_llm=False))
            results.append(evaluation.run_case(row, allow_llm=True))
            progress.progress(i / len(rows), text=f"Case {i}/{len(rows)}")
        progress.empty()
        st.session_state["eval_results"] = results
        st.session_state["eval_baseline"] = baseline if ab else None

    baseline = st.session_state.get("eval_baseline")
    if baseline:
        bm = evaluation.summarise(baseline)
        fm = evaluation.summarise(st.session_state.get("eval_results", []))
        st.markdown("#### Conventional rules engine vs this system")
        st.caption(
            "Same cases, same thresholds, same labels. The only difference is whether "
            "retrieval and the analyst agent are allowed to run."
        )
        st.dataframe(pd.DataFrame([
            {"Metric": "Recall (fraud caught)",
             "Rules only": f"{bm['recall']:.0%}", "With AI": f"{fm['recall']:.0%}",
             "Δ": f"{fm['recall'] - bm['recall']:+.0%}"},
            {"Metric": "Precision",
             "Rules only": f"{bm['precision']:.0%}", "With AI": f"{fm['precision']:.0%}",
             "Δ": f"{fm['precision'] - bm['precision']:+.0%}"},
            {"Metric": "False positive rate",
             "Rules only": f"{bm['fpr']:.0%}", "With AI": f"{fm['fpr']:.0%}",
             "Δ": f"{fm['fpr'] - bm['fpr']:+.0%}"},
            {"Metric": "Legitimate customers wrongly flagged",
             "Rules only": bm["fp"], "With AI": fm["fp"], "Δ": fm["fp"] - bm["fp"]},
        ]), use_container_width=True, hide_index=True)
        removed = bm["fp"] - fm["fp"]
        if removed > 0:
            st.success(
                f"**{removed} legitimate customer{'s' if removed != 1 else ''} would have "
                f"been wrongly declined by a conventional rules engine** and were correctly "
                "allowed here — because the agent retrieved a past case where an analyst "
                "had already ruled that exact pattern legitimate."
            )
        st.divider()

    results = st.session_state.get("eval_results")
    if not results:
        st.info("No evaluation run yet.")
        return

    m = evaluation.summarise(results)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        components.kpi("Recall (fraud caught)", f"{m['recall']:.0%}",
                       f"{m['tp']}/{m['tp'] + m['fn']} fraud cases",
                       "#15803d" if m["recall"] >= 0.85 else "#b45309")
    with c2:
        components.kpi("Precision", f"{m['precision']:.0%}", "of flags that were fraud")
    with c3:
        components.kpi("False positive rate", f"{m['fpr']:.0%}",
                       f"{m['fp']} legitimate customers flagged",
                       "#15803d" if m["fpr"] <= 0.10 else "#b91c1c")
    with c4:
        components.kpi("Groundedness", f"{m['groundedness']:.0%}",
                       "citations that resolve",
                       "#15803d" if m["groundedness"] >= 0.999 else "#b91c1c")

    c1, c2, c3 = st.columns(3)
    with c1:
        components.kpi("F1", f"{m['f1']:.2f}")
    with c2:
        components.kpi("Avg latency", f"{m['avg_latency_ms']} ms")
    with c3:
        components.kpi("Cost for this run", f"${m['total_cost_usd']:.4f}")

    st.markdown("**Confusion matrix**")
    matrix = pd.DataFrame(
        [[m["tp"], m["fn"]], [m["fp"], m["tn"]]],
        index=["Actually fraud", "Actually legitimate"],
        columns=["Flagged", "Allowed"],
    )
    st.dataframe(matrix, use_container_width=True)

    if m["groundedness"] < 0.999:
        st.error(
            f"**{m['fabricated']} fabricated citation(s) detected.** The model cited "
            "case IDs that do not exist in the knowledge store. These were stripped by "
            "the guardrail before reaching a human, but it is a hallucination signal."
        )

    st.markdown("**Per-case results**")
    st.dataframe(
        pd.DataFrame([{
            "kind": r["eval_kind"],
            "expected": "fraud" if r["label"] else "legitimate",
            "score": r["risk_score"],
            "action": r["action"],
            "correct": "✅" if r["correct"] else "❌",
            "model": "yes" if r["llm_used"] else "no",
            "cites": ", ".join(r["cited"]) or "—",
            "ms": r["latency_ms"],
        } for r in results]),
        use_container_width=True, hide_index=True,
    )


# --------------------------------------------------------------------------- #

def _knowledge_panel() -> None:
    seeded = db.list_fraud_cases(source="seed")
    learned = db.list_fraud_cases(source="learned")

    c1, c2, c3 = st.columns(3)
    with c1:
        components.kpi("Cases indexed", len(seeded) + len(learned), "in FAISS")
    with c2:
        components.kpi("Learned this session", len(learned),
                       "from analyst decisions", "#2563eb")
    with c3:
        components.kpi("Vectors", rag.index_size(), "text-embedding-3-large")

    if learned:
        st.markdown("#### Learned during this session")
        st.caption("Every one of these came from an analyst resolving an alert. "
                   "They are retrievable evidence immediately, with no retraining.")
        for case in learned:
            badge = "🔴" if case.outcome == "confirmed_fraud" else "🟢"
            st.markdown(
                f'<div class="sb-cite"><strong>{badge} {case.case_id}</strong> — '
                f'{case.title}<br><span class="sb-sub">{case.narrative}</span></div>',
                unsafe_allow_html=True,
            )

    st.divider()
    st.markdown("#### Search the knowledge store")
    query = st.text_input("Query", placeholder="e.g. impossible travel crypto high value")
    if query:
        for hit in rag.search(query, k=5):
            badge = "🔴" if hit.get("outcome") == "confirmed_fraud" else "🟢"
            st.markdown(
                f'<div class="sb-cite"><strong>{badge} {hit.get("case_id")}</strong> · '
                f'similarity {hit.get("similarity")} · {hit.get("source")}<br>'
                f'{hit.get("title")}<br>'
                f'<span class="sb-sub">{hit.get("analyst_note", "")}</span></div>',
                unsafe_allow_html=True,
            )


# --------------------------------------------------------------------------- #

def _audit_panel() -> None:
    st.markdown(
        "Every decision, tool call, guardrail trip and approval is recorded. This is what "
        "makes the system governable rather than merely functional."
    )
    entries = db.list_audit(limit=300)
    if not entries:
        st.info("No audit entries yet.")
        return

    kinds = sorted({e["event_type"] for e in entries})
    chosen = st.multiselect("Event types", kinds, default=kinds)
    rows = [{
        "time": e["timestamp"][11:19],
        "actor": e["actor"],
        "event": e["event_type"],
        "subject": e["subject_id"],
        "detail": e["detail"][:110],
    } for e in entries if e["event_type"] in chosen]

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=460)
    st.caption(f"{len(rows)} of {len(entries)} entries")
