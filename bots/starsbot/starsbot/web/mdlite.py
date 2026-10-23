"""Минимальный Markdown → HTML для встроенной справки (заголовки, списки, чек-боксы, таблицы, **жирный**, `код`)."""
from __future__ import annotations

import html
import re


def _inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    return re.sub(r"\[([^\]]+)\]\((/[^)\s]*)\)", r'<a href="\2">\1</a>', s)   # только ссылки внутри панели


def render(text: str) -> str:
    out: list[str] = []
    stack: list[int] = []                    # отступы открытых <ul>
    table: list[list[str]] = []

    def close_lists(indent: int = -1) -> None:
        while stack and stack[-1] > indent:
            stack.pop()
            out.append("</ul>")

    def flush_table() -> None:
        if not table:
            return
        head, *rows = table
        out.append("<div class='tw'><table><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr>")
        out.extend("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
        out.append("</table></div>")
        table.clear()

    for line in text.splitlines():
        if line.strip().startswith("|"):
            close_lists()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                table.append(cells)
            continue
        flush_table()
        m = re.match(r"^(\s*)[-*] (?:\[( |x)\] )?(.*)$", line)
        if m:
            indent = len(m.group(1))
            if not stack or indent > stack[-1]:
                stack.append(indent)
                out.append("<ul>")
            else:
                close_lists(indent)
            box = "" if m.group(2) is None else f"<input type='checkbox' disabled{' checked' if m.group(2) == 'x' else ''}> "
            out.append(f"<li>{box}{_inline(m.group(3))}</li>")
            continue
        close_lists()
        h = re.match(r"^(#{1,4}) (.*)$", line)
        if h:
            n = len(h.group(1)) + 1                  # h1 страницы уже есть
            out.append(f"<h{n}>{_inline(h.group(2))}</h{n}>")
        elif line.strip():
            out.append(f"<p>{_inline(line.strip())}</p>")
    flush_table()
    close_lists()
    return "\n".join(out)
