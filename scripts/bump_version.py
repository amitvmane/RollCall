#!/usr/bin/env python3
"""
scripts/bump_version.py

Reads rollCall/version.json, appends a new version entry, and writes it back.

Version number  : auto-incremented by 0.1 from the latest entry
Description     : derived from COMMIT_MSG env var (first non-empty line,
                  conventional-commit prefix stripped)
DeployedOnProd  : always "N" for new entries
DeployedDatetime: current UTC time in "DD-MM-YYYY HH:MM UTC" format

Usage (GitHub Actions):
    env:
      COMMIT_MSG: ${{ github.event.head_commit.message }}
    run: python scripts/bump_version.py
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

VERSION_FILE = os.path.join(
    os.path.dirname(__file__), "..", "rollCall", "version.json"
)

# Conventional-commit prefixes to strip from the description
_CC_PREFIX = re.compile(
    r"^(feat|fix|docs|refactor|test|chore|ci|style|perf|build)"
    r"(\([^)]*\))?!?:\s*",
    re.IGNORECASE,
)


def _clean_commit_msg(raw: str) -> str:
    """Return a clean one-line description from a raw commit message."""
    # Take the first non-empty line only
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("Co-Authored-By"):
            line = _CC_PREFIX.sub("", line)
            # Capitalise first letter
            return line[0].upper() + line[1:] if line else line
    return "Maintenance update"


def main():
    commit_msg = os.environ.get("COMMIT_MSG", "").strip()
    description = _clean_commit_msg(commit_msg) if commit_msg else "Maintenance update"

    with open(VERSION_FILE, "r") as f:
        versions = json.load(f)

    latest_version = max(float(v["Version"]) for v in versions)
    # Round to one decimal to avoid floating-point drift (4.6 + 0.1 → 4.7)
    new_version = round(latest_version + 0.1, 1)

    now_utc = datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M UTC")

    new_entry = {
        "Version": new_version,
        "Description": description,
        "DeployedOnProd": "N",
        "DeployedDatetime": now_utc,
    }

    versions.append(new_entry)

    with open(VERSION_FILE, "w") as f:
        json.dump(versions, f, indent=4)
        f.write("\n")

    print(f"Bumped version {latest_version} → {new_version}: {description}")


if __name__ == "__main__":
    main()
