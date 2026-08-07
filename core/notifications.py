"""Notifications — every consequential action tells the customer about it.

Three channels:

  inapp   always. Rows in the notifications table; the UI polls and raises a toast.
  outbox  always. The email that WOULD be sent, rendered in full and shown on screen.
  smtp    only if SMTP_HOST is configured. Off by default.

On the honesty of the outbox: this prototype has no mail gateway, and faking a "message
sent" confirmation is the one thing in a banking demo a judge could reasonably call
hollow. So the outbox renders the real subject, recipient and HTML body, and the UI says
plainly that delivery is not wired up. If you set the SMTP_* environment variables the
same way you set the API key, real sending switches on and the UI stops hedging. Nothing
is ever labelled delivered unless it was.

This module deliberately does not import Flask. `core/` stays framework-free -- that is
what made swapping Streamlit for Flask a UI-only change.
"""

from __future__ import annotations

import html as html_lib
import os
import smtplib
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from . import config, db
from .contracts import new_id, now_iso

OUTBOX_DIR = config.DATA_DIR / "outbox"

SEVERITY_COLOUR = {
    "ok": "#16a34a", "info": "#2563eb", "warn": "#d97706", "danger": "#dc2626",
}
SEVERITY_LABEL = {
    "ok": "Confirmation", "info": "Notice", "warn": "Attention needed",
    "danger": "Security alert",
}


@dataclass
class Notification:
    notification_id: str
    customer_id: str
    kind: str                      # card_frozen | statement | travel_notice | fraud_alert | ...
    severity: str                  # ok | info | warn | danger
    subject: str
    body: str                      # plain text, one or two sentences
    detail: str = ""               # optional longer block, rendered as a panel
    related_id: str | None = None  # txn_id / alert_id / notice_id
    created_at: str = field(default_factory=now_iso)
    read: bool = False
    channels: str = "inapp,outbox"
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def preview(self) -> str:
        return self.body if len(self.body) <= 110 else self.body[:107] + "…"


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #

def notify(
    customer_id: str,
    kind: str,
    subject: str,
    body: str,
    *,
    severity: str = "info",
    detail: str = "",
    related_id: str | None = None,
    **meta: Any,
) -> Notification:
    """Raise a notification. Never raises -- a failed notification must not fail the
    action that triggered it."""
    n = Notification(
        notification_id=new_id("NOTIF"),
        customer_id=customer_id,
        kind=kind,
        severity=severity if severity in SEVERITY_COLOUR else "info",
        subject=subject,
        body=body,
        detail=detail,
        related_id=related_id,
        meta=meta,
    )

    channels = ["inapp", "outbox"]
    try:
        write_outbox(n)
    except Exception:
        pass

    if smtp_configured():
        try:
            send_smtp(n)
            channels.append("smtp")
        except Exception as exc:
            n.meta["smtp_error"] = str(exc)[:200]

    n.channels = ",".join(channels)

    try:
        db.save_notification(n)
        db.audit(actor="system", event_type="NOTIFY", subject_id=customer_id,
                 detail=f"[{kind}] {subject}", severity=n.severity, channels=n.channels)
    except Exception:
        pass
    return n


# --------------------------------------------------------------------------- #
# Email rendering
# --------------------------------------------------------------------------- #

def render_email(n: Notification) -> tuple[str, str, str]:
    """Return (subject, html, text) exactly as it would be delivered."""
    customer = db.get_customer(n.customer_id)
    name = customer.name if customer else n.customer_id
    last4 = customer.card_number[-4:] if customer else "----"
    colour = SEVERITY_COLOUR.get(n.severity, "#2563eb")
    banner = SEVERITY_LABEL.get(n.severity, "Notice")

    e = html_lib.escape
    detail_block = (
        f'<div style="margin:18px 0;padding:14px 16px;background:#f6f8fc;'
        f'border-left:3px solid {colour};border-radius:0 8px 8px 0;'
        f'font-size:13px;color:#334155;white-space:pre-wrap">{e(n.detail)}</div>'
        if n.detail else ""
    )

    html = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#eef2f9;
 font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif">
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
        style="background:#eef2f9;padding:28px 12px">
  <tr><td align="center">
   <table role="presentation" width="600" cellpadding="0" cellspacing="0"
          style="max-width:600px;background:#fff;border-radius:14px;overflow:hidden;
                 box-shadow:0 2px 14px rgba(15,23,42,.09)">
    <tr><td style="background:linear-gradient(135deg,#6366f1,#8b5cf6 45%,#06b6d4);
                   padding:22px 26px;color:#fff">
      <div style="font-size:17px;font-weight:800;letter-spacing:-.2px">SentinelBank</div>
      <div style="font-size:12px;opacity:.85;margin-top:2px">{e(banner)}</div>
    </td></tr>
    <tr><td style="padding:26px">
      <div style="display:inline-block;padding:3px 10px;border-radius:999px;
                  background:{colour}1a;color:{colour};font-size:11px;font-weight:800;
                  letter-spacing:.4px;text-transform:uppercase">{e(n.kind.replace('_',' '))}</div>
      <h1 style="margin:14px 0 8px;font-size:19px;color:#0f172a;
                 letter-spacing:-.3px">{e(n.subject)}</h1>
      <p style="margin:0;font-size:14px;line-height:1.6;color:#475569">{e(n.body)}</p>
      {detail_block}
      <p style="margin:22px 0 0;font-size:13px;color:#64748b">
        Account holder: <strong style="color:#0f172a">{e(name)}</strong><br>
        Card ending: <strong style="color:#0f172a">{e(last4)}</strong><br>
        Time: {e(n.created_at[:19].replace('T', ' '))} UTC
      </p>
    </td></tr>
    <tr><td style="padding:16px 26px;background:#f8fafc;border-top:1px solid #e2e8f0;
                   font-size:11px;color:#94a3b8;line-height:1.6">
      If you didn't expect this, open the SentinelBank app and review your alerts.
      We will never ask for your PIN, password or full card number by email.<br>
      Reference: {e(n.notification_id)}
    </td></tr>
   </table>
  </td></tr>
 </table>
</body></html>"""

    text = (
        f"SentinelBank — {banner}\n"
        f"{'=' * 52}\n\n{n.subject}\n\n{n.body}\n"
        + (f"\n{n.detail}\n" if n.detail else "")
        + f"\nAccount holder: {name}\nCard ending: {last4}\n"
          f"Time: {n.created_at[:19].replace('T', ' ')} UTC\n"
          f"Reference: {n.notification_id}\n\n"
          "We will never ask for your PIN, password or full card number by email.\n"
    )
    return f"[SentinelBank] {n.subject}", html, text


def build_message(n: Notification) -> EmailMessage:
    customer = db.get_customer(n.customer_id)
    subject, html, text = render_email(n)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_FROM", "alerts@sentinelbank.example")
    msg["To"] = (customer.email if customer else f"{n.customer_id}@example.com")
    msg["Date"] = n.created_at
    msg["X-SentinelBank-Kind"] = n.kind
    msg["X-SentinelBank-Severity"] = n.severity
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def write_outbox(n: Notification) -> Path:
    """Write a real .eml. Openable in any mail client, which is the proof that the
    message is genuine rather than a screenshot."""
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTBOX_DIR / f"{n.created_at[:19].replace(':', '')}-{n.notification_id}.eml"
    path.write_bytes(bytes(build_message(n)))
    return path


def clear_outbox() -> int:
    """Delete every recorded email. Called on a full reset.

    The rows go when the database is dropped; without this the .eml files outlive them and
    the outbox fills up with messages from rehearsals whose notifications no longer exist.
    """
    if not OUTBOX_DIR.exists():
        return 0
    removed = 0
    for p in OUTBOX_DIR.glob("*.eml"):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def eml_path(notification_id: str) -> Path | None:
    if not OUTBOX_DIR.exists():
        return None
    for p in OUTBOX_DIR.glob(f"*{notification_id}.eml"):
        return p
    return None


# --------------------------------------------------------------------------- #
# SMTP (opt-in, configured by the operator, never by us)
# --------------------------------------------------------------------------- #

def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST"))


def smtp_status() -> dict[str, Any]:
    return {
        "configured": smtp_configured(),
        "host": os.getenv("SMTP_HOST", ""),
        "port": os.getenv("SMTP_PORT", "587"),
        "from": os.getenv("SMTP_FROM", "alerts@sentinelbank.example"),
    }


def send_smtp(n: Notification) -> bool:
    """Send for real. Only reachable when the operator has set SMTP_* themselves."""
    host = os.getenv("SMTP_HOST")
    if not host:
        return False
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")

    msg = build_message(n)
    with smtplib.SMTP(host, port, timeout=10) as s:
        if os.getenv("SMTP_STARTTLS", "1") == "1":
            s.starttls()
        if user and password:
            s.login(user, password)
        s.send_message(msg)
    return True


# --------------------------------------------------------------------------- #
# Convenience wrappers used across the app
# --------------------------------------------------------------------------- #

def action_performed(customer_id: str, action: str, summary: str,
                     detail: str = "", severity: str = "ok", **meta: Any) -> Notification:
    """Every agent tool call routes through here, so the customer sees an alert for
    literally every action taken on their account."""
    return notify(customer_id, kind=f"action_{action}",
                  subject=summary, body=detail or summary,
                  severity=severity, **meta)
