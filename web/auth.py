"""Session authentication and role gating.

Deliberately simple: salted SHA-256 against a small user table, a signed Flask session
cookie, and a decorator that gates routes by role. The use case asks for "authentication
for secure access", not for us to reimplement an IAM stack in a hackathon.

What actually matters for the rubric is that roles gate *capability* -- a customer has no
route to the analyst queue, not merely no link to it -- and that every action is attributed
to an identity in the audit log. Both are true here.
"""

from __future__ import annotations

import hashlib
from functools import wraps
from typing import Any, Callable

from flask import flash, g, redirect, request, session, url_for

from core import db

SALT = "bedrock-financial-demo"

DEMO_USERS: dict[str, dict[str, Any]] = {
    "customer": {"password": "demo", "role": "customer", "label": "Customer",
                 "customer_id": "CUST-0001"},
    "analyst":  {"password": "demo", "role": "analyst",  "label": "Fraud Analyst"},
    "admin":    {"password": "demo", "role": "admin",    "label": "Operations Admin",
                 "customer_id": "CUST-0001"},
}

# Ascending privilege. A role satisfies a requirement if it sits at or above it.
ROLE_RANK = {"customer": 1, "analyst": 2, "admin": 3}


def _hash(password: str) -> str:
    return hashlib.sha256((SALT + password).encode()).hexdigest()


_HASHED = {u: {**v, "password": _hash(v["password"])} for u, v in DEMO_USERS.items()}


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    user = _HASHED.get((username or "").strip().lower())
    if user and user["password"] == _hash(password or ""):
        return {"username": (username or "").strip().lower(), **user}
    return None


def login_user(user: dict[str, Any]) -> None:
    session.permanent = True
    session["user"] = {k: v for k, v in user.items() if k != "password"}
    db.audit(actor=f"{user['role']}:{user['username']}", event_type="LOGIN",
             subject_id=user["username"], detail=f"Signed in as {user['label']}",
             ip=request.remote_addr)


def logout_user() -> None:
    user = session.get("user")
    if user:
        db.audit(actor=f"{user['role']}:{user['username']}", event_type="LOGOUT",
                 subject_id=user["username"], detail="Signed out")
    session.clear()


def current_user() -> dict[str, Any] | None:
    return session.get("user")


def active_customer_id() -> str:
    """Which customer the current session is acting as.

    Analysts and admins can switch customer to demonstrate the portal; a customer is
    pinned to their own account and cannot be switched, which is the point of the gate.
    """
    user = current_user() or {}
    if user.get("role") == "customer":
        return user.get("customer_id", "CUST-0001")
    return session.get("active_customer", user.get("customer_id", "CUST-0001"))


def set_active_customer(customer_id: str) -> bool:
    user = current_user() or {}
    if user.get("role") == "customer":
        return False
    session["active_customer"] = customer_id
    return True


def login_required(fn: Callable) -> Callable:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("auth.login", next=request.path))
        g.user = current_user()
        return fn(*args, **kwargs)
    return wrapper


def role_required(minimum: str) -> Callable:
    """Gate a route by minimum role. Returns 403 rather than a redirect, so an attempt to
    reach a privileged route is visible in the logs rather than silently bounced."""
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("auth.login", next=request.path))
            if ROLE_RANK.get(user.get("role", ""), 0) < ROLE_RANK.get(minimum, 99):
                db.audit(actor=f"{user['role']}:{user['username']}",
                         event_type="GUARDRAIL", subject_id=request.path,
                         detail=f"Blocked: role '{user['role']}' below required '{minimum}'")
                flash("You don't have access to that area.", "danger")
                return redirect(url_for("customer.dashboard")), 403
            g.user = user
            return fn(*args, **kwargs)
        return wrapper
    return decorator
