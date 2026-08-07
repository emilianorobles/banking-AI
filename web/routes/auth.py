"""Sign in, sign out, and switching which customer a staff user is viewing."""

from __future__ import annotations

from flask import (Blueprint, flash, redirect, render_template, request,
                   session, url_for)

from core import db

from .. import auth

bp = Blueprint("auth", __name__)


@bp.get("/login")
def login():
    if auth.current_user():
        return redirect(url_for("customer.dashboard"))
    return render_template("login.html", demo_users=auth.DEMO_USERS)


@bp.post("/login")
def login_post():
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    user = auth.authenticate(username, password)

    if not user:
        # Deliberately not "no such user" vs "wrong password" -- that difference is a
        # free account-enumeration oracle, and this is a banking app.
        db.audit(actor=f"anonymous:{username[:32]}", event_type="LOGIN_FAILED",
                 subject_id=username[:32], detail="Invalid credentials",
                 ip=request.remote_addr)
        flash("Those credentials weren't recognised.", "danger")
        return render_template("login.html", demo_users=auth.DEMO_USERS,
                               username=username), 401

    auth.login_user(user)

    # Arm the post-login fraud prompt. Customers only: staff land on /ops/overview, which
    # IS the queue, and a modal about someone else's account would be noise there. The
    # flag is one-shot -- `GET /api/alerts/pending` pops it -- so the prompt fires once
    # per sign-in rather than on every navigation.
    session["alert_popup_pending"] = user["role"] == "customer"

    nxt = request.form.get("next") or request.args.get("next")
    if nxt and nxt.startswith("/"):      # never redirect off-site on a login hop
        return redirect(nxt)
    return redirect(url_for("admin.overview") if user["role"] in ("analyst", "admin")
                    else url_for("customer.dashboard"))


@bp.get("/logout")
def logout():
    auth.logout_user()
    flash("Signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.post("/switch-customer")
@auth.login_required
def switch_customer():
    """Staff view the portal as a chosen customer. A customer cannot switch -- that gate
    is enforced in `auth.set_active_customer`, not merely hidden in the template."""
    target = request.form.get("customer_id", "").strip()
    if not auth.set_active_customer(target):
        flash("You can only view your own account.", "warn")
    else:
        session.pop("chat_history", None)
        session.pop("pending_approval", None)
    return redirect(request.referrer or url_for("customer.dashboard"))


@bp.get("/")
def root():
    user = auth.current_user()
    if not user:
        return redirect(url_for("auth.login"))
    return redirect(url_for("admin.overview") if user["role"] in ("analyst", "admin")
                    else url_for("customer.dashboard"))
