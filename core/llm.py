"""The single gateway to the LLM provider.

Everything goes through `chat()` so that token usage, latency and cost are recorded
automatically for every call. Nothing else in the codebase constructs a model client.

Also implements DEMO_MODE=cached: responses are recorded to disk on live runs and
replayed on cached runs, so the Friday demo survives an API outage or a dead network.
"""

from __future__ import annotations

import hashlib
import json
import time
from functools import lru_cache
from typing import Any

from . import config
from .contracts import LLMTelemetry, new_id, now_iso

# Populated by core.db at import time if available; kept as a hook so this module
# stays import-safe and dependency-free in tests.
_telemetry_sink: Any = None


def set_telemetry_sink(fn) -> None:
    """Register a callable(LLMTelemetry) -> None. core.db wires this up."""
    global _telemetry_sink
    _telemetry_sink = fn


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def _http_client():
    import httpx
    # The lab endpoint sits behind a TLS-intercepting proxy. Verification off is
    # required by the organizer guides; it is a lab constraint, not a design choice.
    return httpx.Client(verify=False, timeout=config.LLM_TIMEOUT_SECONDS)


@lru_cache(maxsize=1)
def get_llm():
    """Chat model. lru_cache so Streamlit reruns and FastAPI requests share one client."""
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        base_url=config.BASE_URL,
        model=config.CHAT_MODEL,
        api_key=config.get_api_key(),
        http_client=_http_client(),
        temperature=config.LLM_TEMPERATURE,
        timeout=config.LLM_TIMEOUT_SECONDS,
        max_retries=0,  # we handle retries explicitly so they show up in the audit log
    )


@lru_cache(maxsize=1)
def get_embeddings():
    """Embedding model.

    check_embedding_ctx_length=False is REQUIRED -- without it langchain does a
    tiktoken-based token count that fails behind this proxy.
    """
    from langchain_openai import OpenAIEmbeddings
    return OpenAIEmbeddings(
        base_url=config.BASE_URL,
        model=config.EMBEDDING_MODEL,
        api_key=config.get_api_key(),
        http_client=_http_client(),
        check_embedding_ctx_length=False,
    )


# --------------------------------------------------------------------------- #
# Response cache (DEMO_MODE=cached)
# --------------------------------------------------------------------------- #

def _cache_key(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}\x00{user}".encode()).hexdigest()[:24]


@lru_cache(maxsize=1)
def _load_cache() -> dict[str, str]:
    if config.CACHED_RESPONSES_PATH.exists():
        try:
            return json.loads(config.CACHED_RESPONSES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_to_cache(key: str, value: str) -> None:
    cache = _load_cache()
    cache[key] = value
    try:
        config.CACHED_RESPONSES_PATH.write_text(
            json.dumps(cache, indent=2), encoding="utf-8"
        )
    except Exception:
        pass  # recording is best-effort; never break a live call over it


def cache_size() -> int:
    return len(_load_cache())


# --------------------------------------------------------------------------- #
# Embedding cache
#
# DEMO_MODE=cached only intercepts chat calls. Retrieval still embeds the query, so
# without this the "offline" fallback dies at the first RAG lookup -- and the citations,
# which are the most compelling part of the demo, silently disappear. We verified this
# by pointing the base URL at a dead port; retrieval returned zero cases.
#
# Query embeddings are cached to disk alongside the chat responses, so a recorded demo
# genuinely runs with no network at all.
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def _load_embed_cache() -> dict[str, list[float]]:
    if config.EMBED_CACHE_PATH.exists():
        try:
            return json.loads(config.EMBED_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_embedding(key: str, vector: list[float]) -> None:
    cache = _load_embed_cache()
    cache[key] = vector
    try:
        config.EMBED_CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass


def embed_cache_size() -> int:
    return len(_load_embed_cache())


def embed_query(text: str) -> list[float]:
    """Embed a retrieval query, using the disk cache when the provider is unreachable.

    Raises LLMUnavailable only when the call fails AND nothing is cached -- callers
    degrade to rules-only rather than crashing.
    """
    key = hashlib.sha256(text.encode()).hexdigest()[:24]
    cached = _load_embed_cache().get(key)

    if config.DEMO_MODE in ("cached", "off"):
        if cached is not None:
            return cached
        if config.DEMO_MODE == "off":
            raise LLMUnavailable("DEMO_MODE=off: embeddings disabled")
        # cached mode with a miss: fall through and try live, then fail cleanly

    try:
        vector = get_embeddings().embed_query(text)
        if config.RECORD_RESPONSES:
            _save_embedding(key, list(vector))
            _load_embed_cache.cache_clear()
        return list(vector)
    except Exception as exc:
        if cached is not None:
            return cached
        raise LLMUnavailable(f"embedding failed and not cached: {exc}") from exc


# --------------------------------------------------------------------------- #
# The one call everything uses
# --------------------------------------------------------------------------- #

def _estimate_tokens(text: str) -> int:
    """~4 chars per token. Only used when the provider omits usage metadata."""
    return max(1, len(text) // 4)


def _extract_usage(response, system: str, user: str) -> tuple[int, int]:
    usage = getattr(response, "usage_metadata", None) or {}
    prompt_tokens = usage.get("input_tokens") or 0
    completion_tokens = usage.get("output_tokens") or 0

    if not prompt_tokens:
        meta = getattr(response, "response_metadata", {}) or {}
        tu = meta.get("token_usage") or meta.get("usage") or {}
        prompt_tokens = tu.get("prompt_tokens") or 0
        completion_tokens = tu.get("completion_tokens") or 0

    if not prompt_tokens:
        prompt_tokens = _estimate_tokens(system + user)
    if not completion_tokens:
        completion_tokens = _estimate_tokens(str(getattr(response, "content", "")))
    return int(prompt_tokens), int(completion_tokens)


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    return (
        prompt_tokens / 1_000_000 * config.COST_PER_1M_PROMPT
        + completion_tokens / 1_000_000 * config.COST_PER_1M_COMPLETION
    )


class LLMUnavailable(RuntimeError):
    """Raised when the model cannot be reached and no cached response exists."""


def chat(
    system: str,
    user: str,
    *,
    agent: str = "unknown",
    temperature: float | None = None,
    cache_key: str | None = None,
) -> tuple[str, LLMTelemetry]:
    """Send one system+user exchange and return (text, telemetry).

    Honours DEMO_MODE:
      live   -- real call, and records the response for later replay
      cached -- replay only; raises LLMUnavailable on a miss
      off    -- always raises LLMUnavailable (callers fall back to rules only)

    On a live call that fails, falls back to the cache before giving up. That
    fallback is what makes an API outage mid-demo survivable.

    `cache_key` supplies a STABLE semantic key instead of hashing the prompt. This
    matters more than it looks: every injected transaction carries a fresh txn_id and
    timestamp, so a prompt hash changes on every single call and the cache would never
    hit -- the offline fallback would be silently useless exactly when it is needed.
    Callers pass a key describing the *scenario* rather than the instance.
    """
    key = cache_key or _cache_key(system, user)
    started = time.perf_counter()

    if config.DEMO_MODE == "off":
        raise LLMUnavailable("DEMO_MODE=off: LLM calls are disabled")

    if config.DEMO_MODE == "cached":
        cached = _load_cache().get(key)
        if cached is None:
            raise LLMUnavailable(
                f"DEMO_MODE=cached but no recorded response for {agent}. "
                "Run the demo once with DEMO_MODE=live to record it."
            )
        tel = _record(agent, 0, 0, int((time.perf_counter() - started) * 1000), cached=True)
        return cached, tel

    # --- live ---
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = get_llm()
        if temperature is not None:
            llm = llm.bind(temperature=temperature)
        response = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        text = str(response.content)
        latency_ms = int((time.perf_counter() - started) * 1000)
        pt, ct = _extract_usage(response, system, user)

        if config.RECORD_RESPONSES:
            _save_to_cache(key, text)
            _load_cache.cache_clear()

        tel = _record(agent, pt, ct, latency_ms)
        return text, tel

    except Exception as exc:
        # Last line of defence: if we recorded this exact prompt before, replay it.
        cached = _load_cache().get(key)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if cached is not None:
            _record(agent, 0, 0, latency_ms, cached=True,
                    error=f"live call failed, replayed from cache: {exc}")
            return cached, _record(agent, 0, 0, latency_ms, cached=True)
        _record(agent, 0, 0, latency_ms, error=str(exc))
        raise LLMUnavailable(f"{agent}: {exc}") from exc


def _record(
    agent: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    *,
    cached: bool = False,
    error: str | None = None,
) -> LLMTelemetry:
    tel = LLMTelemetry(
        call_id=new_id("LLM"),
        timestamp=now_iso(),
        agent=agent,
        model=config.CHAT_MODEL,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        est_cost_usd=estimate_cost(prompt_tokens, completion_tokens),
        cached=cached,
        error=error,
    )
    if _telemetry_sink is not None:
        try:
            _telemetry_sink(tel)
        except Exception:
            pass  # telemetry must never break the request path
    return tel


# --------------------------------------------------------------------------- #
# Connectivity check -- used by the Admin health panel and the setup smoke test
# --------------------------------------------------------------------------- #

def health_check() -> dict[str, Any]:
    result: dict[str, Any] = {
        "mode": config.DEMO_MODE,
        "base_url": config.BASE_URL,
        "chat_model": config.CHAT_MODEL,
        "embedding_model": config.EMBEDDING_MODEL,
        "api_key_present": config.has_api_key(),
        "cached_responses": cache_size(),
        "llm_ok": False,
        "embeddings_ok": False,
        "error": None,
    }
    if config.DEMO_MODE == "cached":
        result["llm_ok"] = cache_size() > 0
        result["embeddings_ok"] = config.FAISS_DIR.exists()
        return result
    try:
        text, _ = chat("You are a health check.", "Reply with exactly: OK", agent="health")
        result["llm_ok"] = bool(text)
    except Exception as exc:
        result["error"] = str(exc)[:300]
    try:
        get_embeddings().embed_query("health check")
        result["embeddings_ok"] = True
    except Exception as exc:
        if result["error"] is None:
            result["error"] = str(exc)[:300]
    return result


if __name__ == "__main__":
    print(json.dumps(health_check(), indent=2))
