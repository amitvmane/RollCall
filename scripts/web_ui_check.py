#!/usr/bin/env python3
"""Static wiring check for the browser apps (web / portal / miniapp).

The Python suite never loads app.js, so nothing catches a UI change that
references a DOM id that doesn't exist, or wires an onclick to a function
nobody defined. Both have shipped to production here:

  * `_cb_vote` kept a decorator it no longer matched, and every panel button
    went dead silently (474d12c, fixed 8877595 — the smoke test now checks
    handler signatures for the Python side; this is the browser side).
  * an admin sign-in button was wired to `openVerifyModal()`, which had never
    existed. Caught only by reading the diff.

This is deliberately static analysis rather than a headless browser: it is
fast, needs no extra runtime, and catches the whole "referenced but not
defined" class. It cannot catch layout or behavioural regressions — those
still need a real browser.

Run:  python scripts/web_ui_check.py
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rollCall", "api")

# (html file, js file) pairs that make up one browser app.
APPS = [
    ("web/index.html", "web/app.js"),
    ("portal/index.html", "portal/app.js"),
]

# Handlers the browser provides, or that come from a <script> we don't parse.
BUILTIN_CALLS = {
    "this", "window", "document", "event", "return", "alert", "confirm",
    "history", "location", "navigator",
}

# ids created at runtime by innerHTML rather than present in the static HTML.
# Anything listed here is asserted to appear in a JS template literal, so the
# allowlist can't silently hide a genuine typo.
DYNAMIC_OK = re.compile(r"^(dues-|tmpl-|sch-|merge-|nrc-|wl-|gh-|stat-|lb-)")


def _read(rel):
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _inline_scripts(html):
    """Concatenated contents of every inline <script> block (no src=)."""
    out = []
    for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I):
        out.append(m.group(1))
    return "\n".join(out)


def check_onclick_handlers(html, js, label, errors):
    """Every inline on*= handler must resolve to something defined in the JS.

    Definitions can live in the external app.js OR in an inline <script> in the
    page itself — the portal defines its help-overlay handlers that way.
    """
    js = js + "\n" + _inline_scripts(html)
    calls = set()
    for m in re.finditer(r'on(?:click|change|input|submit)="([^"]+)"', html):
        # (?<![.\w$]) so `this.classList.add(...)` is read as a method call on an
        # object, not as a bare global named `add`.
        for fn in re.finditer(r'(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(', m.group(1)):
            calls.add(fn.group(1))

    defined = set()
    defined |= set(re.findall(r'window\.([A-Za-z_$][\w$]*)\s*=', js))
    defined |= set(re.findall(r'function\s+([A-Za-z_$][\w$]*)\s*\(', js))
    defined |= set(re.findall(r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()', js))

    for fn in sorted(calls - defined - BUILTIN_CALLS):
        errors.append(f"{label}: inline handler calls {fn}() which is not defined in the JS")


def check_element_ids(html, js, label, errors):
    """Every id the JS looks up must exist in the HTML or be built at runtime."""
    static_ids = set(re.findall(r'id="([^"]+)"', html))

    looked_up = set()
    looked_up |= set(re.findall(r'getElementById\(["\']([^"\']+)["\']\)', js))
    looked_up |= set(re.findall(r'\$\(["\']([^"\']+)["\']\)', js))

    for eid in sorted(looked_up - static_ids):
        if DYNAMIC_OK.match(eid):
            # Must be produced somewhere in the JS, else it's a typo wearing a
            # familiar prefix.
            if f'id="{eid}"' in js or f"id='{eid}'" in js or f'id="${{' in js:
                continue
            errors.append(f"{label}: id '{eid}' matches a dynamic prefix but is never rendered in the JS")
            continue
        if f'id="{eid}"' in js or f"id='{eid}'" in js:
            continue  # rendered via innerHTML
        errors.append(f"{label}: JS looks up id '{eid}' which is in neither the HTML nor any JS template")


def check_duplicate_ids(html, label, errors):
    seen, dupes = set(), set()
    for eid in re.findall(r'id="([^"]+)"', html):
        if eid in seen:
            dupes.add(eid)
        seen.add(eid)
    for eid in sorted(dupes):
        errors.append(f"{label}: duplicate id '{eid}' in the HTML")


def check_html_balance(html, label, errors):
    """A stray or missing </div> silently reparents everything after it."""
    opens = len(re.findall(r"<div\b", html))
    closes = len(re.findall(r"</div>", html))
    if opens != closes:
        errors.append(f"{label}: unbalanced <div> tags — {opens} open vs {closes} close")


def main():
    errors = []
    checked = 0
    for html_rel, js_rel in APPS:
        html, js = _read(html_rel), _read(js_rel)
        if html is None or js is None:
            continue
        checked += 1
        label = html_rel.split("/")[0]
        check_html_balance(html, label, errors)
        check_duplicate_ids(html, label, errors)
        check_onclick_handlers(html, js, label, errors)
        check_element_ids(html, js, label, errors)

    if not checked:
        print("web_ui_check: no apps found — wrong ROOT?", file=sys.stderr)
        return 1

    if errors:
        print(f"FAILED: {len(errors)} wiring problem(s) across {checked} app(s)\n")
        for e in errors:
            print(f"  ✗ {e}")
        return 1

    print(f"PASSED: {checked} browser app(s) — handlers, ids, and div balance all resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
