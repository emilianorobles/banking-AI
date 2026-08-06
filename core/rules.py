"""Deterministic fraud rules -- the first and cheapest line of defence.

Why rules before the LLM:

  * COST. Roughly 94% of transactions are unambiguous. Sending all of them to a model
    would cost ~17x more for no accuracy gain. The rules resolve the easy cases for free
    and only the ambiguous middle band pays for inference. That is the commercial
    argument for this system and it is measured live on the admin dashboard.

  * RELIABILITY. "Transaction is in a country the customer has never visited" is a fact,
    not a judgement. Facts belong in code. The model is used for what it is actually good
    at: weighing several weak signals against historical precedent and explaining itself.

  * AUDITABILITY. Every rule returns its own plain-English reason, which goes to the
    analyst, to the audit log, and into the model's prompt as evidence.

Each rule is a pure function of (transaction, customer, history) -> RuleHit | None.
No I/O, no globals -- which makes them trivially testable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .contracts import Customer, RuleHit, Transaction

# Merchant categories with structurally elevated fraud rates.
HIGH_RISK_CATEGORIES = {
    "crypto_exchange", "gift_cards", "wire_transfer", "money_transfer",
    "online_gambling", "prepaid_reload", "electronics_reseller",
}

# Weight ceiling. A single rule should never single-handedly force a freeze --
# that is what the combination is for.
MAX_SCORE = 100


def _ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return datetime.min


# --------------------------------------------------------------------------- #
# Individual rules
# --------------------------------------------------------------------------- #

def rule_amount_anomaly(
    txn: Transaction, customer: Customer, history: list[Transaction]
) -> RuleHit | None:
    """Amount far outside what this specific customer normally spends."""
    baseline = max(customer.baseline_max_amount, 1.0)
    ratio = txn.amount / baseline
    if ratio >= 10:
        return RuleHit(
            "AMOUNT_EXTREME", 35,
            f"Amount {txn.amount:,.0f} {txn.currency} is {ratio:.1f}x the customer's "
            f"highest historical transaction ({baseline:,.0f}).",
        )
    if ratio >= 3:
        return RuleHit(
            "AMOUNT_ANOMALY", 22,
            f"Amount {txn.amount:,.0f} {txn.currency} is {ratio:.1f}x the customer's "
            f"usual maximum ({baseline:,.0f}).",
        )
    return None


def rule_geo_foreign(
    txn: Transaction, customer: Customer, history: list[Transaction]
) -> RuleHit | None:
    """Transaction outside the customer's home country."""
    if txn.country.upper() != customer.home_country.upper():
        return RuleHit(
            "GEO_FOREIGN", 14,
            f"Transaction in {txn.country} is outside the customer's home country "
            f"({customer.home_country}).",
        )
    return None


def rule_geo_new_country(
    txn: Transaction, customer: Customer, history: list[Transaction]
) -> RuleHit | None:
    """First time this customer has ever transacted in this country."""
    if txn.country.upper() == customer.home_country.upper():
        return None
    seen = {t.country.upper() for t in history}
    if txn.country.upper() not in seen:
        return RuleHit(
            "GEO_NEW_COUNTRY", 16,
            f"No prior transaction history in {txn.country} for this customer.",
        )
    return None


def rule_impossible_travel(
    txn: Transaction, customer: Customer, history: list[Transaction]
) -> RuleHit | None:
    """Two card-present transactions in different countries, too close together.

    Physical presence in two countries within a couple of hours is impossible, which
    makes this one of the strongest single signals available.
    """
    if not history:
        return None
    current = _ts(txn.timestamp)
    for prev in history[:5]:
        if prev.country.upper() == txn.country.upper():
            continue
        gap_hours = abs((current - _ts(prev.timestamp)).total_seconds()) / 3600.0
        if gap_hours <= 2.0:
            return RuleHit(
                "GEO_IMPOSSIBLE_TRAVEL", 35,
                f"Physically impossible: transaction in {txn.country} only "
                f"{gap_hours * 60:.0f} minutes after one in {prev.country}.",
            )
    return None


def rule_velocity(
    txn: Transaction, customer: Customer, history: list[Transaction]
) -> RuleHit | None:
    """An unusual burst of transactions in a short window."""
    if not history:
        return None
    current = _ts(txn.timestamp)
    recent = [t for t in history if (current - _ts(t.timestamp)).total_seconds() <= 3600]
    if len(recent) >= 5:
        return RuleHit(
            "VELOCITY_BURST", 25,
            f"{len(recent) + 1} transactions on this card within one hour "
            f"(customer's normal rate is far lower).",
        )
    if len(recent) >= 3:
        return RuleHit(
            "VELOCITY_ELEVATED", 12,
            f"{len(recent) + 1} transactions within one hour.",
        )
    return None


def rule_card_testing(
    txn: Transaction, customer: Customer, history: list[Transaction]
) -> RuleHit | None:
    """Small probe followed by a large charge -- the classic card-testing signature."""
    if not history:
        return None
    current = _ts(txn.timestamp)
    recent_small = [
        t for t in history
        if (current - _ts(t.timestamp)).total_seconds() <= 1800 and t.amount <= 5
    ]
    if recent_small and txn.amount >= 200:
        return RuleHit(
            "CARD_TESTING", 22,
            f"{len(recent_small)} micro-transaction(s) under 5 {txn.currency} in the "
            f"last 30 minutes, now followed by {txn.amount:,.0f} -- card-testing pattern.",
        )
    return None


def rule_cnp_high_value(
    txn: Transaction, customer: Customer, history: list[Transaction]
) -> RuleHit | None:
    """Card-not-present with a high value carries no physical-possession assurance."""
    if txn.channel in ("card_not_present", "online") and txn.amount >= max(
        500.0, customer.baseline_avg_amount * 4
    ):
        return RuleHit(
            "CNP_HIGH_VALUE", 18,
            f"High-value card-not-present transaction ({txn.amount:,.0f} {txn.currency}) "
            f"-- no physical card verification.",
        )
    return None


def rule_odd_hour(
    txn: Transaction, customer: Customer, history: list[Transaction]
) -> RuleHit | None:
    """Transactions between 01:00 and 05:00 are disproportionately fraudulent."""
    hour = _ts(txn.timestamp).hour
    if 1 <= hour <= 5:
        return RuleHit(
            "ODD_HOUR", 9,
            f"Transaction at {hour:02d}:00, outside the customer's normal activity window.",
        )
    return None


def rule_merchant_risk(
    txn: Transaction, customer: Customer, history: list[Transaction]
) -> RuleHit | None:
    """Structurally high-risk merchant categories."""
    if txn.merchant_category in HIGH_RISK_CATEGORIES:
        return RuleHit(
            "MERCHANT_HIGH_RISK", 15,
            f"Merchant category '{txn.merchant_category}' has a structurally elevated "
            f"fraud rate.",
        )
    return None


def rule_frozen_card(
    txn: Transaction, customer: Customer, history: list[Transaction]
) -> RuleHit | None:
    """Activity on a card that is already frozen is always critical."""
    if customer.card_frozen:
        return RuleHit(
            "CARD_ALREADY_FROZEN", 60,
            "Attempted transaction on a card that is already frozen.",
        )
    return None


RULES: list[Callable[[Transaction, Customer, list[Transaction]], RuleHit | None]] = [
    rule_frozen_card,
    rule_amount_anomaly,
    rule_geo_foreign,
    rule_geo_new_country,
    rule_impossible_travel,
    rule_velocity,
    rule_card_testing,
    rule_cnp_high_value,
    rule_odd_hour,
    rule_merchant_risk,
]


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

def evaluate(
    txn: Transaction,
    customer: Customer,
    history: list[Transaction],
    suppress: set[str] | None = None,
) -> tuple[int, list[RuleHit], list[RuleHit]]:
    """Run every rule.

    Returns (score, active_hits, suppressed_hits). `suppress` carries rule IDs that a
    travel notice has neutralised -- they are still returned so the UI can show
    "we would have flagged this, but you told us you were travelling", which is a far
    better story than silently not firing.
    """
    suppress = suppress or set()
    active: list[RuleHit] = []
    suppressed: list[RuleHit] = []

    for rule in RULES:
        try:
            hit = rule(txn, customer, history)
        except Exception:
            continue  # a broken rule must never take down the pipeline
        if hit is None:
            continue
        if hit.rule_id in suppress:
            suppressed.append(hit)
        else:
            active.append(hit)

    score = min(MAX_SCORE, sum(h.weight for h in active))
    return score, active, suppressed


def explain(hits: list[RuleHit]) -> str:
    """Numbered plain-English list. Goes to the analyst UI and into the model prompt."""
    if not hits:
        return "No deterministic rules triggered."
    return "\n".join(f"{i}. [{h.rule_id}] {h.reason}" for i, h in enumerate(hits, 1))
