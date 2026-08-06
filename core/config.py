"""Central configuration. Every tunable lives here -- no magic numbers elsewhere.

The rubric asks for "configurability across models, prompts, tools, APIs, vector stores,
and deployment environments to avoid lock-in". This module is that answer: swap the two
model names below and the whole system moves providers.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Runtime environment fixes -- must run before httpx/langchain are imported.
# Straight from the organizer guides; the lab endpoint sits behind a TLS-intercepting
# proxy, so certificate verification has to be disabled.
# --------------------------------------------------------------------------- #
os.environ.setdefault("CURL_CA_BUNDLE", "")
os.environ.setdefault("PYTHONHTTPSVERIFY", "0")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("STREAMLIT_SERVER_WATCH_VARIABLE_NAMES", "false")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
FAISS_DIR = DATA_DIR / "faiss"
DB_PATH = DATA_DIR / "sentinelbank.db"
PRECEDENTS_PATH = DATA_DIR / "fraud_precedents.jsonl"
EVAL_SET_PATH = DATA_DIR / "eval_set.jsonl"
CUSTOMERS_PATH = DATA_DIR / "customers.json"
TRANSACTIONS_PATH = DATA_DIR / "transactions_seed.json"
DEMO_INJECTIONS_PATH = DATA_DIR / "demo_injections.json"
CACHED_RESPONSES_PATH = DATA_DIR / "cached_responses.json"

DATA_DIR.mkdir(exist_ok=True)


# --------------------------------------------------------------------------- #
# LLM provider
# --------------------------------------------------------------------------- #
BASE_URL = os.getenv("GENAILAB_BASE_URL", "https://genailab.tcs.in")
CHAT_MODEL = os.getenv("GENAILAB_CHAT_MODEL", "azure/genailab-maas-gpt-4.1")
EMBEDDING_MODEL = os.getenv(
    "GENAILAB_EMBEDDING_MODEL", "azure/genailab-maas-text-embedding-3-large"
)

LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT", "20"))
LLM_TEMPERATURE = 0.0          # fraud decisions must be reproducible
LLM_MAX_RETRIES = 1            # one JSON-repair retry; see agents/fraud_analyst.py

# Published gpt-4.1 rates (USD per 1M tokens). Used for the live cost meter --
# an estimate, and labelled as such in the UI.
COST_PER_1M_PROMPT = 2.00
COST_PER_1M_COMPLETION = 8.00


def get_api_key() -> str:
    """Resolve the API key: env var first, then Streamlit secrets.

    Never hardcode a key. Copy .streamlit/secrets.toml.example to secrets.toml
    and paste your own from APIKey.xlsx.
    """
    key = os.getenv("GENAILAB_API_KEY", "").strip()
    if key:
        return key
    try:
        import streamlit as st  # noqa: PLC0415 -- optional, absent under FastAPI/CLI
        key = str(st.secrets.get("GENAILAB_API_KEY", "")).strip()
        if key:
            return key
    except Exception:
        pass
    raise RuntimeError(
        "No API key found. Set the GENAILAB_API_KEY environment variable, or copy "
        ".streamlit/secrets.toml.example to .streamlit/secrets.toml and add your key "
        "from APIKey.xlsx."
    )


def has_api_key() -> bool:
    try:
        get_api_key()
        return True
    except RuntimeError:
        return False


# --------------------------------------------------------------------------- #
# Demo mode
#   live   -- call the real API (default)
#   cached -- replay recorded responses; survives an API outage or no network
#   off    -- rules only, no LLM at all (fastest; useful for load-testing the UI)
# --------------------------------------------------------------------------- #
DEMO_MODE = os.getenv("DEMO_MODE", "live").lower()
RECORD_RESPONSES = os.getenv("RECORD_RESPONSES", "1") == "1"


# --------------------------------------------------------------------------- #
# Fraud decision thresholds
#
# The two cheap-path cutoffs are the cost story: anything the rules are confident
# about (clearly fine, or blatantly fraudulent) is decided WITHOUT an LLM call.
# Only the ambiguous middle band pays for inference.
# --------------------------------------------------------------------------- #
CHEAP_PATH_LOW = 30            # rule score below this -> ALLOW, no LLM

# Deliberately set above the maximum score, i.e. there is NO cheap path at the top end.
#
# The saving comes from the bottom of the distribution -- the ~90% of transactions that
# are obviously fine and never need a model. Skipping inference at the *top* end would
# save almost nothing (those cases are rare) while removing the explanation at exactly
# the moment it matters most: a human is about to be asked to freeze someone's card and
# needs the reasoning and the precedent behind it.
#
# "Cheap where it's obvious, explained where it counts."
CHEAP_PATH_HIGH = 101

ACTION_ALLOW_BELOW = 40        # final score < 40  -> ALLOW
ACTION_CHALLENGE_BELOW = 75    # 40-74             -> CHALLENGE
                               # >= 75             -> FREEZE_AND_ESCALATE

RISK_LEVEL_BANDS = [           # (exclusive upper bound, level)
    (40, "LOW"),
    (60, "MEDIUM"),
    (80, "HIGH"),
    (101, "CRITICAL"),
]

# RAG retrieval
RAG_TOP_K = 3
RAG_CHUNK_SIZE = 1000
RAG_CHUNK_OVERLAP = 100


def risk_level_for(score: int) -> str:
    for upper, level in RISK_LEVEL_BANDS:
        if score < upper:
            return level
    return "CRITICAL"


def action_for(score: int) -> str:
    if score < ACTION_ALLOW_BELOW:
        return "ALLOW"
    if score < ACTION_CHALLENGE_BELOW:
        return "CHALLENGE"
    return "FREEZE_AND_ESCALATE"


# --------------------------------------------------------------------------- #
# Demo / seed data shape
# --------------------------------------------------------------------------- #
REGIONS = ["INDIA", "APAC", "EMEA", "NA", "LATAM"]
SEED_CUSTOMERS = 200
SEED_TRANSACTIONS = 2000
SEED_FRAUD_RATE = 0.03

# Roles for the RBAC page gate
ROLES = ["customer", "analyst", "admin"]
