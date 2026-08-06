"""SQLite persistence.

WAL mode is essential: the Streamlit app and the FastAPI ingestion service are separate
processes both writing to this file. Without WAL they deadlock on the first concurrent
injection -- which is exactly what happens during the live demo.

Every function opens and closes its own short-lived connection. No global connection,
because Streamlit reruns the script on every interaction and threads change underneath us.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

from . import config, llm
from .contracts import (
    Alert,
    AuditEntry,
    Customer,
    Decision,
    FraudCase,
    LLMTelemetry,
    Transaction,
    TravelNotice,
    new_id,
    now_iso,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    name TEXT, email TEXT, phone TEXT,
    card_number TEXT, account_number TEXT,
    home_country TEXT, home_city TEXT, region TEXT,
    baseline_avg_amount REAL, baseline_max_amount REAL,
    card_frozen INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id TEXT PRIMARY KEY,
    customer_id TEXT, timestamp TEXT,
    amount REAL, currency TEXT,
    merchant TEXT, merchant_category TEXT,
    country TEXT, city TEXT, region TEXT,
    channel TEXT, card_last4 TEXT,
    device_id TEXT, ip_address TEXT,
    is_fraud_label INTEGER
);
CREATE INDEX IF NOT EXISTS idx_txn_customer ON transactions(customer_id);
CREATE INDEX IF NOT EXISTS idx_txn_time ON transactions(timestamp);

CREATE TABLE IF NOT EXISTS decisions (
    txn_id TEXT PRIMARY KEY,
    risk_score INTEGER, risk_level TEXT, action TEXT,
    rule_score INTEGER, rule_hits TEXT,
    suppressed_by_travel INTEGER, travel_notice_id TEXT,
    llm_used INTEGER, confidence REAL, reasoning TEXT,
    cited_case_ids TEXT, retrieved_case_ids TEXT,
    injection_detected INTEGER, injection_evidence TEXT,
    groundedness_ok INTEGER, dlp_blocked INTEGER, guardrail_notes TEXT,
    latency_ms INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
    est_cost_usd REAL, decided_at TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    txn_id TEXT, customer_id TEXT,
    risk_score INTEGER, risk_level TEXT, action TEXT,
    status TEXT, summary TEXT, region TEXT,
    created_at TEXT, resolved_at TEXT, resolved_by TEXT,
    outcome TEXT, analyst_note TEXT, learned_case_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_status ON alerts(status);

CREATE TABLE IF NOT EXISTS travel_notices (
    notice_id TEXT PRIMARY KEY,
    customer_id TEXT, countries TEXT,
    start_date TEXT, end_date TEXT,
    created_at TEXT, created_via TEXT, active INTEGER
);
CREATE INDEX IF NOT EXISTS idx_travel_customer ON travel_notices(customer_id);

CREATE TABLE IF NOT EXISTS audit_log (
    entry_id TEXT PRIMARY KEY,
    timestamp TEXT, actor TEXT, event_type TEXT,
    subject_id TEXT, detail TEXT, metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_subject ON audit_log(subject_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp);

CREATE TABLE IF NOT EXISTS llm_telemetry (
    call_id TEXT PRIMARY KEY,
    timestamp TEXT, agent TEXT, model TEXT,
    prompt_tokens INTEGER, completion_tokens INTEGER,
    latency_ms INTEGER, est_cost_usd REAL,
    cached INTEGER, error TEXT
);

CREATE TABLE IF NOT EXISTS fraud_cases (
    case_id TEXT PRIMARY KEY,
    title TEXT, narrative TEXT, outcome TEXT,
    pattern_tags TEXT, region TEXT, channel TEXT,
    amount_band TEXT, analyst_note TEXT,
    source TEXT, created_at TEXT
);

-- Counts transactions that never needed an LLM call. Powers the cost meter.
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER DEFAULT 0
);
"""


# --------------------------------------------------------------------------- #
# Connection handling
# --------------------------------------------------------------------------- #

@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Idempotent. Safe to call on every app start."""
    with connect() as conn:
        conn.executescript(SCHEMA)


def reset_db() -> None:
    """Drop everything. Used by the Reset Demo button."""
    with connect() as conn:
        for table in (
            "customers", "transactions", "decisions", "alerts",
            "travel_notices", "audit_log", "llm_telemetry", "fraud_cases", "counters",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(SCHEMA)


def _j(value: Any) -> str:
    return json.dumps(value, default=str)


def _unj(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


# --------------------------------------------------------------------------- #
# Customers
# --------------------------------------------------------------------------- #

def upsert_customers(customers: Iterable[Customer]) -> None:
    with connect() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO customers VALUES
               (:customer_id,:name,:email,:phone,:card_number,:account_number,
                :home_country,:home_city,:region,:baseline_avg_amount,
                :baseline_max_amount,:card_frozen)""",
            [{**c.to_dict(), "card_frozen": int(c.card_frozen)} for c in customers],
        )


def get_customer(customer_id: str) -> Customer | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE customer_id=?", (customer_id,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["card_frozen"] = bool(d["card_frozen"])
    return Customer(**d)


def list_customers(limit: int = 500) -> list[Customer]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM customers ORDER BY customer_id LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["card_frozen"] = bool(d["card_frozen"])
        out.append(Customer(**d))
    return out


def set_card_frozen(customer_id: str, frozen: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE customers SET card_frozen=? WHERE customer_id=?",
            (int(frozen), customer_id),
        )


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #

def insert_transaction(txn: Transaction) -> None:
    with connect() as conn:
        d = txn.to_dict()
        d["is_fraud_label"] = None if txn.is_fraud_label is None else int(txn.is_fraud_label)
        conn.execute(
            """INSERT OR REPLACE INTO transactions VALUES
               (:txn_id,:customer_id,:timestamp,:amount,:currency,:merchant,
                :merchant_category,:country,:city,:region,:channel,:card_last4,
                :device_id,:ip_address,:is_fraud_label)""",
            d,
        )


def insert_transactions(txns: Iterable[Transaction]) -> None:
    rows = []
    for t in txns:
        d = t.to_dict()
        d["is_fraud_label"] = None if t.is_fraud_label is None else int(t.is_fraud_label)
        rows.append(d)
    with connect() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO transactions VALUES
               (:txn_id,:customer_id,:timestamp,:amount,:currency,:merchant,
                :merchant_category,:country,:city,:region,:channel,:card_last4,
                :device_id,:ip_address,:is_fraud_label)""",
            rows,
        )


def _row_to_txn(row: sqlite3.Row) -> Transaction:
    d = dict(row)
    if d.get("is_fraud_label") is not None:
        d["is_fraud_label"] = bool(d["is_fraud_label"])
    return Transaction(**d)


def get_transaction(txn_id: str) -> Transaction | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM transactions WHERE txn_id=?", (txn_id,)).fetchone()
    return _row_to_txn(row) if row else None


def recent_transactions(customer_id: str | None = None, limit: int = 25) -> list[Transaction]:
    with connect() as conn:
        if customer_id:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE customer_id=? "
                "ORDER BY timestamp DESC LIMIT ?",
                (customer_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transactions ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
    return [_row_to_txn(r) for r in rows]


def customer_transaction_history(customer_id: str, before: str, limit: int = 50) -> list[Transaction]:
    """Transactions strictly before `before`. Used by the velocity/geo rules."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE customer_id=? AND timestamp < ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (customer_id, before, limit),
        ).fetchall()
    return [_row_to_txn(r) for r in rows]


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #

def save_decision(decision: Decision) -> None:
    d = decision.to_dict()
    d["rule_hits"] = _j(d["rule_hits"])
    d["cited_case_ids"] = _j(d["cited_case_ids"])
    d["retrieved_case_ids"] = _j(d["retrieved_case_ids"])
    d["guardrail_notes"] = _j(d["guardrail_notes"])
    for k in ("suppressed_by_travel", "llm_used", "injection_detected",
              "groundedness_ok", "dlp_blocked"):
        d[k] = int(bool(d[k]))
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO decisions VALUES
               (:txn_id,:risk_score,:risk_level,:action,:rule_score,:rule_hits,
                :suppressed_by_travel,:travel_notice_id,:llm_used,:confidence,
                :reasoning,:cited_case_ids,:retrieved_case_ids,:injection_detected,
                :injection_evidence,:groundedness_ok,:dlp_blocked,:guardrail_notes,
                :latency_ms,:prompt_tokens,:completion_tokens,:est_cost_usd,:decided_at)""",
            d,
        )


def get_decision(txn_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM decisions WHERE txn_id=?", (txn_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["rule_hits"] = _unj(d["rule_hits"], [])
    d["cited_case_ids"] = _unj(d["cited_case_ids"], [])
    d["retrieved_case_ids"] = _unj(d["retrieved_case_ids"], [])
    d["guardrail_notes"] = _unj(d["guardrail_notes"], [])
    for k in ("suppressed_by_travel", "llm_used", "injection_detected",
              "groundedness_ok", "dlp_blocked"):
        d[k] = bool(d[k])
    return d


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #

def save_alert(alert: Alert) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO alerts VALUES
               (:alert_id,:txn_id,:customer_id,:risk_score,:risk_level,:action,
                :status,:summary,:region,:created_at,:resolved_at,:resolved_by,
                :outcome,:analyst_note,:learned_case_id)""",
            alert.to_dict(),
        )


def list_alerts(
    status: str | None = None, region: str | None = None, limit: int = 100
) -> list[Alert]:
    sql = "SELECT * FROM alerts WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if region and region != "ALL":
        sql += " AND region=?"
        params.append(region)
    sql += " ORDER BY risk_score DESC, created_at DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [Alert(**dict(r)) for r in rows]


def get_alert(alert_id: str) -> Alert | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM alerts WHERE alert_id=?", (alert_id,)).fetchone()
    return Alert(**dict(row)) if row else None


def get_alert_for_txn(txn_id: str) -> Alert | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM alerts WHERE txn_id=?", (txn_id,)).fetchone()
    return Alert(**dict(row)) if row else None


def resolve_alert(
    alert_id: str, outcome: str, resolved_by: str, note: str = "",
    learned_case_id: str | None = None,
) -> None:
    status = "APPROVED" if outcome == "confirmed_fraud" else "REJECTED"
    with connect() as conn:
        conn.execute(
            "UPDATE alerts SET status=?, outcome=?, resolved_by=?, resolved_at=?, "
            "analyst_note=?, learned_case_id=? WHERE alert_id=?",
            (status, outcome, resolved_by, now_iso(), note, learned_case_id, alert_id),
        )


# --------------------------------------------------------------------------- #
# Travel notices
# --------------------------------------------------------------------------- #

def save_travel_notice(notice: TravelNotice) -> None:
    d = notice.to_dict()
    d["countries"] = _j(d["countries"])
    d["active"] = int(d["active"])
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO travel_notices VALUES
               (:notice_id,:customer_id,:countries,:start_date,:end_date,
                :created_at,:created_via,:active)""",
            d,
        )


def list_travel_notices(customer_id: str | None = None, active_only: bool = True) -> list[TravelNotice]:
    sql = "SELECT * FROM travel_notices WHERE 1=1"
    params: list[Any] = []
    if customer_id:
        sql += " AND customer_id=?"
        params.append(customer_id)
    if active_only:
        sql += " AND active=1"
    sql += " ORDER BY created_at DESC"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["countries"] = _unj(d["countries"], [])
        d["active"] = bool(d["active"])
        out.append(TravelNotice(**d))
    return out


def cancel_travel_notice(notice_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE travel_notices SET active=0 WHERE notice_id=?", (notice_id,))


# --------------------------------------------------------------------------- #
# Fraud cases (mirror of the FAISS index, so we can render and validate citations)
# --------------------------------------------------------------------------- #

def save_fraud_case(case: FraudCase) -> None:
    d = case.to_dict()
    d["pattern_tags"] = _j(d["pattern_tags"])
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO fraud_cases VALUES
               (:case_id,:title,:narrative,:outcome,:pattern_tags,:region,
                :channel,:amount_band,:analyst_note,:source,:created_at)""",
            d,
        )


def save_fraud_cases(cases: Iterable[FraudCase]) -> None:
    for c in cases:
        save_fraud_case(c)


def get_fraud_case(case_id: str) -> FraudCase | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM fraud_cases WHERE case_id=?", (case_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["pattern_tags"] = _unj(d["pattern_tags"], [])
    return FraudCase(**d)


def list_fraud_cases(source: str | None = None) -> list[FraudCase]:
    sql = "SELECT * FROM fraud_cases"
    params: list[Any] = []
    if source:
        sql += " WHERE source=?"
        params.append(source)
    sql += " ORDER BY created_at DESC"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pattern_tags"] = _unj(d["pattern_tags"], [])
        out.append(FraudCase(**d))
    return out


def known_case_ids() -> set[str]:
    """Used by the groundedness guardrail to detect fabricated citations."""
    with connect() as conn:
        rows = conn.execute("SELECT case_id FROM fraud_cases").fetchall()
    return {r["case_id"] for r in rows}


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #

def audit(actor: str, event_type: str, subject_id: str, detail: str, **metadata: Any) -> None:
    """Write an audit entry. Deliberately forgiving -- never break a request over logging."""
    entry = AuditEntry(
        entry_id=new_id("AUD"),
        timestamp=now_iso(),
        actor=actor,
        event_type=event_type,
        subject_id=subject_id,
        detail=detail,
        metadata=metadata,
    )
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?)",
                (entry.entry_id, entry.timestamp, entry.actor, entry.event_type,
                 entry.subject_id, entry.detail, _j(entry.metadata)),
            )
    except Exception:
        pass


def list_audit(subject_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql = "SELECT * FROM audit_log"
    params: list[Any] = []
    if subject_id:
        sql += " WHERE subject_id=?"
        params.append(subject_id)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["metadata"] = _unj(d["metadata"], {})
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Telemetry + counters (the cost meter)
# --------------------------------------------------------------------------- #

def save_telemetry(tel: LLMTelemetry) -> None:
    try:
        with connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_telemetry VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tel.call_id, tel.timestamp, tel.agent, tel.model, tel.prompt_tokens,
                 tel.completion_tokens, tel.latency_ms, tel.est_cost_usd,
                 int(tel.cached), tel.error),
            )
    except Exception:
        pass


def bump(counter: str, by: int = 1) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO counters(name,value) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=value+?",
            (counter, by, by),
        )


def get_counter(name: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
    return int(row["value"]) if row else 0


def cost_summary() -> dict[str, Any]:
    """Everything the Admin cost meter needs, in one query pass."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(prompt_tokens),0) pt, "
            "COALESCE(SUM(completion_tokens),0) ct, COALESCE(SUM(est_cost_usd),0) cost, "
            "COALESCE(AVG(latency_ms),0) avg_ms FROM llm_telemetry WHERE error IS NULL"
        ).fetchone()
        latencies = [
            r["latency_ms"] for r in conn.execute(
                "SELECT latency_ms FROM llm_telemetry WHERE error IS NULL "
                "ORDER BY latency_ms"
            ).fetchall()
        ]
        total_txns = conn.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]
        llm_txns = conn.execute(
            "SELECT COUNT(*) n FROM decisions WHERE llm_used=1"
        ).fetchone()["n"]

    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0
    avg_cost_per_llm_call = (row["cost"] / row["n"]) if row["n"] else 0.0
    # What it would have cost to send every transaction to the model.
    naive_cost = avg_cost_per_llm_call * total_txns if total_txns else 0.0

    return {
        "llm_calls": row["n"],
        "prompt_tokens": row["pt"],
        "completion_tokens": row["ct"],
        "actual_cost_usd": round(row["cost"], 4),
        "naive_cost_usd": round(naive_cost, 4),
        "saved_usd": round(max(0.0, naive_cost - row["cost"]), 4),
        "avg_latency_ms": int(row["avg_ms"]),
        "p95_latency_ms": int(p95),
        "total_transactions": total_txns,
        "llm_transactions": llm_txns,
        "avoided": max(0, total_txns - llm_txns),
        "avoided_pct": round(100.0 * (total_txns - llm_txns) / total_txns, 1) if total_txns else 0.0,
    }


def region_stats(region: str | None = None) -> list[dict[str, Any]]:
    """Per-region KPI rows for the admin dashboard."""
    sql = """
        SELECT t.region AS region,
               COUNT(*) AS transactions,
               COALESCE(SUM(CASE WHEN d.action='FREEZE_AND_ESCALATE' THEN 1 ELSE 0 END),0) AS frozen,
               COALESCE(SUM(CASE WHEN d.action='CHALLENGE' THEN 1 ELSE 0 END),0) AS challenged,
               COALESCE(SUM(CASE WHEN d.suppressed_by_travel=1 THEN 1 ELSE 0 END),0) AS travel_suppressed,
               COALESCE(AVG(d.risk_score),0) AS avg_risk,
               COALESCE(SUM(t.amount),0) AS volume
        FROM transactions t JOIN decisions d ON d.txn_id = t.txn_id
    """
    params: list[Any] = []
    if region and region != "ALL":
        sql += " WHERE t.region=?"
        params.append(region)
    sql += " GROUP BY t.region ORDER BY frozen DESC"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# Wire LLM telemetry straight into the DB.
llm.set_telemetry_sink(save_telemetry)
