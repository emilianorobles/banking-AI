"""SentinelBank Flask application factory.

The entire UI. All business logic stays in `core/` -- these modules only route, render
and serialise. That boundary is why this migration was possible at all: swapping the
whole presentation layer touched no rule, no agent and no part of the pipeline.
"""

from __future__ import annotations

import os
from datetime import timedelta

from flask import Flask, render_template, session

from core import config, db, money as money_mod
from core.money import fmt as money_fmt


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")

    # A demo-stable key so sessions survive a reload during rehearsal. Overridable for
    # anything resembling a real deployment.
    app.secret_key = os.getenv("SENTINELBANK_SECRET_KEY") or "sentinelbank-demo-" + "0" * 16
    app.permanent_session_lifetime = timedelta(hours=8)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        JSON_SORT_KEYS=False,
        TEMPLATES_AUTO_RELOAD=True,
    )

    db.init_db()

    from .routes import admin, agent, customer, demo, drill, ingest, notifications
    from . import auth as auth_module
    from .routes import auth as auth_routes

    app.register_blueprint(auth_routes.bp)
    app.register_blueprint(customer.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(demo.bp)
    app.register_blueprint(agent.bp)
    app.register_blueprint(drill.bp)
    app.register_blueprint(notifications.bp)
    app.register_blueprint(ingest.bp)

    # ---- template globals -------------------------------------------------
    @app.context_processor
    def inject_globals():
        from core import llm, rag, rules
        user = session.get("user")
        # Non-fatal on purpose. A rule ID that has drifted from RULE_META disables that
        # rule silently (evaluate() swallows the KeyError), so it is worth surfacing --
        # but not worth refusing to serve a page over, least of all mid-demo.
        rule_problems = rules.selfcheck()
        health = {
            "mode": config.DEMO_MODE,
            "api_key": config.has_api_key(),
            "index": rag.index_size(),
            "cached": llm.cache_size(),
            "model": config.CHAT_MODEL,
            "rules_ok": not rule_problems,
            "rule_problems": rule_problems,
        }
        # Staff get a customer switcher in the header; a customer never does, so we do not
        # pay for the query on their pages.
        customers = []
        if user and user.get("role") in ("analyst", "admin"):
            customers = db.list_customers(limit=200)

        return {
            "user": user,
            "sb_health": health,
            "active_customer_id": auth_module.active_customer_id() if user else None,
            "all_customers": customers,
            # Injected into the page as JSON so the browser reads the same table Python
            # does. See core/money.py for why this is not a second copy in a .js file.
            "currency_symbols": money_mod.SYMBOLS,
        }

    # Both filters delegate to core.money, which owns the symbol table. Changing these two
    # is most of the currency work: every template that shows a figure goes through one of
    # them, so `1,240.00 SGD` becomes `S$1,240.00 SGD` everywhere at once.
    @app.template_filter("money")
    def money(value, currency: str = "") -> str:
        return money_fmt(value, currency, dp=0)

    @app.template_filter("money2")
    def money2(value, currency: str = "") -> str:
        return money_fmt(value, currency)

    @app.template_filter("shortdt")
    def shortdt(value) -> str:
        return str(value or "")[:16].replace("T", " ")

    @app.template_filter("pct")
    def pct(value, digits: int = 0) -> str:
        try:
            return f"{float(value):+.{digits}f}%"
        except (TypeError, ValueError):
            return str(value)

    # ---- error handlers ---------------------------------------------------
    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404,
                               message="That page doesn't exist."), 404

    @app.errorhandler(500)
    def server_error(e):
        app.logger.exception("Unhandled error")
        return render_template("error.html", code=500,
                               message=f"Something went wrong: {e}"), 500

    return app
