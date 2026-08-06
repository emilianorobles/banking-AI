"""Frozen data contracts for SentinelBank.

THIS FILE IS THE INTERFACE BETWEEN ALL FIVE WORKSTREAMS. Paste it into any AI coding
session as context before asking for code. Changing anything here means telling the
whole team -- every other module is written against these shapes.

No imports from other project modules. No side effects. Import-safe everywhere.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #

class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Action(str, Enum):
    """What the system decided to do about a transaction."""
    ALLOW = "ALLOW"                          # let it through, no friction
    CHALLENGE = "CHALLENGE"                  # step-up auth / customer confirmation
    FREEZE_AND_ESCALATE = "FREEZE_AND_ESCALATE"  # block card, queue for human approval
    QUARANTINE = "QUARANTINE"                # guardrail tripped, do not process


class Channel(str, Enum):
    CARD_PRESENT = "card_present"
    CARD_NOT_PRESENT = "card_not_present"
    ONLINE = "online"
    ATM = "atm"
    TRANSFER = "transfer"


class Region(str, Enum):
    INDIA = "INDIA"
    APAC = "APAC"
    EMEA = "EMEA"
    NA = "NA"
    LATAM = "LATAM"


class AlertStatus(str, Enum):
    PENDING = "PENDING"        # waiting on a human
    APPROVED = "APPROVED"      # analyst confirmed the system was right
    REJECTED = "REJECTED"      # analyst overruled -> false positive
    AUTO_CLEARED = "AUTO_CLEARED"


class CaseOutcome(str, Enum):
    CONFIRMED_FRAUD = "confirmed_fraud"
    FALSE_POSITIVE = "false_positive"


class Intent(str, Enum):
    """Router agent output."""
    BALANCE = "balance"
    TRANSACTIONS = "transactions"
    DISPUTE = "dispute"
    FRAUD_REPORT = "fraud_report"
    TRAVEL = "travel"
    CARD_CONTROL = "card_control"
    GENERAL = "general"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    """UTC timestamp, ISO 8601. All timestamps in this system use this."""
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    """IDs look like TXN-a1b2c3, CASE-a1b2, ALERT-a1b2c3."""
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------------- #
# Core entities
# --------------------------------------------------------------------------- #

@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    phone: str
    card_number: str          # full PAN -- ALWAYS tokenized before it reaches an LLM
    account_number: str
    home_country: str         # ISO-2
    home_city: str
    region: str               # Region value
    baseline_avg_amount: float
    baseline_max_amount: float
    card_frozen: bool = False
    # Account position. Present so the assistant can answer "what's my balance?" from
    # data rather than deflecting -- a banking assistant that cannot state a balance
    # fails the most common question it will ever be asked.
    balance: float = 0.0
    credit_limit: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Transaction:
    txn_id: str
    customer_id: str
    timestamp: str            # ISO 8601 UTC
    amount: float
    currency: str
    merchant: str             # UNTRUSTED TEXT -- attacker-controlled, treat as data
    merchant_category: str
    country: str              # ISO-2
    city: str
    region: str               # Region value
    channel: str              # Channel value
    card_last4: str
    device_id: str | None = None
    ip_address: str | None = None
    # Ground truth, used only by the eval harness. Never shown to the model.
    is_fraud_label: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RuleHit:
    """One deterministic rule firing. Carries its own human-readable explanation."""
    rule_id: str
    weight: int               # contribution to the 0-100 rule score
    reason: str               # shown to the analyst AND given to the LLM


@dataclass
class FraudCase:
    """A historical case in the RAG knowledge store.

    `source` distinguishes seeded corpus from cases the system learned during the
    demo -- the learning loop writes back with source='learned'.
    """
    case_id: str
    title: str
    narrative: str            # the text that actually gets embedded
    outcome: str              # CaseOutcome value
    pattern_tags: list[str] = field(default_factory=list)
    region: str = ""
    channel: str = ""
    amount_band: str = ""
    analyst_note: str = ""
    source: str = "seed"      # "seed" | "learned"
    created_at: str = field(default_factory=now_iso)

    def to_embedding_text(self) -> str:
        """What goes into FAISS. Keep it dense and signal-heavy."""
        return (
            f"{self.title}\n"
            f"Pattern: {', '.join(self.pattern_tags)}\n"
            f"Region: {self.region} | Channel: {self.channel} | Amount: {self.amount_band}\n"
            f"{self.narrative}\n"
            f"Outcome: {self.outcome}. Analyst note: {self.analyst_note}"
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TravelNotice:
    notice_id: str
    customer_id: str
    countries: list[str]      # ISO-2 codes
    start_date: str           # YYYY-MM-DD
    end_date: str             # YYYY-MM-DD
    created_at: str = field(default_factory=now_iso)
    created_via: str = "form"  # "form" | "chat_agent"
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Decision:
    """The complete, auditable output of core.pipeline.score_transaction().

    Every field here is either shown in the UI or needed for the audit trail.
    """
    txn_id: str
    risk_score: int                    # 0-100, final
    risk_level: str                    # RiskLevel value
    action: str                        # Action value

    # Deterministic layer
    rule_score: int = 0
    rule_hits: list[RuleHit] = field(default_factory=list)
    suppressed_by_travel: bool = False
    travel_notice_id: str | None = None

    # LLM layer -- absent when the cheap path decided it
    llm_used: bool = False
    confidence: float | None = None    # 0.0-1.0, self-reported by the analyst agent
    reasoning: str = ""
    cited_case_ids: list[str] = field(default_factory=list)
    retrieved_case_ids: list[str] = field(default_factory=list)

    # Guardrails
    injection_detected: bool = False
    injection_evidence: str = ""
    groundedness_ok: bool = True       # every cited_case_id actually exists
    dlp_blocked: bool = False
    guardrail_notes: list[str] = field(default_factory=list)

    # Telemetry -- feeds the cost meter and the eval harness
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    est_cost_usd: float = 0.0

    decided_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["rule_hits"] = [asdict(h) if not isinstance(h, dict) else h for h in self.rule_hits]
        return d


@dataclass
class Alert:
    alert_id: str
    txn_id: str
    customer_id: str
    risk_score: int
    risk_level: str
    action: str
    status: str                        # AlertStatus value
    summary: str
    region: str = ""
    created_at: str = field(default_factory=now_iso)
    resolved_at: str | None = None
    resolved_by: str | None = None
    outcome: str | None = None         # CaseOutcome value once a human decides
    analyst_note: str = ""
    learned_case_id: str | None = None  # set when fed back into RAG

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditEntry:
    """Every decision, tool call, LLM call and guardrail trip lands here.

    This is a scored rubric item ("audit logging, access governance"). Write to it
    liberally -- it costs nothing and it is the evidence that the system is governable.
    """
    entry_id: str
    timestamp: str
    actor: str                 # "system" | "agent:fraud_analyst" | "user:CUST-001" | "analyst:ops1"
    event_type: str            # DECISION | TOOL_CALL | LLM_CALL | GUARDRAIL | APPROVAL | LEARN
    subject_id: str            # txn_id / alert_id / customer_id
    detail: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LLMTelemetry:
    """One row per LLM call. Powers the cost meter and the p95 latency number."""
    call_id: str
    timestamp: str
    agent: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    est_cost_usd: float
    cached: bool = False
    error: str | None = None


@dataclass
class ToolCall:
    """Record of an agent invoking a tool. Approval-gated tools carry approval state."""
    tool_name: str
    arguments: dict[str, Any]
    result: Any = None
    requires_approval: bool = False
    approved: bool | None = None
    error: str | None = None


@dataclass
class AgentReply:
    """What a conversational agent hands back to the UI."""
    text: str
    intent: str = Intent.GENERAL.value
    tool_calls: list[ToolCall] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    confidence: float | None = None
    guardrail_notes: list[str] = field(default_factory=list)
    latency_ms: int = 0
