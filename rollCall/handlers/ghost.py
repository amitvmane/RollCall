"""
Ghost tracking handlers: /toggle_ghost_tracking, /set_absent_limit, /clear_absent, /mark_absent,
and the ghost_callback_handler (also handles reconf, proxy_add/cancel, delconf, ovrd, mabs).
"""
import asyncio
import logging
from datetime import datetime

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot_state import (
    bot, _ghost_selections, _pending_reconf, _pending_deletes, _pending_overrides,
    _pending_proxy_add, _log_task_exc, _get_display_name, format_mention_with_name,
    _esc_md, get_rc_db_id, _build_ghost_select_keyboard, _fmt_ended_at,
    reply_error,
)
from exceptions import insufficientPermissions, rollCallNotStarted, incorrectParameter
from functions import admin_rights, roll_call_not_started
from models import User
from rollcall_manager import manager
from db import (
    get_ghost_count, increment_ghost_count, reset_ghost_count, decrement_ghost_count,
    get_ghost_leaderboard, get_user_ghost_count_by_name, get_ghost_count_by_proxy_name,
    mark_rollcall_absent_done, get_unprocessed_rollcalls,
    add_ghost_event, get_rollcall_in_users, save_ghost_selections, load_ghost_selections,
    reset_streak_on_ghost, log_admin_action, upsert_chat_member,
    increment_user_stat, increment_rollcall_stat,
)


def _ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/toggle_ghost_tracking")
async def toggle_ghost_tracking(message):
    try:
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        cid = message.chat.id
        parts = message.text.strip().split()
        arg = parts[1].lower() if len(parts) > 1 else None
        if arg in ("true", "on", "1", "enable", "enabled"):
            new_state = True
        elif arg in ("false", "off", "0", "disable", "disabled"):
            new_state = False
        else:
            new_state = not manager.get_ghost_tracking_enabled(cid)
        manager.set_ghost_tracking_enabled(cid, new_state)
        log_admin_action(cid, message.from_user.id, message.from_user.first_name, "toggle_ghost_tracking", details=f"enabled={new_state}")
        if not manager.get_shh_mode(cid):
            await bot.send_message(cid, f"👻 Ghost tracking is now {'ENABLED' if new_state else 'DISABLED'}.")

        rollcalls = manager.get_rollcalls(cid)
        if rollcalls:
            from handlers.lifecycle import _update_panel
            for i, rc in enumerate(rollcalls):
                await _update_panel(cid, i + 1, rc)
    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/set_absent_limit")
async def set_absent_limit(message):
    try:
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        cid = message.chat.id
        parts = message.text.strip().split()
        if len(parts) < 2:
            await bot.send_message(cid, "Usage: /set_absent_limit <number>\nExample: /set_absent_limit 3")
            return
        try:
            limit = int(parts[1])
            if limit < 1:
                raise ValueError()
        except ValueError:
            await bot.send_message(cid, "⚠️ Please provide a positive integer. Example: /set_absent_limit 3")
            return
        manager.set_absent_limit(cid, limit)
        if not manager.get_shh_mode(cid):
            await bot.send_message(
                cid,
                f"✅ Ghost limit set to {limit}.\n"
                f"Users who ghost {limit}+ session(s) will be asked to reconfirm their IN vote. 👻"
            )
    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/clear_absent")
async def clear_absent(message):
    try:
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        cid = message.chat.id
        parts = message.text.strip().split(None, 1)
        if len(parts) < 2:
            await bot.send_message(cid, "Usage: /clear_absent <name>\nExample: /clear_absent John")
            return
        target_name = parts[1].strip()

        record = get_user_ghost_count_by_name(cid, target_name)
        if not record:
            leaderboard = get_ghost_leaderboard(cid)
            best = None
            best_score = None
            try:
                from Levenshtein import distance as lev_distance
                for entry in leaderboard:
                    name = entry.get('user_name') or entry.get('proxy_name') or ""
                    score = lev_distance(target_name.lower(), name.lower())
                    if best_score is None or score < best_score:
                        best_score = score
                        best = entry
            except ImportError:
                pass
            if best and best_score is not None and best_score <= 3:
                record = best
            else:
                await bot.send_message(cid, f"⚠️ No ghost record found for '{target_name}'.")
                return

        proxy_name = record.get('proxy_name')
        user_id = record.get('user_id', -1)
        reset_ghost_count(cid, user_id, proxy_name=proxy_name)
        name = record.get('user_name') or proxy_name or target_name
        await bot.send_message(cid, f"✅ {name}'s ghost record has been cleared. Fresh start! 👻➡️✅")
    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/mark_absent")
async def mark_absent(message):
    try:
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        cid = message.chat.id

        if not manager.get_ghost_tracking_enabled(cid):
            await bot.send_message(
                cid,
                "⚠️ Ghost tracking is not enabled for this group.\n"
                "Admin can enable it with /toggle_ghost_tracking"
            )
            return

        sessions = get_unprocessed_rollcalls(cid, days=30)
        if not sessions:
            await bot.send_message(cid, "✅ No sessions need absent marking — you're all caught up!")
            return

        markup = InlineKeyboardMarkup(row_width=1)
        for s in sessions:
            date_str = _fmt_ended_at(s.get('ended_at'))
            title = s.get('title') or "Untitled"
            label = f"📋 {title} — {date_str}"
            markup.add(InlineKeyboardButton(label, callback_data=f"mabs_sel_{s['id']}"))

        await bot.send_message(cid, "Which session do you want to review?", reply_markup=markup)
    except Exception as e:
        await reply_error(message, e)


@bot.callback_query_handler(func=lambda call: call.data and (
    call.data.startswith("ghost_") or call.data.startswith("reconf_") or call.data.startswith("mabs_")
    or call.data.startswith("proxy_add_") or call.data.startswith("proxy_cancel_")
    or call.data.startswith("delconf_")
    or call.data.startswith("ovrd_yes_") or call.data.startswith("ovrd_no_")
))
async def ghost_callback_handler(call):
    try:
        cid = call.message.chat.id
        data = call.data

        # ── ghost_no ─────────────────────────────────────────────────────────
        if data.startswith("ghost_no_"):
            rc_db_id = int(data.split("_", 2)[2])
            mark_rollcall_absent_done(rc_db_id)
            _ghost_selections.pop((cid, rc_db_id), None)
            await bot.answer_callback_query(call.id, "✅ Got it!")
            await bot.edit_message_text("✅ No ghosts — everyone showed up! Great session! 🎉", cid, call.message.message_id)
            return

        # ── ghost_yes ────────────────────────────────────────────────────────
        if data.startswith("ghost_yes_"):
            rc_db_id = int(data.split("_", 2)[2])
            in_users = get_rollcall_in_users(rc_db_id)
            if not in_users:
                await bot.answer_callback_query(call.id, "No IN users found for this session.")
                return
            saved = load_ghost_selections(cid, rc_db_id)
            _ghost_selections[(cid, rc_db_id)] = saved if saved else set()
            markup = _build_ghost_select_keyboard(rc_db_id, in_users, _ghost_selections[(cid, rc_db_id)])
            await bot.answer_callback_query(call.id)
            await bot.edit_message_text(
                "👻 Who ghosted? Tap to select, then tap Done.",
                cid, call.message.message_id, reply_markup=markup
            )
            return

        # ── ghost_togp (proxy) ───────────────────────────────────────────────
        if data.startswith("ghost_togp_"):
            parts = data.split("_", 3)
            rc_db_id = int(parts[2])
            proxy_name = parts[3]
            key = (cid, rc_db_id)
            if key not in _ghost_selections:
                saved = load_ghost_selections(cid, rc_db_id)
                _ghost_selections[key] = saved if saved else set()
            if proxy_name in _ghost_selections[key]:
                _ghost_selections[key].discard(proxy_name)
            else:
                _ghost_selections[key].add(proxy_name)
            logging.info(f"[{_ts()}] ghost_togp: {proxy_name}, key={key}, selected now={_ghost_selections[key]}")
            save_ghost_selections(cid, rc_db_id, _ghost_selections[key])
            in_users = get_rollcall_in_users(rc_db_id)
            markup = _build_ghost_select_keyboard(rc_db_id, in_users, _ghost_selections[key])
            await bot.answer_callback_query(call.id)
            try:
                await bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=markup)
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise
            return

        # ── ghost_tog (real user) ────────────────────────────────────────────
        if data.startswith("ghost_tog_"):
            parts = data.split("_")
            if len(parts) < 4:
                await bot.answer_callback_query(call.id)
                return
            rc_db_id = int(parts[2])
            user_id = int(parts[3])
            key = (cid, rc_db_id)
            if key not in _ghost_selections:
                saved = load_ghost_selections(cid, rc_db_id)
                _ghost_selections[key] = saved if saved else set()
            if user_id in _ghost_selections[key]:
                _ghost_selections[key].discard(user_id)
            else:
                _ghost_selections[key].add(user_id)
            save_ghost_selections(cid, rc_db_id, _ghost_selections[key])
            in_users = get_rollcall_in_users(rc_db_id)
            markup = _build_ghost_select_keyboard(rc_db_id, in_users, _ghost_selections[key])
            await bot.answer_callback_query(call.id)
            try:
                await bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=markup)
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise
            return

        # ── ghost_done ───────────────────────────────────────────────────────
        if data.startswith("ghost_done_"):
            rc_db_id = int(data.split("_", 2)[2])
            key = (cid, rc_db_id)
            if key not in _ghost_selections:
                saved = load_ghost_selections(cid, rc_db_id)
                if saved:
                    _ghost_selections[key] = saved
            selected = _ghost_selections.pop(key, set())
            mark_rollcall_absent_done(rc_db_id)

            logging.info(f"[{_ts()}] ghost_done: key={key}, selected from map={selected}")

            if not selected:
                await bot.answer_callback_query(call.id, "No ghosts selected — marking all as attended.")
                await bot.edit_message_text("✅ No ghosts selected — all marked as attended.", cid, call.message.message_id)
                return

            in_users = get_rollcall_in_users(rc_db_id)
            user_map = {u['user_id']: u for u in in_users if u['user_id'] is not None}
            proxy_map = {u['proxy_name']: u for u in in_users if u.get('proxy_name') is not None}
            lines = []

            logging.info(f"[{_ts()}] Ghost callback: in_users={[u.get('first_name') or u.get('proxy_name') for u in in_users]}, selected={selected}")

            for item in selected:
                if isinstance(item, int):
                    u = user_map.get(item)
                    if not u:
                        logging.warning(f"[{_ts()}] Ghost: real user {item} not found in user_map")
                        continue
                    name = u.get('first_name') or u.get('username') or str(item)
                    logging.info(f"[{_ts()}] Ghosting real user: {name}")
                    increment_ghost_count(cid, item, name)
                    add_ghost_event(rc_db_id, cid, item, name)
                    reset_streak_on_ghost(cid, item)
                    new_count = get_ghost_count(cid, item)
                    lines.append(f"👻 {name} — ghosted {new_count} session(s) total")
                else:
                    proxy_name = str(item)
                    if proxy_name not in proxy_map:
                        logging.warning(f"[{_ts()}] Ghost: proxy {proxy_name} not found in proxy_map: {list(proxy_map.keys())}")
                        continue
                    logging.info(f"[{_ts()}] Ghosting proxy user: {proxy_name}")
                    increment_ghost_count(cid, -1, proxy_name, proxy_name=proxy_name)
                    add_ghost_event(rc_db_id, cid, None, user_name=proxy_name, proxy_name=proxy_name)
                    new_count = get_ghost_count_by_proxy_name(cid, proxy_name)
                    lines.append(f"👻 {proxy_name} (via /sif) — ghosted {new_count} session(s) total")

            # Forgive 1 absence for every IN user who actually attended (not selected).
            # decrement_ghost_count floors at 0, so users with no prior absences stay at 0.
            for u in in_users:
                real_uid = u.get('user_id')
                proxy_name = u.get('proxy_name')
                if proxy_name:
                    if proxy_name in selected:
                        continue
                    decrement_ghost_count(cid, -1, proxy_name=proxy_name)
                elif real_uid is not None:
                    if real_uid in selected:
                        continue
                    decrement_ghost_count(cid, real_uid)

            summary = "\n".join(lines)
            await bot.answer_callback_query(call.id, f"{len(selected)} ghost(s) recorded.")
            await bot.edit_message_text(
                f"👻 Ghost session recorded!\n\n{summary}",
                cid, call.message.message_id
            )
            return

        # ── reconf_in ────────────────────────────────────────────────────────
        if data.startswith("reconf_in_"):
            parts = data.split("_")
            rc_number = int(parts[2])
            uid = int(parts[3])
            if call.from_user.id != uid:
                await bot.answer_callback_query(call.id, "This confirmation is not for you.")
                return
            state = _pending_reconf.pop((cid, uid), {})
            rollcalls = manager.get_rollcalls(cid)
            if rc_number >= len(rollcalls):
                await bot.answer_callback_query(call.id, "Roll call no longer active.")
                return
            rc = rollcalls[rc_number]
            _username = call.from_user.username or None
            _display = _get_display_name(call.from_user)
            upsert_chat_member(cid, uid, _display, _username)
            user = User(_display, _username, uid, rc.allNames)
            user.comment = state.get('comment', '')
            result = rc.addIn(user)
            rc.save()
            rc_db_id = get_rc_db_id(rc)
            if result not in ('AB', 'AC', 'AU') and rc_db_id and isinstance(uid, int):
                increment_user_stat(cid, uid, "total_in")
                increment_rollcall_stat(rc_db_id, "total_in")
            await bot.answer_callback_query(call.id, "💪 You're IN!")
            await bot.edit_message_text(
                f"💪 {user.name} committed to IN!\n\n{rc.allList().replace('__RCID__', str(rc_number + 1))}",
                cid, call.message.message_id
            )
            return

        # ── reconf_out ───────────────────────────────────────────────────────
        if data.startswith("reconf_out_"):
            parts = data.split("_")
            rc_number = int(parts[2])
            uid = int(parts[3])
            if call.from_user.id != uid:
                await bot.answer_callback_query(call.id, "This confirmation is not for you.")
                return
            _pending_reconf.pop((cid, uid), None)
            rollcalls = manager.get_rollcalls(cid)
            if rc_number >= len(rollcalls):
                await bot.answer_callback_query(call.id, "Roll call no longer active.")
                return
            rc = rollcalls[rc_number]
            _username = call.from_user.username or None
            user = User(_get_display_name(call.from_user), _username, uid, rc.allNames)
            rc.addOut(user)
            rc.save()
            rc_db_id = get_rc_db_id(rc)
            if rc_db_id and isinstance(uid, int):
                increment_user_stat(cid, uid, "total_out")
                increment_rollcall_stat(rc_db_id, "total_out")
            await bot.answer_callback_query(call.id, "❌ Marked as Out")
            await bot.edit_message_text(
                f"❌ {user.name} is out.\n\n{rc.allList().replace('__RCID__', str(rc_number + 1))}",
                cid, call.message.message_id
            )
            return

        # ── proxy_add ────────────────────────────────────────────────────────
        if data.startswith("proxy_add_"):
            parts = data.split("_", 3)
            rc_number = int(parts[2])
            proxy_name = parts[3]

            rc = manager.get_rollcall(cid, rc_number)
            if not rc:
                await bot.answer_callback_query(call.id, "Rollcall not found")
                return

            proxy_owner_id = call.from_user.id
            pending = _pending_proxy_add.pop((cid, proxy_owner_id, proxy_name), {})
            comment = pending.get('comment', '')

            user = User(proxy_name, None, proxy_name, rc.allNames)
            user.comment = comment
            rc.set_proxy_owner(proxy_name, proxy_owner_id)

            # rc.addIn → _save_user_to_db writes the proxy row with the right
            # status (in or waitlist), preserving comment and owner from above.
            rc.addIn(user)
            rc.save()

            await bot.answer_callback_query(call.id, f"✅ Added {proxy_name}")
            await bot.edit_message_text(f"✅ {proxy_name} added to IN list", cid, call.message.message_id)
            from handlers.lifecycle import _update_panel
            await _update_panel(cid, rc_number + 1, rc, force_new=True)
            return

        # ── proxy_cancel ─────────────────────────────────────────────────────
        if data.startswith("proxy_cancel_"):
            parts = data.split("_", 3)
            proxy_name = parts[3] if len(parts) > 3 else ""
            _pending_proxy_add.pop((cid, call.from_user.id, proxy_name), None)
            await bot.answer_callback_query(call.id, "❌ Cancelled")
            await bot.edit_message_text("❌ Cancelled — user not added", cid, call.message.message_id)
            return

        # ── delconf_yes / delconf_no ─────────────────────────────────────────
        if data.startswith("delconf_yes_") or data.startswith("delconf_no_"):
            parts = data.split("_", 3)
            if len(parts) < 4:
                await bot.answer_callback_query(call.id)
                return
            confirmed = parts[1] == "yes"
            cb_rc_number = int(parts[2])
            admin_id = int(parts[3])

            if call.from_user.id != admin_id:
                await bot.answer_callback_query(call.id, "This action is not for you.", show_alert=True)
                return

            pending = _pending_deletes.pop((cid, admin_id), None)
            if pending and pending.get('rc_number') != cb_rc_number:
                _pending_deletes[(cid, admin_id)] = pending
                await bot.answer_callback_query(call.id, "Rollcall mismatch — please retry.", show_alert=True)
                return
            if not confirmed or pending is None:
                await bot.answer_callback_query(call.id, "❌ Cancelled")
                await bot.edit_message_text("❌ Delete cancelled.", cid, call.message.message_id)
                return

            name = pending['name']
            rc_number = pending['rc_number']
            rc = manager.get_rollcall(cid, rc_number)
            if rc and rc.delete_user(name):
                rc.save()
                log_admin_action(cid, admin_id, call.from_user.first_name, "delete_user", target_name=name, rollcall_id=getattr(rc, 'db_id', None) or getattr(rc, 'id', None), details=rc.title)
                await bot.answer_callback_query(call.id, f"✅ Deleted {name}")
                await bot.edit_message_text(f"✅ *{_esc_md(name)}* removed from rollcall #{rc_number + 1}.", cid, call.message.message_id, parse_mode="Markdown")
            else:
                await bot.answer_callback_query(call.id, "User not found")
                await bot.edit_message_text(f"⚠️ User *{_esc_md(name)}* not found.", cid, call.message.message_id, parse_mode="Markdown")
            return

        # ── ovrd_yes / ovrd_no ───────────────────────────────────────────────
        if data.startswith("ovrd_yes_") or data.startswith("ovrd_no_"):
            parts = data.split("_", 3)
            confirmed = parts[1] == "yes"
            admin_id = int(parts[3])

            if call.from_user.id != admin_id:
                await bot.answer_callback_query(call.id, "This action is not for you.", show_alert=True)
                return

            pending = _pending_overrides.pop((cid, admin_id), None)
            if not confirmed or pending is None:
                await bot.answer_callback_query(call.id, "❌ Cancelled")
                await bot.edit_message_text("❌ Override cancelled.", cid, call.message.message_id)
                return

            user = pending['user']
            status = pending['new_status']
            rc_number = pending['rc_number']
            rc = manager.get_rollcall(cid, rc_number)
            if not rc:
                await bot.answer_callback_query(call.id, "Rollcall not found")
                await bot.edit_message_text("⚠️ Rollcall not found.", cid, call.message.message_id)
                return

            from db import delete_user_by_id
            rc_db_id = get_rc_db_id(rc)
            if rc_db_id is not None:
                delete_user_by_id(rc_db_id, user.user_id)
            rc._load_users_from_db()
            if status == 'in':
                result = rc.addIn(user)
            elif status == 'out':
                result = rc.addOut(user)
            else:
                result = rc.addMaybe(user)
            rc.save()
            if not manager.get_shh_mode(cid):
                await bot.send_message(cid, f"✅ Done! {user.name}'s status for '{rc.title}' updated to {status.upper()}.")

            from handlers.lifecycle import _update_panel
            await _update_panel(cid, rc_number + 1, rc)
            log_admin_action(cid, admin_id, call.from_user.first_name, "set_status", target_name=f"{user.name} → {status}", rollcall_id=getattr(rc, 'db_id', None) or getattr(rc, 'id', None), details=rc.title)
            await bot.answer_callback_query(call.id, f"✅ Moved to {status.upper()}")
            await bot.edit_message_text(
                f"✅ *{_esc_md(user.name)}* → *{status.upper()}* in rollcall #{rc_number + 1}.",
                cid, call.message.message_id, parse_mode="Markdown"
            )
            return

        # ── mabs_sel ─────────────────────────────────────────────────────────
        if data.startswith("mabs_sel_"):
            rc_db_id = int(data.split("_", 2)[2])
            in_users = get_rollcall_in_users(rc_db_id)
            if not in_users:
                await bot.answer_callback_query(call.id, "No IN users found for this session.")
                await bot.edit_message_text("⚠️ No IN users were found for this session.", cid, call.message.message_id)
                return
            _ghost_selections[(cid, rc_db_id)] = set()
            markup = _build_ghost_select_keyboard(rc_db_id, in_users, set())
            await bot.answer_callback_query(call.id)
            await bot.edit_message_text(
                "👻 Who ghosted? Tap to select, then tap Done.",
                cid, call.message.message_id, reply_markup=markup
            )
            return

        await bot.answer_callback_query(call.id, "Unknown ghost action")

    except Exception as e:
        err_str = str(e)
        if "query is too old" in err_str or "query ID is invalid" in err_str:
            logging.warning(f"[{_ts()}] Stale ghost callback ignored ({call.data[:50]}): {err_str[:80]}")
            return
        logging.exception("Error in ghost_callback_handler")
        try:
            await bot.answer_callback_query(call.id, "⚠️ Something went wrong.")
        except Exception:
            pass
