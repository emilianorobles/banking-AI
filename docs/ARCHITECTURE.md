# BedRock Financial — Architecture

> Companion to [DEMO_SCRIPT.md](DEMO_SCRIPT.md). Section headings map to slides.

---

## 1. The problem, precisely

Banking support handles millions of queries daily. Three costs compound:

1. **Customers wait**, and get inconsistent answers, because queries are routed manually across fragmented systems.
2. **Fraud teams drown.** Alerts arrive unprioritised and unexplained, so genuine fraud sits in a queue behind noise.
3. **False positives churn customers.** Every wrongly declined transaction is a support call, and some of those customers leave.

Most fraud demos optimise for (2) and ignore (3). We measure both, separately, because they are different harms with different costs.

---

## 2. The shape of the answer

**Rules first. Model second. Human last.**

```
                    ┌─────────────────────────────────────────┐
   Streamlit  ─────►│                                         │
   (3 portals)      │        core/pipeline.py                 │
                    │      score_transaction()                │
   FastAPI    ─────►│                                         │
   POST /api/       │   one entry point, eight explicit steps │
   transactions     └──────────────────┬──────────────────────┘
                                       │
   ① PII tokenization vault ───────────┤  raw identifiers never leave the perimeter
   ② 10 deterministic rules ───────────┤  facts, in code
   ③ travel-notice suppression ────────┤  kills geographic false positives
   ④ CHEAP PATH ──────────────────────►│  score < 30 → decide, NO model call  (~96%)
   ⑤ balanced RAG retrieval ───────────┤  fraud AND false-positive precedent
   ⑥ fraud analyst agent ──────────────┤  reasons, cites, reflects, retries
   ⑦ guardrails ──────────────────────┤  groundedness · DLP · injection · audit
   ⑧ asymmetric blend + route ────────►│  ALLOW │ CHALLENGE │ FREEZE │ QUARANTINE
                                       │
                    ┌──────────────────┴──────────────────────┐
                    │  SQLite (WAL)          FAISS (90 cases) │
                    └──────────────────┬──────────────────────┘
                                       │
              ★ LEARNING LOOP ─────────┘
              analyst resolves an alert → narrative embedded → indexed →
              the next similar transaction cites the case just closed
```

---

## 3. Why each layer exists

### Rules before the model

Three independent reasons, and each one is a slide bullet:

- **Cost.** ~96% of transactions are unambiguous. Sending them all to a model costs ~17× more for no accuracy gain. Measured on the seed set: **1,934 of 2,000 transactions resolved with zero inference.**
- **Reliability.** *"This transaction is in a country the customer has never visited"* is a fact, not a judgement. Facts belong in code, where they are testable and cannot drift.
- **Latency.** The cheap path returns in ~9 ms. Model inference takes 4–13 s. You cannot put that on the authorisation path for every transaction.

There is deliberately **no cheap path at the top end**. Escalations always get an explanation, because a human is about to be asked to freeze someone's card and needs the reasoning and the precedent. *Cheap where it's obvious, explained where it counts.*

### Balanced retrieval

We embed ~90 **analyst case narratives**, not the 2,000 transactions. A transaction is structured data; rules handle it better than vector search ever will. What is worth retrieving is *institutional knowledge* — why something turned out to be fraud, or why it turned out not to be.

Retrieval queries `confirmed_fraud` and `false_positive` **separately** and merges.

> **We found this the hard way.** Plain top-k similarity returned three confirmed-fraud precedents for almost any flagged transaction, because the corpus holds more fraud cases than false positives — as would any real bank's. The agent then reasoned faithfully from one-sided evidence and concluded "fraud" every time. The bias was in the retrieval, not the model. Splitting the query by outcome fixed it.

### The asymmetric blend

The model's authority differs by direction:

| Direction | Weight | Why |
|---|---|---|
| Model argues **safer** than the rules | **0.65** | Rules can only ever *add* points. They cannot express "this is 4× baseline but it's the same annual premium paid for four years." That needs precedent and context. |
| Model argues **more dangerous** | **0.25** | Rules already achieve 100% recall on our eval set. They need no help finding risk. |
| Rule score ≥ 90 | **floor** | "Two countries forty minutes apart" is physics, not opinion. |

> **Also measured.** With a symmetric blend, adding the model left recall unchanged at 100% but raised the average risk score on legitimate customers from 19.1 to 24.9 and blocked one who would otherwise have been allowed. Shown a list of rules that fired plus fraud precedents, a language model agrees with them. Agreeableness in a fraud system means declining good customers.

### Human last

No irreversible action executes on a model's say-so.

- `freeze_card`, `unfreeze_card`, `raise_dispute` are **approval-gated in the tool registry** — the agent returns a *proposal*, and the UI renders a confirm button.
- Automated freezes stop the bleeding immediately but land in the supervisor queue as `PENDING`; an analyst confirms or reverses.
- `customer_id` is injected from the authenticated session and **discarded if the model supplies one**, so a prompt injection cannot read another account.

---

## 4. Agent roles

| Agent | Role | Escalates to |
|---|---|---|
| **Router** | Classifies intent, narrows the available tool surface | The relevant specialist |
| **Customer Service** | Answers grounded in account data via tools | Supervisor for any state change |
| **Fraud Analyst** | Scores ambiguous transactions against retrieved precedent | Supervisor queue above risk 75 |
| **Guardrail** | Validates schema, citations, PII egress, injection | Blocks and logs |
| **Supervisor** | **A human.** Approves or reverses in the admin portal | Terminal |

**Handoff, retry, reflection, escalation** — all four are real code paths, not prompt instructions:
- *Retry* — JSON parse failure re-prompts once with the parse error appended.
- *Reflection* — verdicts heading for a freeze, or with confidence < 0.6, get re-checked against the evidence and may be revised.
- *Escalation* — risk ≥ 75 routes to a human queue.
- *Handoff* — the router restricts which tools each intent may reach.

**Tools:** `get_account_summary` · `list_recent_transactions` · `list_travel_notices` · `search_fraud_precedents` · `set_travel_notice` · `raise_dispute` 🔒 · `freeze_card` 🔒 · `unfreeze_card` 🔒 (🔒 = human approval required)

---

## 5. Security

| Control | Implementation |
|---|---|
| **PII tokenization** | `4532015112830366` → `<PAN_9b0893>`. Stable per value, so the model can reason about "the same card" without ever seeing a digit. Luhn-validated so random numbers aren't flagged. Detokenized only when rendering to the authenticated owner. |
| **DLP egress** | Model output is scanned for PAN/Aadhaar/SSN/IBAN before it reaches a user or a log. Hard redaction — at egress there is no legitimate reason to emit one. |
| **Prompt injection** | Merchant names and descriptions are attacker-controlled. They are delimiter-fenced and labelled as data, and scanned for instruction-shaped content. A hit **quarantines** the transaction — we do not ask the model what it thinks about text engineered to manipulate the model. |
| **Groundedness** | Every cited case ID is checked against the knowledge store. Fabricated IDs are stripped, flagged, and reported as a metric. |
| **Audit** | Every decision, tool call, guardrail trip, login and approval writes to `audit_log` with an actor and timestamp. |
| **RBAC** | Three roles; a customer has no route to the analyst queue. |
| **Secrets** | Key in gitignored `.streamlit/secrets.toml` or env var. Never hardcoded — note that the organizer's own `Creating_RAG.pdf` hardcodes one, which we deliberately did not copy. |

---

## 6. Evaluation

`python -m core.evaluation --compare`

We report **blocked** and **challenged** separately. A block is a churn event; a challenge is a push notification. A single blended "false positive rate" hides the distinction a bank actually cares about.

The eval set includes 8 cases mirroring real production false positives — relocation, annual premiums at long-used merchants, festival spending bursts, quarterly business travel, hotel pre-authorisations. **7 of 8 defeat the rules engine**, which is the point: they are only resolvable by reading the customer's history and retrieving precedent.

Targets: recall ≥ 85% · 0 legitimate customers blocked · groundedness 100%.

---

## 7. Deliberate trade-offs

| Choice | Alternative | Why |
|---|---|---|
| Explicit Python orchestration | LangGraph / CrewAI | In a regulated domain you must point at the line that made the decision. Also removes a framework dependency from a one-day build. |
| Streamlit | React + FastAPI | Both organizer guides are Streamlit; the team has mixed coding experience. UI plumbing scores zero rubric points. |
| Structured-JSON tool calling | Native function calling | Support through this proxy is not guaranteed. Portability is the "avoid lock-in" requirement, and every proposed call is loggable and scope-checkable before execution. |
| SQLite + FAISS | Postgres + managed vector store | Prototype scope. Both are drop-in replacements, which is why all config is centralised in `core/config.py`. |
| ~90 curated case narratives | Embedding all transactions | Retrieval quality beats corpus size. Small index rebuilds in 12 s and returns visibly relevant results. |

---

## 8. Adoption path

1. **Shadow mode** — run alongside the existing engine, compare decisions, tune thresholds on real traffic. No customer impact.
2. **Analyst assist** — surface reasoning and cited precedent in the existing case tool. The learning loop starts filling from day one.
3. **Selective autonomy** — auto-allow the high-confidence bottom band first (the cheapest, safest win). Escalations keep human approval.

**Prototype → enterprise:** swap SQLite for Postgres, FAISS for a managed vector store, add a queue in front of ingestion, and replace the demo auth with the bank's IAM. The `core/` boundary does not change — every one of those is a config swap, which is exactly why the layers are separated the way they are.
