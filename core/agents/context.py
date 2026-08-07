"""Who is asking, and about whose account.

The chat surface is one widget shared by three roles, so every layer below it needs two
facts that used to be one: the *account in scope* (which it already had, as a bare
`customer_id` string) and the *person acting* (which it did not have at all).

That gap was a governance defect, not a cosmetic one. Every audit row written from the
agent path was hardcoded `f"customer:{customer_id}"`, so an admin freezing a card through
chat was logged as the customer whose card it was. The audit log is a scored rubric item
and it was recording the wrong actor for the one class of action where the actor matters
most.

`AgentContext` is deliberately NOT in `core/contracts.py`. It belongs to the agent layer
rather than the data model, nothing persisted is written against it, and `contracts.py` is
a five-person interface that should not move for one subsystem's convenience.
"""

from __future__ import annotations

from dataclasses import dataclass

# Ascending privilege, mirroring web/auth.py ROLE_RANK. Duplicated deliberately: `core/`
# must not import from `web/` -- the whole point of the layering is that the pipeline runs
# headless, in the eval harness and behind the FastAPI service, with no Flask in sight.
ROLES = ("customer", "analyst", "admin")
STAFF_ROLES = frozenset({"analyst", "admin"})

DEFAULT_ROLE = "customer"


@dataclass(frozen=True)
class AgentContext:
    """One chat turn's identity.

    Frozen because it is read at half a dozen points in a single turn and none of them
    should be able to widen their own privileges partway through.
    """

    customer_id: str
    role: str = DEFAULT_ROLE
    username: str = ""

    @property
    def actor(self) -> str:
        """The audit-log actor string, e.g. `analyst:analyst`, `customer:CUST-0001`.

        A customer is identified by their account id rather than their login name,
        because that is the id every other row in the audit log is keyed by and a
        customer has exactly one. Staff are identified by who they signed in as.
        """
        if self.role == "customer":
            return f"customer:{self.customer_id}"
        return f"{self.role}:{self.username or self.role}"

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF_ROLES

    @classmethod
    def for_customer(cls, customer_id: str) -> AgentContext:
        """A plain customer context.

        The compatibility shim: `core/record_demo.py`, `ui/customer.py` and the eval
        harness all call the agent with a bare `customer_id` and no notion of a session.
        They get exactly the behaviour they had before this module existed.
        """
        return cls(customer_id=customer_id, role=DEFAULT_ROLE, username=customer_id)


def normalise_role(role: str | None) -> str:
    """Fail closed. An unrecognised role is the least-privileged one, never the most."""
    candidate = (role or "").strip().lower()
    return candidate if candidate in ROLES else DEFAULT_ROLE
