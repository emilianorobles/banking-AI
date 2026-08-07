"""The evaluation harness -- what separates a measured system from a demo.

Runs the REAL pipeline (rules, RAG, agent, guardrails) over a held-out labelled set and
reports accuracy, false positives, groundedness, latency and cost.

The metrics that matter for this domain, in order:

  RECALL          -- fraud we caught. Missing fraud costs money directly.
  FALSE POSITIVE  -- legitimate customers we wrongly flagged. This is the metric most
                     fraud demos quietly omit, and the one that actually drives customer
                     attrition. Our eval set is deliberately weighted with hard
                     legitimate cases (declared travel, recurring large payments,
                     card-present high-value) to make it hard to look good here.
  GROUNDEDNESS    -- proportion of cited case IDs that actually exist. Anything below
                     100% means the model fabricated evidence, which in a regulated
                     context is worse than being wrong.

Run:  python -m core.evaluation
      python -m core.evaluation --limit 10
"""

from __future__ import annotations

import argparse
import json
import statistics
from typing import Any

from . import config, db, pipeline, security
from .contracts import Transaction


def load_eval_set() -> list[dict[str, Any]]:
    if not config.EVAL_SET_PATH.exists():
        raise SystemExit("Missing eval_set.jsonl. Run: python -m data.generate")
    rows = []
    with config.EVAL_SET_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# Two different harms, deliberately measured separately.
#
# BLOCKING a legitimate customer is a churn event: their card stops working in a shop,
# they call support, and some of them leave. CHALLENGING them is a push notification.
# Reporting a single "false positive rate" that conflates the two hides the distinction
# a bank actually cares about -- and a graded response is the entire reason the CHALLENGE
# tier exists. So we report both, and never quote one without the other.
BLOCKING_ACTIONS = {"FREEZE_AND_ESCALATE", "QUARANTINE"}
FRICTION_ACTIONS = {"CHALLENGE"} | BLOCKING_ACTIONS

# Detection: any response above ALLOW counts as having caught the fraud.
FLAGGED_ACTIONS = FRICTION_ACTIONS


def run_case(row: dict[str, Any], *, allow_llm: bool = True) -> dict[str, Any]:
    """Score one labelled case through the full pipeline. Never persists.

    `allow_llm=False` runs the deterministic rules alone. Running both and diffing them
    is how we show what the AI layer is actually worth -- see `compare()`.
    """
    payload = {k: v for k, v in row.items() if k != "eval_kind"}
    label = bool(payload.pop("is_fraud_label", False))
    txn = Transaction(**payload, is_fraud_label=label)

    decision = pipeline.score_transaction(txn, persist=False, allow_llm=allow_llm)

    flagged = decision.action in FLAGGED_ACTIONS
    known = db.known_case_ids()
    _, fabricated = security.validate_citations(decision.cited_case_ids, known)

    return {
        "txn_id": txn.txn_id,
        "eval_kind": row.get("eval_kind", "unknown"),
        "label": label,
        "flagged": flagged,
        "blocked": decision.action in BLOCKING_ACTIONS,
        "correct": flagged == label,
        "risk_score": decision.risk_score,
        "action": decision.action,
        "llm_used": decision.llm_used,
        "cited": decision.cited_case_ids,
        "fabricated": fabricated,
        "grounded": not fabricated,
        "latency_ms": decision.latency_ms,
        "cost_usd": decision.est_cost_usd,
        "confidence": decision.confidence,
    }


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(1 for r in results if r["label"] and r["flagged"])
    fn = sum(1 for r in results if r["label"] and not r["flagged"])
    fp = sum(1 for r in results if not r["label"] and r["flagged"])
    tn = sum(1 for r in results if not r["label"] and not r["flagged"])

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0

    # Groundedness is measured only over cases that actually cited something.
    cited_cases = [r for r in results if r["cited"]]
    grounded = sum(1 for r in cited_cases if r["grounded"])
    groundedness = (grounded / len(cited_cases)) if cited_cases else 1.0
    fabricated_total = sum(len(r["fabricated"]) for r in results)

    latencies = [r["latency_ms"] for r in results] or [0]

    # Harm to legitimate customers, split by severity.
    legit = [r for r in results if not r["label"]]
    blocked_legit = sum(1 for r in legit if r["blocked"])
    friction_legit = sum(1 for r in legit if r["flagged"])
    avg_score_legit = (statistics.mean([r["risk_score"] for r in legit])
                       if legit else 0.0)
    avg_score_fraud = (statistics.mean(
        [r["risk_score"] for r in results if r["label"]]) if tp + fn else 0.0)

    return {
        "n": len(results),
        "tp": tp, "fn": fn, "fp": fp, "tn": tn,
        "legit_n": len(legit),
        "blocked_legit": blocked_legit,
        "friction_legit": friction_legit,
        "block_rate_legit": blocked_legit / len(legit) if legit else 0.0,
        "friction_rate_legit": friction_legit / len(legit) if legit else 0.0,
        "avg_score_legit": avg_score_legit,
        "avg_score_fraud": avg_score_fraud,
        "separation": avg_score_fraud - avg_score_legit,
        "accuracy": (tp + tn) / len(results) if results else 0.0,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "fpr": fpr,
        "groundedness": groundedness,
        "fabricated": fabricated_total,
        "cases_with_citations": len(cited_cases),
        "llm_used": sum(1 for r in results if r["llm_used"]),
        "avg_latency_ms": int(statistics.mean(latencies)),
        "p95_latency_ms": int(sorted(latencies)[int(len(latencies) * 0.95) - 1]) if latencies else 0,
        "total_cost_usd": sum(r["cost_usd"] for r in results),
    }


def compare(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Run the same cases twice -- rules only, then the full agentic pipeline.

    This is the "how does AI improve on a conventional approach" answer, measured rather
    than asserted. A pure rules engine is the honest conventional baseline: it catches
    fraud well but over-flags legitimate customers, because a threshold cannot tell a
    genuine annual insurance premium from an anomalous charge. The retrieval layer can,
    because a human analyst already wrote down that distinction in a past case.

    Takes the raw eval-set rows, NOT the output of `run_case` -- it scores them itself.
    A caller that has already scored both arms should use `compare_results` instead of
    paying for every case a second time.
    """
    return compare_results(
        [run_case(r, allow_llm=False) for r in rows],
        [run_case(r, allow_llm=True) for r in rows],
    )


def compare_results(baseline: list[dict[str, Any]],
                    full: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble the A/B from two sets of already-scored results.

    Split out from `compare` so a UI that scores the arms itself -- reporting progress as
    it goes, because thirty-eight model calls is a long silence -- can reuse the metrics
    without re-running anything.
    """
    bm, fm = summarise(baseline), summarise(full)
    return {
        "baseline": bm,
        "full": fm,
        "baseline_results": baseline,
        "full_results": full,
        "delta": {
            "recall": fm["recall"] - bm["recall"],
            "precision": fm["precision"] - bm["precision"],
            "fpr": fm["fpr"] - bm["fpr"],
            "f1": fm["f1"] - bm["f1"],
            "false_positives_removed": bm["fp"] - fm["fp"],
            "blocks_removed": bm["blocked_legit"] - fm["blocked_legit"],
            "fraud_missed_added": fm["fn"] - bm["fn"],
            "avg_score_legit": fm["avg_score_legit"] - bm["avg_score_legit"],
            "separation": fm["separation"] - bm["separation"],
        },
    }


def format_comparison(c: dict[str, Any]) -> str:
    b, f, d = c["baseline"], c["full"], c["delta"]
    return f"""
RULES ONLY  vs  RULES + RAG + AGENT      ({b['n']} cases)
{'=' * 62}
                             conventional   agentic     delta
  Recall (fraud caught)          {b['recall']:6.1%}    {f['recall']:6.1%}   {d['recall']:+6.1%}
  Fraud cases newly missed                              {d['fraud_missed_added']:+6d}

  HARM TO LEGITIMATE CUSTOMERS  (n={b['legit_n']})
  Blocked outright               {b['blocked_legit']:6d}    {f['blocked_legit']:6d}   {d['blocks_removed']:+6d}
  Given step-up challenge        {b['friction_legit']:6d}    {f['friction_legit']:6d}
  Avg risk score assigned        {b['avg_score_legit']:6.1f}    {f['avg_score_legit']:6.1f}   {d['avg_score_legit']:+6.1f}

  SEPARATION  (fraud score minus legitimate score -- higher is better)
  Separation                     {b['separation']:6.1f}    {f['separation']:6.1f}   {d['separation']:+6.1f}

  Groundedness                   {b['groundedness']:6.1%}    {f['groundedness']:6.1%}
  Cost of the agentic run                            ${f['total_cost_usd']:.4f}

  Note: a blocked legitimate customer is a churn event; a challenged one gets a
  push notification. Reporting them separately is deliberate.
"""


def format_report(m: dict[str, Any]) -> str:
    return f"""
EVALUATION REPORT  ({m['n']} cases, model used on {m['llm_used']})
{'=' * 58}
  Recall (fraud caught)      {m['recall']:6.1%}   target >= 85%
  Precision                  {m['precision']:6.1%}
  False positive rate        {m['fpr']:6.1%}   target <= 10%
  F1                         {m['f1']:6.2f}
  Accuracy                   {m['accuracy']:6.1%}

  Groundedness               {m['groundedness']:6.1%}   target 100%
  Fabricated citations       {m['fabricated']:6d}   ({m['cases_with_citations']} cases cited evidence)

  Avg latency                {m['avg_latency_ms']:6d} ms
  p95 latency                {m['p95_latency_ms']:6d} ms
  Cost for this run          ${m['total_cost_usd']:.4f}

  Confusion matrix        flagged   allowed
    actually fraud        {m['tp']:>7}   {m['fn']:>7}
    actually legitimate   {m['fp']:>7}   {m['tn']:>7}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the BedRock Financial evaluation harness.")
    parser.add_argument("--limit", type=int, default=0, help="only run the first N cases")
    parser.add_argument("--compare", action="store_true",
                        help="run rules-only vs full pipeline and report the delta")
    args = parser.parse_args()

    rows = load_eval_set()
    if args.limit:
        rows = rows[:args.limit]

    if args.compare:
        print(f"Comparing {len(rows)} cases: rules only vs full agentic pipeline...\n")
        result = compare(rows)
        print(format_comparison(result))
        return 0

    print(f"Running {len(rows)} cases through the full pipeline "
          f"(DEMO_MODE={config.DEMO_MODE})...\n")

    results = []
    for i, row in enumerate(rows, 1):
        result = run_case(row)
        results.append(result)
        mark = "OK" if result["correct"] else "!!"
        print(f"  {mark} {i:>2}/{len(rows)}  {result['eval_kind']:<32} "
              f"score={result['risk_score']:>3} {result['action']:<20} "
              f"{result['latency_ms']:>5}ms")

    m = summarise(results)
    print(format_report(m))

    ok = m["recall"] >= 0.85 and m["fpr"] <= 0.10 and m["groundedness"] >= 0.999
    print("RESULT: " + ("PASS" if ok else "BELOW TARGET"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
