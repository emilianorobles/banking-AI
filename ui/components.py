"""Shared render helpers.

Kept in one place so the customer and admin portals stay visually consistent and so
there is exactly one definition of "what a decision looks like on screen".
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from core import db
from core.contracts import Decision, Transaction

RISK_COLOURS = {
    "LOW": "#15803d",
    "MEDIUM": "#b45309",
    "HIGH": "#c2410c",
    "CRITICAL": "#b91c1c",
}

ACTION_LABELS = {
    "ALLOW": ("Allowed", "#15803d"),
    "CHALLENGE": ("Step-up verification", "#b45309"),
    "FREEZE_AND_ESCALATE": ("Card frozen — escalated", "#b91c1c"),
    "QUARANTINE": ("Quarantined — security", "#7c3aed"),
}


def inject_css() -> None:
    st.markdown(
        """
        <style>
          .sb-card{border:1px solid rgba(128,128,128,.25);border-radius:10px;
                   padding:1rem 1.1rem;margin-bottom:.75rem;}
          .sb-pill{display:inline-block;padding:.15rem .6rem;border-radius:999px;
                   font-size:.75rem;font-weight:700;color:#fff;}
          .sb-kpi{font-size:1.9rem;font-weight:800;line-height:1.1;margin:.1rem 0;}
          .sb-kpi-label{font-size:.78rem;opacity:.7;text-transform:uppercase;
                        letter-spacing:.05em;font-weight:700;}
          .sb-sub{font-size:.8rem;opacity:.65;}
          .sb-rule{border-left:3px solid #94a3b8;padding:.35rem .7rem;margin:.3rem 0;
                   font-size:.88rem;background:rgba(148,163,184,.08);}
          .sb-cite{border-left:3px solid #2563eb;padding:.4rem .7rem;margin:.35rem 0;
                   font-size:.85rem;background:rgba(37,99,235,.07);}
          .sb-danger{border-left:3px solid #b91c1c;padding:.5rem .8rem;
                     background:rgba(185,28,28,.08);border-radius:6px;}
          .sb-ok{border-left:3px solid #15803d;padding:.5rem .8rem;
                 background:rgba(21,128,61,.08);border-radius:6px;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def risk_pill(level: str, score: int | None = None) -> str:
    colour = RISK_COLOURS.get(level, "#64748b")
    text = f"{level}" + (f" · {score}" if score is not None else "")
    return f'<span class="sb-pill" style="background:{colour}">{text}</span>'


def kpi(label: str, value: Any, sub: str = "", colour: str | None = None) -> None:
    style = f"color:{colour};" if colour else ""
    st.markdown(
        f'<div class="sb-card"><div class="sb-kpi-label">{label}</div>'
        f'<div class="sb-kpi" style="{style}">{value}</div>'
        f'<div class="sb-sub">{sub}</div></div>',
        unsafe_allow_html=True,
    )


def decision_card(txn: Transaction, decision: Decision | dict, *, show_internals: bool = True) -> None:
    """The single most important view in the product: why we decided what we decided."""
    d = decision.to_dict() if isinstance(decision, Decision) else dict(decision)

    label, colour = ACTION_LABELS.get(d["action"], (d["action"], "#64748b"))
    st.markdown(
        f'<div class="sb-card">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;'
        f'flex-wrap:wrap;gap:.5rem;">'
        f'<div><strong style="font-size:1.05rem;">{txn.amount:,.2f} {txn.currency}</strong>'
        f'<span class="sb-sub"> · {txn.merchant_category} · {txn.city}, {txn.country}'
        f' · {txn.channel}</span></div>'
        f'<div>{risk_pill(d["risk_level"], d["risk_score"])}'
        f'<span class="sb-pill" style="background:{colour};margin-left:.35rem;">{label}</span>'
        f'</div></div></div>',
        unsafe_allow_html=True,
    )

    if d.get("injection_detected"):
        st.markdown(
            f'<div class="sb-danger"><strong>⛔ Prompt injection blocked</strong><br>'
            f'<span class="sb-sub">{d.get("injection_evidence", "")[:300]}</span></div>',
            unsafe_allow_html=True,
        )
        st.caption("The untrusted text was never evaluated as an instruction.")

    if d.get("reasoning"):
        st.markdown(f"**Assessment**  \n{d['reasoning']}")

    cols = st.columns(4)
    cols[0].metric("Rule score", d.get("rule_score", 0))
    cols[1].metric("Final score", d.get("risk_score", 0))
    conf = d.get("confidence")
    cols[2].metric("Confidence", f"{conf:.0%}" if conf else "—")
    cols[3].metric("Latency", f"{d.get('latency_ms', 0)} ms")

    hits = d.get("rule_hits") or []
    if hits:
        st.markdown("**Rules triggered**")
        for hit in hits:
            h = hit if isinstance(hit, dict) else hit.__dict__
            st.markdown(
                f'<div class="sb-rule"><strong>{h["rule_id"]}</strong> (+{h["weight"]}) '
                f'— {h["reason"]}</div>',
                unsafe_allow_html=True,
            )
    elif not d.get("injection_detected"):
        st.caption("No deterministic rules triggered.")

    if d.get("suppressed_by_travel"):
        st.markdown(
            '<div class="sb-ok"><strong>✈ Travel notice applied</strong><br>'
            '<span class="sb-sub">Geography rules would have flagged this transaction. '
            'The customer told us about this trip in advance, so they were suppressed — '
            'a false positive prevented. Amount, velocity and channel checks stayed '
            'active.</span></div>',
            unsafe_allow_html=True,
        )

    cited = d.get("cited_case_ids") or []
    if cited:
        st.markdown("**Historical precedent cited**")
        for case_id in cited:
            case = db.get_fraud_case(case_id)
            if case is None:
                st.markdown(
                    f'<div class="sb-danger">{case_id} — <strong>not found in the '
                    f'knowledge store</strong> (fabricated citation)</div>',
                    unsafe_allow_html=True,
                )
                continue
            badge = "🔴 confirmed fraud" if case.outcome == "confirmed_fraud" else "🟢 false positive"
            learned = " · **learned during this session**" if case.source == "learned" else ""
            st.markdown(
                f'<div class="sb-cite"><strong>{case.case_id}</strong> · {badge}{learned}<br>'
                f'{case.title}<br><span class="sb-sub">{case.analyst_note}</span></div>',
                unsafe_allow_html=True,
            )

    if show_internals:
        with st.expander("Guardrails, telemetry & audit"):
            g1, g2, g3 = st.columns(3)
            g1.markdown(f"**Grounded**  \n{'✅ yes' if d.get('groundedness_ok', True) else '❌ fabricated citation'}")
            g2.markdown(f"**DLP**  \n{'⚠️ redacted' if d.get('dlp_blocked') else '✅ clean'}")
            g3.markdown(f"**Model used**  \n{'yes' if d.get('llm_used') else 'no — rules only'}")

            if d.get("retrieved_case_ids"):
                st.caption("Retrieved: " + ", ".join(d["retrieved_case_ids"]))
            for note in d.get("guardrail_notes") or []:
                st.caption(f"· {note}")
            if d.get("llm_used"):
                st.caption(
                    f"· tokens: {d.get('prompt_tokens', 0)} in / "
                    f"{d.get('completion_tokens', 0)} out · "
                    f"est. cost ${d.get('est_cost_usd', 0):.5f}"
                )


def transactions_table(txns: list[Transaction], *, with_decisions: bool = True) -> None:
    if not txns:
        st.info("No transactions yet.")
        return
    rows = []
    for t in txns:
        row = {
            "When": t.timestamp[:16].replace("T", " "),
            "Amount": f"{t.amount:,.2f} {t.currency}",
            "Merchant": t.merchant[:40],
            "Category": t.merchant_category,
            "Location": f"{t.city}, {t.country}",
            "Channel": t.channel,
        }
        if with_decisions:
            d = db.get_decision(t.txn_id) or {}
            row["Risk"] = d.get("risk_score", "—")
            row["Decision"] = d.get("action", "not scored")
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def health_badge() -> None:
    """Compact provider/index status. Catches a dead key before it ruins a demo."""
    from core import config, llm as llm_mod, rag

    mode = config.DEMO_MODE
    key_ok = config.has_api_key()
    index_n = rag.index_size()

    bits = [f"mode **{mode}**"]
    bits.append("key ✅" if key_ok else "key ❌")
    bits.append(f"index **{index_n}** cases" if index_n else "index ❌")
    bits.append(f"cached **{llm_mod.cache_size()}**")
    st.caption(" · ".join(bits))
