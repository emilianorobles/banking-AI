"""Load generated data into SQLite and build the FAISS index.

Idempotent and safe to re-run. `--reset` wipes everything first, which is what the
Reset Demo button calls -- rehearsing the demo repeatedly needs a clean, known state.

Run:  python -m core.seed
      python -m core.seed --reset
      python -m core.seed --no-index      (skip embeddings; useful with no API key)
"""

from __future__ import annotations

import argparse
import json
import sys

from . import config, db
from .contracts import Customer, Transaction  # noqa: F401


def _load_json(path):
    if not path.exists():
        raise SystemExit(
            f"Missing {path.name}. Run this first:\n    python -m data.generate"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def score_history(txns: list[Transaction]) -> int:
    """Score seeded transactions with rules only, in bulk.

    Deliberately rules-only: this is historical backfill, it must work with no API key,
    and sending 2,000 transactions to a model to populate a dashboard would contradict
    the entire cost argument the dashboard is there to make.
    """
    from . import pipeline
    from .contracts import Alert, new_id

    decisions, alerts = [], []
    for txn in txns:
        try:
            decision = pipeline.score_transaction(txn, persist=False, allow_llm=False)
        except Exception:
            continue
        decisions.append(decision)
        if decision.action in ("CHALLENGE", "FREEZE_AND_ESCALATE", "QUARANTINE"):
            alerts.append(Alert(
                alert_id=new_id("ALERT"),
                txn_id=txn.txn_id,
                customer_id=txn.customer_id,
                risk_score=decision.risk_score,
                risk_level=decision.risk_level,
                action=decision.action,
                status="PENDING",
                summary=(f"{txn.amount:,.2f} {txn.currency} at {txn.merchant_category} "
                         f"in {txn.city}, {txn.country} — {decision.risk_level} "
                         f"({decision.risk_score})"),
                region=txn.region,
                created_at=txn.timestamp,
            ))

    db.save_decisions_bulk(decisions)
    db.save_alerts_bulk(alerts)
    print(f"  scored           {len(decisions):>5}  ({len(alerts)} alerts raised, rules only)")
    return len(decisions)


def seed(reset: bool = False, build_index: bool = True) -> dict:
    if reset:
        print("Resetting database...")
        db.reset_db()
        # Delete the persisted index too. Resetting SQLite alone leaves learned cases
        # retrievable from FAISS but absent from the mirror the groundedness guardrail
        # checks against, so they get reported as fabricated citations.
        try:
            from . import rag
            rag.reset_index(delete_disk=True)
        except Exception:
            pass
        # Same reasoning for the outbox: the notification rows go with the database, and
        # .eml files that outlive them are messages from a rehearsal that no longer exists.
        try:
            from . import notifications
            removed = notifications.clear_outbox()
            if removed:
                print(f"  outbox cleared   {removed:>5}  recorded emails")
        except Exception:
            pass
        if not build_index:
            print("  ! index deleted and NOT rebuilt (--no-index).")
            print("    Retrieval is unavailable until: python -m core.seed --index-only")
    else:
        db.init_db()

    customers = [Customer(**c) for c in _load_json(config.CUSTOMERS_PATH)]
    db.upsert_customers(customers)
    print(f"  customers        {len(customers):>5}")

    txns = [Transaction(**t) for t in _load_json(config.TRANSACTIONS_PATH)]
    db.insert_transactions(txns)
    print(f"  transactions     {len(txns):>5}")

    # Travel notices required by the hard eval cases. Without these the
    # medical-abroad case is testing geography rules rather than the thing we meant
    # to test, and the evaluation quietly measures the wrong thing.
    notices = 0
    if config.HARD_NOTICES_PATH.exists():
        from . import travel
        for n in json.loads(config.HARD_NOTICES_PATH.read_text(encoding="utf-8")):
            try:
                travel.create_notice(n["customer_id"], n["countries"],
                                     n["start_date"], n["end_date"], created_via="form")
                notices += 1
            except Exception:
                pass
        print(f"  travel notices   {notices:>5}  (required by hard eval cases)")

    summary = {"customers": len(customers), "transactions": len(txns),
               "cases": 0, "travel_notices": notices}

    # Score the seeded history with rules only.
    #
    # Without this the decisions table is empty, so the admin dashboard shows nothing,
    # the alert queue is bare, and the cost meter reports "0% resolved without a model"
    # -- the exact opposite of the claim it exists to support. Rules-only scoring is
    # ~9 ms per transaction and makes no API calls, so this stays fast and offline.
    scored = score_history(txns)
    summary["scored"] = scored

    if build_index:
        try:
            from . import rag
            print("  building FAISS index (this calls the embedding API)...")
            store = rag.build_index()
            summary["cases"] = store.index.ntotal
            print(f"  fraud cases      {summary['cases']:>5}  (FAISS index built)")
        except Exception as exc:
            print(f"  ! index build failed: {exc}")
            print("    The app still runs -- rules work without embeddings.")
            print("    Fix your API key and re-run: python -m core.seed --index-only")
    else:
        # Still mirror the cases into SQLite so citation validation and the UI work.
        from . import rag
        cases = rag.load_precedents_from_disk()
        db.save_fraud_cases(cases)
        summary["cases"] = len(cases)
        print(f"  fraud cases      {len(cases):>5}  (SQLite only, no embeddings)")

    db.audit(actor="system", event_type="SEED", subject_id="database",
             detail=f"Seeded {len(customers)} customers, {len(txns)} transactions",
             **summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed SentinelBank.")
    parser.add_argument("--reset", action="store_true", help="wipe all tables first")
    parser.add_argument("--no-index", action="store_true",
                        help="skip embeddings (no API key needed)")
    parser.add_argument("--index-only", action="store_true",
                        help="only rebuild the FAISS index")
    args = parser.parse_args()

    if args.index_only:
        from . import rag
        store = rag.build_index()
        print(f"FAISS index rebuilt: {store.index.ntotal} cases")
        return

    print(f"Seeding {config.DB_PATH.name}...")
    seed(reset=args.reset, build_index=not args.no_index)
    print("\nDone. Next: streamlit run app.py")


if __name__ == "__main__":
    sys.exit(main())
