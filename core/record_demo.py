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


def _preflight() -> list[str]:
    """Put the hero account into the state the scenarios assume.

    This is not a convenience. Recording against a frozen hero card poisons the cache
    silently and completely: `CARD_ALREADY_FROZEN` is +60, so the *legitimate* beats score
    ~100, record as FREEZE_AND_ESCALATE, and the offline demo then shows an everyday
    grocery run being blocked as fraud. The recorder has to guarantee its own preconditions
    rather than trust whoever ran it last -- and whoever ran it last is usually a fraud
    injection two minutes earlier.
    """
    from . import db, travel

    demo = json.loads(config.DEMO_INJECTIONS_PATH.read_text(encoding="utf-8"))
    hero = demo["hero_customer_id"]
    fixed: list[str] = []

    customer = db.get_customer(hero)
    if customer and customer.card_frozen:
        db.set_card_frozen(hero, False)
        fixed.append("unfroze the hero card")

    notice = demo.get("travel_notice")
    if notice and not travel.active_notices(hero):
        travel.create_notice(notice["customer_id"], notice["countries"],
                             notice["start_date"], notice["end_date"], created_via="form")
        fixed.append(f"filed the {', '.join(notice['countries'])} travel notice")

    # The staff beats ask about the queue. Recording them against an EMPTY queue captures
    # "there is nothing pending" and replays that on stage — the same silent-poisoning
    # shape as the frozen-card case above, one level up. If the queue is empty, put a
    # real alert in it by scoring a real fraud scenario through the real pipeline.
    if not db.list_alerts(status="PENDING", limit=1):
        from . import pipeline
        scenario = next((s for s in _scenarios() if s["key"].startswith("fraud")), None)
        if scenario is not None:
            pipeline.score_transaction(_build(scenario), persist=True)
            fixed.append("injected one pending alert (the queue was empty)")
            # That injection may have frozen the hero card. Undo it, or every legitimate
            # beat recorded afterwards scores ~100 — which is the bug this function exists
            # to prevent, reintroduced by this function.
            after = db.get_customer(hero)
            if after is not None and after.card_frozen:
                db.set_card_frozen(hero, False)
                fixed.append("re-unfroze the hero card after the injection")

    return fixed


def record() -> int:
    """Run every demo scenario live so its responses land in the cache."""
    from . import pipeline

    if config.DEMO_MODE != "live":
        print(f"DEMO_MODE is '{config.DEMO_MODE}'. Recording needs live calls.")
        print("Re-run without DEMO_MODE set, or with DEMO_MODE=live.")
        return 1

    for change in _preflight():
        print(f"  pre-flight: {change}")

    scenarios = _scenarios()
    print(f"\nRecording {len(scenarios)} demo scenarios (live calls)...\n")

    # `expect` in the JSON is prose for the presenter, so we sanity-check the one thing
    # that actually goes wrong: a legitimate beat recording as blocked. That is the shape
    # every state-leak failure takes, and it is invisible until the network is down.
    suspicious = []
    for scenario in scenarios:
        txn = _build(scenario)
        decision = pipeline.score_transaction(txn, persist=False)
        marker = "model" if decision.llm_used else "rules only"

        legit = scenario["key"].startswith("legit")
        off = legit and decision.action in ("FREEZE_AND_ESCALATE", "QUARANTINE")
        print(f"  {'!!' if off else 'OK'} {scenario['key']:<20} {decision.action:<20} "
              f"score={decision.risk_score:>3}  ({marker})"
              + ("   <- a LEGITIMATE beat recorded as blocked" if off else ""))
        if off:
            suspicious.append(scenario["key"])

    if suspicious:
        # Recording the wrong answer is worse than not recording at all, because it only
        # fails when the network is down and nobody can debug it.
        print(f"\n  WARNING: {len(suspicious)} legitimate scenario(s) recorded as blocked: "
              f"{', '.join(suspicious)}")
        print("  Something on the hero account is still dirty. Fix it and re-record — the "
              "cache now holds responses that will show an everyday purchase as fraud.")

    # Also record the agent exchanges used in the chat beats, for every persona.
    #
    # The quick-action prompts are READ FROM `personas.py`, not copied here. They used to
    # be hand-copied from the widget template into this list, which is the arrangement
    # CLAUDE.md warns about in as many words -- a button whose prompt was never recorded
    # dies offline, and it will be the button you press on stage. Sourcing both the
    # template and the recorder from one declaration makes that class of bug unreachable.
    hero = json.loads(config.DEMO_INJECTIONS_PATH.read_text(encoding="utf-8"))["hero_customer_id"]
    print()
    for role, extras in (("customer", CUSTOMER_EXTRA_BEATS),
                         ("admin", STAFF_EXTRA_BEATS)):
        _record_chat(role, hero, extras)

    llm._load_cache.cache_clear()
    print(f"\nCache now holds {llm.cache_size()} responses "
          f"({config.CACHED_RESPONSES_PATH.name})")
    print("\nRecording ran real agent turns, so it may have left state behind -- a travel\n"
          "notice, a notification, a generated statement. Approval-gated tools only ever\n"
          "proposed, so nothing was frozen or disputed. Run the pre-flight check on the\n"
          "Demo control page before rehearsing.")
    return 0


# Beats beyond the quick actions: the questions a judge is most likely to type.
CUSTOMER_EXTRA_BEATS = [
    "Any suspicious activity on my account?",
    "Show me my last 5 transactions",
    "Why was my card frozen?",
    "Plan my travel budget for Spain for 8 days",
    "I lost my card",
    "Alert me if I spend more than 5000 this month",
    "Send 2000 to Priya",
    "I want to top up my phone",
    "How do I pay my tax?",
    "Change my email address",
]

# Staff beats, recorded as ADMIN because admin's tool scope is a strict superset of the
# analyst's -- so one recording replays correctly for both, and they share one cache
# namespace (`chat-staff-`, see personas.Persona.cache_prefix).
#
# EVERY BEAT MUST BE ID-FREE. The cache key is the normalised question, so a beat phrased
# "Why did ALERT-4f21a0 score 92?" is keyed to an ID that the next reseed destroys and is
# guaranteed to miss on any other machine. Questions that name an ID are answered live, or
# offline by the primary_tool fallback, which is what that fallback is for.
STAFF_EXTRA_BEATS = [
    "Which alert is riskiest right now?",
    "How many alerts are pending?",
    "Have we seen this pattern before?",
    "Show me the pending queue for EMEA",
    "What is the false positive rate looking like?",
]


def _record_chat(role: str, hero: str, extra: list[str]) -> None:
    """Record one persona's beats: its quick actions, verbatim, plus the extras."""
    from .agents import customer_agent, personas
    from .agents.context import AgentContext

    persona = personas.for_role(role)
    ctx = AgentContext(customer_id=hero, role=role, username=role)
    beats = [q for _label, q in persona.quick_actions] + extra

    print(f"  [{role}] {len(beats)} chat beats")
    for message in beats:
        try:
            reply = customer_agent.respond(message, ctx)
            print(f"    {message[:46]:<48} -> {len(reply.text)} chars")
        except Exception as exc:
            print(f"    {message[:46]:<48} -> FAILED: {exc}")


def _verify_chat() -> int:
    """Replay every recorded chat beat with DEMO_MODE=cached and count the misses.

    This did not exist. The scenarios were verified and the twelve chat beats were not,
    so the chat cache had never been proven to replay -- which is the single thing it is
    for. A miss on the first step is unambiguous: the recorded decision is not reachable
    under the key the lookup computes.

    `_finalise` legitimately misses -- it passes no cache key by design and falls through
    to `_readable_fallback` -- and that leaves no note, so it does not register here.

    ANY `llm_unavailable` note is a failure. An earlier version only counted it when no
    tool had run, which quietly passed a beat that had degraded to `primary_tool`: the
    reply looks fine because it is real account data, but the recorded decision never
    replayed. For a beat that is on a BUTTON, degrading is failing -- the button exists to
    demonstrate the recorded path.
    """
    from .agents import customer_agent, personas
    from .agents.context import AgentContext

    hero = json.loads(config.DEMO_INJECTIONS_PATH.read_text(encoding="utf-8"))["hero_customer_id"]
    failures = 0
    for role, extra in (("customer", CUSTOMER_EXTRA_BEATS), ("admin", STAFF_EXTRA_BEATS)):
        persona = personas.for_role(role)
        ctx = AgentContext(customer_id=hero, role=role, username=role)
        beats = [q for _label, q in persona.quick_actions] + extra
        print(f"\n  [{role}] {len(beats)} chat beats")
        for message in beats:
            try:
                reply = customer_agent.respond(message, ctx)
            except Exception as exc:
                failures += 1
                print(f"    !! {message[:44]:<46} FAILED: {exc}")
                continue
            missed = any("llm_unavailable" in n for n in reply.guardrail_notes)
            if missed:
                failures += 1
                degraded = " (degraded to primary_tool)" if reply.tool_calls else ""
                print(f"    !! {message[:44]:<46} CACHE MISS{degraded}")
            else:
                print(f"    OK {message[:44]:<46} {len(reply.text)} chars")
    return failures


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

    # The chat beats are the other half of the demo and were never verified until now.
    print("\nVerifying chat beats...")
    failures += _verify_chat()

    print()
    if failures:
        print(f"{failures} beat(s) not fully cached. Run without --verify to record them.")
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
