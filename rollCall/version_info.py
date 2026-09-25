"""What, exactly, is running.

A standalone leaf module (no telebot, no db, no bot_state) so it can be
imported and logged before anything else has had a chance to fail — the
whole point is to know what shipped even when the rest of startup goes
wrong. Same reasoning as backup_status.py, which is deliberately isolated
for a related reason: runner.py is `__main__`, and importing it from
elsewhere re-executes its module-level code.

Why this exists: a deploy is `git pull && make build`, and the two things
that can silently not-be-what-you-expect are the CODE (did the pull/build
actually pick up the commit you think it did) and the DEPENDENCIES (did the
lock file's pins land, especially after a base-image bump). Nothing printed
either one. A support conversation about "did 10.5 actually deploy" used to
mean grepping the changelog for a string that might be in the image; now
it's the first thing in `docker compose logs`.
"""
import importlib.metadata
import json
import os
import platform
import re
import sys

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Written by the Dockerfile's final layer from build ARGs (see dockerfile).
# Absent entirely on `python runner.py` outside a container, or on an image
# built before this existed — both fall back to "unknown" rather than
# raising, because a missing provenance file must never block startup.
_BUILD_INFO_PATH = os.path.join(_BASE_DIR, ".build_info.json")

# (distribution name as seen by pip, human label). importlib.metadata reads
# whatever is actually installed — the wheel that landed, not the pin in
# requirements.lock — so a version mismatch after a dependency bump shows up
# here even if the lock file itself looks right.
_TRACKED_PACKAGES = [
    ("pyTelegramBotAPI", "pyTelegramBotAPI"),
    ("fastapi", "FastAPI"),
    ("uvicorn", "Uvicorn"),
    ("pydantic", "Pydantic"),
    ("psycopg2-binary", "psycopg2"),
    ("matplotlib", "Matplotlib"),
    ("pillow", "Pillow"),
    ("pywebpush", "pywebpush"),
    ("sentry-sdk", "Sentry SDK"),
]


def _package_version(dist_name: str) -> str:
    """Installed version of a distribution, or a reason it's absent.

    Distinguishes "not installed" from "installed but unreadable" — the
    former is expected for optional deps (sentry-sdk); the latter would be
    a packaging problem worth seeing rather than swallowing identically.
    """
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"
    except Exception as e:
        return f"unreadable ({type(e).__name__})"


def _build_info() -> dict:
    """{commit, built_at} baked in at image-build time, or 'unknown' for both."""
    try:
        with open(_BUILD_INFO_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return {
            "commit": (data.get("commit") or "unknown").strip() or "unknown",
            "built_at": (data.get("built_at") or "unknown").strip() or "unknown",
        }
    except (OSError, ValueError):
        return {"commit": "unknown", "built_at": "unknown"}


_DSN_CREDENTIALS = re.compile(r"://[^:/@]+:[^@/]+@")


def _redact_dsn(url: str) -> str:
    """postgresql://user:pass@host/db -> postgresql://***@host/db.

    validate_environment() elsewhere logs DATABASE_URL unredacted (it only
    strips query params, not userinfo) — a pre-existing pattern this file
    doesn't otherwise touch. But this is a new call site, logged on every
    single boot, so it isn't worth reproducing a credential leak just for
    consistency with a line it doesn't share code with.
    """
    return _DSN_CREDENTIALS.sub("://***@", url)


def deployed_app_version() -> str:
    """The changelog version /version would show — 'unknown' if unreadable.

    Reads version.json directly rather than importing handlers.core, which
    would pull in telebot and defeat the point of this being a leaf module.
    """
    try:
        with open(os.path.join(_BASE_DIR, "version.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        for entry in reversed(data):
            if entry.get("DeployedOnProd") == "Y":
                return str(entry.get("Version", "unknown"))
        return "unknown (none marked deployed)"
    except (OSError, ValueError, TypeError):
        return "unknown (version.json unreadable)"


def collect() -> dict:
    """Everything this module knows, as one dict — used by the log banner
    and available to anything else (a future /health field, a support
    script) that wants the same facts without re-deriving them."""
    build = _build_info()
    return {
        "app_version": deployed_app_version(),
        "commit": build["commit"],
        "built_at": build["built_at"],
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "platform": platform.platform(),
        "packages": {label: _package_version(dist) for dist, label in _TRACKED_PACKAGES},
    }


def log_startup_banner(logger, *, db_type: str = "?", database_url: str = "",
                       rest_api_enabled: bool = False) -> None:
    """Log one block naming everything that shipped. Called once, at boot.

    Takes db_type/database_url/rest_api_enabled as parameters rather than
    importing config or db — this stays a leaf module, and runner.py already
    has all three by the point it calls this.

    Best-effort throughout: this exists to make deploys legible, not to be
    one more thing that can crash a deploy. Any single field failing to
    resolve degrades to a placeholder for that field alone.
    """
    try:
        info = collect()
    except Exception:
        logger.exception("⚠️  Could not collect version info — continuing without it")
        return

    logger.info("=" * 60)
    logger.info("📦 Versions")
    logger.info("=" * 60)
    logger.info(f"   App:        {info['app_version']}  "
               f"(commit {info['commit']}, built {info['built_at']})")
    logger.info(f"   Python:     {info['python']}  [{info['platform']}]")
    logger.info(f"   Database:   {db_type}"
               + (f"  ({_redact_dsn(database_url.split('?')[0])})" if database_url else ""))

    pkgs = info["packages"]
    logger.info(f"   Telegram:   pyTelegramBotAPI {pkgs['pyTelegramBotAPI']}")
    if rest_api_enabled:
        logger.info(f"   REST API:   FastAPI {pkgs['FastAPI']} / Uvicorn {pkgs['Uvicorn']} "
                   f"/ Pydantic {pkgs['Pydantic']}")
    if db_type == "postgresql":
        logger.info(f"   PG driver:  psycopg2 {pkgs['psycopg2']}")
    logger.info(f"   Cards:      Matplotlib {pkgs['Matplotlib']} / Pillow {pkgs['Pillow']}")
    logger.info(f"   Web push:   pywebpush {pkgs['pywebpush']}")
    logger.info(f"   Sentry SDK: {pkgs['Sentry SDK']}")
    logger.info("=" * 60)
