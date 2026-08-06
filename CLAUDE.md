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
- [ ] **← YOU ARE HERE: set the API key, then `python -m core.seed --index-only`**
- [ ] Verify LLM path: citations, groundedness, the A/B improvement
- [ ] Record `DEMO_MODE=cached` responses, test with Wi-Fi off
- [ ] `docs/DEMO_SCRIPT.md`, slide deck
- [ ] Demo rehearsed 3× under 10:00, screen recording captured

### Measured so far (rules only, no LLM yet)

| | |
|---|---|
| Transactions needing no model | **96.7%** (1,934 / 2,000) — the cost argument |
| Rule separation | fraud mean score **89.0** vs legitimate **2.0** |
| Eval recall | **100%** (10/10 fraud caught) |
| Eval false-positive rate | **25%** ← rules alone over-flag recurring large payments |

That 25% FPR is not a bug to hide — it is the baseline the AI layer has to beat, and
`python -m core.evaluation --compare` is what proves it does. Run that once the key is
set; the delta is the strongest slide in the deck.
