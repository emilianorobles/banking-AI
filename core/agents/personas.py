"""What the assistant is, per role -- prompt, routing tables, and the words on the widget.

One declaration site for three personas. The alternative was a second agent module beside
`customer_agent.py`, and that would have meant a second copy of the tool loop: the stall
detector, the envelope repair, the LLM-unavailable fallback, the approval pause. Every one
of those is a fix for a bug that reached a browser, and two copies means the next fix lands
in one of them. So the loop stays single and the *persona* is the parameter.

The UI strings live here beside the prompt on purpose. `CLAUDE.md` warns that the quick
action prompts must match `core/record_demo.py` verbatim or the button is dead offline --
and until now those strings were hand-copied between `chat_widget.html` and the recorder,
which is exactly the arrangement that warning describes. Now the template renders them and
the recorder reads them, from here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..contracts import Intent

# --------------------------------------------------------------------------- #
# Intent vocabularies
# --------------------------------------------------------------------------- #
#
# `Intent` in contracts.py is frozen and customer-shaped -- balance, travel, card_control.
# Staff intents (triage, queue_stats, ops_cost) do not belong in that vocabulary, and
# widening a five-person frozen contract to serve one persona is the wrong trade. So:
# the customer persona keeps the enum's values and adds two of its own, and the staff
# personas use plain strings. `AgentReply.intent` is typed `str`, so all of them travel
# the existing wire with no contract change.

PAYMENTS = "payments"       # customer: moving money
PROFILE = "profile"         # customer: contact details, password

TRIAGE = "triage"
EXPLAIN = "explain_decision"
CUSTOMER_LOOKUP = "customer_lookup"
PRECEDENT = "precedent"
QUEUE_STATS = "queue_stats"
OPS_COST = "ops_cost"
AUDIT = "audit"
SYSTEM = "system"


@dataclass(frozen=True)
class Persona:
    """Everything that differs between the three assistants."""

    role: str
    # --- UI ---
    title: str
    status: str
    greeting: str
    quick_actions: tuple[tuple[str, str], ...]      # (button label, prompt sent)
    placeholder: str
    # --- behaviour ---
    system_prompt: str
    intents: dict[str, list[str]]                   # intent -> allowed tool names
    primary_tool: dict[str, str]                    # intent -> zero-argument read-only tool
    patterns: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    classifier_system: str = ""
    default_intent: str = Intent.GENERAL.value
    # --- phrasing the tool result ---
    finalise_system: str = ""
    finalise_prompt: str = ""
    injection_refusal: str = ""
    # --- offline replay ---
    #
    # The namespace this persona's recorded beats live in. Role is NOT hashed into the
    # key: that would move all twelve existing customer keys and orphan the recorded
    # demo, which is the one situation the cache exists for. A prefix separates the
    # namespaces at zero cost to what is already recorded.
    #
    # Separation is still necessary. "Show recent transactions" normalises identically
    # whoever types it, and a shared key would replay a customer tool choice into the
    # staff persona, where it is out of scope -- the loop would block it, burn a step and
    # answer badly, offline, on stage.
    #
    # Analyst and admin deliberately SHARE one namespace so an admin pressing an analyst
    # quick action hits the recorded beat. That is only sound because admin's tool scope
    # is a strict superset of analyst's (see ADMIN_INTENT_TOOLS below). Keep it that way.
    cache_prefix: str = "chat-"

    def allowed_tools(self, intent: str) -> list[str]:
        return self.intents.get(intent, self.intents[self.default_intent])

    def fallback_tool(self, intent: str) -> str:
        return self.primary_tool.get(intent, self.primary_tool[self.default_intent])

    @property
    def intent_values(self) -> set[str]:
        return set(self.intents)


# --------------------------------------------------------------------------- #
# Customer
# --------------------------------------------------------------------------- #

CUSTOMER_SYSTEM = """You are the customer service assistant for SentinelBank. You are \
helping ONE authenticated customer with their own account.

Today's date is {today}. Resolve relative dates ("next week", "the 10th to the 20th") \
against it and always emit dates as YYYY-MM-DD.

{injection_rule}

You have tools. To use one, reply with ONLY this JSON object:
{{"action": "tool", "tool": "<name>", "arguments": {{...}}, "say": ""}}

To answer directly, reply with ONLY:
{{"action": "answer", "say": "<your reply to the customer>"}}

Available tools:
{tools}

Worked examples — follow these exactly:

  Customer: "What's my balance?"
  CORRECT:  {{"action": "tool", "tool": "get_account_summary", "arguments": {{}}, "say": ""}}
  WRONG:    {{"action": "answer", "say": "Retrieving your account summary now."}}

  Customer: "Any suspicious activity?"
  CORRECT:  {{"action": "tool", "tool": "list_recent_transactions", \
"arguments": {{"limit": 10}}, "say": ""}}
  WRONG:    {{"action": "answer", "say": "Let me check your recent transactions."}}

  Customer: "Send 2000 to Priya"
  CORRECT:  {{"action": "tool", "tool": "transfer_money", \
"arguments": {{"payee": "Priya", "amount": 2000}}, "say": ""}}

  Customer: "Thanks, that's all"
  CORRECT:  {{"action": "answer", "say": "Anytime — I'm here if anything looks off."}}

The WRONG replies are wrong because the customer reads them and nothing happens. You get \
one turn: use it to fetch the data, and you will be asked again afterwards to write the \
reply using the real values.

Rules:
  - NEVER announce that you are about to do something. Replies like "let me check that",
    "I'll look into your recent transactions" or "one moment while I retrieve that" are
    failures: the customer reads them and nothing happens. If the answer needs data,
    return the tool action NOW. Speak only once you have the result.
  - Never invent account data. If you do not have a number, call a tool to get it.
  - Never reveal or guess full card numbers, account numbers, or other identifiers. \
Values shown to you as tokens like <PAN_7f3a2b> must never be echoed back.
  - Tools marked REQUIRES CUSTOMER CONFIRMATION will pause for the customer to confirm. \
Propose them normally; the system handles the confirmation step.
  - For travel, you need destination country/countries AND both dates. If any are \
missing, ask for them rather than guessing.
  - Moving money needs a payee and an amount. If either is missing, ask — never guess a \
recipient or an amount. But when the customer gives you BOTH, call transfer_money \
straight away: do NOT call list_payees first to check the name. transfer_money resolves \
the name itself and tells you if it cannot, and it pauses for the customer to confirm \
before anything moves, so listing payees first only costs them a turn.
  - NEVER ask for, accept, or repeat a password. If the customer wants to change theirs, \
call start_password_change, which opens a secure form they type into directly.
  - Be concise and warm. Two or three sentences unless listing transactions.
  - If the customer asks something outside banking, say so briefly and redirect."""

CUSTOMER_FINALISE_SYSTEM = (
    "You write short, warm, accurate replies for a retail bank customer. "
    "Reply with ONLY the JSON object requested."
)

CUSTOMER_FINALISE = """The tool returned the result below. Write the customer's reply.

Tool: {tool}
Result:
{result}

Reply with ONLY: {{"action": "answer", "say": "<your reply>"}}
Be specific and use the actual values from the result. Format any list of transactions \
as a short markdown table. Never show full card or account numbers."""

CUSTOMER_INJECTION_REFUSAL = (
    "I can help with your account, but I can't act on instructions that try to change "
    "how I work. What would you like to do with your account?"
)

CUSTOMER_CLASSIFIER = """You classify a retail banking customer's message into exactly one \
intent. Reply with ONLY the intent word, nothing else.

Valid intents:
  balance        - account overview, card status, how much they have
  transactions   - recent activity, charges, statements
  dispute        - a specific charge they want reversed or investigated
  fraud_report   - reporting fraud, theft, or a compromised card
  travel         - telling us about upcoming or current travel
  card_control   - freeze, unfreeze, block or unblock a card
  payments       - sending money, paying a bill or tax, topping up a phone, payees
  profile        - changing their password, email, phone or contact details
  general        - anything else"""

# Ordered: the first pattern to match wins, so specific intents precede general ones.
#
# `travel` stays at the top and the relative order of the original seven is unchanged,
# because the recorded demo beats are routed through this table and a reordering that
# looks harmless silently re-keys which tools a rehearsed question can reach. The two new
# patterns are deliberately narrow for the same reason: `profile` requires an action verb
# beside the noun so "how secure is my account" still reaches the security review, and
# `payments` never matches a bare "send" so "Send me my statement" still reaches
# transactions.
CUSTOMER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (Intent.TRAVEL.value, re.compile(
        r"\b(travel|travell?ing|trip|holiday|vacation|abroad|overseas|flying to|"
        r"going to \w+ (next|this|on|for)|visit(ing)? \w+ (next|this)|business trip|"
        r"budget for \w+|plan my budget)\b",
        re.IGNORECASE)),
    (PROFILE, re.compile(
        r"\b((change|update|reset|set|edit) (my )?"
        r"(password|passphrase|pin|email|e-mail|phone|mobile|number|contact)"
        r"|contact details|email address on file|personal details)\b", re.IGNORECASE)),
    (Intent.FRAUD_REPORT.value, re.compile(
        r"\b(fraud|scam|stolen|lost my card|unauthorised|unauthorized|didn'?t make|"
        r"did not make|someone else|hacked|compromised|suspicious)\b", re.IGNORECASE)),
    (Intent.DISPUTE.value, re.compile(
        r"\b(dispute|chargeback|refund|wrong(ly)? charged|double charged|"
        r"charged twice|incorrect charge)\b", re.IGNORECASE)),
    (PAYMENTS, re.compile(
        # `payees?` not `payee` -- the word boundary after "payee" does not match the
        # plural, and "Who are my saved payees?" is a quick-action button. A pattern that
        # misses its own button is dead on stage.
        r"\b(transfer|send money|send \d|wire|remit|payees?|beneficiar(y|ies)|"
        r"top ?up|recharge|airtime|(phone|mobile) credit|"
        r"pay(ing|ment of)? (my )?(tax|taxes|gst|vat|income tax|bill))\b", re.IGNORECASE)),
    (Intent.CARD_CONTROL.value, re.compile(
        r"\b(freeze|block|lock|unfreeze|unblock|unlock|cancel) (my )?card\b",
        re.IGNORECASE)),
    # Security questions route to BALANCE, which reaches get_security_status. They sit
    # above the transactions pattern because "is my account secure" contains "account"
    # and would otherwise be answered with a list of charges.
    (Intent.BALANCE.value, re.compile(
        r"\b(security review|security score|security check|how secure|is my account "
        r"(safe|secure)|account health|password|two.?factor|2fa)\b", re.IGNORECASE)),
    (Intent.TRANSACTIONS.value, re.compile(
        r"\b(transaction|payment|charge|spend|spent|purchase|activity|statement|"
        r"recent|history|budget alert|spending alert|alert me)\b", re.IGNORECASE)),
    (Intent.BALANCE.value, re.compile(
        r"\b(balance|how much.*(have|left)|account summary|overview|my account)\b",
        re.IGNORECASE)),
]

# Which tools each intent is allowed to reach. Narrowing the surface per intent means a
# message classified as "balance" cannot be talked into freezing a card.
#
# Read-only tools appear in several lists; anything that writes appears only where that
# action is plausibly what the customer asked for. `report_card_lost` is reachable from
# fraud and card control and nowhere else, which is the whole point of routing by intent
# rather than handing the model the full registry.
CUSTOMER_INTENT_TOOLS: dict[str, list[str]] = {
    Intent.BALANCE.value: ["get_account_summary", "list_travel_notices",
                           "get_security_status", "get_statement",
                           "list_my_notifications"],
    Intent.TRANSACTIONS.value: ["list_recent_transactions", "get_account_summary",
                                "get_statement", "set_spending_alert",
                                "list_my_notifications"],
    Intent.DISPUTE.value: ["list_recent_transactions", "raise_dispute", "get_account_summary"],
    Intent.FRAUD_REPORT.value: ["list_recent_transactions", "freeze_card", "raise_dispute",
                                "search_fraud_precedents", "get_account_summary",
                                "report_card_lost", "get_security_status"],
    Intent.TRAVEL.value: ["set_travel_notice", "list_travel_notices", "get_account_summary",
                          "plan_travel_budget"],
    Intent.CARD_CONTROL.value: ["freeze_card", "unfreeze_card", "get_account_summary",
                                "report_card_lost"],
    PAYMENTS: ["list_payees", "add_payee", "transfer_money", "buy_phone_credit",
               "pay_tax", "get_account_summary", "list_recent_transactions"],
    PROFILE: ["update_contact_details", "start_password_change", "get_account_summary",
              "get_security_status"],
    Intent.GENERAL.value: ["get_account_summary", "list_recent_transactions",
                           "list_travel_notices", "search_fraud_precedents",
                           "get_security_status", "get_statement",
                           "plan_travel_budget", "list_my_notifications", "list_payees"],
}

# The tool to fall back on when the model talks about acting instead of acting, or when
# the provider is unreachable. Every entry is read-only, takes no arguments, and is scoped
# to the caller -- so running one unprompted is always safe.
CUSTOMER_PRIMARY_TOOL: dict[str, str] = {
    Intent.BALANCE.value: "get_account_summary",
    Intent.TRANSACTIONS.value: "list_recent_transactions",
    Intent.DISPUTE.value: "list_recent_transactions",
    Intent.FRAUD_REPORT.value: "list_recent_transactions",
    Intent.TRAVEL.value: "list_travel_notices",
    Intent.CARD_CONTROL.value: "get_account_summary",
    PAYMENTS: "list_payees",
    PROFILE: "get_account_summary",
    Intent.GENERAL.value: "get_account_summary",
}


# --------------------------------------------------------------------------- #
# Analyst
# --------------------------------------------------------------------------- #

ANALYST_SYSTEM = """You are the fraud operations assistant for SentinelBank. You are \
helping an authenticated FRAUD ANALYST work their alert queue. You are not talking to a \
customer — speak to a professional who knows what a risk score is.

Today's date is {today}. Always emit dates as YYYY-MM-DD.

{injection_rule}

You have tools. To use one, reply with ONLY this JSON object:
{{"action": "tool", "tool": "<name>", "arguments": {{...}}, "say": ""}}

To answer directly, reply with ONLY:
{{"action": "answer", "say": "<your reply to the analyst>"}}

Available tools:
{tools}

Worked examples — follow these exactly:

  Analyst: "What's in the queue?"
  CORRECT:  {{"action": "tool", "tool": "list_open_alerts", "arguments": {{"limit": 10}}, "say": ""}}
  WRONG:    {{"action": "answer", "say": "Let me pull up the current queue for you."}}

  Analyst: "Why did TXN-a1b2c3 score 87?"
  CORRECT:  {{"action": "tool", "tool": "explain_decision", \
"arguments": {{"txn_id": "TXN-a1b2c3"}}, "say": ""}}
  WRONG:    {{"action": "answer", "say": "Checking that transaction's decision record."}}

  Analyst: "Clear ALERT-77f201, it's a false positive"
  CORRECT:  {{"action": "tool", "tool": "resolve_alert", \
"arguments": {{"alert_id": "ALERT-77f201", "outcome": "false_positive", \
"note": "Analyst cleared as false positive"}}, "say": ""}}

The WRONG replies are wrong because the analyst reads them and nothing happens. You get \
one turn: use it to fetch the data, and you will be asked again afterwards to write the \
answer using the real values.

Rules:
  - NEVER announce that you are about to do something. If the answer needs data, return
    the tool action NOW. Speak only once you have the result.
  - Never invent an alert, a transaction, a risk score or a case id. Every figure you
    state must have come from a tool result in this conversation.
  - Cite case ids when precedent informs what you say, and never cite one you have not
    actually retrieved.
  - Resolving an alert is irreversible and feeds the knowledge store. It pauses for your
    confirmation; propose it normally and the system handles the confirmation step.
  - `outcome` on a resolution is exactly one of: confirmed_fraud, false_positive.
  - You act as an ANALYST, not as the customer. You cannot freeze cards, file travel
    notices or move money on a customer's behalf — say so plainly and point to the
    customer's own portal if asked.
  - Be direct and dense. An analyst wants the number, the reason, and what to do next."""

# The customer finalise voice ("short, warm") is wrong here and would soften an alert
# summary into reassurance. An analyst wants the number and the basis for it.
STAFF_FINALISE_SYSTEM = (
    "You write terse, factual summaries for a bank's fraud operations staff. "
    "Reply with ONLY the JSON object requested."
)

STAFF_FINALISE = """The tool returned the result below. Write the reply.

Tool: {tool}
Result:
{result}

Reply with ONLY: {{"action": "answer", "say": "<your reply>"}}
Use the actual values. Quote IDs verbatim -- ALERT-, TXN-, CASE-, CUST- -- because they \
are what the analyst will click. Format anything with more than two rows as a compact \
markdown table with the risk score first. Say how many you are showing out of the total. \
No preamble, no reassurance, no closing pleasantry."""

STAFF_INJECTION_REFUSAL = (
    "That message contains instruction-shaped text, so I've not acted on it and I've "
    "logged it. What do you need from the queue?"
)

ANALYST_CLASSIFIER = """You classify a fraud analyst's message into exactly one intent. \
Reply with ONLY the intent word, nothing else.

Valid intents:
  triage           - working the alert queue, opening or resolving alerts
  explain_decision - why a specific transaction or alert scored what it did
  customer_lookup  - facts about a particular customer's account
  precedent        - searching historical fraud cases
  queue_stats      - counts, throughput, how the queue looks overall
  general          - anything else"""

ANALYST_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (EXPLAIN, re.compile(
        r"\b(why (was|did|is)|explain|reason(ing)?|what (triggered|fired|caused)|"
        r"which rules?|on what basis|justif)", re.IGNORECASE)),
    (QUEUE_STATS, re.compile(
        r"\b(how many|queue (stats|statistics|size|depth)|backlog|throughput|"
        r"open count|statistics|summary of the queue)\b", re.IGNORECASE)),
    # TRIAGE sits ABOVE customer_lookup and precedent deliberately. "Resolve ALERT-4d8172
    # as a false positive, the customer confirmed it was genuine" contains the word
    # "customer", and with customer_lookup first it routed there -- putting `resolve_alert`
    # out of scope and making the assistant say the resolution function was unavailable.
    # An instruction to act on an alert outranks a mention of who it belongs to.
    (TRIAGE, re.compile(
        r"\b(queue|alert|ALERT-|pending|triage|resolve|clear|confirm(ed)? fraud|"
        r"false positive|escalat|highest.risk|worst|review)\b", re.IGNORECASE)),
    (PRECEDENT, re.compile(
        # `seen .{0,30}before` rather than `seen (this|that) before`: the recorded beat is
        # "Have we seen this pattern before?", and the strict form does not match its own
        # button because of the word in the middle.
        r"\b(precedent|similar case|past case|historical|knowledge store|"
        r"seen .{0,30}before|case ?id|CASE-)\b", re.IGNORECASE)),
    (CUSTOMER_LOOKUP, re.compile(
        r"\b(customer|account holder|CUST-|who is|their (account|history|balance)|"
        r"profile of)\b", re.IGNORECASE)),
]

ANALYST_INTENT_TOOLS: dict[str, list[str]] = {
    TRIAGE: ["list_open_alerts", "get_alert_detail", "queue_stats", "resolve_alert",
             "explain_decision", "search_fraud_precedents"],
    EXPLAIN: ["explain_decision", "get_alert_detail", "search_fraud_precedents",
              "list_open_alerts", "list_recent_transactions"],
    CUSTOMER_LOOKUP: ["customer_360", "get_account_summary", "list_recent_transactions",
                      "get_security_status", "list_travel_notices"],
    PRECEDENT: ["search_fraud_precedents", "get_alert_detail", "explain_decision"],
    QUEUE_STATS: ["queue_stats", "list_open_alerts"],
    # `resolve_alert` is reachable from `general` as well as `triage`. Routing is a
    # narrowing convenience, not the security boundary -- the approval gate and the role
    # check in `tools.execute` are -- so an analyst who phrases a resolution unusually
    # should still be able to act rather than be told the function does not exist.
    Intent.GENERAL.value: ["list_open_alerts", "queue_stats", "get_alert_detail",
                           "explain_decision", "search_fraud_precedents", "customer_360",
                           "get_account_summary", "list_recent_transactions",
                           "resolve_alert"],
}

ANALYST_PRIMARY_TOOL: dict[str, str] = {
    TRIAGE: "list_open_alerts",
    EXPLAIN: "list_open_alerts",
    CUSTOMER_LOOKUP: "get_account_summary",
    PRECEDENT: "list_open_alerts",
    QUEUE_STATS: "queue_stats",
    Intent.GENERAL.value: "queue_stats",
}


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #

ADMIN_SYSTEM = ANALYST_SYSTEM.replace(
    "You are the fraud operations assistant for SentinelBank. You are "
    "helping an authenticated FRAUD ANALYST work their alert queue. You are not talking "
    "to a customer — speak to a professional who knows what a risk score is.",
    "You are the operations assistant for SentinelBank. You are helping an authenticated "
    "OPERATIONS ADMIN, who owns both the fraud queue and the running of the system "
    "itself — model spend, provider health, and the audit trail. Speak to a professional; "
    "give them the number and its basis.",
).replace(
    "  - Be direct and dense. An analyst wants the number, the reason, and what to do next.",
    "  - Cost figures are measured from recorded telemetry, never estimated. If a tool\n"
    "    returns no telemetry, say the meter is empty rather than inventing a number.\n"
    "  - Be direct and dense. Give the number, its basis, and what to do next.",
)

ADMIN_CLASSIFIER = """You classify an operations admin's message into exactly one intent. \
Reply with ONLY the intent word, nothing else.

Valid intents:
  triage           - working the alert queue, opening or resolving alerts
  explain_decision - why a specific transaction or alert scored what it did
  customer_lookup  - facts about a particular customer's account
  precedent        - searching historical fraud cases
  queue_stats      - counts, throughput, how the queue looks overall
  ops_cost         - model spend, token usage, cost per transaction
  system           - provider health, index size, uptime, configuration
  audit            - the audit log, who did what and when
  general          - anything else"""

ADMIN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (OPS_COST, re.compile(
        r"\b(cost|spend|spent|token|price|pricing|budget|saving|cheap|expensive|"
        r"per transaction|\$)", re.IGNORECASE)),
    (SYSTEM, re.compile(
        r"\b(health|status|uptime|provider|endpoint|circuit break|fallback|"
        r"index size|cache|model|configuration|config|is everything (ok|up|working))\b",
        re.IGNORECASE)),
    (AUDIT, re.compile(
        r"\b(audit|who (did|changed|approved|resolved)|log|trail|history of changes|"
        r"guardrail (trip|hit)|access)\b", re.IGNORECASE)),
    *ANALYST_PATTERNS,
]

ADMIN_INTENT_TOOLS: dict[str, list[str]] = {
    **ANALYST_INTENT_TOOLS,
    OPS_COST: ["cost_summary", "queue_stats", "system_health"],
    SYSTEM: ["system_health", "cost_summary", "queue_stats"],
    AUDIT: ["search_audit_log", "get_alert_detail", "explain_decision"],
    Intent.GENERAL.value: ANALYST_INTENT_TOOLS[Intent.GENERAL.value]
    + ["cost_summary", "system_health", "search_audit_log"],
}

ADMIN_PRIMARY_TOOL: dict[str, str] = {
    **ANALYST_PRIMARY_TOOL,
    OPS_COST: "cost_summary",
    SYSTEM: "system_health",
    AUDIT: "search_audit_log",
}


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #

CUSTOMER = Persona(
    role="customer",
    title="SentinelBank Assistant",
    status="Can act on your account · every action is logged",
    greeting=(
        "Hello. I can check your balance and recent activity, send money to a payee, "
        "top up a phone, pay tax, freeze or unfreeze your card, raise a dispute, file a "
        "travel notice, update your contact details, or run a security review."
    ),
    quick_actions=(
        # These six are recorded in the offline demo cache. Changing the prompt text
        # invalidates a recording; re-run `python -m core.record_demo` if you touch one.
        ("Account health", "How is my account looking?"),
        ("Security review", "Run a security review on my account"),
        ("Recent activity", "Show my recent transactions"),
        ("Travel budget", "I'm going to Spain for 8 days, plan my budget"),
        ("Statement", "Send me my statement"),
        ("Block card", "Freeze my card, I think it's been stolen"),
        ("My payees", "Who are my saved payees?"),
        ("Change password", "I want to change my password"),
    ),
    placeholder="Ask, or tell me what to do…",
    system_prompt=CUSTOMER_SYSTEM,
    intents=CUSTOMER_INTENT_TOOLS,
    primary_tool=CUSTOMER_PRIMARY_TOOL,
    patterns=CUSTOMER_PATTERNS,
    classifier_system=CUSTOMER_CLASSIFIER,
    finalise_system=CUSTOMER_FINALISE_SYSTEM,
    finalise_prompt=CUSTOMER_FINALISE,
    injection_refusal=CUSTOMER_INJECTION_REFUSAL,
    cache_prefix="chat-",          # unchanged: every recorded key must stay where it is
)

ANALYST = Persona(
    role="analyst",
    title="Fraud Ops Assistant",
    status="Reads the queue · resolutions ask you to confirm",
    greeting=(
        "Fraud ops assistant. I can work the alert queue, explain why any transaction "
        "scored what it did, search the case knowledge store, pull a customer's full "
        "picture, and resolve an alert once you confirm it."
    ),
    quick_actions=(
        ("Open queue", "What's in the alert queue right now?"),
        ("Highest risk", "Show me the highest-risk open alert"),
        ("Queue stats", "Give me the queue statistics"),
        ("Precedents", "Search the knowledge store for similar past cases"),
    ),
    placeholder="Ask about the queue, an alert, or a customer…",
    system_prompt=ANALYST_SYSTEM,
    intents=ANALYST_INTENT_TOOLS,
    primary_tool=ANALYST_PRIMARY_TOOL,
    patterns=ANALYST_PATTERNS,
    classifier_system=ANALYST_CLASSIFIER,
    finalise_system=STAFF_FINALISE_SYSTEM,
    finalise_prompt=STAFF_FINALISE,
    injection_refusal=STAFF_INJECTION_REFUSAL,
    cache_prefix="chat-staff-",
)

ADMIN = Persona(
    role="admin",
    title="Operations Assistant",
    status="Queue, cost and system health · writes ask you to confirm",
    greeting=(
        "Operations assistant. Everything the fraud-ops assistant does, plus what the "
        "model has cost, whether the providers are up, and who did what in the audit log."
    ),
    quick_actions=(
        ("Open queue", "What's in the alert queue right now?"),
        ("Queue stats", "Give me the queue statistics"),
        ("Model cost", "What has the model cost so far?"),
        ("System health", "Run a system health check"),
        ("Audit trail", "Show me the most recent audit log entries"),
    ),
    placeholder="Ask about the queue, cost, health, or the audit log…",
    system_prompt=ADMIN_SYSTEM,
    intents=ADMIN_INTENT_TOOLS,
    primary_tool=ADMIN_PRIMARY_TOOL,
    patterns=ADMIN_PATTERNS,
    classifier_system=ADMIN_CLASSIFIER,
    finalise_system=STAFF_FINALISE_SYSTEM,
    finalise_prompt=STAFF_FINALISE,
    injection_refusal=STAFF_INJECTION_REFUSAL,
    # Shares the analyst namespace on purpose -- see `cache_prefix` on Persona.
    cache_prefix="chat-staff-",
)

PERSONAS: dict[str, Persona] = {p.role: p for p in (CUSTOMER, ANALYST, ADMIN)}


def for_role(role: str | None) -> Persona:
    """Fail closed. An unknown role gets the least-privileged persona, never the most."""
    return PERSONAS.get((role or "").strip().lower(), CUSTOMER)


def quick_actions(role: str | None) -> list[dict[str, str]]:
    """Template- and recorder-friendly view of the quick action row."""
    return [{"label": label, "q": prompt} for label, prompt in for_role(role).quick_actions]
