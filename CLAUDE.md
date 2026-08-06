# CLAUDE.md — SentinelBank

> **Read this first.** This file is the single source of truth for this project. If you are a
> fresh Claude Code / Cline session on any machine, everything you need is here plus
> [docs/PLAN.md](docs/PLAN.md). Do not re-derive the architecture — follow it.

---

## What we are building

**SentinelBank** — an AI-powered banking customer query resolution and fraud alert system, for the
**TCS AI Friday Season 2 Regional Finale**. Built in one day (Thursday), presented Friday in 10 minutes.

The spec is `Use Case.pdf`:
- Multi-agent conversational modules that categorize and route customer queries
- Real-time fraud detection with alert prioritization
- Advanced UI with a **supervisor dashboard**, authentication, multi-source data aggregation
- Success metrics: query resolution accuracy, fraud response time reduction, uptime >99%

**Page 2 of `Use Case.pdf` is the judging rubric and the real spec.** Six scored sections:
UX/Interface · Data Architecture · Core AI & Agentic Architecture · Technical Implementation ·
**Testing & QA** · Demo Readiness. The last two are where we differentiate — most teams skip them.

---

## ⚠️ Environment gotchas (these will waste your time if you skip them)

**1. The LangChain stack must be pinned to 0.3.x.** The machine originally had
`langchain-openai 1.1.11` against `langchain-core 0.3.86`, which raises
`ImportError: cannot import name 'ContextOverflowError'`. Both organizer guides are 0.3-era code.

```bash
pip install -r requirements.txt
```

Verify before doing anything else:
```bash
python -c "from langchain_openai import ChatOpenAI, OpenAIEmbeddings; from langchain_community.vectorstores import FAISS; print('STACK OK')"
```

**2. The LLM endpoint is TCS-internal and needs SSL verification disabled.**

| | |
|---|---|
| Base URL | `https://genailab.tcs.in` (OpenAI-compatible) |
| Chat model | `azure/genailab-maas-gpt-4.1` |
| Embeddings | `azure/genailab-maas-text-embedding-3-large` |
| Required | `http_client=httpx.Client(verify=False)` |
| Required on embeddings | `check_embedding_ctx_length=False` (avoids a tiktoken round-trip that fails behind the proxy) |

All of this is already handled in `core/llm.py`. **Use `get_llm()` / `get_embeddings()` — never construct clients inline.**

**3. Secrets.** The API key goes in `.streamlit/secrets.toml` (gitignored) or env var `GENAILAB_API_KEY`.
Get your key from `APIKey.xlsx`. **Never hardcode it.** Note: `Creating_RAG.pdf` page 3 contains a live
hardcoded key — that is an anti-pattern we deliberately do not copy.

**4. SQLite runs in WAL mode** because Streamlit and FastAPI both write to it. Already set in `core/db.py`.

---

## Architecture

```
Streamlit app.py (Customer · Analyst · Admin portals)  ──┐
FastAPI api/main.py  POST /api/transactions ────────────┤
                                                        ▼
                                          core/pipeline.py :: score_transaction()
   ① PII tokenization vault      core/security.py   raw PII never reaches the LLM
   ② deterministic rules         core/rules.py      6 rules → score + hits
   ③ travel-notice suppression   core/travel.py     kills geo false positives
   ④ cheap path: score <30 or >90 decides with NO LLM CALL  (~94% of volume)
   ⑤ RAG retrieval               core/rag.py        FAISS, top-k historical cases
   ⑥ fraud analyst agent         core/agents/fraud_analyst.py  JSON verdict + citations
   ⑦ guardrails                  core/security.py   schema · groundedness · DLP · injection
   ⑧ decision router             ALLOW / CHALLENGE / FREEZE+ESCALATE → human approval
                                                        ▼
                     SQLite (core/db.py) + FAISS index (data/faiss/)
                                                        ▼
   ★ LEARNING LOOP: analyst resolves an alert → narrative embedded → added to FAISS →
     the next similar transaction is caught citing the case just resolved.
```

### Design decisions — do not silently change these

| Decision | Why |
|---|---|
| **Streamlit for UI, not Flask/React** | Both organizer guides are Streamlit; chat, dataframes, charts, forms are one-liners. Team has mixed coding experience. |
| **FastAPI kept, but thin (~80 lines)** | It is the live-demo injection mechanism (POST from a phone → dashboard lights up) and it makes the "separated layers" rubric claim true. All logic stays in `core/`. |
| **Explicit Python orchestration, not LangGraph** | Regulated domain: a traceable, auditable, deterministic control flow beats a framework graph. Also removes an install risk. Say this out loud in the pitch. |
| **Rules run before the LLM** | ~94% of transactions never hit the LLM. This is the entire cost-effectiveness argument, and it is measured live in the Admin cost meter. |
| **Only ~80 fraud-case narratives in FAISS, not all transactions** | Small index = fast, cheap, and visibly relevant retrieval on stage. |
| **PII tokenization, not masking** | `4532...` → `<PAN_7f3a>`. The model still reasons about "the same card" without ever seeing it. |
| **JSON parsed with a repair-retry, not `response_format`** | JSON mode is unreliable through this proxy. The retry doubles as our rubric "retry logic" evidence. |

---

## Repo layout & file ownership

Nobody edits another person's files. This is what keeps 5 AI-assisted sessions from colliding.

| Owner | Files |
|---|---|
| **A** Integrator | `core/contracts.py` `config.py` `db.py` `llm.py` `security.py` `pipeline.py` · `api/main.py` · `app.py` · **only A merges to main** |
| **B** Fraud+RAG | `core/rules.py` `rag.py` `travel.py` `agents/fraud_analyst.py` `evaluation.py` |
| **C** Customer portal | `ui/customer.py` · `core/agents/router.py` `customer_agent.py` `tools.py` |
| **D** Admin portal | `ui/admin.py` `demo_control.py` `components.py` |
| **E** Data/docs/deck | `data/*` `docs/*` · slides · demo script · manual QA |

**Before asking an AI to write any file, paste `core/contracts.py` into the session as context.**
That single habit is what makes independently-generated code fit together.

---

## Conventions

- `core/contracts.py` is **frozen**. Changing it means telling all 5 people. Everything is a `@dataclass`.
- Every module in `core/` is import-safe with no side effects — no DB or network calls at import time.
- Every LLM call goes through `core/llm.py` so token/latency telemetry is recorded automatically.
- Every decision, tool call, and guardrail trip writes to `audit_log`. No exceptions — it is a rubric item.
- IDs: `TXN-xxxxxx`, `CASE-xxxx`, `ALERT-xxxx`, `CUST-xxxx`.
- Timestamps: ISO 8601 strings in UTC.
- `DEMO_MODE=cached` replays recorded LLM responses so the demo survives an API outage.

Run things with:
```bash
streamlit run app.py                    # the app
uvicorn api.main:app --port 8000        # the ingestion API
python -m core.pipeline --selftest      # end-to-end check
python -m core.evaluation               # the eval harness
```

---

## BUILD STATUS

Update this section as you go. It is how a new session knows where to pick up.

**Everything below is built and verified EXCEPT the LLM-dependent paths, which are
blocked on an API key.** The whole app runs today with `DEMO_MODE=off` (rules only).

- [x] Repo scaffolding, `CLAUDE.md`, `docs/PLAN.md`
- [x] Dependency stack pinned to 0.3.x and verified (`STACK OK`)
- [x] `core/contracts.py` — frozen schemas
- [x] `core/config.py`, `core/db.py` (WAL), `core/llm.py` (telemetry + cached mode)
- [x] `core/security.py` — PII vault, DLP, injection scan · **tested**
- [x] `data/generate.py` → 200 customers, 2,000 txns, 51 precedents, 30 eval cases
- [x] `core/rules.py` (10 rules), `core/travel.py` — **96.7% of txns need no LLM**
- [x] `core/rag.py` — FAISS build/query/`add_case` (the learning loop)
- [x] `core/agents/` — fraud_analyst, router, customer_agent, 8 tools w/ approval gating
- [x] `core/pipeline.py` — full orchestration · `--selftest` **PASSES**
- [x] `ui/customer.py`, `ui/admin.py`, `ui/demo_control.py`, `app.py` + auth/RBAC
- [x] `api/main.py` — FastAPI ingestion
- [x] `core/evaluation.py` — eval harness **+ rules-only vs agentic A/B comparison**
- [x] API key configured; LLM **and** embeddings verified live
- [x] FAISS index built (90 cases); citations resolve and are validated
- [x] Balanced retrieval + asymmetric blend (see "Two defects the harness caught")
- [x] Eval set hardened: 8 cases mirroring real production false positives
- [x] `core/record_demo.py` — offline cache recorder + verifier
- [x] `docs/DEMO_SCRIPT.md`, `docs/ARCHITECTURE.md`
- [x] Offline fallback verified against a dead endpoint — all 7 beats, citations intact
- [x] Full UI walkthrough in a browser; three demo-breaking bugs found and fixed
- [ ] **← NEXT: slide deck** (docs/ARCHITECTURE.md headings map to slides)
- [ ] Demo rehearsed 3× under 10:00, screen recording captured
- [ ] Re-run `python -m core.record_demo` after any prompt/scenario change

### Three bugs the browser walkthrough caught that CLI tests could not

1. **Nested expander crash.** `decision_card()` opened an expander inside the alert
   queue's expander. Streamlit raises `StreamlitAPIException` and silently drops every
   widget after it — the **Confirm fraud / False positive buttons never rendered**, killing
   the human-in-the-loop beat and the learning-loop climax. Fixed with `use_expander=False`.

2. **Cost meter inverted.** On a clean reseed it read *"2 transactions scored, 0% resolved
   without a model"* with a negative saving. Seeding never scored anything, cost came from
   unscoped telemetry, and "avoided" counted `llm_used` rather than rule score.

3. **`width="stretch"` doesn't exist in Streamlit 1.45.1** (20 call sites). It throws, and
   on a form it means no submit button — login was impossible.

### Stateful demo traps (both are pre-flight checklist items)

- Injecting fraud **freezes the hero's card**. `CARD_ALREADY_FROZEN` is +60, so every later
  transaction scores ~100 and each beat looks like fraud. Click **Unfreeze hero card**
  between rehearsals.
- A reseed **wipes the travel notice**. Without it the Spain beat isn't testing suppression
  at all. Click **File travel notice (Spain)** before running the demo.

### Two defects the evaluation harness caught (both fixed)

Worth knowing, because both are invisible in a demo and both would have been asked about.

**1. Retrieval was biased toward fraud.** Plain top-k similarity returned three
confirmed-fraud precedents for almost any flagged transaction — the corpus holds more
fraud cases than false positives, as any real bank's would. The agent reasoned faithfully
from one-sided evidence and concluded "fraud" every time. `rag.search_balanced()` now
queries both outcomes separately and merges.

**2. The model piled on instead of exonerating.** Measured: a symmetric blend left recall
at 100% but pushed the average risk score on legitimate customers from 19.1 to 24.9 and
blocked one the rules had allowed. `pipeline._blend()` is now asymmetric — the model's
incriminating opinion carries 0.25, its exculpatory opinion 0.65. Rules are already
excellent at finding risk and structurally incapable of exoneration, since they only ever
add points.

### Measured

| | |
|---|---|
| Transactions needing no model | **96.0%** (1,966 / 2,047) — the cost argument |
| Cost per model-scored transaction | **$0.00757** (measured tokens, gpt-4.1 rates) |
| Projected cost | **$0.61** rules-first vs **$15.50** all-model — 96% saving |
| Rule separation | fraud mean **89.0** vs legitimate **2.0** |
| Recall | **100%** (rules alone, and with the agent) |
| Legitimate customers blocked | **0** — before and after the agent |
| Legitimate customers challenged | **12 → 9** with the agent |
| Groundedness | **100%** — no fabricated citations |
| Hard eval cases that defeat rules alone | **7 of 8**; agent resolves 3 |
| p95 latency | ~3.4 s model calls · ~9 ms cheap path |

**Be honest about the A/B in the pitch.** The rules are strong enough that the model adds
little raw detection accuracy. Its real value is the explanation an analyst needs to act,
the cited precedent that makes it auditable, and the learning loop. Saying that — and
showing you measured it — beats claiming an improvement you cannot evidence.
