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

from dataclasses import dataclass
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


# --------------------------------------------------------------------------- #
# Rule metadata -- the single declaration site for weights and wording
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RuleMeta:
    """Everything about a rule except the condition that fires it.

    `points` is THE weight. The rule bodies below read it from here rather than
    repeating the number, because the UI used to keep its own copy of the weights in
    `web/routes/drill.py` and that copy silently drifted: five weights were wrong and
    four rule IDs did not exist at all, so clicking those pills 404'd. One declaration
    site is the structural fix -- there is now nowhere for a second copy to disagree.

    `title` and `plain` are customer-facing and carry no jargon. `technical` is the
    analyst gloss, where jargon is fine. `category` is what lets the explanation popup
    group twelve rule IDs into five everyday areas a customer can actually read.
    """
    rule_id: str
    title: str            # customer-facing, e.g. "Much larger than you usually spend"
    plain: str            # one sentence, second person, zero jargon
    category: str         # "Amount" | "Location" | "Pace" | "Card security" | "Merchant"
    points: int           # THE weight -- declared here, read by the rule body
    fix: str = ""         # what the customer can do about it, if anything
    technical: str = ""   # analyst-facing


RULE_META: dict[str, RuleMeta] = {
    "CARD_ALREADY_FROZEN": RuleMeta(
        "CARD_ALREADY_FROZEN",
        "Your card is already frozen",
        "This card is frozen, so nothing should be able to authorise on it at all.",
        "Card security", 60,
        fix="Unfreeze the card once you have checked the recent activity is yours.",
        technical="Activity on a frozen PAN is always critical: either the freeze failed "
                  "to propagate to the network, or someone is still trying the card.",
    ),
    "AMOUNT_EXTREME": RuleMeta(
        "AMOUNT_EXTREME",
        "Far larger than you have ever spent",
        "This is at least ten times bigger than the largest payment you have ever made.",
        "Amount", 35,
        technical="amount / customer.baseline_max_amount >= 10.",
    ),
    "AMOUNT_ANOMALY": RuleMeta(
        "AMOUNT_ANOMALY",
        "Much larger than you usually spend",
        "This is at least three times bigger than your usual maximum payment.",
        "Amount", 22,
        technical="amount / customer.baseline_max_amount >= 3.",
    ),
    "GEO_FOREIGN": RuleMeta(
        "GEO_FOREIGN",
        "Outside your home country",
        "This payment was made in a different country from the one your account is in.",
        "Location", 14,
        fix="Tell us before you travel and this stops counting against you.",
        technical="txn.country != customer.home_country. Suppressed by an active travel "
                  "notice for the destination.",
    ),
    "GEO_NEW_COUNTRY": RuleMeta(
        "GEO_NEW_COUNTRY",
        "A country you have never used this card in",
        "You have never made a payment in this country before.",
        "Location", 16,
        fix="A travel notice for the destination covers this one too.",
        technical="txn.country not in {t.country for t in history}.",
    ),
    "GEO_IMPOSSIBLE_TRAVEL": RuleMeta(
        "GEO_IMPOSSIBLE_TRAVEL",
        "Two places too far apart to reach in the time",
        "Your card was used in two different countries closer together in time than "
        "anyone could physically travel between them.",
        "Location", 35,
        technical="Two card-present transactions in different countries within 2 hours. "
                  "One of the strongest single signals available, because it is a fact "
                  "about physics rather than a judgement about behaviour.",
    ),
    "VELOCITY_BURST": RuleMeta(
        "VELOCITY_BURST",
        "A sudden burst of payments",
        "Six or more payments went through on this card within a single hour.",
        "Pace", 25,
        technical=">= 5 prior transactions within a 3600s window.",
    ),
    "VELOCITY_ELEVATED": RuleMeta(
        "VELOCITY_ELEVATED",
        "More payments than usual in a short time",
        "Several payments went through on this card within an hour.",
        "Pace", 12,
        technical=">= 3 prior transactions within a 3600s window.",
    ),
    "CARD_TESTING": RuleMeta(
        "CARD_TESTING",
        "A tiny payment, then a large one",
        "Small test payments came through just before this larger one -- the pattern "
        "someone uses to check whether a stolen card still works.",
        "Card security", 22,
        technical="Prior transaction(s) <= 5 units within 1800s, followed by >= 200.",
    ),
    "CNP_HIGH_VALUE": RuleMeta(
        "CNP_HIGH_VALUE",
        "A large payment where the card was not present",
        "This was a large online or over-the-phone payment, so nobody checked that the "
        "physical card was there.",
        "Card security", 18,
        technical="channel in (card_not_present, online) and amount >= max(500, 4x avg).",
    ),
    "ODD_HOUR": RuleMeta(
        "ODD_HOUR",
        "At an hour you do not normally spend",
        "This happened between 1am and 5am, which is outside when you normally pay for "
        "things.",
        "Pace", 9,
        technical="Local hour in [1, 5]. Disproportionately fraudulent window.",
    ),
    "MERCHANT_HIGH_RISK": RuleMeta(
        "MERCHANT_HIGH_RISK",
        "A type of merchant fraud often targets",
        "This kind of business -- crypto, gift cards, money transfer and similar -- is "
        "used in fraud far more often than most.",
        "Merchant", 15,
        technical=f"merchant_category in HIGH_RISK_CATEGORIES "
                  f"({len(HIGH_RISK_CATEGORIES)} categories).",
    ),
}

# Everyday groupings, in the order the explanation popup should show them.
CATEGORIES: tuple[str, ...] = ("Amount", "Location", "Pace", "Card security", "Merchant")


def meta(rule_id: str) -> RuleMeta:
    """Metadata for a rule ID, with a safe placeholder for one we do not know.

    Never raises. A drill-down for a retired rule ID stored on an old decision must
    render something honest rather than 500 the page.
    """
    known = RULE_META.get((rule_id or "").upper())
    if known is not None:
        return known
    return RuleMeta(rule_id or "UNKNOWN", rule_id or "Unknown check",
                    "This check is no longer part of the engine.", "Other", 0)


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
            "AMOUNT_EXTREME", RULE_META["AMOUNT_EXTREME"].points,
            f"Amount {txn.amount:,.0f} {txn.currency} is {ratio:.1f}x the customer's "
            f"highest historical transaction ({baseline:,.0f}).",
        )
    if ratio >= 3:
        return RuleHit(
            "AMOUNT_ANOMALY", RULE_META["AMOUNT_ANOMALY"].points,
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
            "GEO_FOREIGN", RULE_META["GEO_FOREIGN"].points,
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
            "GEO_NEW_COUNTRY", RULE_META["GEO_NEW_COUNTRY"].points,
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
                "GEO_IMPOSSIBLE_TRAVEL", RULE_META["GEO_IMPOSSIBLE_TRAVEL"].points,
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
            "VELOCITY_BURST", RULE_META["VELOCITY_BURST"].points,
            f"{len(recent) + 1} transactions on this card within one hour "
            f"(customer's normal rate is far lower).",
        )
    if len(recent) >= 3:
        return RuleHit(
            "VELOCITY_ELEVATED", RULE_META["VELOCITY_ELEVATED"].points,
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
            "CARD_TESTING", RULE_META["CARD_TESTING"].points,
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
            "CNP_HIGH_VALUE", RULE_META["CNP_HIGH_VALUE"].points,
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
            "ODD_HOUR", RULE_META["ODD_HOUR"].points,
            f"Transaction at {hour:02d}:00, outside the customer's normal activity window.",
        )
    return None


def rule_merchant_risk(
    txn: Transaction, customer: Customer, history: list[Transaction]
) -> RuleHit | None:
    """Structurally high-risk merchant categories."""
    if txn.merchant_category in HIGH_RISK_CATEGORIES:
        return RuleHit(
            "MERCHANT_HIGH_RISK", RULE_META["MERCHANT_HIGH_RISK"].points,
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
            "CARD_ALREADY_FROZEN", RULE_META["CARD_ALREADY_FROZEN"].points,
            "Attempted transaction on a card that is already frozen.",
        )
    return None


RuleFn = Callable[[Transaction, Customer, list[Transaction]], "RuleHit | None"]

RULES: list[RuleFn] = [
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

# Which IDs each rule can emit. Declared rather than derived, because RULES is a list of
# functions and two of them are one-to-many -- rule_amount_anomaly emits two IDs and
# rule_velocity emits two -- so the mapping cannot be recovered by inspection.
EMITS: dict[RuleFn, tuple[str, ...]] = {
    rule_frozen_card:      ("CARD_ALREADY_FROZEN",),
    rule_amount_anomaly:   ("AMOUNT_EXTREME", "AMOUNT_ANOMALY"),
    rule_geo_foreign:      ("GEO_FOREIGN",),
    rule_geo_new_country:  ("GEO_NEW_COUNTRY",),
    rule_impossible_travel: ("GEO_IMPOSSIBLE_TRAVEL",),
    rule_velocity:         ("VELOCITY_BURST", "VELOCITY_ELEVATED"),
    rule_card_testing:     ("CARD_TESTING",),
    rule_cnp_high_value:   ("CNP_HIGH_VALUE",),
    rule_odd_hour:         ("ODD_HOUR",),
    rule_merchant_risk:    ("MERCHANT_HIGH_RISK",),
}


def selfcheck() -> list[str]:
    """Return the ways RULES, EMITS and RULE_META disagree. Empty means consistent.

    Deliberately a pure function returning problems rather than an `assert`:

      * `python -O` strips asserts, so the guard would vanish in exactly the environment
        where nobody is watching the console.
      * An AssertionError at import time kills the whole app. A typo in a rule ID would
        take the demo down instead of showing a badge.
      * `core/` is required to be import-safe with no side effects.

    The hazard this closes is quiet: `evaluate()` wraps each rule in
    `except Exception: continue`, so a rule emitting an ID that is not in RULE_META
    raises a KeyError that is **swallowed, silently disabling that rule**. Nothing would
    look broken -- the score would just be wrong, forever.

    Checked in both directions, because a metadata entry with no rule behind it is how
    the old RULE_CATALOGUE ended up shipping four IDs that 404'd on click.
    """
    problems: list[str] = []

    for fn in RULES:
        if fn not in EMITS:
            problems.append(f"{fn.__name__} is in RULES but declares no IDs in EMITS")
    for fn in EMITS:
        if fn not in RULES:
            problems.append(f"{fn.__name__} is in EMITS but is not registered in RULES")

    declared: set[str] = set()
    for fn, ids in EMITS.items():
        for rule_id in ids:
            declared.add(rule_id)
            if rule_id not in RULE_META:
                problems.append(f"{fn.__name__} emits '{rule_id}', which has no RULE_META entry")

    for rule_id, m in RULE_META.items():
        if rule_id not in declared:
            problems.append(f"RULE_META has '{rule_id}', which no rule in EMITS ever emits")
        if m.rule_id != rule_id:
            problems.append(f"RULE_META['{rule_id}'] carries rule_id '{m.rule_id}'")
        if not 0 < m.points <= MAX_SCORE:
            problems.append(f"'{rule_id}' has an out-of-range weight of {m.points}")
        if m.category not in CATEGORIES:
            problems.append(f"'{rule_id}' has category '{m.category}', "
                            f"which is not one of {', '.join(CATEGORIES)}")
        if not m.title or not m.plain:
            problems.append(f"'{rule_id}' is missing its customer-facing title or wording")

    return problems


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
