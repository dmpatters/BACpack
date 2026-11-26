"""The PICS as a web page. Converts only the Markdown subset `pics` writes
(headings, paragraphs, bullet lists, pipe tables, inline code), so no
Markdown library is needed."""
from __future__ import annotations

import html
import re

from .pics import EDITOR_NOTE

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\|(\s*:?-+:?\s*\|)+$")
_ESCAPED_PIPE = "\x00"

_STYLE = """
:root { color-scheme: light dark; }
body { font: 15px/1.5 system-ui, sans-serif; margin: 0; padding: 24px 32px 48px; }
main { max-width: 1100px; margin: 0 auto; }
h1 { margin-top: 0; }
h2 { margin-top: 2em; border-bottom: 1px solid color-mix(in srgb, currentColor 25%, transparent); }
table { border-collapse: collapse; margin: 0.5em 0 1em; }
th, td { border: 1px solid color-mix(in srgb, currentColor 25%, transparent); padding: 4px 10px;
  text-align: left; vertical-align: top; }
tbody tr:nth-child(even) { background: color-mix(in srgb, currentColor 6%, transparent); }
code { font-size: 0.9em; }
"""


def _inline(text: str) -> str:
    parts = text.split("`")
    if len(parts) % 2 == 0:
        # An unmatched backtick stays a literal one.
        parts[-2:] = [parts[-2] + "`" + parts[-1]]
    return "".join(
        f"<code>{html.escape(part, quote=False)}</code>" if i % 2 else html.escape(part, quote=False)
        for i, part in enumerate(parts)
    )


def _cells(row: str) -> list[str]:
    inner = row.strip().replace("\\|", _ESCAPED_PIPE)[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [_inline(cell.strip().replace(_ESCAPED_PIPE, "|")) for cell in inner.split("|")]


def _table(rows: list[str]) -> str:
    head, body = rows[0], [r for r in rows[1:] if not _TABLE_SEPARATOR.match(r.strip())]
    lines = ["<table>", "<thead><tr>" + "".join(f"<th>{c}</th>" for c in _cells(head)) + "</tr></thead>", "<tbody>"]
    lines += ["<tr>" + "".join(f"<td>{c}</td>" for c in _cells(r)) + "</tr>" for r in body]
    lines += ["</tbody>", "</table>"]
    return "\n".join(lines)


def _kind(line: str) -> str:
    if not line.strip():
        return "blank"
    if _HEADING.match(line):
        return "heading"
    if line.startswith("- "):
        return "item"
    if line.lstrip().startswith("|"):
        return "row"
    return "text"


def markdown_to_html(markdown: str) -> str:
    out: list[str] = []
    run: list[str] = []
    run_kind = "blank"

    def flush() -> None:
        if run_kind == "item":
            out.append("<ul>\n" + "\n".join(f"<li>{_inline(line[2:])}</li>" for line in run) + "\n</ul>")
        elif run_kind == "row":
            out.append(_table(run))
        elif run_kind == "text":
            out.append("<p>" + "\n".join(_inline(line) for line in run) + "</p>")
        run.clear()

    for line in markdown.splitlines():
        kind = _kind(line)
        if kind != run_kind or kind == "heading":
            flush()
            run_kind = kind
        if kind == "heading":
            match = _HEADING.match(line)
            assert match is not None
            level = len(match.group(1))
            out.append(f"<h{level}>{_inline(match.group(2).strip())}</h{level}>")
        elif kind != "blank":
            run.append(line)
    flush()
    return "\n".join(out)


def render_pics_html(markdown: str) -> str:
    """The page for the PICS text `pics.render_pics` returns."""
    body = markdown_to_html(markdown.replace(EDITOR_NOTE, "", 1))
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>BACpack PICS</title>\n<style>{_STYLE}</style>\n</head>\n<body>\n<main>\n{body}\n"
        '<p>Also as Markdown: <a href="/api/pics.md">PICS.md</a></p>\n</main>\n</body>\n</html>\n'
    )
