#!/usr/bin/env python3
"""Compare a pip-audit JSON report against security/audit_baseline.json.

Why this exists rather than just running `pip-audit` as a blocking CI step:
requirements.lock already carried six known-vulnerable pins when scanning was
first switched on (2026-08-17). A plain blocking audit would have failed main
from the first run, and the usual reaction to a permanently-red job is to stop
looking at it. So this is a ratchet instead:

  * a vulnerable package that is NOT in the baseline  -> FAIL (new regression)
  * a baselined package still at its recorded pin     -> WARN (known debt)
  * a baselined package at a DIFFERENT pin, still     -> FAIL (someone bumped it
    vulnerable                                            and it did not help)
  * a baselined package that is now clean             -> FAIL with "prune me"

That last one matters: without it the baseline silently rots into a list of
things that stopped being true, which is how these files become worthless.

Usage:
    pip-audit -r requirements.lock --no-deps --format json --output audit.json
    python scripts/check_audit_baseline.py audit.json

Exit codes: 0 = clean or known-only, 1 = regression / stale baseline.
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "security" / "audit_baseline.json"


def _load_report(path):
    """pip-audit's JSON shape has moved between releases: older builds emit a
    bare list of dependency objects, newer ones wrap it in {"dependencies": [...]}.
    Accept both so a pip-audit upgrade doesn't silently turn this into a no-op
    that reports 'clean' forever."""
    with open(path) as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        deps = data.get("dependencies", [])
    elif isinstance(data, list):
        deps = data
    else:
        raise SystemExit(f"Unrecognised pip-audit JSON in {path}: {type(data).__name__}")
    if not deps:
        raise SystemExit(
            f"{path} lists zero dependencies — pip-audit probably failed to resolve "
            "the lock file. Refusing to report 'clean' off an empty report."
        )
    return deps


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 1
    deps = _load_report(argv[1])
    baseline = json.loads(BASELINE_PATH.read_text())["packages"]
    # pip-audit normalises names; match case-insensitively on both sides so a
    # "Pillow" vs "pillow" difference can't be mistaken for a new package.
    baseline_lc = {k.lower(): (k, v) for k, v in baseline.items()}

    failures, warnings, seen_vulnerable = [], [], set()

    for dep in deps:
        vulns = dep.get("vulns") or []
        if not vulns:
            continue
        name = (dep.get("name") or "").lower()
        version = dep.get("version") or "?"
        ids = ", ".join(v.get("id", "?") for v in vulns)
        seen_vulnerable.add(name)

        if name not in baseline_lc:
            failures.append(
                f"NEW vulnerable dependency: {name}=={version}\n"
                f"    {len(vulns)} advisory/advisories: {ids}\n"
                f"    Fix by bumping the pin, or (if genuinely not exploitable here) add it\n"
                f"    to {BASELINE_PATH.relative_to(REPO_ROOT)} with a note explaining why."
            )
            continue

        orig_name, entry = baseline_lc[name]
        if version != entry["pinned"]:
            failures.append(
                f"{orig_name} was bumped {entry['pinned']} -> {version} but is STILL vulnerable\n"
                f"    {len(vulns)} advisory/advisories: {ids}\n"
                f"    Baseline says {entry['fixed_in']} or later clears it."
            )
        else:
            warnings.append(
                f"{orig_name}=={version} — known, {len(vulns)} advisory/advisories "
                f"(fixed in {entry['fixed_in']})"
            )

    # Baseline entries that no longer show up are stale — prune them, otherwise
    # the file stops describing reality and a real regression could hide behind
    # an entry that was written for a version nobody runs any more.
    stale = [orig for lc, (orig, _) in baseline_lc.items() if lc not in seen_vulnerable]
    for orig in stale:
        failures.append(
            f"{orig} is in the baseline but the audit reports it CLEAN.\n"
            f"    Remove its entry from {BASELINE_PATH.relative_to(REPO_ROOT)} — a stale\n"
            f"    baseline can mask a future regression in that package."
        )

    if warnings:
        print("Known vulnerable pins (recorded in the baseline, not blocking):")
        for w in warnings:
            print(f"  · {w}")
        print()
    if failures:
        print("FAILED — dependency audit regressions:\n")
        for f in failures:
            print(f"  ✗ {f}\n")
        return 1

    print(f"OK — no new vulnerable dependencies ({len(warnings)} known, baselined).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
