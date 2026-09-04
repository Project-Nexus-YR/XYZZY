"""Renders the public, unauthenticated `/share/{token}` page.

Artifact content is member-authored text, exactly as untrusted as a chat
message — nothing here ever writes it into the page as raw HTML. Every
fragment of it is escaped by :func:`html.escape` before it is placed inside a
fixed tag, and the small markdown-ish transform below (headings, lists, code
fences, bold, inline code) mirrors what the web client's own ``renderMarkdown``
does in ``web/index.html``: escape first, then apply a closed set of
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
<style>
@font-face{{font-family:'IBM Plex Mono';font-style:normal;font-weight:400;
  font-display:swap;src:url('/static/fonts/IBMPlexMono-400.woff2') format('woff2')}}
@font-face{{font-family:'IBM Plex Mono';font-style:normal;font-weight:500;
  font-display:swap;src:url('/static/fonts/IBMPlexMono-500.woff2') format('woff2')}}
@font-face{{font-family:'IBM Plex Sans';font-style:normal;font-weight:400;
  font-display:swap;src:url('/static/fonts/IBMPlexSans-400.woff2') format('woff2')}}
@font-face{{font-family:'IBM Plex Sans';font-style:normal;font-weight:500;
  font-display:swap;src:url('/static/fonts/IBMPlexSans-500.woff2') format('woff2')}}
@font-face{{font-family:'IBM Plex Sans';font-style:normal;font-weight:600;
  font-display:swap;src:url('/static/fonts/IBMPlexSans-600.woff2') format('woff2')}}
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{
  --bg:#f7f6f4; --ink:#17181a; --ink-2:#55524b; --line:rgba(23,20,16,.12);
  --mono:'IBM Plex Mono','SFMono-Regular',Consolas,monospace;
  --sans:'IBM Plex Sans','Segoe UI',system-ui,sans-serif;
}}
@media (prefers-color-scheme: dark){{
  :root:not([data-theme="light"]){{
    --bg:#17181a; --ink:#f2f0ec; --ink-2:#a9a59c; --line:rgba(242,240,236,.14);
  }}
}}
body{{
  background:var(--bg); color:var(--ink); font-family:var(--sans);
  font-size:16px; line-height:1.6; -webkit-font-smoothing:antialiased;
}}
main{{max-width:640px;margin:0 auto;padding:64px 24px 96px}}
.eyebrow{{
  font-family:var(--mono);font-size:12px;letter-spacing:.06em;color:var(--ink-2);
  text-transform:uppercase;margin-bottom:12px;
}}
h1{{font-size:28px;font-weight:600;letter-spacing:-.01em;margin-bottom:8px}}
.published{{font-family:var(--mono);font-size:12px;color:var(--ink-2);margin-bottom:40px}}
.doc h3,.doc h4,.doc h5{{margin:28px 0 10px;font-weight:600;letter-spacing:-.01em}}
.doc h3{{font-size:20px}} .doc h4{{font-size:17px}} .doc h5{{font-size:15px}}
.doc p{{margin:14px 0}}
.doc ul{{margin:14px 0;padding-left:1.3em}}
.doc li{{margin:4px 0}}
.doc code{{
  font-family:var(--mono);font-size:.92em;background:var(--line);
  padding:.1em .35em;border-radius:3px;
}}
.doc pre{{
  font-family:var(--mono);font-size:13px;background:var(--line);padding:14px 16px;
  border-radius:6px;overflow-x:auto;white-space:pre;margin:16px 0;
}}
.doc strong{{font-weight:600}}
footer{{
  margin-top:64px;padding-top:20px;border-top:1px solid var(--line);
  font-family:var(--mono);font-size:12px;color:var(--ink-2);
}}
footer a{{color:inherit}}
</style>
</head>
<body>
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
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{ --bg:#f7f6f4; --ink:#17181a; --ink-2:#55524b; }
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){ --bg:#17181a; --ink:#f2f0ec; --ink-2:#a9a59c; }
}
body{background:var(--bg);color:var(--ink);font-family:'Segoe UI',system-ui,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh}
main{text-align:center;padding:24px}
h1{font-size:20px;font-weight:600;margin-bottom:8px}
p{color:var(--ink-2);font-size:14px}
</style>
</head>
<body>
<main>
<h1>This link isn't live.</h1>
<p>The share may have been revoked, or the link may be mistyped.</p>
</main>
</body>
</html>
"""
