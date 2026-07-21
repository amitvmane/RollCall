"""
Web voting handler — /weblink, /weblogin, and /mytoken commands.

/weblink — shares the permanent group URL for bookmarking.
/weblogin name — admin issues a single-use login URL for a member who can't
  use the Telegram verify flow (e.g. Telegram is down).
/mytoken [off] — DMs the member their persistent personal login code for the
  web (self-serve, Telegram-independent once saved).

Requires WEB_BASE_URL env var.
"""
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import db as _db
from bot_state import bot, reply_error, _esc_md
from exceptions import incorrectParameter, insufficientPermissions, parameterMissing
from functions import admin_rights
from rollcall_manager import manager
from services.web import get_group_web_token


def _web_base_url() -> str:
    return os.environ.get("WEB_BASE_URL", "").rstrip("/")


@bot.message_handler(func=lambda m: m.text.split()[0].split("@")[0].lower() == "/weblink")
async def weblink_cmd(message):
    cid = message.chat.id
    try:
        base = _web_base_url()
        if not base:
            await bot.send_message(
                cid,
                "Web voting is not configured. Set WEB_BASE_URL on the server to enable it."
            )
            return

        group_token = get_group_web_token(cid)
        group_url = f"{base}/web/group/{group_token}"

        # /weblink itself stays open to every member (the voting link below is
        # useful to anyone) — but web-admin status is real mutating power
        # (start/end rollcall, silent mode, proxy votes, schedule editing) and
        # must respect the SAME admin gate every other admin command in the
        # bot uses. Grant it only when the caller passes admin_rights() —
        # i.e. either the group hasn't locked itself down with /set_admins
        # (matches today's default-open behavior), or they're a real
        # Telegram admin/creator if it has. Never block the command itself
        # on this — a non-admin still gets their voting link.
        if message.from_user and await admin_rights(message, manager):
            user = message.from_user
            tg_name = user.first_name or (f"@{user.username}" if user.username else str(user.id))
            _db.set_web_admin(cid, user.id, tg_name)

        rollcalls = manager.get_rollcalls(cid)

        lines = [
            "🔗 *Web voting links*",
            "",
            "📌 *Bookmark this link* — works even when Telegram is down:",
            group_url,
        ]

        if rollcalls:
            lines += ["", "Direct links for active rollcalls:"]
            for i, rc in enumerate(rollcalls, start=1):
                token = getattr(rc, "web_token", None)
                if token:
                    lines.append(f"#{i} *{_esc_md(rc.title)}*: {base}/web/join/{token}")
                else:
                    lines.append(f"#{i} *{_esc_md(rc.title)}* — direct link unavailable")

        lines += ["", "_Share the bookmark link with anyone — no Telegram needed._"]

        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(InlineKeyboardButton("🌐 Open Voting Page", url=group_url))

        await bot.send_message(cid, "\n".join(lines), parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        await reply_error(cid, e)


# ── /weblogin ────────────────────────────────────────────────────────────────

_WEBLOGIN_TTL_DAYS = 7


def _resolve_member_for_weblogin(chat_id: int, name_arg: str) -> tuple:
    """Return (tg_user_id, display_name) by matching name_arg against chat_members.

    Strips leading @ for username matches. Raises incorrectParameter with a
    helpful message if no unique match is found.
    """
    needle = name_arg.lstrip("@").lower()
    members = _db.get_active_members(chat_id)
    matched = []
    for m in members:
        first = (m.get("first_name") or "").lower()
        uname = (m.get("username") or "").lower()
        if needle == first or needle == uname:
            matched.append(m)
    if not matched:
        all_names = ", ".join(
            m.get("first_name") or m.get("username") or str(m["user_id"])
            for m in members[:10]
        )
        hint = f"\n\nKnown members: {all_names}" if all_names else ""
        raise incorrectParameter(
            f"No member '{name_arg}' found in this group's chat history.{hint}"
        )
    if len(matched) > 1:
        names = ", ".join(
            m.get("first_name") or m.get("username") or str(m["user_id"])
            for m in matched
        )
        raise incorrectParameter(f"Ambiguous name — multiple matches: {names}")
    m = matched[0]
    display = m.get("first_name") or (f"@{m['username']}" if m.get("username") else str(m["user_id"]))
    return m["user_id"], display


@bot.message_handler(func=lambda m: m.text.split()[0].split("@")[0].lower() == "/weblogin")
async def weblogin_cmd(message):
    """Admin-only: issue a single-use login URL for a member who can't use Telegram verify."""
    cid = message.chat.id
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /weblogin")

        base = _web_base_url()
        if not base:
            await bot.send_message(
                cid,
                "Web voting is not configured. Set WEB_BASE_URL on the server first."
            )
            return

        args = message.text.split()[1:]
        if not args:
            raise parameterMissing("Usage: /weblogin <name or @username>")

        name_arg = " ".join(args)
        tg_user_id, display_name = _resolve_member_for_weblogin(cid, name_arg)

        token = uuid.uuid4().hex
        expires_at = datetime.now(timezone.utc) + timedelta(days=_WEBLOGIN_TTL_DAYS)

        admin = message.from_user
        admin_uid = admin.id if admin else 0
        admin_name = (admin.first_name if admin else None) or "admin"

        _db.create_web_direct_login_token(
            token=token,
            chat_id=cid,
            tg_user_id=tg_user_id,
            tg_name=display_name,
            created_by_uid=admin_uid,
            created_by_name=admin_name,
            expires_at=expires_at,
        )

        login_url = f"{base}/api/v1/auth/weblogin/{token}"
        expiry_str = expires_at.strftime("%d %b %Y")

        await bot.send_message(
            cid,
            f"🔐 *Web login link for {_esc_md(display_name)}*\n\n"
            f"`{login_url}`\n\n"
            f"Single-use · expires {expiry_str}\n"
            f"_Share via WhatsApp or email. The link logs them in automatically._",
            parse_mode="Markdown",
        )

        logging.info(
            "[weblogin_cmd] admin=%s issued weblogin for user=%s chat=%s expires=%s",
            admin_uid, tg_user_id, cid, expiry_str,
        )
    except Exception as e:
        await reply_error(message, e)


# ── /mytoken — persistent personal login code ────────────────────────────────

from services.web import hash_login_token  # shared with the redeem API route


@bot.message_handler(func=lambda m: m.text.split()[0].split("@")[0].lower() == "/mytoken")
async def mytoken_cmd(message):
    """Self-serve persistent login code, DM-only so it never lands in group
    history. Re-running replaces the previous code; '/mytoken off' revokes."""
    cid = message.chat.id
    try:
        user = message.from_user
        if user is None:
            return

        base = _web_base_url()
        if not base:
            await bot.send_message(
                cid,
                "Web voting is not configured. Set WEB_BASE_URL on the server to enable it."
            )
            return

        args = message.text.split()[1:]
        if args and args[0].lower() in ("off", "revoke"):
            existed = _db.delete_member_login_token(user.id)
            await bot.send_message(
                cid,
                "🔓 Your login code has been revoked." if existed
                else "You don't have a login code to revoke.",
            )
            return

        token = secrets.token_urlsafe(12)
        ok = _db.upsert_member_login_token(
            user.id, hash_login_token(token),
            first_name=user.first_name, username=user.username,
        )
        if not ok:
            raise incorrectParameter("Could not create a login code — try again.")

        # Code goes out alone, on its own message — nothing else on the line
        # to fight with when tapping/long-pressing to copy. Instructions +
        # a direct button to the portal's login screen follow separately.
        portal_url = f"{base}/portal/"
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(InlineKeyboardButton("🌐 Open portal & log in", url=portal_url))
        # Two separate DMs (code, then instructions) — each can fail on its
        # own, and the two failure modes need different messages. If the
        # first fails, nothing reached the user and re-running /mytoken is
        # the right advice. If the first succeeds but the second doesn't,
        # the user already has a live, usable code — telling them "I
        # couldn't DM you, try again" would be wrong (misleadingly implies
        # nothing arrived) and re-running would needlessly burn the code
        # they just received (upsert_member_login_token overwrites it).
        try:
            await bot.send_message(user.id, f"`{token}`", parse_mode="Markdown")
        except Exception:
            # Bot can't DM users who never opened a private chat with it. The
            # code was already replaced above, which is fine — the old one
            # stops working and the retry issues a fresh one.
            if message.chat.type != "private":
                await bot.send_message(
                    cid,
                    f"📪 I couldn't DM you, {_esc_md(user.first_name or 'there')} — "
                    "open a private chat with me (tap my name → Start) and run /mytoken again.",
                    parse_mode="Markdown",
                )
            return

        try:
            await bot.send_message(
                user.id,
                "🔑 *Your personal web login code* — tap it above to copy.\n\n"
                "Open the portal below, choose *Login with code*, and paste it in.\n\n"
                "⚠️ Anyone with this code can act as you — keep it private.\n"
                "Run /mytoken again to replace it, or /mytoken off to revoke.",
                parse_mode="Markdown",
                reply_markup=markup,
            )
        except Exception:
            logging.warning(
                "[mytoken] instructions DM failed after the code DM already succeeded uid=%s",
                user.id, exc_info=True,
            )
            if message.chat.type != "private":
                await bot.send_message(
                    cid,
                    f"📬 Sent you your login code, {_esc_md(user.first_name or 'there')} — "
                    f"open {portal_url} and choose *Login with code* to use it.",
                    parse_mode="Markdown",
                )
            return

        if message.chat.type != "private":
            await bot.send_message(cid, "📬 Sent you a DM with your personal login code.")
    except Exception as e:
        await reply_error(message, e)
