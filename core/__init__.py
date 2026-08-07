"""BedRock Financial core package.

Importing `core` applies the runtime environment fixes in core.config (SSL, OpenMP,
Streamlit watcher) before anything else touches httpx or langchain.
"""

from . import config  # noqa: F401  -- imported for its import-time side effects

__all__ = ["config"]
