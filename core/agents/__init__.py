"""Agent layer: personas, router, customer service, fraud analyst, and the tool registry.

`staff_tools` is imported for its side effect -- the `@tool` decorators register the
analyst and admin tools into `tools.REGISTRY`. Without this line the staff persona's
allowlist names tools that do not exist, and every staff turn dead-ends in a scope block
that reads like a model failure rather than a missing import.
"""

from . import staff_tools as _staff_tools  # noqa: F401  (registration side effect)
