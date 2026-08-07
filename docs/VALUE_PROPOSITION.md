# BedRock Financial — the value proposition

> **What this document is for.** It is the anchor for the deck, the answer sheet for the
> Q&A, and the place the numbers live so nobody has to remember them. The in-app
> **Business case** page (`/ops/business-case`) renders the live version of the
> commercial figures; this document carries the argument around them.
>
> **The one rule for using it: do not oversell the AI.** The strongest version of this
> pitch is the honest one, and it is stronger than the exaggerated one. Read
> [What the AI actually adds](#what-the-ai-actually-adds) before you write a slide.

---

## The problem, in a bank's words

A retail bank's fraud function has three costs, and only one of them is fraud.

| Cost | What it looks like | Who feels it |
|---|---|---|
| **Fraud losses** | Money leaves and does not come back | The bank |
| **False positives** | A good customer's card declines at a checkout | The customer, then the call centre, then churn |
| **Analyst time** | A queue of alerts with a score and no reasoning | The fraud team |

Most fraud systems optimise the first and treat the other two as acceptable. The second is
usually the larger number. An industry rule of thumb is that false declines cost several
times what card fraud does — because a declined genuine transaction is lost revenue, a
support call, and sometimes a lost customer.

And a fourth cost has appeared recently: teams reaching for a language model on every
transaction, paying inference on volume that never needed judgement in the first place.

---

## What we built

Nine stages, in a fixed order, all traceable. The commercially interesting part is stage 4.

```
PII tokenised → deterministic rules → travel suppression
   → ④ CHEAP PATH: a clearly-low or clearly-high rule score is already a decision
   → retrieval of similar past cases → AI analyst → guardrails → route → notify
                                                    ↓
                   analyst resolves an alert → the case is embedded and indexed →
                   the next similar transaction cites the decision just made
```

### The capabilities, and what each is worth

| Capability | What it does | Why a bank cares |
|---|---|---|
| **Rules-first routing** | 12 deterministic checks score every transaction; only the ambiguous middle band reaches a model | ~97% of volume costs nothing to decide. This is the entire deployability argument |
| **PII tokenisation** | `4532…` becomes `<PAN_7f3a>` before any text reaches a model | The model reasons about "the same card" having never seen a card number. Answers the data-residency question before it is asked |
| **Travel-notice suppression** | A filed notice cancels the geography rules for that destination | Removes the single largest category of false positive at its source, deterministically |
| **Retrieval over past cases** | Similar historical cases, balanced across fraud and false-positive outcomes | Judgement grounded in precedent rather than in the model's priors |
| **Cited reasoning** | Every claim points at a real case ID; unresolvable ones are dropped | An auditable decision. A regulator can follow it |
| **Guardrails** | Schema validation, groundedness check, DLP egress scan, injection detection | The model cannot fabricate evidence, leak PII, or be instructed by a merchant name |
| **Human-in-the-loop** | Freezes are proposals until an analyst confirms | The bank never delegates an irreversible action to a model |
| **The learning loop** | A resolved alert is embedded and indexed immediately | The system that catches the second attack cites the analyst who caught the first |
| **Explainability** | Every figure opens the working behind it, in plain English | The customer can interrogate a decision made about their money |
| **Provider resilience** | Primary → recorded cache → secondary provider → rules only, behind a circuit breaker | Fraud screening does not stop because a vendor has an outage |

---

## The measured numbers

Everything here is reproducible from this repository. Where a figure is live it is marked
**live**; where it comes from the 38-case evaluation harness it is marked **benchmark**.

### Cost

| | |
|---|---|
| Transactions needing no model | **96.0%** (1,966 / 2,047) — **benchmark** |
| Cost per model-scored transaction | **$0.00757**, measured tokens at gpt-4.1 rates |
| Projected cost, rules-first | **$0.61** |
| Projected cost, all-model | **$15.50** |
| Saving | **96%** |

The cheap-path rate is a property of the rule scores, not of spend, so it holds even before
a single model call is priced. The Business case page shows it live.

### Accuracy — the A/B that matters

| Measure | Rules only | With the agent |
|---|---|---|
| Fraud caught (recall) | **100%** | **100%** |
| Legitimate customers **blocked** | **0** | **0** |
| Legitimate customers **challenged** | 12 | **7** |
| False positive rate | 43% | **25%** |
| Precision | 45% | **59%** |
| Fabricated citations | — | **0** |
| Cost of the agentic run | — | **$0.17** |

### Performance

| | |
|---|---|
| Cheap path | ~9 ms, no network |
| Model path | ~3.4 s average, 7.4 s p95 |
| Rule separation | fraud mean **89.0** vs legitimate **2.0** |
| Groundedness | **100%** across 18 cases that cited evidence |
| Offline replay | all 7 demo beats in **16–212 ms** with the primary dead, citations intact |

---

## What the AI actually adds

**Say this precisely, because a judge will test it.**

The model adds **no raw detection**. Recall is 100% with or without it, and the rules alone
block nobody legitimate. Anyone who claims their LLM "catches more fraud" than a good rules
engine on a set this size is either not measuring or not telling you.

What it measurably adds is **precision**. On the same 38 cases it removed **5 of the 12**
step-up challenges the rules imposed on legitimate customers — false positive rate 43% →
25%, precision 45% → 59% — **with zero extra fraud missed.**

> **The line to say out loud:** five real customers not interrupted, for seventeen cents of
> inference, with no extra fraud missed.

Then the three things that do not fit in a metric, and are worth more than the ones that do:

1. **An explanation an analyst can act on.** Not a score — a reason, in words, with the
   evidence attached. The queue stops being a list of numbers to triage.
2. **A cited precedent.** The decision is auditable because it points at real prior cases.
3. **The learning loop.** An analyst's judgement becomes retrievable evidence in seconds,
   not in the next model training cycle.

### Why the model is weighted asymmetrically

`pipeline._blend()` gives the model's **exculpatory** opinion 0.65 and its **incriminating**
opinion 0.25. This is not a hedge; it is the measured correction to a real failure.

A symmetric blend left recall at 100% but pushed the average risk score on legitimate
customers from 19.1 to 24.9, and blocked one customer the rules had allowed. Given a page of
fired rules and a set of fraud precedents, the model is agreeable — it piles on. Meanwhile
rules are already excellent at *detecting* risk and **structurally incapable of exonerating**,
because they only ever add points.

So we let the model do the thing the rules cannot, and discount it at the thing they already
do well. That asymmetry is the design, and it is measurable.

---

## Objection handling

**"Your false positive rate is 25%. That's terrible."**
Nothing legitimate is ever *blocked* — 0, before and after the agent. The 25% is customers
asked to confirm. Step-up verification is the correct action on a genuinely ambiguous
transaction; the harness's ≤10% target counts a challenge as a failure, which is stricter
than the behaviour deserves. We report it against that target anyway rather than moving the
goalposts.

**"Why not just use the LLM for everything?"**
Cost and reliability. 97% of transactions are unambiguous; sending them to a model costs
~25× more for no accuracy gain, and adds a 3.4-second network dependency to a decision that
takes 9 ms. "Transaction is in a country this customer has never visited" is a fact, not a
judgement — facts belong in code.

**"Why not LangGraph / an agent framework?"**
Regulated domain. A traceable, auditable, deterministic control flow beats a framework graph
when someone has to explain a decision to a regulator. It also removes an install-time
dependency risk. The orchestration is explicit Python and fits on one screen.

**"How do you know the model isn't making up its evidence?"**
Every cited case ID is validated against the index. Unresolvable IDs are stripped and the
attempt is recorded as a guardrail trip. Measured groundedness is 100% with 0 fabricated
citations across the 18 evaluation cases that cited evidence.

**"What about prompt injection through a merchant name?"**
Merchant text is treated as untrusted data throughout. It is injection-scanned before any
prompt assembly, and a hit quarantines the transaction. The customer-facing tools do not
return merchant names to the model at all.

**"What happens when your provider goes down?"**
It did, repeatedly, during the build. Four stages: primary → recorded cache → secondary
provider → rules only, with a circuit breaker so a dead primary costs milliseconds instead
of a 20-second timeout per call. **The honest limit:** the secondary serves no embedding
model, so retrieval degrades to its cache — citations survive for recorded scenarios, but a
brand-new transaction gets no precedents while the primary is down. Pre-flight reports this
in as many words.

**"Is this real data?"**
No — 2,000 generated transactions on one bank's shape. The architecture and the measurements
are real and reproducible from this repository. We are not claiming it has run a bank.

**"What would it take to deploy this?"**
The rules and the pipeline are production-shaped already. What a real deployment needs that
this does not have: a real transaction feed instead of the ingestion endpoint, a mail gateway
(the outbox writes real `.eml` files and the UI says plainly that delivery is not wired up),
a tuned rule set on the bank's own loss data, and a model deployment inside the bank's
boundary — which the tokenisation layer already anticipates.

---

## Suggested slide mapping

Ten minutes, ten slides, one idea each.

| # | Slide | The one line | Source |
|---|---|---|---|
| 1 | The problem | False declines cost more than fraud, and nobody explains either | Top of this document |
| 2 | The architecture | Nine stages, all traceable — and 97% never reach a model | The pipeline diagram |
| 3 | Rules first | Facts belong in code; judgement belongs in the model | `core/rules.py`, 12 checks |
| 4 | The cheap path | 97% resolved for nothing. This is the deployability argument | Business case page, **live** |
| 5 | Retrieval + citation | Judgement grounded in precedent, not in priors | Alert queue, precedents panel |
| 6 | The A/B | The model adds precision, not detection. Say it plainly | Evaluation page, builds itself |
| 7 | Explainability | Every figure opens its own working, in plain English | Any **Why?** button |
| 8 | Human-in-the-loop | The bank never delegates an irreversible action | The approval gate; voice will not bypass it |
| 9 | The learning loop | The analyst who catches the first attack is cited on the second | Resolve an alert, then re-inject |
| 10 | Resilience + limits | Four-stage degradation, and what it cannot do | Pre-flight output |

Slide 10 is not a weakness. Volunteering the limit is what makes the other nine credible,
and it is the slide most teams do not have.
