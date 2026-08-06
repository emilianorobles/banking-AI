"""Travel notices -- the false-positive killer.

A customer who tells us they are travelling should not have their card declined at
dinner in Barcelona. This module is deliberately small and deterministic: geography
rules get suppressed for declared destinations and dates, and nothing else changes.

Two entry points, one implementation:
  * the form in the customer portal          -> created_via="form"
  * the `set_travel_notice` agent tool       -> created_via="chat_agent"

Suppression only ever touches GEO rules. A stolen card used in a declared country at
3am for 12x the customer's usual amount still trips velocity and amount rules -- a
travel notice is not a blanket amnesty, and saying so out loud is worth a mark.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from . import db
from .contracts import Transaction, TravelNotice, new_id

# Rules that a travel notice is allowed to suppress.
SUPPRESSIBLE_RULES = {"GEO_FOREIGN", "GEO_IMPOSSIBLE_TRAVEL", "GEO_NEW_COUNTRY"}

# Common country names -> ISO-2, so the chat agent can pass "Spain" and it just works.
COUNTRY_ALIASES: dict[str, str] = {
    "spain": "ES", "france": "FR", "germany": "DE", "italy": "IT",
    "united kingdom": "GB", "uk": "GB", "england": "GB", "britain": "GB",
    "united states": "US", "usa": "US", "us": "US", "america": "US",
    "india": "IN", "singapore": "SG", "japan": "JP", "china": "CN",
    "australia": "AU", "canada": "CA", "brazil": "BR", "mexico": "MX",
    "uae": "AE", "dubai": "AE", "netherlands": "NL", "switzerland": "CH",
    "thailand": "TH", "indonesia": "ID", "malaysia": "MY", "vietnam": "VN",
    "south africa": "ZA", "nigeria": "NG", "kenya": "KE", "portugal": "PT",
    "ireland": "IE", "belgium": "BE", "sweden": "SE", "norway": "NO",
    "poland": "PL", "turkey": "TR", "argentina": "AR", "chile": "CL",
}


def normalise_country(value: str) -> str:
    """Accept 'Spain', 'spain', or 'ES' and return 'ES'."""
    v = (value or "").strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return COUNTRY_ALIASES.get(v.lower(), v.upper()[:2])


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:10]).date()


def create_notice(
    customer_id: str,
    countries: list[str],
    start_date: str | date,
    end_date: str | date,
    created_via: str = "form",
) -> TravelNotice:
    """Create and persist a travel notice. Country names are normalised to ISO-2."""
    codes = [normalise_country(c) for c in countries if str(c).strip()]
    if not codes:
        raise ValueError("At least one destination country is required.")

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if end < start:
        raise ValueError("End date cannot be before start date.")

    notice = TravelNotice(
        notice_id=new_id("TRV"),
        customer_id=customer_id,
        countries=codes,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        created_via=created_via,
    )
    db.save_travel_notice(notice)
    db.audit(
        actor=f"customer:{customer_id}",
        event_type="TRAVEL_NOTICE",
        subject_id=notice.notice_id,
        detail=f"Travel notice for {', '.join(codes)} from {notice.start_date} to {notice.end_date}",
        via=created_via,
    )
    return notice


def active_notices(customer_id: str) -> list[TravelNotice]:
    return db.list_travel_notices(customer_id=customer_id, active_only=True)


def covering_notice(txn: Transaction) -> TravelNotice | None:
    """Return the notice that covers this transaction's country and date, if any.

    A one-day grace period on each side absorbs timezone drift and overnight flights --
    the alternative is declining a card at an airport at 00:30 local time.
    """
    try:
        txn_date = _parse_date(txn.timestamp)
    except Exception:
        return None

    for notice in active_notices(txn.customer_id):
        if txn.country.upper() not in notice.countries:
            continue
        try:
            start = _parse_date(notice.start_date) - timedelta(days=1)
            end = _parse_date(notice.end_date) + timedelta(days=1)
        except Exception:
            continue
        if start <= txn_date <= end:
            return notice
    return None


def is_suppressed(txn: Transaction) -> tuple[bool, str | None]:
    """(suppressed, notice_id). Used by the pipeline to drop geo rule hits."""
    notice = covering_notice(txn)
    return (notice is not None), (notice.notice_id if notice else None)


def cancel(notice_id: str, actor: str = "customer") -> None:
    db.cancel_travel_notice(notice_id)
    db.audit(actor=actor, event_type="TRAVEL_NOTICE",
             subject_id=notice_id, detail="Travel notice cancelled")


def describe(notice: TravelNotice) -> str:
    return f"{', '.join(notice.countries)} · {notice.start_date} → {notice.end_date}"
