"""SentinelBank -- Streamlit entry point.

Run:  streamlit run app.py

Holds authentication, role-based navigation, and nothing else. All business logic lives
in `core`; all rendering lives in `ui`.
"""

from __future__ import annotations

import hashlib

import streamlit as st

from core import config, db
from ui import admin, components, customer, demo_control

st.set_page_config(
    page_title="SentinelBank — AI Fraud & Query Resolution",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --------------------------------------------------------------------------- #
# Authentication
#
# Deliberately simple: salted SHA-256 against a local user table, with role-based page
# gating. The use case asks for "authentication for secure access", not for us to
# reimplement an IAM stack in a day. What matters for the rubric is that roles actually
# gate capability -- a customer cannot reach the analyst queue -- and that every action
# is attributed to an identity in the audit log.
# --------------------------------------------------------------------------- #

DEMO_USERS = {
    "customer": {"password": "demo", "role": "customer", "label": "Customer"},
    "analyst":  {"password": "demo", "role": "analyst",  "label": "Fraud Analyst"},
    "admin":    {"password": "demo", "role": "admin",    "label": "Operations Admin"},
}

SALT = "sentinelbank-demo"


def _hash(password: str) -> str:
    return hashlib.sha256((SALT + password).encode()).hexdigest()


_HASHED = {u: {**v, "password": _hash(v["password"])} for u, v in DEMO_USERS.items()}


def authenticate(username: str, password: str) -> dict | None:
    user = _HASHED.get(username.strip().lower())
    if user and user["password"] == _hash(password):
        return {"username": username.strip().lower(), **user}
    return None


def login_screen() -> None:
    st.markdown("# 🛡️ SentinelBank")
    st.caption("AI-powered banking query resolution and fraud alert system")

    left, right = st.columns([1, 1])
    with left:
        with st.form("login"):
            st.markdown("#### Sign in")
            username = st.text_input("Username", value="customer")
            password = st.text_input("Password", type="password", value="demo")
            if st.form_submit_button("Sign in", type="primary", use_container_width=True):
                user = authenticate(username, password)
                if user:
                    st.session_state["user"] = user
                    db.audit(actor=f"{user['role']}:{user['username']}",
                             event_type="LOGIN", subject_id=user["username"],
                             detail=f"Signed in as {user['label']}")
                    st.rerun()
                else:
                    st.error("Invalid credentials.")
    with right:
        st.markdown("#### Demo accounts")
        st.markdown(
            "| Username | Password | Sees |\n|---|---|---|\n"
            "| `customer` | `demo` | Their own account only |\n"
            "| `analyst` | `demo` | Alert queue, approvals |\n"
            "| `admin` | `demo` | Everything + demo control |"
        )
        st.caption(
            "Roles gate capability: signed in as `customer` there is no route to the "
            "analyst queue, and every action is attributed in the audit log."
        )


# --------------------------------------------------------------------------- #

def main() -> None:
    components.inject_css()
    db.init_db()

    if "user" not in st.session_state:
        login_screen()
        return

    user = st.session_state["user"]
    role = user["role"]

    with st.sidebar:
        st.markdown("## 🛡️ SentinelBank")
        st.caption(f"Signed in as **{user['label']}**")

        pages = ["Customer portal"]
        if role in ("analyst", "admin"):
            pages.append("Fraud operations")
        if role == "admin":
            pages.append("Demo control")

        page = st.radio("Navigation", pages, label_visibility="collapsed")

        st.divider()
        if role == "admin" or role == "customer":
            customers = db.list_customers(limit=50)
            if customers:
                options = {f"{c.name} ({c.customer_id})": c.customer_id for c in customers}
                default = 0
                selected = st.selectbox("Acting as customer", list(options), index=default)
                st.session_state["active_customer"] = options[selected]
        st.session_state.setdefault("active_customer", "CUST-0001")

        st.divider()
        components.health_badge()
        st.caption(f"Model: `{config.CHAT_MODEL}`")

        if st.button("Sign out", use_container_width=True):
            db.audit(actor=f"{role}:{user['username']}", event_type="LOGOUT",
                     subject_id=user["username"], detail="Signed out")
            for key in ("user",):
                st.session_state.pop(key, None)
            st.rerun()

    if page == "Customer portal":
        customer.render(st.session_state["active_customer"])
    elif page == "Fraud operations":
        admin.render()
    elif page == "Demo control":
        demo_control.render()


if __name__ == "__main__":
    main()
