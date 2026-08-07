"""Blueprints. One module per surface, each one thin.

A route here is allowed to: read the session, call `core/`, and shape the result for a
template or a JSON response. It is not allowed to make a decision about a transaction,
score anything, or talk to the model directly. Everything that reasons lives in `core/`,
which is what made replacing Streamlit a presentation-only change.
"""
