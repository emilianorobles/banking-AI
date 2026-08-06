"""Record every model response the demo needs, then prove the demo works without a network.

This is the outage insurance. Run it Thursday evening, commit the resulting cache, and
the Friday demo survives the lab endpoint going down, the venue wifi failing, or the key
being rate-limited.

    python -m core.record_demo            # record, then verify
    python -m core.record_demo --verify   # verify only (no network calls)

Verification genuinely re-runs every scenario with DEMO_MODE=cached. If a scenario is
missing from the cache it FAILS -- a fallback you have not tested is not a fallback.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from . import config, db, llm
from .contracts import Transaction, new_id


def _scenarios() -> list[dict]:
    if not config.DEMO_INJECTIONS_PATH.exists():
        raise SystemExit("Missing demo_injections.json. Run: python -m data.generate")
    return json.loads(config.DEMO_INJECTIONS_PATH.read_text(encoding="utf-8"))["scenarios"]


def _build(scenario: dict) -> Transaction:
    payload = dict(scenario["txn"])
    payload["txn_id"] = new_id("TXN")
    payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    return Transaction(**payload)


def record() -> int:
    """Run every demo scenario live so its responses land in the cache."""
    from . import pipeline

    if config.DEMO_MODE != "live":
        print(f"DEMO_MODE is '{config.DEMO_MODE}'. Recording needs live calls.")
        print("Re-run without DEMO_MODE set, or with DEMO_MODE=live.")
        return 1

    scenarios = _scenarios()
    print(f"Recording {len(scenarios)} demo scenarios (live calls)...\n")

    for scenario in scenarios:
        txn = _build(scenario)
        decision = pipeline.score_transaction(txn, persist=False)
        marker = "model" if decision.llm_used else "rules only"
        print(f"  {scenario['key']:<20} {decision.action:<20} "
              f"score={decision.risk_score:>3}  ({marker})")

    # Also record the customer-agent exchanges used in the chat beats.
    from .agents import customer_agent
    hero = json.loads(config.DEMO_INJECTIONS_PATH.read_text(encoding="utf-8"))["hero_customer_id"]
    chat_beats = [
        "Any suspicious activity on my account?",
        "Show me my last 5 transactions",
        "Why was my card frozen?",
    ]
    print()
    for message in chat_beats:
        try:
            reply = customer_agent.respond(message, hero)
            print(f"  chat: {message[:44]:<46} -> {len(reply.text)} chars")
        except Exception as exc:
            print(f"  chat: {message[:44]:<46} -> FAILED: {exc}")

    llm._load_cache.cache_clear()
    print(f"\nCache now holds {llm.cache_size()} responses "
          f"({config.CACHED_RESPONSES_PATH.name})")
    return 0


def verify() -> int:
    """Re-run every scenario in cached mode. Fails loudly on a cache miss."""
    os.environ["DEMO_MODE"] = "cached"
    config.DEMO_MODE = "cached"
    llm._load_cache.cache_clear()

    # Reimport so the pipeline picks up the mode change.
    from . import pipeline

    scenarios = _scenarios()
    print(f"Verifying {len(scenarios)} scenarios with DEMO_MODE=cached "
          f"({llm.cache_size()} cached responses)...\n")

    failures = 0
    for scenario in scenarios:
        txn = _build(scenario)
        decision = pipeline.score_transaction(txn, persist=False)

        unavailable = any("llm_unavailable" in n for n in decision.guardrail_notes)
        expects_model = not scenario["key"].startswith(("legit_home", "attack_injection"))

        if unavailable and expects_model:
            failures += 1
            print(f"  !! {scenario['key']:<20} CACHE MISS — this scenario would degrade "
                  f"to rules only during an outage")
        else:
            print(f"  OK {scenario['key']:<20} {decision.action:<20} "
                  f"score={decision.risk_score:>3}  "
                  f"cites={decision.cited_case_ids or '-'}")

    print()
    if failures:
        print(f"{failures} scenario(s) not fully cached. Run without --verify to record them.")
        print("Do NOT rely on the offline fallback until this passes.")
        return 1

    print("PASS — the full demo runs with no network access.")
    print("Final check before Friday: turn wifi OFF and run this again.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Record and verify the offline demo cache.")
    parser.add_argument("--verify", action="store_true",
                        help="verify only; makes no live calls")
    args = parser.parse_args()

    db.init_db()
    if args.verify:
        return verify()

    code = record()
    if code:
        return code
    print("\n" + "=" * 60)
    return verify()


if __name__ == "__main__":
    sys.exit(main())
