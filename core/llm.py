"""The single gateway to the LLM provider.

Everything goes through `chat()` so that token usage, latency and cost are recorded
automatically for every call. Nothing else in the codebase constructs a model client.

Also implements DEMO_MODE=cached: responses are recorded to disk on live runs and
replayed on cached runs, so the Friday demo survives an API outage or a dead network.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from functools import lru_cache
from typing import Any

from . import config
from .contracts import LLMTelemetry, new_id, now_iso

# Populated by core.db at import time if available; kept as a hook so this module
# stays import-safe and dependency-free in tests.
_telemetry_sink: Any = None

# How many calls the secondary provider has served this process. Surfaced in
# health_check() so "are we still on the primary?" is answerable at a glance on stage.
_fallback_calls: int = 0


def fallback_calls() -> int:
    return _fallback_calls


# --------------------------------------------------------------------------- #
# Primary circuit breaker
#
# A dead endpoint costs a full timeout on EVERY call, and the fallback behind it is
# worthless if reaching it takes 20 seconds. Measured on a dead primary: 17-26s per
# pipeline beat, every answer correct, demo ruined. Two consecutive failures now open
# the circuit for a minute; calls skip the primary entirely until it closes, and one
# probe reopens it so a recovered endpoint is picked up without a restart.
# --------------------------------------------------------------------------- #
PRIMARY_FAILURE_THRESHOLD = 2
PRIMARY_COOLDOWN_SECONDS = float(os.getenv("PRIMARY_COOLDOWN", "60"))

_primary_failures: int = 0
_primary_open_until: float = 0.0


def _primary_available() -> bool:
    return time.monotonic() >= _primary_open_until


def _circuit_remaining() -> float:
    return max(0.0, _primary_open_until - time.monotonic())


def _note_primary_failure() -> None:
    global _primary_failures, _primary_open_until
    _primary_failures += 1
    if _primary_failures >= PRIMARY_FAILURE_THRESHOLD:
        _primary_open_until = time.monotonic() + PRIMARY_COOLDOWN_SECONDS


def _note_primary_success() -> None:
    """Any success closes the circuit. A recovered endpoint should be trusted again
    immediately -- we are protecting against dead air, not rationing requests."""
    global _primary_failures, _primary_open_until
    _primary_failures = 0
    _primary_open_until = 0.0


def reset_circuit() -> None:
    """Force the primary back into play. Wired to the pre-flight check, so a presenter
    who sees the endpoint recover does not have to restart the app."""
    _note_primary_success()


def circuit_state() -> dict[str, Any]:
    return {
        "primary_open": not _primary_available(),
        "consecutive_failures": _primary_failures,
        "reopens_in_seconds": round(_circuit_remaining()),
    }


def set_telemetry_sink(fn) -> None:
    """Register a callable(LLMTelemetry) -> None. core.db wires this up."""
    global _telemetry_sink
    _telemetry_sink = fn


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def _http_client():
    """Client for the PRIMARY, on the short budget.

    The lab endpoint sits behind a TLS-intercepting proxy. Verification off is required
    by the organizer guides; it is a lab constraint, not a design choice.

    The timeout here is what actually bounds the wait -- `ChatOpenAI(timeout=...)` is
    passed too, but when an explicit `http_client` is supplied it is httpx that enforces
    it. Both are set so neither can be the thing that was forgotten.
    """
    import httpx
    return httpx.Client(verify=False, timeout=config.PRIMARY_TIMEOUT_SECONDS)


@lru_cache(maxsize=1)
def _fallback_http_client():
    """Client for the SECONDARY, on the longer budget.

    A separate client, not the primary's: they previously shared one, which meant the two
    stages could never have different timeouts -- and the whole point of a failover is
    that the first stage gives up quickly and the second one is allowed to take its time,
    because by then it is the only thing that can still answer.
    """
    import httpx
    return httpx.Client(verify=False, timeout=config.FALLBACK_TIMEOUT_SECONDS)


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
        timeout=config.PRIMARY_TIMEOUT_SECONDS,
        max_retries=0,  # we handle retries explicitly so they show up in the audit log
    )


@lru_cache(maxsize=1)
def get_fallback_llm():
    """Secondary chat model, used only when the primary fails.

    Same OpenAI-compatible interface, so nothing above this line changes. `verify=False`
    for the same reason as the primary: this machine sits behind a TLS-intercepting
    corporate proxy, and without it every outbound HTTPS call raises
    CERTIFICATE_VERIFY_FAILED regardless of provider.
    """
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        base_url=config.FALLBACK_BASE_URL,
        model=config.FALLBACK_CHAT_MODEL,
        api_key=config.get_fallback_api_key(),
        http_client=_fallback_http_client(),
        temperature=config.LLM_TEMPERATURE,
        timeout=config.FALLBACK_TIMEOUT_SECONDS,
        max_retries=0,
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

    # Embeddings share the primary's circuit breaker: same host, same outage. This is
    # what actually dominates a dead-endpoint demo -- `rag.search_balanced` embeds twice
    # per transaction, so a 20s timeout each turned every beat into a 15-25s wait even
    # after the chat path had been protected. There is no secondary here on purpose:
    # Groq serves no embedding model, so retrieval survives on the disk cache alone.
    if not _primary_available():
        if cached is not None:
            return cached
        raise LLMUnavailable(
            f"primary circuit open ({_circuit_remaining():.0f}s) and query not cached"
        )

    try:
        vector = get_embeddings().embed_query(text)
        _note_primary_success()
        if config.RECORD_RESPONSES:
            _save_embedding(key, list(vector))
            _load_embed_cache.cache_clear()
        return list(vector)
    except Exception as exc:
        _note_primary_failure()
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
    # Circuit breaker. Once the primary has failed twice in a row we stop calling it for
    # a minute and go straight to cache or fallback. Without this every call still pays
    # the full 20s timeout before recovering: the selftest measured 17-26s per beat with
    # the primary dead, which is a destroyed demo even though every answer was correct.
    if not _primary_available():
        return _after_primary_failed(
            system, user, agent, temperature, key, started,
            RuntimeError(f"primary circuit open, {_circuit_remaining():.0f}s remaining"),
        )

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

        _note_primary_success()
        tel = _record(agent, pt, ct, latency_ms)
        return text, tel

    except Exception as exc:
        _note_primary_failure()
        return _after_primary_failed(system, user, agent, temperature, key, started, exc)


def _after_primary_failed(system: str, user: str, agent: str,
                          temperature: float | None, key: str, started: float,
                          exc: Exception) -> tuple[str, LLMTelemetry]:
    """The stages after the primary has failed or been skipped:

        2. the recorded cache   -- instant (~40ms), deterministic, no network at all
        3. a secondary provider -- live, handles anything, needs network
        4. raise LLMUnavailable -- every caller treats this as "answer from the database"

    Stage 4 is not a crash. `customer_agent` falls through to a read-only tool scoped to
    the caller and says plainly that it is answering from records; `pipeline` keeps the
    deterministic rule score and notes that the model was unreachable. So the chain always
    terminates in an answer, never in an error page.

    Cache before fallback is deliberate, and worth keeping even though "primary then
    secondary then offline" is the more natural way to say it. A scripted demo beat
    replays in ~40ms with exactly the wording that was rehearsed; routing it to a
    different model instead would be slower AND might phrase the climax differently in
    front of judges. The secondary provider is for what the cache cannot cover -- an
    improvised question -- which is precisely the case where a fresh answer beats a stale
    one. So the ordering is not primary/secondary/offline by accident; it is
    "instant and rehearsed" before "live and improvised".

    Bounded by config.PRIMARY_TIMEOUT_SECONDS: the primary gets a short budget so a dead
    endpoint cannot hold a beat open, and the circuit breaker means that budget is paid at
    most twice before the primary is skipped outright for a minute.
    """
    cached = _load_cache().get(key)
    latency_ms = int((time.perf_counter() - started) * 1000)
    if cached is not None:
        return cached, _record(agent, 0, 0, latency_ms, cached=True,
                               error=f"primary unavailable, replayed from cache: {exc}")

    if config.has_fallback():
        try:
            return _chat_fallback(system, user, agent, temperature, key, started)
        except Exception as fb_exc:
            _record(agent, 0, 0, int((time.perf_counter() - started) * 1000),
                    error=f"primary: {exc} | fallback: {fb_exc}")
            raise LLMUnavailable(
                f"{agent}: primary and fallback both failed "
                f"({config.FALLBACK_PROVIDER_NAME}: {fb_exc})"
            ) from fb_exc

    _record(agent, 0, 0, latency_ms, error=str(exc))
    raise LLMUnavailable(f"{agent}: {exc}") from exc


def _chat_fallback(system: str, user: str, agent: str, temperature: float | None,
                   key: str, started: float) -> tuple[str, LLMTelemetry]:
    """Run the same exchange against the secondary provider.

    A success is still recorded to the cache under the primary's key, so the next outage
    replays it without needing either provider. The fallback earns its keep once and the
    answer is ours from then on.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = get_fallback_llm()
    if temperature is not None:
        llm = llm.bind(temperature=temperature)
    response = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    text = str(response.content)
    latency_ms = int((time.perf_counter() - started) * 1000)
    pt, ct = _extract_usage(response, system, user)

    if config.RECORD_RESPONSES:
        _save_to_cache(key, text)
        _load_cache.cache_clear()

    # Counted here rather than written to the audit log directly: `core/db.py` imports
    # this module to install the telemetry sink, so importing db from here would be a
    # cycle. The telemetry row carries the provider in its `error` field and is persisted
    # through that sink, so the audit trail is complete either way.
    global _fallback_calls
    _fallback_calls += 1

    tel = _record(agent, pt, ct, latency_ms,
                  error=f"served by fallback provider ({config.FALLBACK_PROVIDER_NAME})")
    return text, tel


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
        "fallback_configured": config.has_fallback(),
        "fallback_provider": (config.FALLBACK_PROVIDER_NAME if config.has_fallback()
                              else None),
        "fallback_model": (config.FALLBACK_CHAT_MODEL if config.has_fallback() else None),
        "fallback_calls": fallback_calls(),
        # How much dead air a failing primary can cost, so it is answerable on stage
        # rather than guessed at.
        "primary_timeout_s": config.PRIMARY_TIMEOUT_SECONDS,
        "fallback_timeout_s": config.FALLBACK_TIMEOUT_SECONDS,
    }
    if config.DEMO_MODE == "cached":
        result["llm_ok"] = cache_size() > 0
        result["embeddings_ok"] = config.FAISS_DIR.exists()
        return result
    try:
        # A unique nonce so the probe can never be answered from the cache. Without it
        # the recorded "Reply with exactly: OK" replays and health_check reports the
        # primary as healthy while it is refusing every call -- which is the one thing
        # a health check must never do.
        text, tel = chat("You are a health check.",
                         f"Reply with exactly: OK  [{new_id('HC')}]", agent="health")
        result["llm_ok"] = bool(text) and not tel.cached
        result["chat_served_by"] = (
            "cache" if tel.cached
            else config.FALLBACK_PROVIDER_NAME if (tel.error or "").startswith("served by")
            else "primary"
        )
    except Exception as exc:
        result["error"] = str(exc)[:300]
        result["chat_served_by"] = "nothing — all providers failed"
    try:
        get_embeddings().embed_query("health check")
        result["embeddings_ok"] = True
        _note_primary_success()
    except Exception as exc:
        # Counts toward the breaker on purpose. Together with the chat probe above, a
        # health check against a dead primary trips the circuit -- so running pre-flight
        # before the demo means the FIRST scripted beat is already fast, instead of
        # paying a 23s timeout to discover what pre-flight just found out.
        _note_primary_failure()
        if result["error"] is None:
            result["error"] = str(exc)[:300]

    # Re-read after the probes: both are mutated by the calls above, and the values
    # captured when the dict was built are already stale by one call.
    result["circuit"] = circuit_state()
    result["fallback_calls"] = fallback_calls()
    result["primary_ok"] = result.get("chat_served_by") == "primary"
    return result


if __name__ == "__main__":
    print(json.dumps(health_check(), indent=2))
