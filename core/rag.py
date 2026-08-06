"""The fraud precedent knowledge store -- FAISS over historical case narratives.

Design note worth defending in the pitch: we do NOT embed the 2,000 transactions. A
transaction is structured data; rules handle it better and faster than a vector search
ever could. What we embed is the ~50 analyst case narratives -- the institutional
knowledge about *why* something was fraud. That is the part a model genuinely benefits
from retrieving, the index stays small enough to rebuild in seconds, and retrieval is
visibly relevant on stage rather than returning near-duplicate rows.

The learning loop lives here too: `add_case()` writes a resolved alert back into the
index, so a case an analyst closes at 10:04 is retrievable evidence at 10:05.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from . import config, db, llm
from .contracts import FraudCase, Transaction, new_id, now_iso

_lock = threading.Lock()
_store: Any = None  # the live FAISS instance


# --------------------------------------------------------------------------- #
# Index lifecycle
# --------------------------------------------------------------------------- #

def load_precedents_from_disk() -> list[FraudCase]:
    if not config.PRECEDENTS_PATH.exists():
        return []
    cases: list[FraudCase] = []
    with config.PRECEDENTS_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                cases.append(FraudCase(**json.loads(line)))
    return cases


def _to_documents(cases: list[FraudCase]) -> tuple[list[str], list[dict]]:
    texts = [c.to_embedding_text() for c in cases]
    metadatas = [
        {
            "case_id": c.case_id,
            "title": c.title,
            "outcome": c.outcome,
            "region": c.region,
            "channel": c.channel,
            "amount_band": c.amount_band,
            "pattern_tags": ", ".join(c.pattern_tags),
            "analyst_note": c.analyst_note,
            "source": c.source,
        }
        for c in cases
    ]
    return texts, metadatas


def build_index(cases: list[FraudCase] | None = None, persist: bool = True):
    """Build the FAISS index from scratch and mirror the cases into SQLite.

    The SQLite mirror is what the groundedness guardrail checks citations against, and
    what the UI renders when showing a cited case.
    """
    from langchain_community.vectorstores import FAISS

    cases = cases if cases is not None else load_precedents_from_disk()
    if not cases:
        raise RuntimeError(
            "No fraud precedents found. Run: python -m data.generate"
        )

    texts, metadatas = _to_documents(cases)
    store = FAISS.from_texts(texts, llm.get_embeddings(), metadatas=metadatas)

    db.save_fraud_cases(cases)
    if persist:
        config.FAISS_DIR.mkdir(parents=True, exist_ok=True)
        store.save_local(str(config.FAISS_DIR))

    global _store
    with _lock:
        _store = store
    return store


def load_index(rebuild_if_missing: bool = True):
    """Return the live index, loading from disk or building it on first use."""
    global _store
    with _lock:
        if _store is not None:
            return _store

    from langchain_community.vectorstores import FAISS

    if config.FAISS_DIR.exists() and any(config.FAISS_DIR.iterdir()):
        try:
            store = FAISS.load_local(
                str(config.FAISS_DIR),
                llm.get_embeddings(),
                # Safe here: we wrote this file ourselves in build_index().
                allow_dangerous_deserialization=True,
            )
            with _lock:
                _store = store
            return store
        except Exception:
            pass  # corrupt or stale index -- fall through and rebuild

    if not rebuild_if_missing:
        raise RuntimeError("FAISS index not available and rebuild is disabled.")
    return build_index()


def index_ready() -> bool:
    return _store is not None or (
        config.FAISS_DIR.exists() and any(config.FAISS_DIR.iterdir())
    )


def index_size() -> int:
    try:
        store = load_index(rebuild_if_missing=False)
        return store.index.ntotal
    except Exception:
        return 0


def reset_index(delete_disk: bool = False) -> None:
    """Drop the in-memory handle so the next call reloads from disk.

    `delete_disk=True` also removes the persisted index. This matters on a database
    reset: dropping the fraud_cases table without clearing the index leaves cases that
    are retrievable from FAISS but absent from SQLite. The groundedness guardrail
    validates citations against SQLite, so those orphans get reported as FABRICATED
    CITATIONS -- a false hallucination alarm on the exact metric we showcase. The index
    and its SQLite mirror must be reset together or not at all.
    """
    global _store
    with _lock:
        _store = None
    if delete_disk and config.FAISS_DIR.exists():
        import shutil
        shutil.rmtree(config.FAISS_DIR, ignore_errors=True)


def consistency_check() -> dict[str, Any]:
    """Compare the FAISS index against its SQLite mirror.

    Surfaced in the admin Knowledge store panel so drift is visible before a demo
    rather than discovered during one.
    """
    known = db.known_case_ids()
    vectors = index_size()
    try:
        store = load_index(rebuild_if_missing=False)
        indexed = {
            (d.metadata or {}).get("case_id")
            for d in store.docstore._dict.values()  # noqa: SLF001 -- no public accessor
        }
        indexed.discard(None)
    except Exception:
        indexed = set()

    return {
        "vectors": vectors,
        "sqlite_cases": len(known),
        "orphans": sorted(indexed - known),      # in FAISS, missing from SQLite
        "unindexed": sorted(known - indexed),    # in SQLite, missing from FAISS
        "consistent": not (indexed - known),
    }


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #

def transaction_to_query(txn: Transaction, rule_reasons: list[str] | None = None) -> str:
    """Turn a transaction into a fraud-signature query.

    We deliberately query with the *pattern*, not the raw row. Merchant names and exact
    amounts are noise for similarity search; what retrieves useful precedent is the
    shape of the event -- channel, geography, amount band, and which rules fired.
    """
    band = "low" if txn.amount < 200 else "medium" if txn.amount < 1500 else "high"
    parts = [
        f"{txn.channel} transaction in {txn.country} ({txn.region})",
        f"merchant category {txn.merchant_category}",
        f"amount band {band}",
    ]
    if rule_reasons:
        parts.append("signals: " + "; ".join(rule_reasons))
    return ". ".join(parts)


def search(
    query: str, k: int | None = None, outcome: str | None = None
) -> list[dict[str, Any]]:
    """Return the top-k most similar historical cases with similarity scores.

    `outcome` restricts results to 'confirmed_fraud' or 'false_positive' -- used by
    search_balanced() to guarantee the agent sees precedent from both directions.
    """
    k = k or config.RAG_TOP_K
    try:
        store = load_index()
    except Exception:
        return []

    # Embed via the cached path, then search by vector. Going through FAISS's own
    # similarity_search() would embed internally with a live call, which breaks the
    # offline fallback -- see the note on llm.embed_query().
    try:
        vector = llm.embed_query(query)
    except Exception:
        return []

    try:
        if outcome:
            hits = store.similarity_search_with_score_by_vector(
                vector, k=k, filter={"outcome": outcome})
        else:
            hits = store.similarity_search_with_score_by_vector(vector, k=k)
    except Exception:
        return []

    results: list[dict[str, Any]] = []
    for doc, score in hits:
        meta = dict(doc.metadata or {})
        meta["snippet"] = doc.page_content[:400]
        # FAISS returns L2 distance -- smaller is closer. Map to a 0-1 similarity
        # so the UI can show something a human can read.
        meta["similarity"] = round(1.0 / (1.0 + float(score)), 3)
        results.append(meta)
    return results


def search_balanced(query: str, k: int | None = None) -> list[dict[str, Any]]:
    """Retrieve confirmed-fraud AND false-positive precedents separately, then merge.

    Why this exists: plain top-k similarity is biased by whatever the corpus happens to
    contain more of. Ours holds more confirmed-fraud cases than false positives (as would
    any real bank's), so an unfiltered search returns three fraud precedents for almost
    any flagged transaction -- and an agent reasoning faithfully from that evidence
    concludes "fraud" every time. The retrieval, not the model, was the bias.

    Splitting the query by outcome guarantees the agent sees both sides: what this pattern
    looks like when it turned out to be fraud, and what it looks like when it turned out
    to be a legitimate customer. That is how a human analyst actually works a case, and it
    is what makes the false-positive precedents in the corpus reachable at all.
    """
    k = k or config.RAG_TOP_K
    half = max(1, k // 2)

    fraud = search(query, k=half + 1, outcome="confirmed_fraud")
    legit = search(query, k=half + 1, outcome="false_positive")

    # Interleave so the top of the list alternates -- neither side gets primacy.
    merged: list[dict[str, Any]] = []
    for i in range(max(len(fraud), len(legit))):
        if i < len(fraud):
            merged.append(fraud[i])
        if i < len(legit):
            merged.append(legit[i])
    return merged[:k + 1]


def search_for_transaction(
    txn: Transaction, rule_reasons: list[str] | None = None, k: int | None = None
) -> list[dict[str, Any]]:
    return search_balanced(transaction_to_query(txn, rule_reasons), k=k)


def format_precedents(cases: list[dict[str, Any]]) -> str:
    """Render retrieved cases for the analyst agent's prompt.

    The case_id is stated explicitly and prominently, because the model is required to
    cite by ID and we validate those IDs afterwards.
    """
    if not cases:
        return "No similar historical cases found in the knowledge store."

    def block(c: dict[str, Any]) -> str:
        return (
            f"[{c.get('case_id')}] {c.get('title')}\n"
            f"  Region: {c.get('region')} | Channel: {c.get('channel')} | "
            f"Similarity: {c.get('similarity')}\n"
            f"  Patterns: {c.get('pattern_tags')}\n"
            f"  Analyst note: {c.get('analyst_note')}"
        )

    # Grouped and labelled rather than interleaved, so the model cannot skim past the
    # exonerating half. Both headings always appear, even when empty -- an absence of
    # false-positive precedent is itself information.
    fraud = [c for c in cases if c.get("outcome") == "confirmed_fraud"]
    legit = [c for c in cases if c.get("outcome") == "false_positive"]

    out = ["### Cases like this that turned out to be FRAUD"]
    out.append("\n\n".join(block(c) for c in fraud) if fraud
               else "(none retrieved — no close fraud precedent for this pattern)")
    out.append("\n### Cases like this that turned out to be LEGITIMATE (false positives)")
    out.append("\n\n".join(block(c) for c in legit) if legit
               else "(none retrieved — no close false-positive precedent for this pattern)")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# The learning loop
# --------------------------------------------------------------------------- #

def add_case(case: FraudCase) -> None:
    """Add one case to the live index and persist it. This is the learning loop.

    Adds to the in-memory index immediately so the very next transaction can retrieve
    it -- that immediacy is the whole point of the demo beat.
    """
    store = load_index()
    texts, metadatas = _to_documents([case])
    store.add_texts(texts, metadatas=metadatas)
    db.save_fraud_case(case)
    try:
        store.save_local(str(config.FAISS_DIR))
    except Exception:
        pass  # in-memory add already succeeded; persistence is best-effort
    db.audit(
        actor="system", event_type="LEARN", subject_id=case.case_id,
        detail=f"Case indexed into the knowledge store: {case.title}",
        source=case.source, outcome=case.outcome,
    )


def learn_from_alert(
    alert, txn: Transaction, outcome: str, analyst_note: str, analyst: str = "analyst"
) -> FraudCase:
    """Convert a resolved alert into a retrievable precedent.

    Called when an analyst approves or rejects an alert in the admin portal. The
    narrative is written from the transaction facts so it reads like the seeded corpus.
    """
    band = "low" if txn.amount < 200 else "medium" if txn.amount < 1500 else "high"
    verdict = ("confirmed as fraud" if outcome == "confirmed_fraud"
               else "cleared as a false positive")

    case = FraudCase(
        case_id=new_id("CASE"),
        title=f"{txn.merchant_category} {txn.channel} in {txn.country} — {verdict}",
        narrative=(
            f"A {txn.channel} transaction of {txn.amount:,.2f} {txn.currency} at a "
            f"{txn.merchant_category} merchant in {txn.city}, {txn.country} was flagged "
            f"with a risk score of {alert.risk_score}. On review by {analyst}, it was "
            f"{verdict}. {analyst_note}"
        ),
        outcome=outcome,
        pattern_tags=[txn.merchant_category, txn.channel, "analyst_reviewed"],
        region=txn.region,
        channel=txn.channel,
        amount_band=band,
        analyst_note=analyst_note or f"Reviewed and {verdict} by {analyst}.",
        source="learned",
        created_at=now_iso(),
    )
    add_case(case)
    return case


def learned_case_count() -> int:
    return len(db.list_fraud_cases(source="learned"))
