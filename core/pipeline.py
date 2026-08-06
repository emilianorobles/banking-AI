"""The scoring pipeline -- explicit orchestration, one function, fully auditable.

This is deliberately a readable sequence rather than a framework graph. In a regulated
domain you have to be able to point at the line that made the decision, and every step
writes to the audit log as it goes.

    tokenize PII -> rules -> travel suppression -> injection check
                 -> cheap path? -> RAG -> analyst agent -> guardrails -> route

The cheap path is the commercial argument: transactions the rules are confident about
never reach the model. Roughly 94% of volume exits at step 4.

Run a self-test:  python -m core.pipeline --selftest
"""

from __future__ import annotations

import time
from typing import Any, Callable

from . import config, db, llm, rag, rules, security, travel
from .agents import fraud_analyst
from .contracts import (
    Alert,
    Customer,
    Decision,
    RuleHit,
    Transaction,
    new_id,
)


def _customer_context(
    customer: Customer, history: list[Transaction], txn: Transaction | None = None
) -> str:
    """Compact behavioural profile for the analyst prompt. No PII -- tokens only."""
    countries = sorted({t.country for t in history})
    notices = travel.active_notices(customer.customer_id)
    lines = [
        f"Home country: {customer.home_country} ({customer.home_city}), region {customer.region}",
        f"Typical transaction: {customer.baseline_avg_amount:,.2f}; "
        f"historical maximum: {customer.baseline_max_amount:,.2f}",
        f"Countries seen in history ({len(history)} transactions): "
        f"{', '.join(countries) if countries else 'none'}",
        f"Card currently frozen: {'YES' if customer.card_frozen else 'no'}",
    ]

    # Merchant and category familiarity. A large charge at a merchant the customer has
    # used before is a very different proposition from a large charge at a new one --
    # this is the single most useful legitimacy signal the rules cannot express, because
    # rules only ever add risk and never subtract it.
    if txn is not None:
        same_merchant = [t for t in history if t.merchant.lower() == txn.merchant.lower()]
        same_category = [t for t in history if t.merchant_category == txn.merchant_category]
        if same_merchant:
            amounts = [t.amount for t in same_merchant]
            lines.append(
                f"MERCHANT FAMILIARITY: the customer has used '{txn.merchant}' "
                f"{len(same_merchant)} time(s) before "
                f"(amounts {min(amounts):,.2f}-{max(amounts):,.2f}). "
                "This is an established relationship, not a new payee."
            )
        else:
            lines.append(f"MERCHANT FAMILIARITY: no prior transactions with '{txn.merchant}'.")
        if same_category:
            cat_max = max(t.amount for t in same_category)
            lines.append(
                f"CATEGORY HISTORY: {len(same_category)} prior '{txn.merchant_category}' "
                f"transaction(s), largest {cat_max:,.2f}."
            )
        else:
            lines.append(f"CATEGORY HISTORY: no prior '{txn.merchant_category}' activity.")
    if notices:
        lines.append("Active travel notices: " + "; ".join(travel.describe(n) for n in notices))
    else:
        lines.append("Active travel notices: none")
    return "\n".join(lines)


# Asymmetric blend weights. See _blend() for the reasoning.
LLM_WEIGHT_EXCULPATORY = 0.65   # model argues the transaction is SAFER than rules think
LLM_WEIGHT_INCRIMINATING = 0.25  # model argues it is MORE dangerous


def _blend(rule_score: int, llm_score: int) -> int:
    """Combine the deterministic and model scores -- asymmetrically, and deliberately so.

    We measured this. With a symmetric blend the model made outcomes WORSE: it left
    recall unchanged at 100% but pushed the average risk score on legitimate customers
    from 19.1 to 24.9 and blocked one who would otherwise have been allowed. Shown a list
    of rules that fired plus fraud precedents, a language model piles on. It is agreeable,
    and agreeableness in a fraud system means declining good customers.

    So we split the model's authority by direction, which matches where each layer is
    actually competent:

      * The rules are already excellent at DETECTING risk -- 100% recall on our eval set.
        They need no help finding fraud, so the model's incriminating opinion is
        discounted heavily. It can nudge, not drive.

      * What rules cannot do is EXONERATE. A rule can only ever add points; it has no way
        to express "this is a 4x-baseline charge, but it is the same annual insurance
        premium this customer has paid for four years." That judgement needs retrieved
        precedent and context, and it is exactly what the model is good at. So when the
        model argues a transaction is safer than the rules think, we listen.

    A very high rule score still sets a floor. "Two countries forty minutes apart" is a
    fact of physics, not an opinion, and no amount of model confidence talks past it.
    """
    weight = (LLM_WEIGHT_EXCULPATORY if llm_score < rule_score
              else LLM_WEIGHT_INCRIMINATING)
    blended = round((1 - weight) * rule_score + weight * llm_score)

    if rule_score >= 90:
        blended = max(blended, rule_score)
    return max(0, min(100, int(blended)))


def _summarise(txn: Transaction, decision: Decision) -> str:
    return (
        f"{txn.amount:,.2f} {txn.currency} at {txn.merchant_category} in "
        f"{txn.city}, {txn.country} — {decision.risk_level} ({decision.risk_score})"
    )


def score_transaction(
    txn: Transaction,
    *,
    persist: bool = True,
    allow_llm: bool = True,
    on_step: Callable[[str], None] | None = None,
) -> Decision:
    """Score one transaction end to end. This is the system's single entry point.

    Never raises: any internal failure degrades to the deterministic rule score, which
    is the conservative outcome. A fraud system that crashes is worse than one that
    falls back to rules.

    `on_step` receives a short label as each stage begins. The UI uses it to show the
    pipeline working rather than a spinner -- retrieval and inference take seconds, and
    naming the stage turns dead air into a visible demonstration of the architecture.
    """
    started = time.perf_counter()

    def step(label: str) -> None:
        if on_step is not None:
            try:
                on_step(label)
            except Exception:
                pass

    customer = db.get_customer(txn.customer_id)
    if customer is None:
        decision = Decision(
            txn_id=txn.txn_id, risk_score=50, risk_level="MEDIUM", action="CHALLENGE",
            reasoning="Unknown customer — cannot establish a behavioural baseline.",
            guardrail_notes=["unknown_customer"],
        )
        if persist:
            _persist(txn, decision, customer=None)
        return decision

    history = db.customer_transaction_history(txn.customer_id, txn.timestamp, limit=50)

    # --- 1. PII tokenization vault -------------------------------------------
    step("Tokenizing personal data — nothing raw reaches the model")
    vault = security.PIIVault()
    # Seed the vault with the customer's known identifiers so they are tokenized even
    # when they appear in an unexpected field (e.g. stuffed into a merchant name).
    for kind, value in (("PAN", customer.card_number), ("ACCT", customer.account_number),
                        ("EMAIL", customer.email), ("PHONE", customer.phone)):
        vault.tokenize_value(kind, value)
    masked_txn, pii_found = vault.tokenize_obj(txn.to_dict())

    # --- 2. Deterministic rules + 3. travel suppression -----------------------
    step(f"Running {len(rules.RULES)} deterministic rules")
    suppressed_by_travel, notice_id = travel.is_suppressed(txn)
    suppress_set = travel.SUPPRESSIBLE_RULES if suppressed_by_travel else set()
    rule_score, active_hits, suppressed_hits = rules.evaluate(
        txn, customer, history, suppress=suppress_set
    )

    decision = Decision(
        txn_id=txn.txn_id,
        risk_score=rule_score,
        risk_level=config.risk_level_for(rule_score),
        action=config.action_for(rule_score),
        rule_score=rule_score,
        rule_hits=active_hits,
        suppressed_by_travel=bool(suppressed_hits),
        travel_notice_id=notice_id if suppressed_hits else None,
    )
    if pii_found:
        decision.guardrail_notes.append(
            f"pii_tokenized:{len(pii_found)} ({', '.join(sorted({p.kind for p in pii_found}))})"
        )
    if suppressed_hits:
        decision.guardrail_notes.append(
            f"travel_suppressed:{','.join(h.rule_id for h in suppressed_hits)}"
        )

    # --- 4. Prompt-injection check on untrusted fields ------------------------
    step("Scanning attacker-controlled fields for injection")
    injection = security.detect_injection(txn.merchant, txn.city, txn.merchant_category)
    if injection.detected:
        # An attacker trying to steer the scoring system is itself conclusive. We do not
        # ask the model what it thinks about text engineered to manipulate the model.
        decision.injection_detected = True
        decision.injection_evidence = injection.summary
        decision.risk_score = max(decision.risk_score, 95)
        decision.risk_level = "CRITICAL"
        decision.action = "QUARANTINE"
        decision.reasoning = (
            "Quarantined: the transaction's free-text fields contain instruction-shaped "
            "content targeting the analysis system "
            f"({', '.join(injection.categories)}). The content was never evaluated as an "
            "instruction. An attempt to manipulate fraud scoring is treated as strong "
            "evidence of fraud and routed to a human."
        )
        decision.guardrail_notes.append(f"prompt_injection_blocked:{','.join(injection.categories)}")
        decision.latency_ms = int((time.perf_counter() - started) * 1000)
        if persist:
            _persist(txn, decision, customer)
        return decision

    # --- 5. Cheap path: skip the LLM when the rules are already confident -----
    ambiguous = config.CHEAP_PATH_LOW <= rule_score <= config.CHEAP_PATH_HIGH
    if not allow_llm or not ambiguous or config.DEMO_MODE == "off":
        decision.reasoning = (
            f"Resolved deterministically (rule score {rule_score}). "
            + ("No rules triggered." if not active_hits else rules.explain(active_hits))
            + " No model inference required."
        )
        decision.latency_ms = int((time.perf_counter() - started) * 1000)
        if persist:
            _persist(txn, decision, customer)
        return decision

    # --- 6. RAG retrieval -----------------------------------------------------
    step("Retrieving similar historical cases — both fraud and false positives")
    reasons = [h.reason for h in active_hits]
    precedents = rag.search_for_transaction(txn, reasons)
    decision.retrieved_case_ids = [p.get("case_id", "") for p in precedents if p.get("case_id")]

    # --- 7. Fraud analyst agent ----------------------------------------------
    step("Fraud analyst agent reasoning over the evidence")
    verdict = fraud_analyst.analyse(
        masked_txn=masked_txn,
        rules_text=rules.explain(active_hits),
        precedents_text=rag.format_precedents(precedents),
        customer_context=_customer_context(customer, history, txn),
    )

    if verdict.error:
        decision.guardrail_notes.append(f"llm_unavailable:{verdict.error[:120]}")
        decision.reasoning = (
            f"Model unavailable — decision fell back to deterministic rules "
            f"(score {rule_score}). {rules.explain(active_hits)}"
        )
        decision.latency_ms = int((time.perf_counter() - started) * 1000)
        if persist:
            _persist(txn, decision, customer)
        return decision

    decision.llm_used = True
    decision.confidence = verdict.confidence
    decision.cited_case_ids = verdict.cited_case_ids
    decision.prompt_tokens = verdict.prompt_tokens
    decision.completion_tokens = verdict.completion_tokens
    decision.est_cost_usd = verdict.est_cost_usd
    if verdict.parse_retries:
        decision.guardrail_notes.append(f"json_repair_retries:{verdict.parse_retries}")
    if verdict.reflected:
        decision.guardrail_notes.append("reflection:revised" if verdict.revised else "reflection:held")

    # --- 8. Guardrails --------------------------------------------------------
    step("Validating citations, scanning output for data leakage")
    grounded, fabricated = security.validate_citations(
        verdict.cited_case_ids, db.known_case_ids()
    )
    decision.groundedness_ok = grounded
    if not grounded:
        # The model cited evidence that does not exist. Drop the fabricated IDs and
        # flag it -- this is exactly what the eval harness measures.
        decision.guardrail_notes.append(f"fabricated_citations:{','.join(fabricated)}")
        decision.cited_case_ids = [c for c in verdict.cited_case_ids if c not in fabricated]

    dlp = security.scan_outbound(verdict.reasoning)
    decision.dlp_blocked = dlp.blocked
    reasoning = dlp.safe_text
    if dlp.blocked:
        decision.guardrail_notes.append(f"dlp_egress_redacted:{','.join(dlp.violations)}")

    # --- 9. Route -------------------------------------------------------------
    final_score = _blend(rule_score, verdict.risk_score)
    decision.risk_score = final_score
    decision.risk_level = config.risk_level_for(final_score)
    decision.action = config.action_for(final_score)
    decision.reasoning = reasoning
    if verdict.key_factors:
        decision.guardrail_notes.append("factors:" + " | ".join(verdict.key_factors))
    if verdict.what_would_change_my_mind:
        decision.guardrail_notes.append("counterfactual:" + verdict.what_would_change_my_mind)

    decision.latency_ms = int((time.perf_counter() - started) * 1000)
    if persist:
        _persist(txn, decision, customer)
    return decision


# --------------------------------------------------------------------------- #
# Persistence + alerting
# --------------------------------------------------------------------------- #

def _persist(txn: Transaction, decision: Decision, customer: Customer | None) -> None:
    db.insert_transaction(txn)
    db.save_decision(decision)
    db.bump("transactions_scored")
    if decision.llm_used:
        db.bump("llm_scored")
    else:
        db.bump("llm_avoided")

    db.audit(
        actor="system", event_type="DECISION", subject_id=txn.txn_id,
        detail=f"{decision.action} at risk {decision.risk_score} ({decision.risk_level})",
        rule_score=decision.rule_score,
        llm_used=decision.llm_used,
        cited=decision.cited_case_ids,
        injection=decision.injection_detected,
        grounded=decision.groundedness_ok,
        latency_ms=decision.latency_ms,
    )

    # Anything not simply allowed becomes an alert for the supervisor queue.
    if decision.action in ("CHALLENGE", "FREEZE_AND_ESCALATE", "QUARANTINE"):
        alert = Alert(
            alert_id=new_id("ALERT"),
            txn_id=txn.txn_id,
            customer_id=txn.customer_id,
            risk_score=decision.risk_score,
            risk_level=decision.risk_level,
            action=decision.action,
            status="PENDING",
            summary=_summarise(txn, decision),
            region=txn.region,
        )
        db.save_alert(alert)
        db.audit(actor="system", event_type="ALERT", subject_id=alert.alert_id,
                 detail=f"Alert raised for {txn.txn_id}: {alert.summary}")

        # A freeze is proposed by the system but only a human makes it permanent.
        # We freeze the card immediately to stop the bleeding, and the analyst
        # confirms or reverses it in the admin portal.
        if decision.action == "FREEZE_AND_ESCALATE" and customer is not None:
            db.set_card_frozen(customer.customer_id, True)
            db.audit(actor="system", event_type="CARD_FREEZE",
                     subject_id=customer.customer_id,
                     detail=f"Card frozen pending analyst approval (alert {alert.alert_id})")


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    """End-to-end check. Used as the milestone gate during the build."""
    import json

    print(f"DEMO_MODE={config.DEMO_MODE}  api_key={'yes' if config.has_api_key() else 'NO'}")
    db.init_db()

    demo = json.loads(config.DEMO_INJECTIONS_PATH.read_text(encoding="utf-8"))

    # Unfreeze the hero card first. A previous fraud injection leaves it frozen, and
    # CARD_ALREADY_FROZEN (+60) then fires on every subsequent transaction, pushing the
    # legitimate scenarios to 100 and failing the test for the wrong reason. The same
    # trap applies on stage: rehearsing the demo twice without unfreezing makes every
    # beat look like fraud, which is why it is a pre-flight checklist item.
    hero_id = demo.get("hero_customer_id")
    if hero_id:
        db.set_card_frozen(hero_id, False)

    # File the demo travel notice if it is missing. Without it the "legitimate purchase
    # in Spain" scenario is not testing travel suppression at all -- it is testing an
    # unconfigured system, and the check would pass while the feature was broken.
    notice = demo.get("travel_notice")
    if notice and not travel.active_notices(notice["customer_id"]):
        travel.create_notice(notice["customer_id"], notice["countries"],
                             notice["start_date"], notice["end_date"])
        print(f"(reset: unfroze {hero_id}, filed travel notice)")
    else:
        print(f"(reset: unfroze {hero_id})")

    failures = 0

    for scenario in demo["scenarios"]:
        payload = dict(scenario["txn"])
        payload.setdefault("timestamp", __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat())
        payload["txn_id"] = new_id("TXN")
        txn = Transaction(**payload)

        decision = score_transaction(txn, persist=False)
        flag = "OK "
        if scenario["key"].startswith("legit") and decision.action not in ("ALLOW", "CHALLENGE"):
            flag, failures = "!! ", failures + 1
        if scenario["key"] == "attack_injection" and not decision.injection_detected:
            flag, failures = "!! ", failures + 1
        if scenario["key"] == "fraud_classic" and decision.risk_score < 60:
            flag, failures = "!! ", failures + 1

        print(f"{flag}{scenario['key']:<20} score={decision.risk_score:>3} "
              f"{decision.action:<20} llm={'Y' if decision.llm_used else 'n'} "
              f"cites={decision.cited_case_ids or '-'} {decision.latency_ms}ms")
        if decision.rule_hits:
            for hit in decision.rule_hits:
                print(f"      - {hit.rule_id} (+{hit.weight})")

    print(f"\n{'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
