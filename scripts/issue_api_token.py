#!/usr/bin/env python3
"""
Issue a new REST API token for a chat.

Run this on the bot's host (the script needs DATABASE_URL pointing at the
same DB the bot uses, or it falls back to sqlite:///rollcall.db). Prints
the plaintext token exactly once — store it immediately, the bot only
keeps the SHA-256 hash.

Usage:
    python scripts/issue_api_token.py \\
        --chat-id -1001999000001 \\
        --scopes read,vote,admin \\
        --label "Webapp prod" \\
        [--expires-days 30] \\
        [--issued-by 168415137]

When scopes are omitted, defaults to 'read,vote' (no admin). Tokens with
the 'admin' scope can DELETE rollcalls and (in future PRs) mutate chat
settings, so issue them sparingly.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chat-id", type=int, required=True,
                        help="Telegram chat id the token will be scoped to")
    parser.add_argument("--scopes", default="read,vote",
                        help="Comma-separated scopes: read, vote, admin (default: read,vote)")
    parser.add_argument("--label", default=None,
                        help="Friendly name shown in token listings (e.g. 'Webapp prod')")
    parser.add_argument("--expires-days", type=int, default=None,
                        help="Token expires after this many days (default: no expiry)")
    parser.add_argument("--issued-by", type=int, default=None,
                        help="Telegram user id of the issuer (audit only)")
    args = parser.parse_args()

    valid_scopes = {"read", "vote", "admin"}
    requested = [s.strip() for s in args.scopes.split(",") if s.strip()]
    invalid = [s for s in requested if s not in valid_scopes]
    if invalid:
        print(f"ERROR: unknown scope(s): {invalid}. Valid: read, vote, admin.", file=sys.stderr)
        return 2
    if not requested:
        print("ERROR: at least one scope is required.", file=sys.stderr)
        return 2

    # rollCall package needs to be importable
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    sys.path.insert(0, os.path.join(repo_root, "rollCall"))

    # We don't want bot_state to construct a real AsyncTeleBot, so set a
    # dummy token if none configured.
    os.environ.setdefault("TELEGRAM_TOKEN", "dummy:for_token_issue")

    from db import _hash_token, generate_api_token, insert_api_token  # noqa: E402

    token = generate_api_token()
    token_hash = _hash_token(token)
    expires_at = None
    if args.expires_days is not None:
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=args.expires_days)

    insert_api_token(
        token_hash=token_hash,
        chat_id=args.chat_id,
        scopes=",".join(sorted(set(requested))),
        label=args.label,
        issued_by_user_id=args.issued_by,
        expires_at=expires_at,
    )

    print("=" * 60)
    print("Token issued. Store this NOW — it is not recoverable.")
    print("=" * 60)
    print(f"  Chat id:    {args.chat_id}")
    print(f"  Scopes:     {','.join(sorted(set(requested)))}")
    if args.label:
        print(f"  Label:      {args.label}")
    if expires_at:
        print(f"  Expires:    {expires_at.isoformat()} UTC")
    if args.issued_by:
        print(f"  Issued by:  {args.issued_by}")
    print()
    print(f"  Token:      {token}")
    print()
    print("Use it as:")
    print(f'  curl -H "Authorization: Bearer {token}" \\')
    print(f"       http://localhost:8081/api/v1/chats/{args.chat_id}/rollcalls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
