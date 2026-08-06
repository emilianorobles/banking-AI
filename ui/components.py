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
    """One stylesheet for the whole app.

    Colours are expressed against Streamlit's theme variables where possible so the UI
    works in both light and dark mode -- the demo laptop's theme is not worth gambling on.
    """
    st.markdown(
        """
        <style>
          :root{
            --sb-line:rgba(128,128,128,.22);
            --sb-soft:rgba(128,128,128,.07);
            --sb-red:#dc2626; --sb-amber:#d97706; --sb-green:#16a34a;
            --sb-blue:#2563eb; --sb-violet:#7c3aed;
          }

          .sb-card{border:1px solid var(--sb-line);border-radius:14px;
                   padding:1rem 1.15rem;margin-bottom:.75rem;background:var(--sb-soft);}
          .sb-card-tight{padding:.7rem .9rem;}
          .sb-pill{display:inline-block;padding:.15rem .6rem;border-radius:999px;
                   font-size:.72rem;font-weight:800;color:#fff;letter-spacing:.02em;}
          .sb-kpi{font-size:1.85rem;font-weight:800;line-height:1.05;margin:.15rem 0;
                  letter-spacing:-.02em;}
          .sb-kpi-label{font-size:.7rem;opacity:.65;text-transform:uppercase;
                        letter-spacing:.07em;font-weight:800;}
          .sb-sub{font-size:.78rem;opacity:.62;line-height:1.35;}

          /* Hero banner */
          .sb-hero{border-radius:18px;padding:1.4rem 1.6rem;margin-bottom:1rem;
                   background:linear-gradient(120deg,rgba(37,99,235,.16),
                              rgba(124,58,237,.10) 55%,rgba(22,163,74,.10));
                   border:1px solid var(--sb-line);}
          .sb-hero h2{margin:0 0 .2rem;font-size:1.55rem;letter-spacing:-.02em;}
          .sb-hero p{margin:0;font-size:.9rem;opacity:.72;}

          /* Score ring */
          .sb-ring{width:104px;height:104px;border-radius:50%;display:grid;
                   place-items:center;margin:0 auto;}
          .sb-ring-inner{width:82px;height:82px;border-radius:50%;display:grid;
                         place-items:center;background:var(--background-color,#0e1117);}
          .sb-ring-score{font-size:1.6rem;font-weight:900;line-height:1;}
          .sb-ring-max{font-size:.62rem;opacity:.6;font-weight:700;}

          /* Recommendation card */
          .sb-rec{border:1px solid var(--sb-line);border-left-width:4px;
                  border-radius:12px;padding:.8rem 1rem;margin-bottom:.6rem;
                  background:var(--sb-soft);}
          .sb-rec-title{font-weight:800;font-size:.95rem;margin-bottom:.2rem;
                        display:flex;gap:.45rem;align-items:center;}
          .sb-rec-body{font-size:.85rem;opacity:.78;line-height:1.45;}

          /* Action item */
          .sb-action{display:flex;gap:.7rem;align-items:flex-start;padding:.65rem .8rem;
                     border-radius:10px;margin-bottom:.45rem;border:1px solid var(--sb-line);}
          .sb-dot{flex:0 0 auto;width:.55rem;height:.55rem;border-radius:50%;
                  margin-top:.42rem;}

          /* Security check row */
          .sb-check{display:flex;gap:.6rem;align-items:flex-start;padding:.42rem 0;
                    border-bottom:1px solid var(--sb-line);font-size:.86rem;}
          .sb-check:last-child{border-bottom:0;}

          /* Legacy blocks */
          .sb-rule{border-left:3px solid #94a3b8;padding:.35rem .7rem;margin:.3rem 0;
                   font-size:.88rem;background:rgba(148,163,184,.08);border-radius:0 8px 8px 0;}
          .sb-cite{border-left:3px solid var(--sb-blue);padding:.4rem .7rem;margin:.35rem 0;
                   font-size:.85rem;background:rgba(37,99,235,.07);border-radius:0 8px 8px 0;}
          .sb-danger{border-left:3px solid var(--sb-red);padding:.55rem .85rem;
                     background:rgba(220,38,38,.09);border-radius:0 8px 8px 0;}
          .sb-ok{border-left:3px solid var(--sb-green);padding:.55rem .85rem;
                 background:rgba(22,163,74,.09);border-radius:0 8px 8px 0;}

          /* Tighten Streamlit chrome a little */
          div[data-testid="stMetricValue"]{font-size:1.5rem;}
          section[data-testid="stSidebar"] .sb-sub{font-size:.75rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# Dashboard building blocks
# --------------------------------------------------------------------------- #

def _grade_colour(score: int) -> str:
    if score >= 88:
        return "#16a34a"
    if score >= 72:
        return "#65a30d"
    if score >= 55:
        return "#d97706"
    return "#dc2626"


def score_ring(score: int, label: str, grade: str = "") -> None:
    """A conic-gradient score ring. Cheaper and sharper than a plotly gauge."""
    colour = _grade_colour(score)
    st.markdown(
        f'<div class="sb-ring" style="background:conic-gradient({colour} '
        f'{score * 3.6}deg, rgba(128,128,128,.18) 0deg);">'
        f'<div class="sb-ring-inner">'
        f'<div style="text-align:center;">'
        f'<div class="sb-ring-score" style="color:{colour};">{score}</div>'
        f'<div class="sb-ring-max">/ 100</div></div></div></div>'
        f'<div style="text-align:center;margin-top:.5rem;">'
        f'<div style="font-weight:800;font-size:.9rem;">{label}</div>'
        f'<div class="sb-sub">{grade}</div></div>',
        unsafe_allow_html=True,
    )


def hero(title: str, subtitle: str) -> None:
    st.markdown(
        f'<div class="sb-hero"><h2>{title}</h2><p>{subtitle}</p></div>',
        unsafe_allow_html=True,
    )


def stat(label: str, value: Any, sub: str = "", colour: str | None = None,
         icon: str = "") -> None:
    style = f"color:{colour};" if colour else ""
    prefix = f"{icon} " if icon else ""
    st.markdown(
        f'<div class="sb-card sb-card-tight"><div class="sb-kpi-label">{prefix}{label}</div>'
        f'<div class="sb-kpi" style="{style}">{value}</div>'
        f'<div class="sb-sub">{sub}</div></div>',
        unsafe_allow_html=True,
    )


def recommendation_card(rec: Any) -> None:
    st.markdown(
        f'<div class="sb-rec" style="border-left-color:{rec.colour};">'
        f'<div class="sb-rec-title">{rec.icon} {rec.title}</div>'
        f'<div class="sb-rec-body">{rec.body}</div></div>',
        unsafe_allow_html=True,
    )


def action_item(item: Any) -> None:
    colour = {"urgent": "#dc2626", "soon": "#d97706", "info": "#2563eb"}.get(
        item.priority, "#64748b")
    st.markdown(
        f'<div class="sb-action" style="border-left:4px solid {colour};">'
        f'<span class="sb-dot" style="background:{colour};"></span>'
        f'<div><div style="font-weight:700;font-size:.88rem;">{item.title}</div>'
        f'<div class="sb-sub">{item.detail}</div></div></div>',
        unsafe_allow_html=True,
    )


def security_check(label: str, passed: bool, detail: str) -> None:
    mark = "✅" if passed else "⚠️"
    colour = "" if passed else "color:#d97706;"
    st.markdown(
        f'<div class="sb-check"><span>{mark}</span>'
        f'<div><span style="font-weight:650;{colour}">{label}</span><br>'
        f'<span class="sb-sub">{detail}</span></div></div>',
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


def decision_card(
    txn: Transaction,
    decision: Decision | dict,
    *,
    show_internals: bool = True,
    use_expander: bool = True,
) -> None:
    """The single most important view in the product: why we decided what we decided.

    `use_expander=False` renders the internals inline instead of in an expander. Required
    when this card is itself drawn inside an expander -- Streamlit raises
    StreamlitAPIException on nested expanders, which silently kills every widget after
    it in that container. That is exactly what happened in the alert queue: the analyst
    approve/reject buttons never rendered.
    """
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
        container = (st.expander("Guardrails, telemetry & audit") if use_expander
                     else st.container())
        with container:
            if not use_expander:
                st.markdown("**Guardrails, telemetry & audit**")
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
