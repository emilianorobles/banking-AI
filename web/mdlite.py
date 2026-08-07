"""A small, safe markdown subset for chat replies.

The agent is *told* to "format any list of transactions as a short markdown table"
(`FINALISE_PROMPT` in `core/agents/customer_agent.py`), and it does. Nothing rendered
those tables, so the customer saw raw pipes and dashes. This is the renderer that makes
that instruction true rather than broken.

Two decisions worth keeping:

**It stays server-side.** `_render()` in `web/routes/agent.py` was the only escaping
boundary between model output and `bubble()`'s `innerHTML`, and moving rendering to the
client would create a second place that has to be right -- one that would re-escape what
the server already escaped, or worse, not escape at all. Recorded offline replies are
stored as text, so the server stays authoritative and the client stays a dumb sink.

**It is a line-based block parser, not a pile of `re.sub` calls.** A pipe table is a block
construct: a row only means anything relative to the separator line above it. The
line-at-a-time substitution this replaces structurally could not express that, which is
why tables never worked no matter how many patterns were added.

Order matters and is the whole safety argument:

    1. hold fenced code blocks out of the way
    2. `html.escape` every line -- BEFORE any markup is introduced
    3. block pass  (tables, headings, lists, quotes, paragraphs)
    4. inline pass (**bold**, *italic*, `code`)

Escaping first means merchant names and other outside text can never become markup. Every
tag this module emits is one it wrote itself, from a fixed vocabulary.
"""

from __future__ import annotations

import html
import re

__all__ = ["render"]

_FENCE = re.compile(r"^\s*```")
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_ULI = re.compile(r"^\s*[-*+]\s+(.*)$")
_OLI = re.compile(r"^\s*(\d{1,3})[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*&gt;\s?(.*)$")          # after escaping, '>' is '&gt;'
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")

# A table separator: |---|:--:|--:| with at least one cell.
_SEP_CELL = re.compile(r"^\s*:?-{2,}:?\s*$")

# Inline marks. Applied last, to already-escaped text.
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\*\w])\*([^\*\n]+?)\*(?!\*)")


def _cells(line: str) -> list[str]:
    """Split a pipe row into cells, dropping the leading/trailing pipe if present."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_separator(line: str) -> bool:
    if "|" not in line:
        return False
    cells = _cells(line)
    return bool(cells) and all(_SEP_CELL.match(c) for c in cells)


def _alignments(sep_line: str) -> list[str]:
    out = []
    for c in _cells(sep_line):
        c = c.strip()
        if c.endswith(":") and not c.startswith(":"):
            out.append("right")
        elif c.startswith(":") and c.endswith(":"):
            out.append("center")
        else:
            out.append("left")
    return out


def _inline(text: str) -> str:
    """Inline marks on already-escaped text. Code first, so `**` inside a span is literal."""
    out = _CODE.sub(lambda m: f'<code class="mono">{m.group(1)}</code>', text)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    return out


def _looks_numeric(value: str) -> bool:
    """A cell that should be right-aligned: money, counts, percentages.

    Tolerant of the currency symbols core/money.py emits, and of a trailing ISO code,
    because that is exactly what a transaction table is full of.
    """
    v = value.strip()
    if not v:
        return False
    v = re.sub(r"^(US\$|S\$|A\$|C\$|R\$|CHF|AED|[₹£€¥฿$])\s*", "", v)
    v = re.sub(r"\s*[A-Z]{3}$", "", v).strip()
    v = v.replace(",", "").rstrip("%").lstrip("+-")
    return bool(v) and re.fullmatch(r"\d+(\.\d+)?", v) is not None


def _render_table(rows: list[str], sep: str) -> str:
    """`<table class="tbl">` -- which is why the dead `.msg table` CSS starts working."""
    align = _alignments(sep)
    header = _cells(rows[0])
    body = [_cells(r) for r in rows[1:]]
    width = max([len(header)] + [len(b) for b in body] or [0])

    # A column is numeric if the separator said so, or if every populated body cell in it
    # looks like a number. The second half is what saves us when the model omits `--:`.
    numeric = []
    for i in range(width):
        if i < len(align) and align[i] == "right":
            numeric.append(True)
            continue
        col = [b[i] for b in body if i < len(b) and b[i].strip()]
        numeric.append(bool(col) and all(_looks_numeric(c) for c in col))

    def cell(tag: str, value: str, i: int) -> str:
        cls = ' class="num"' if i < len(numeric) and numeric[i] else ""
        return f"<{tag}{cls}>{_inline(value)}</{tag}>"

    out = ['<div class="tbl-wrap"><table class="tbl"><thead><tr>']
    out += [cell("th", header[i] if i < len(header) else "", i) for i in range(width)]
    out.append("</tr></thead><tbody>")
    for b in body:
        out.append("<tr>")
        out += [cell("td", b[i] if i < len(b) else "", i) for i in range(width)]
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def render(text: str) -> str:
    """Markdown subset -> HTML. Safe for `innerHTML`: everything is escaped first."""
    if not text:
        return ""

    # --- 1. hold fenced blocks out, so nothing below reformats their contents -----
    fenced: list[str] = []

    def _stash(lines: list[str]) -> str:
        fenced.append(html.escape("\n".join(lines)))
        return f"\x00FENCE{len(fenced) - 1}\x00"

    raw_lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    i = 0
    while i < len(raw_lines):
        if _FENCE.match(raw_lines[i]):
            block, i = [], i + 1
            while i < len(raw_lines) and not _FENCE.match(raw_lines[i]):
                block.append(raw_lines[i])
                i += 1
            i += 1                                   # consume the closing fence
            lines.append(_stash(block))
        else:
            # --- 2. escape every line before any markup exists ---------------------
            lines.append(html.escape(raw_lines[i]))
            i += 1

    # --- 3. block pass ------------------------------------------------------------
    out: list[str] = []
    para: list[str] = []
    i = 0

    def flush_para() -> None:
        if para:
            out.append("<p>" + _inline("<br>".join(para)) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]

        if line.startswith("\x00FENCE"):
            flush_para()
            idx = int(line[6:-1])
            out.append(f'<pre class="code">{fenced[idx]}</pre>')
            i += 1
            continue

        if not line.strip():
            flush_para()
            i += 1
            continue

        # Table: a header row, then a separator row. Checked before lists, because a
        # separator line is also a plausible horizontal rule.
        if ("|" in line and i + 1 < len(lines) and _is_separator(lines[i + 1])):
            flush_para()
            sep = lines[i + 1]
            rows = [line]
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append(lines[j])
                j += 1
            out.append(_render_table(rows, sep))
            i = j
            continue

        if _HR.match(line):
            flush_para()
            out.append('<hr class="divider">')
            i += 1
            continue

        m = _HEADING.match(line)
        if m:
            flush_para()
            level = min(4, max(3, len(m.group(1)) + 2))   # h1 in a chat bubble is absurd
            out.append(f"<h{level}>{_inline(m.group(2).strip())}</h{level}>")
            i += 1
            continue

        if _QUOTE.match(line):
            flush_para()
            quoted = []
            while i < len(lines) and (m := _QUOTE.match(lines[i])):
                quoted.append(m.group(1))
                i += 1
            out.append('<blockquote class="note">' + _inline("<br>".join(quoted)) +
                       "</blockquote>")
            continue

        if _ULI.match(line) or _OLI.match(line):
            flush_para()
            ordered = _OLI.match(line) is not None
            items = []
            while i < len(lines):
                mu, mo = _ULI.match(lines[i]), _OLI.match(lines[i])
                if ordered and mo:
                    items.append(mo.group(2))
                elif not ordered and mu:
                    items.append(mu.group(1))
                else:
                    break
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(it)}</li>" for it in items) +
                       f"</{tag}>")
            continue

        para.append(line.strip())
        i += 1

    flush_para()
    return "".join(out)
