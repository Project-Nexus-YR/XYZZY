"""Renders the public, unauthenticated `/share/{token}` page.

Artifact content is member-authored text, exactly as untrusted as a chat
message — nothing here ever writes it into the page as raw HTML. Every
fragment of it is escaped by :func:`html.escape` before it is placed inside a
fixed tag, and the small markdown-ish transform below (headings, lists, code
fences, bold, inline code) mirrors what the web client's own ``renderMarkdown``
does in ``web/js/util.js``: escape first, then apply a closed set of
substitutions to the escaped text, never to the original. There is no path
from artifact content to an unescaped attribute, a `<script>` tag, or a raw
`href` — only text nodes inside `<p>`, `<li>`, `<h3>`-`<h5>`, `<pre>`, `<code>`,
and `<strong>`.
"""

from __future__ import annotations

import html
import re

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_HEADING = re.compile(r"^(#{1,3})\s+(.*)")
_BULLET = re.compile(r"^-\s+(.*)")


def _inline(escaped: str) -> str:
    escaped = _INLINE_CODE.sub(r"<code>\1</code>", escaped)
    return _BOLD.sub(r"<strong>\1</strong>", escaped)


def render_share_markdown(text: str) -> str:
    """The same small, closed markdown subset the web client renders, applied
    to already-escaped text server-side."""
    lines = html.escape(text or "").split("\n")
    out: list[str] = []
    in_list = False
    in_code = False
    para: list[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append(f"<p>{' '.join(para)}</p>")
            para = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in lines:
        if line.startswith("```"):
            if in_code:
                out.append("</pre>")
                in_code = False
            else:
                flush_para()
                close_list()
                out.append("<pre>")
                in_code = True
            continue
        if in_code:
            out.append(line + "\n")
            continue
        heading = _HEADING.match(line)
        if heading:
            flush_para()
            close_list()
            level = len(heading.group(1)) + 2
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        bullet = _BULLET.match(line)
        if bullet:
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(bullet.group(1))}</li>")
            continue
        close_list()
        if line.strip() == "":
            flush_para()
            continue
        para.append(_inline(line))
    flush_para()
    close_list()
    if in_code:
        out.append("</pre>")
    return "".join(out)


def render_share_page(*, title: str, content: str, published_at: str) -> str:
    """A self-contained, static-feeling document page for one shared artifact.

    Nothing from the room reaches this page beyond the artifact's own title and
    text: no member names, no room name, no ids. The footer's one link is fixed
    text pointing at the project, never anything derived from the request.
    """
    safe_title = html.escape(title or "Untitled")
    body_html = render_share_markdown(content)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{safe_title} · XYZZY</title>
<link rel="stylesheet" href="/static/share.css">
</head>
<body class="share-doc">
<main>
<div class="eyebrow">Decision brief</div>
<h1>{safe_title}</h1>
<div class="published">Published {html.escape(published_at)}</div>
<div class="doc">{body_html}</div>
<footer>Decided with
<a href="https://github.com/Project-Nexus-YR/XYZZY" rel="noopener">XYZZY</a></footer>
</main>
</body>
</html>
"""


def render_share_not_found_page() -> str:
    """The one page every unknown, malformed, or revoked token gets — no wording
    here may differ between those three cases."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Not found · XYZZY</title>
<link rel="stylesheet" href="/static/share.css">
</head>
<body class="share-error">
<main>
<h1>This link isn't live.</h1>
<p>The share may have been revoked, or the link may be mistyped.</p>
</main>
</body>
</html>
"""
