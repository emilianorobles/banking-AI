"""Currency formatting -- one table, declared once, used by Python and by the page.

The obvious alternative was a symbol table in `.py` and a matching one in `.js`. That is
exactly how `RULE_CATALOGUE` drifted from `core/rules.py`: two copies of the same facts
and no mechanism that makes them disagree loudly. Generating the `.js` from the `.py`
would fix it but needs a build step, and the demo has to run with the wifi off and no
toolchain. So Python declares the table and `base.html` injects it as JSON into
`window.SB_CURRENCY` -- one declaration site, no build.

It lives in `core/` rather than `web/` because most of the call sites are
`core/insights.py` and `core/agents/tools.py`, and `core/` may not import from `web/`.

>>> fmt(3854.59, "INR")
'₹3,854.59 INR'
>>> fmt(980, "USD")
'US$980.00 USD'

WINDOWS CONSOLE HAZARD. Most of these symbols are outside cp1252, and this project has
already lost a launch to exactly that: an arrow in `run_web.py`'s startup banner raised
UnicodeEncodeError on a Windows console before Flask ever bound a port. The rule is
therefore: **`fmt()` output must never reach a bare `print()`**. It is for HTML, for JSON
and for `.eml` files written with an explicit utf-8 encoding -- all of which are fine.
"""

from __future__ import annotations

SYMBOLS: dict[str, str] = {
    "INR": "₹",     # rupee
    "GBP": "£",
    "EUR": "€",
    "USD": "US$",        # not a bare $ -- five of these currencies would claim it
    "SGD": "S$",
    "BRL": "R$",
    "AUD": "A$",
    "CAD": "C$",
    "JPY": "¥",
    "CHF": "CHF",        # no symbol in common use
    "AED": "AED",
    "THB": "฿",     # baht
}

# Currencies conventionally written without decimal places.
ZERO_DECIMAL: frozenset[str] = frozenset({"JPY"})


def symbol(code: str) -> str:
    """The symbol for an ISO code, falling back to the code itself.

    An unknown currency renders as its code rather than a guess or a bare `$` -- being
    wrong about which dollar a figure is in is worse than being plain.
    """
    return SYMBOLS.get((code or "").strip().upper(), (code or "").strip().upper())


def fmt(amount, currency: str = "", *, dp: int | None = None, code: bool = True) -> str:
    """Format an amount with its symbol, and by default its ISO code as well.

    The code is kept because the symbol alone is genuinely ambiguous for a bank: `$`, `S$`
    and `A$` are different money, and a customer looking at a foreign charge needs to know
    which. `code=False` is for places that have already said the currency once.

    Never raises on bad input -- it is used in templates, where an exception is a blank page.
    """
    ccy = (currency or "").strip().upper()
    if dp is None:
        dp = 0 if ccy in ZERO_DECIMAL else 2
    try:
        body = f"{float(amount):,.{dp}f}"
    except (TypeError, ValueError):
        return str(amount)

    if not ccy:
        return body

    sym = symbol(ccy)
    # A multi-letter "symbol" like CHF or AED is a word, so it takes a space; a real
    # symbol sits tight against the digits.
    out = f"{sym} {body}" if sym.isalpha() and len(sym) > 1 else f"{sym}{body}"
    return f"{out} {ccy}" if code and sym != ccy else out
