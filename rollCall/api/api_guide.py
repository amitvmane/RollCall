"""
/help/api — the partner API guide (docs/API.md) as a page on the help site.

docs/API.md stays the one copy: this renders it on request (cached by mtime),
so the page and the file can't drift. The renderer handles exactly the subset
the guide uses — headings, paragraphs, lists, tables, fenced code, `code`,
**bold**, [links](url) — and escapes everything first, so nothing in the
file can inject markup or script.
"""

import html
import re
from pathlib import Path

import copy

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "API.md"
_cache: dict = {}


def _inline(text: str) -> str:
    t = html.escape(text, quote=True)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)

    def link(m):
        label, url = m.group(1), m.group(2)
        if not re.match(r"^(https://|/|#)", html.unescape(url)):
            return label   # only https, site-relative or anchors
        return f'<a href="{url}">{label}</a>'
    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, t)


def render(md: str) -> str:
    out, para, lines, i = [], [], md.split("\n"), 0

    def flush():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(lines[i + 1:j])) + "</code></pre>")
            i = j + 1
            continue
        m = re.match(r"^(#{1,3}) (.*)", line)
        if m:
            flush()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
        elif line.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            head, body = rows[0], rows[1:]
            out.append("<div class='tbl'><table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>" + "".join(
                           "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
                       + "</tbody></table></div>")
            continue
        elif re.match(r"^[-*] ", line):
            flush()
            items = []
            while i < len(lines) and re.match(r"^[-*] ", lines[i]):
                items.append(lines[i][2:])
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
            continue
        elif not line.strip():
            flush()
        else:
            para.append(line.strip())
        i += 1
    flush()
    return "\n".join(out)


_PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>RollCall — API for partners</title>
<link rel="icon" href="/web/logo.svg" type="image/svg+xml"/>
<script src="/shared/theme-init.js"></script>
<script src="/shared/actions.js"></script>
<link rel="stylesheet" href="/shared/tokens.css"/>
<link rel="stylesheet" href="/shared/base.css"/>
<link rel="stylesheet" href="/help/style.css"/>
</head><body>
<div class="topbar">
  <div class="topbar-left">
    <img src="/web/logo.svg" alt="" class="topbar-logo" width="26" height="26" aria-hidden="true"/>
    <span class="logo">RollCall API</span>
  </div>
  <div class="topbar-right">
    <a href="/help/" class="btn-theme" title="Command reference" style="text-decoration:none;display:flex;align-items:center;justify-content:center">❓</a>
    <a href="/" class="btn-theme" title="Back to RollCall" style="text-decoration:none;display:flex;align-items:center;justify-content:center">🏠</a>
    <button class="btn-theme" title="Theme" aria-label="Theme" data-theme-toggle data-on-click="toggleTheme">🌙</button>
  </div>
</div>
<main class="api-guide">
%s
</main>
</body></html>"""


@router.get("/help/api", response_class=HTMLResponse, include_in_schema=False)
async def api_guide() -> HTMLResponse:
    try:
        mtime = _GUIDE.stat().st_mtime
    except OSError:
        return HTMLResponse("<p>The API guide isn't available on this server.</p>", status_code=404)
    if _cache.get("mtime") != mtime:
        _cache.update(mtime=mtime, html=_PAGE % render(_GUIDE.read_text(encoding="utf-8")))
    return HTMLResponse(_cache["html"])


# ── Interactive reference (Swagger UI) — partner routes only ────────────────
# FastAPI's own /api/docs lists EVERY route (web page, admin console, auth
# internals) and is off by default for that reason. Partners get a spec with
# only what docs/API.md documents: the bearer-token /chats/{chat_id}/... routes,
# minus the deprecated number-based paths, plus only the schemas those use.
# Swagger UI is vendored (help/swagger/, pinned 5.34.1) so the page loads
# nothing from a CDN and keeps the site's strict CSP.

_PARTNER_PREFIX = "/api/v1/chats/"
_spec_cache: dict = {}


def _refs(node, found: set) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for v in node.values():
            _refs(v, found)
    elif isinstance(node, list):
        for v in node:
            _refs(v, found)


def partner_spec(full: dict) -> dict:
    paths = {}
    for path, ops in full.get("paths", {}).items():
        if not path.startswith(_PARTNER_PREFIX):
            continue
        kept = {m: op for m, op in ops.items() if isinstance(op, dict) and not op.get("deprecated")}
        if kept:
            paths[path] = kept
    schemas = full.get("components", {}).get("schemas", {})
    needed: set = set()
    _refs(paths, needed)
    while True:   # schemas reference schemas
        before = len(needed)
        _refs({k: schemas[k] for k in needed if k in schemas}, needed)
        if len(needed) == before:
            break
    return {
        "openapi": full.get("openapi", "3.1.0"),
        "info": {"title": "RollCall partner API", "version": "v1",
                 "description": "Rollcalls, votes and settings for ONE group, with an app key made under "
                                "Admin → Integrations on the group's web page. Guide: /help/api"},
        "paths": copy.deepcopy(paths),
        "components": {
            "schemas": {k: copy.deepcopy(schemas[k]) for k in sorted(needed) if k in schemas},
            "securitySchemes": {"appKey": {"type": "http", "scheme": "bearer",
                                           "description": "An app key (rc_…) from Admin → Integrations"}},
        },
        "security": [{"appKey": []}],
    }


@router.get("/api/v1/partner/openapi.json", include_in_schema=False)
async def partner_openapi(request: Request) -> JSONResponse:
    if "spec" not in _spec_cache:
        _spec_cache["spec"] = partner_spec(request.app.openapi())
    return JSONResponse(_spec_cache["spec"])


_REFERENCE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>RollCall — API reference</title>
<link rel="icon" href="/web/logo.svg" type="image/svg+xml"/>
<link rel="stylesheet" href="/help/swagger/swagger-ui.css"/>
<link rel="stylesheet" href="/help/style.css"/>
</head><body class="api-ref">
<div class="api-ref-bar"><a href="/help/api">‹ API guide</a> · <a href="/help/">Command reference</a></div>
<div id="swagger"></div>
<script src="/help/swagger/swagger-ui-bundle.js"></script>
<script src="/help/swagger-init.js"></script>
</body></html>"""


@router.get("/help/api/reference", response_class=HTMLResponse, include_in_schema=False)
async def api_reference() -> HTMLResponse:
    return HTMLResponse(_REFERENCE)
