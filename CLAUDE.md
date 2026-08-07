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

**4. SQLite runs in WAL mode** because the web app, the ingestion endpoint and the eval
harness all write to it. Already set in `core/db.py`.

---

## Architecture

```
Flask web/ (Customer · Analyst · Admin portals + chat agent)  ──┐
POST /api/transactions   (Flask web/routes/ingest.py           │
                          or FastAPI api/main.py) ─────────────┤
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
   ⑨ notification            core/notifications.py  toast + centre + real .eml outbox
                                                        ▼
   ★ LEARNING LOOP: analyst resolves an alert → narrative embedded → added to FAISS →
     the next similar transaction is caught citing the case just resolved.
```

### Design decisions — do not silently change these

| Decision | Why |
|---|---|
| **Flask + hand-written HTML/CSS/JS, not Streamlit** | Streamlit was the right first call — one-liner widgets, mixed-experience team — but it caps how the product can look, and the judging rubric scores UX. `core/` was already UI-agnostic, so the swap cost nothing architecturally and buys a real drill-down interaction Streamlit cannot express. Say it in the pitch: *we replaced the entire presentation layer without touching a rule, an agent, or the pipeline.* |
| **No chart library, no CDN** | Charts are hand-drawn SVG in `web/static/js/charts.js`. A CDN would put the demo one wifi failure away from an unstyled page, and offline survival is a stated requirement. |
| **FastAPI kept, but thin (~80 lines)** | It is the live-demo injection mechanism (POST from a phone → dashboard lights up) and it makes the "separated layers" rubric claim true. All logic stays in `core/`. |
| **Explicit Python orchestration, not LangGraph** | Regulated domain: a traceable, auditable, deterministic control flow beats a framework graph. Also removes an install risk. Say this out loud in the pitch. |
| **A secondary chat provider behind a circuit breaker** | The TCS endpoint 503s during working hours. `core/llm.py` degrades in four stages — primary → recorded cache → secondary provider → rules only — and a breaker stops it paying a 20s timeout per call once the primary is down. Nothing above `llm.chat()` knows any of this happened. |
| **Rules run before the LLM** | ~94% of transactions never hit the LLM. This is the entire cost-effectiveness argument, and it is measured live in the Admin cost meter. |
| **Only ~80 fraud-case narratives in FAISS, not all transactions** | Small index = fast, cheap, and visibly relevant retrieval on stage. |
| **PII tokenization, not masking** | `4532...` → `<PAN_7f3a>`. The model still reasons about "the same card" without ever seeing it. |
| **JSON parsed with a repair-retry, not `response_format`** | JSON mode is unreliable through this proxy. The retry doubles as our rubric "retry logic" evidence. |

---

## Repo layout & file ownership

Nobody edits another person's files. This is what keeps 5 AI-assisted sessions from colliding.

| Owner | Files |
|---|---|
| **A** Integrator | `core/contracts.py` `config.py` `db.py` `llm.py` `security.py` `pipeline.py` `notifications.py` · `api/main.py` · `web/__init__.py` `web/auth.py` · **only A merges to main** |
| **B** Fraud+RAG | `core/rules.py` `rag.py` `travel.py` `agents/fraud_analyst.py` `evaluation.py` |
| **C** Customer portal | `web/routes/customer.py` `agent.py` `drill.py` · `web/templates/customer/*` · `core/agents/router.py` `customer_agent.py` `tools.py` · `core/insights.py` |
| **D** Admin portal | `web/routes/admin.py` `demo.py` `notifications.py` · `web/templates/admin/*` |
| **E** Data/docs/deck | `data/*` `docs/*` · slides · demo script · manual QA |

Shared and owned by whoever touches them last, but tell the others: `web/static/css/glass.css`,
`web/static/js/*.js`, `web/templates/base.html`, `web/templates/partials/*`. A change to any
of those four is visible on every page in both portals.

The Streamlit UI (`app.py`, `ui/*`) is retired. Leave it alone; it is the fallback.

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
python run_web.py                       # the app — http://127.0.0.1:5000
uvicorn api.main:app --port 8000        # the ingestion API (optional; Flask mirrors it)
python -m core.pipeline --selftest      # end-to-end check
python -m core.evaluation               # the eval harness
streamlit run app.py                    # the retired Streamlit UI, kept as a fallback
```

Sign in as `customer`, `analyst` or `admin` — password `demo`.

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
- [x] **UI migrated from Streamlit to Flask** — `web/`, one process, `python run_web.py`
- [x] Clickable drill-downs on every figure — `GET /api/drill/<kind>/<key>`, 8 kinds
- [x] Notifications wired: toast + centre + real `.eml` outbox on every action
- [x] Agent action set completed — 14 tools incl. statement, travel budget, security review
- [x] **Light/dark theme + UI fixes** (branch `ui/theme-toggle-and-polish`) — header toggle,
      two first-class themes, gauge arc bug fixed, dropdowns readable.
      QA page at `web/static/_harness.html` (see "The theme system" below)
- [ ] **← NEXT: slide deck** (docs/ARCHITECTURE.md headings map to slides)
- [ ] Demo rehearsed 3× under 10:00, screen recording captured
- [ ] Re-run `python -m core.record_demo` after any prompt/scenario change

---

## The Flask UI (`web/`)

Streamlit is retired but still in the repo (`app.py`, `ui/`) as a fallback. Nothing in
`core/` changed to make the swap — that is the claim, and it is worth making out loud:
**the entire presentation layer was replaced without touching a rule, an agent, or the
pipeline.**

| Path | What it is |
|---|---|
| `web/__init__.py` | App factory. Registers 8 blueprints, injects `user`/`sb_health`/`all_customers`, template filters |
| `web/auth.py` | Session auth + `role_required`. Gates are on the **route**, so a customer gets 403 on `/ops/*`, not a hidden link |
| `web/routes/drill.py` | **The drill-down API.** `GET /api/drill/<kind>/<key>` — health · security · protection · spend · txn · alert · cost · rule |
| `web/routes/{customer,admin,demo,agent,notifications,ingest}.py` | One module per surface, all thin |
| `web/static/css/glass.css` | The whole design system. No framework, no web fonts, no CDN |
| `web/static/js/charts.js` | 8 hand-drawn SVG chart types. **No CDN on purpose** — the demo has to survive with the wifi off |
| `web/static/js/app.js` | Toasts, modals, the generic drill renderer, balance masking, notification polling |

**Adding a drill-down takes no front-end work.** Add a handler to `DISPATCH` in
`routes/drill.py` and put `data-drill="kind:key"` on any element. `wireDrilldowns()` finds
it and `renderDrill()` draws whatever the endpoint returns.

**Customer-scoped drill kinds read `auth.active_customer_id()` from the session, never the
key in the URL.** Passing someone else's id returns your own data. `alert` and `cost` are
staff-only and audit the attempt.

## Provider resilience — what happens when TCS goes down

It does go down. Observed during working hours:
`503 no_db_connection — "the authentication database is temporarily unreachable"`.

`llm.chat()` degrades in four stages, and nothing above it knows:

| | | |
|---|---|---|
| 1 | **Primary** (TCS gpt-4.1) | normal path |
| 2 | **Recorded cache** | ~40 ms, deterministic, no network. Scripted beats replay exactly as rehearsed |
| 3 | **Secondary provider** (Groq, opt-in) | ~2 s, handles anything the cache cannot — an improvised question from a judge |
| 4 | **Rules only** | the agent answers from the database and says so |

Cache **before** fallback is deliberate: a rehearsed beat should replay identically and
instantly, not be re-improvised by a different model live on stage.

**Enable the secondary provider** by setting `FALLBACK_API_KEY` in
`.streamlit/secrets.toml` (gitignored) — see `secrets.toml.example`. Unset, stages 1, 2
and 4 still work exactly as before.

### The circuit breaker is what makes it usable

Measured with the primary dead and *no* breaker: **17–26 s per beat**, every answer
correct, demo destroyed. The cost was the timeout, paid on every call — and a fallback you
reach after 20 s is worthless.

Two consecutive primary failures now open the circuit for 60 s; calls skip the primary
entirely. Same breaker covers **embeddings**, which is where most of the time went —
`rag.search_balanced()` embeds twice per transaction, so protecting only chat barely
helped. Any success closes it, so a recovered endpoint is picked up without a restart.

With the breaker primed, all 7 beats run in **16–212 ms** with the primary dead, citations
intact. **Press Run pre-flight check before the demo**: it probes the primary and absorbs
that first 20 s timeout, so beat 1 is already fast — and it now reports honestly which
provider is actually serving chat.

### What the fallback cannot do

**Groq serves no embedding model.** Retrieval therefore still depends on the primary or on
`data/cached_embeddings.json`. Citations survive for the scripted scenarios because those
query vectors are recorded; a brand-new transaction gets no precedents while the primary is
down. Pre-flight says so in as many words. Don't claim otherwise on stage — the honest
line is *"retrieval degrades to its cache, chat fails over to a second provider."*

Free tier: 1,000 requests/day, **8,000 tokens/minute**. Ample for a 10-minute demo, too
tight to push the 38-case evaluation harness through.

### Every action alerts the customer

`core/notifications.py` was written but wired to nothing. It now fires from three places:

- `tools.execute()` — one central hook, so a tool added later cannot forget. A write tool
  declares `notify_subject` and the alert follows automatically.
- `pipeline._persist()` — on `FREEZE_AND_ESCALATE`/`QUARANTINE` (danger) and `CHALLENGE` (warn).
- The analyst resolving an alert, either way.

Each one raises a toast, a row in the notification centre, and a **real `.eml`** in
`data/outbox/` that opens in any mail client. Nothing is labelled delivered unless SMTP is
configured (`SMTP_HOST` etc.) and the send succeeded — the UI says plainly when it is not.

### The theme system

Two first-class themes, not a dark theme with a fallback. Three rules decide which applies:

```
:root                                          -> dark (base, the only complete declaration)
:root[data-theme="light"]                      -> light, pinned by the user
@media light + :root:not([data-theme])         -> light, following the OS
```

No `[data-theme="dark"]` block exists — dark is the base, and a second copy would drift.
`partials/theme_boot.html` sets the attribute **before the stylesheet loads** (so there is no
flash) and is included in all three heads: `base.html`, `login.html`, `error.html`. It only
writes the attribute when the user has actually chosen; absent means "keep following the OS",
the same contract as `sb.maskBalance`.

Three things to know before editing:

- **The light palette is written twice** — once for `[data-theme="light"]`, once inside the
  media query — because a selector list cannot straddle a media boundary. Both blocks say
  `EDIT BOTH`, and `_harness.html` asserts they declare an identical property set, so
  forgetting fails a test instead of shipping a theme that is only right for people who
  clicked the toggle.
- **No rule below the token blocks may contain a colour literal.** 25 of them used to. A
  derived `rgb(var(--x-rgb) / .16)` is fine; a raw `rgba(99,102,241,.16)` is not, because it
  silently stops following the theme. The only exceptions are the `@media print` block
  (paper is white) and the email-preview iframe in `customer/email_view.html`.
- **Charts recolour with no redraw.** `charts.js` emits `var(--token, fallback)` rather than
  resolving colours at draw time, so a theme flip repaints every chart without replaying the
  entry animations. The fallback is mandatory: an unresolvable `var()` in a paint slot
  computes to **black**, not to a sensible default.

**`web/static/_harness.html` is the QA surface.** It renders every component and every chart
type in both themes side by side, and runs six assertions — no black paint, charts recolour
live, no colour literal survived, every token resolves in both themes, WCAG contrast, and the
light-block drift check. It needs no backend:

```bash
python3 -m http.server 8765 --directory web/static   # then open /_harness.html
```

Press **Reload CSS** before **Run assertions** after editing `glass.css`, or the browser
grades the cached copy.

**Both themes clear WCAG AA at every gradient stop.** `--grad-a` and `--grad-danger` carry
white text on `.btn-primary`, `.btn-danger` and `.msg.user` at 700-weight `.86rem` — normal
text by WCAG's definition, not large — so each stop needs 4.5:1 on its own:

| | dark was | dark now | light |
|---|---|---|---|
| `--grad-a` | 4.47 / 4.23 / **2.43** | 5.34 / 4.77 / 5.36 | 7.90 / 7.10 / 5.36 |
| `--grad-danger` | 3.67 / **2.80** | 4.70 / 5.18 | 6.29 / 5.18 |

The dark stops are the brightest shade of each hue that still passes, so dark stays more
vibrant than light at indigo and violet. Cyan and orange had no headroom, which is why both
themes land on `#0e7490` and `#c2410c` — the bright cyan end of the old brand gradient could
not survive white text at any usable shade. `--grad-b` is exempt: it carries the near-black
`--on-ok`, so it wants a bright background and measures 6.86 / 8.67.

Assertion 5 in the harness reports all of these, so brightening a stop back fails a test.

### Three bugs the browser walkthrough caught that CLI tests could not

1. **Nested expander crash.** `decision_card()` opened an expander inside the alert
   queue's expander. Streamlit raises `StreamlitAPIException` and silently drops every
   widget after it — the **Confirm fraud / False positive buttons never rendered**, killing
   the human-in-the-loop beat and the learning-loop climax. Fixed with `use_expander=False`.
   *The Flask queue renders them as plain form buttons; the failure mode cannot recur.*

2. **Cost meter inverted.** On a clean reseed it read *"2 transactions scored, 0% resolved
   without a model"* with a negative saving. Seeding never scored anything, cost came from
   unscoped telemetry, and "avoided" counted `llm_used` rather than rule score.

3. **`width="stretch"` doesn't exist in Streamlit 1.45.1** (20 call sites). It throws, and
   on a form it means no submit button — login was impossible.

### Three the Flask migration caught

4. **`build_statement()` still unpacked the old tuple shapes.** `HealthComponent` and
   `SecurityCheck` became dataclasses carrying their own evidence; two loops in
   `core/insights.py` still did `for label, earned, maximum, detail in ...` and raised
   `TypeError` on any statement. CLI tests never touched that path.

5. **The agent silently refused to freeze a card.** The model returns
   `{"action": "freeze_card", "tool": "freeze_card", …}` instead of the literal
   `{"action": "tool", …}` the prompt asks for. The loop fell through to the prose branch
   with an empty `say`, so *"freeze my card, it's been stolen"* answered **"Could you
   rephrase that?"** and did nothing. `customer_agent._normalise()` now repairs the
   envelope — same reasoning as the JSON repair-retry on the fraud path.

6. **The startup banner crashed on Windows.** A `→` in `run_web.py`'s print killed the
   launch under cp1252 before Flask ever bound a port — on exactly the kind of console the
   demo runs from. ASCII only in anything that prints at startup.

7. **The offline chat cache expired at midnight.** `customer_agent` used the default cache
   key, which hashes the whole prompt — and its system prompt embeds `date.today()`, while
   its user prompt carries the conversation history. So a cache recorded on Thursday missed
   on Friday, and the same question keyed differently depending on what preceded it. The
   recorded chat beats would have failed to replay on the one occasion they exist for.
   `_chat_cache_key()` now keys on intent + normalised question + loop step, the same
   scenario-stable approach `fraud_analyst` already used.

8. **The recorder poisoned its own cache.** `record_demo` ran against a hero card left
   frozen by an earlier fraud injection, so `CARD_ALREADY_FROZEN` (+60) pushed the
   *legitimate* beats to 100 and they recorded as `FREEZE_AND_ESCALATE`. Verification
   passed, because it replayed the same wrong answers. Offline, beat 1 would have shown an
   everyday grocery run being blocked as fraud. `record_demo._preflight()` now resets the
   card and travel notice itself, and the run warns loudly if any `legit_*` scenario
   records as blocked. **A tool must guarantee its own preconditions when getting them
   wrong fails silently.**

9. **"Full reset" was not full.** `db.reset_db()` dropped a hand-written list of nine
   tables. `notifications` and `spending_alerts` were added later and never appended, so a
   reset left the notification centre showing every alert from the previous rehearsal, and
   the `.eml` outbox kept files whose notifications no longer existed. It now enumerates
   from `sqlite_master`, and `seed.seed(reset=True)` clears the outbox — so the next table
   someone adds cannot be forgotten.

10. **The security score changed on every restart.** `security_posture()` derived the
    placeholder password age from the builtin `hash(customer_id)`. Python salts string
    hashing per process, so three consecutive runs gave 213, 90 and 37 days — moving the
    security score between 71 and 86 and the health grade between Good and Excellent.
    Restart the app mid-demo and a judge watches the customer's security grade change by
    itself. Now `hashlib.sha256`. **Anything presented to a user as a stable fact must
    never depend on `hash()`.**

11. **Every gauge above 50 drew the wrong arc.** `SBCharts.gauge` set the SVG large-arc
    flag from `(to - from) > .5`, confusing "more than half the *gauge*" with "more than
    half a *circle*". The gauge is a half circle, so its sweep can never exceed 180° and
    the flag must always be 0. Above 50 the browser took the long way round and drew the
    complement — at 71 it rendered 308px of arc where 169.5px was correct, appearing as
    two stubs wrapping under the dial. The grey track hid it because `arcPath(0, 1)` is
    exactly 180°, where both flag values draw the same path. Worst part: at 97% the
    complement is 245px against a correct 232px, so the **cost gauge looked almost right**
    and would never have been caught by eye. Verify arcs by measuring `getTotalLength()`
    against the value they claim to show, not by looking at them.

    Same pass: `bars()` truncated any label over 7 characters by keeping the *last* five,
    so "Legitimate" rendered as "imate" and "AMERICAS" as "ricas" — on bars with 260px of
    room. Now fits the label to the available width and elides from the end.

12. **The offline chat cache missed whenever the provider was down** — i.e. always, when
    it mattered. `_chat_cache_key` included the routed intent, but `router.classify` asks
    the *model* when no pattern matches. So "Why was my card frozen?" was recorded under
    `card_control` (API up) and looked up under `general` (API down): the response sat
    behind a key the lookup could no longer compute. The key is now the normalised
    question plus loop step and nothing else. **Never derive a fallback's cache key from
    anything that depends on the thing being fallen back from.**

    Two further gaps in the same path, both found by pointing `GENAILAB_BASE_URL` at a
    dead host and talking to the agent:

    - `_finalise()` (the second call, which phrases the tool result) passed no cache key,
      so it hashed a prompt containing live balances and timestamps — a hash that never
      repeats and therefore never replays. Its `_readable_fallback` only covered four of
      the fourteen tools, so the rest dumped **raw JSON into the chat bubble**. It now
      covers every tool and formats the *real* result rather than replaying recorded
      prose: stale figures shown as current are a worse failure in a bank than plain
      wording.
    - A question that was never recorded got a dead end. On the first step, an
      unreachable model now falls through to `router.primary_tool(intent)` — read-only and
      scoped to the caller — and answers from the database, saying plainly that it is
      doing so. "What is my balance?" returns the real balance with the endpoint dead.

13. **Run evaluation crashed with `Transaction.__init__() got an unexpected keyword
    argument 'label'`.** `evaluation.compare()` takes the raw eval-set *rows* and scores
    them itself; the route handed it the *results* of `run_case`, whose dicts carry a
    `label` key. Passing rows would have fixed the crash but scored every case a third
    time. `compare_results(baseline, full)` is now the pure function over already-scored
    arms, `compare(rows)` delegates to it, and the route scores each arm once — rules
    first, so the progress bar moves before the first model call. **A function that takes
    `rows` and one that returns rows are not the same shape; name and type them so the
    difference is visible at the call site.**

### Stateful demo traps (all three are pre-flight checklist items)

The **Run pre-flight check** button on Demo control fixes the first two and reports what it
changed. Press it between rehearsals.

- Injecting fraud **freezes the hero's card**. `CARD_ALREADY_FROZEN` is +60, so every later
  transaction scores ~100 and each beat looks like fraud.
- A reseed **wipes the travel notice**. Without it the Spain beat isn't testing suppression
  at all.
- **Re-run `python -m core.record_demo` after touching a prompt, a tool, or a chat quick
  action.** The chat beats it records must match `chat_widget.html` verbatim — a quick-action
  button whose prompt was never recorded is dead offline, and it will be the button you
  press on stage.

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
| Legitimate customers challenged | **12 → 7** with the agent (5 false positives removed) |
| False positive rate | **43% → 25%** · precision **45% → 59%** |
| Groundedness | **100%** — no fabricated citations |
| Hard eval cases that defeat rules alone | **7 of 8**; agent resolves 3 |
| p95 latency | ~3.4 s model calls · ~9 ms cheap path |

**Re-verified after the Flask migration** (38 cases, live): recall **100%**, legitimate
customers blocked **0**, groundedness **100%** with 0 fabricated citations across 18 cases
that cited evidence, avg latency 3,439 ms, p95 7,352 ms, $0.1724 for the run. Legitimate
customers challenged came in at 7 rather than 9 — the index had gained one analyst-learned
case by then, which is the loop doing its job.

The harness prints **RESULT: BELOW TARGET** because it counts a *challenge* as a false
positive against a ≤10% target. Nothing legitimate is ever blocked; the 25% is customers
asked to confirm. Know that distinction before a judge asks — the honest answer is that
step-up verification is the correct action on a genuinely ambiguous transaction, and the
target is stricter than the behaviour deserves.

**Be honest about the A/B in the pitch — and it is better than we first wrote it up.**
The model adds no raw *detection*: recall is 100% either way, and rules alone already block
nobody legitimate. What it measurably adds is precision. On the same 38 cases it removed
**5 of the 12** step-up challenges the rules imposed on legitimate customers — false
positive rate 43% → 25%, precision 45% → 59% — **with zero extra fraud missed**. That is
five real customers not interrupted, for $0.17 of inference.

Then the parts that do not fit in a metric: the explanation an analyst needs to act, the
cited precedent that makes the decision auditable, and the learning loop. Lead with the
measured number, follow with those. Run it live on the Evaluation page — the A/B table
builds itself.
