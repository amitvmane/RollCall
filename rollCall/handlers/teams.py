"""
Teams on Telegram: the panel's ⚽ Teams button swaps the panel for the
team sheet, the way 📋 Lists does, and 🔙 Back returns to the panel.
/teams [::N] posts the same sheet as a message of its own.
/teams on|off (admin) — teams are opt-in per group and off by default.

Callback data: tm:<op>:<rollcall db id>[:<arg>]
  form:<n>  make n teams          shuf      reshuffle
  mv:<tok>  move a player on      lock / unlock (admin)
  pub       post teams to group   back      return to the panel
  view      redraw

All rules live in services.teams (shared with the web page). Changes are
made under the chat write lock; anything sent to Telegram happens after it.
"""
import logging
from types import SimpleNamespace

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot_state import bot, best_effort, reply_error, safe_edit_text
from exceptions import incorrectParameter, insufficientPermissions, rollCallNotStarted
from functions import admin_rights
from rollcall_manager import manager
from services import teams as teams_svc
from services.common import find_rollcall_index
from utils.text import parse_rc_suffix

_MAX_LABEL = 28


def _cb(op: str, rc_id: int, arg: str = "") -> str:
    return f"tm:{op}:{rc_id}" + (f":{arg}" if arg else "")


def teams_keyboard(rc_id: int, v: dict) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    if not v["formed"]:
        markup.add(*[InlineKeyboardButton(f"⚽ {n} teams", callback_data=_cb("form", rc_id, str(n)))
                     for n in range(teams_svc.MIN_TEAMS, teams_svc.MAX_TEAMS + 1)])
    elif not v["locked"]:
        buttons = []
        for team in v["teams"]:
            nxt = teams_svc.LABELS[team["no"] % v["count"]]
            for p in team["players"]:
                label = f"{team['emoji']} {p['name']}"[:_MAX_LABEL] + f" → {nxt}"
                buttons.append(InlineKeyboardButton(
                    label, callback_data=_cb("mv", rc_id, teams_svc.button_token(p["key"]))))
        if buttons:
            markup.add(*buttons)
        markup.add(
            InlineKeyboardButton("🔀 Reshuffle", callback_data=_cb("shuf", rc_id)),
            InlineKeyboardButton("📣 Publish", callback_data=_cb("pub", rc_id)),
        )
        nxt_count = v["count"] % teams_svc.MAX_TEAMS + 1
        if nxt_count < teams_svc.MIN_TEAMS:
            nxt_count = teams_svc.MIN_TEAMS
        markup.add(
            InlineKeyboardButton(f"🔢 Make {nxt_count} teams", callback_data=_cb("form", rc_id, str(nxt_count))),
            InlineKeyboardButton("🔒 Lock", callback_data=_cb("lock", rc_id)),
        )
    else:
        markup.add(
            InlineKeyboardButton("📣 Publish", callback_data=_cb("pub", rc_id)),
            InlineKeyboardButton("🔓 Unlock", callback_data=_cb("unlock", rc_id)),
        )
    markup.add(InlineKeyboardButton("🔙 Back", callback_data=_cb("back", rc_id)))
    return markup


def _sheet_text(rc, v: dict) -> str:
    text = teams_svc.render_text(rc, v)
    if v["formed"] and not v["locked"]:
        text += "\n\nTap a player to move them to the next team."
    elif not v["formed"]:
        text += "\n\nPick how many teams — players are split at random."
    return text


async def show_teams(call, cid: int, rc) -> None:
    """The panel's ⚽ Teams button — show the sheet in place of the panel."""
    if not teams_svc.enabled(cid):
        await bot.answer_callback_query(call.id, teams_svc.OFF_MESSAGE, show_alert=True)
        return
    await bot.answer_callback_query(call.id)
    v = teams_svc.view(rc)
    await safe_edit_text(cid, call.message.message_id, _sheet_text(rc, v),
                         reply_markup=teams_keyboard(rc.id, v))


@bot.message_handler(func=lambda m: bool(m.text) and m.text.split()[0].split("@")[0].lower() == "/teams")
async def teams_cmd(message):
    """/teams [::N] — post the team sheet (with its buttons) for a rollcall.
    /teams on|off — an admin turns teams on or off for the group."""
    cid = message.chat.id
    try:
        args = message.text.split()[1:]
        if args and args[0].lower() in ("on", "off"):
            if not await admin_rights(message, manager):
                raise insufficientPermissions("Only an admin can turn teams on or off.")
            on = args[0].lower() == "on"
            teams_svc.set_enabled(cid, on, message.from_user.id,
                                  message.from_user.first_name or message.from_user.username or "Admin")
            await bot.send_message(cid, "⚽ Teams on — use /teams or the ⚽ Teams button on a rollcall."
                                   if on else "⚽ Teams off — the Teams button is hidden.")
            await _redraw_open_panels(cid)
            return
        if not teams_svc.enabled(cid):
            await bot.send_message(cid, teams_svc.OFF_MESSAGE)
            return
        idx, _rest = parse_rc_suffix(args)
        rollcalls = manager.get_rollcalls(cid)
        if not rollcalls:
            raise rollCallNotStarted("No rollcall is open — teams are made from an open game's IN list.")
        if idx >= len(rollcalls):
            raise incorrectParameter(f"There is no rollcall ::{idx + 1}.")
        rc = rollcalls[idx]
        v = teams_svc.view(rc)
        await bot.send_message(cid, _sheet_text(rc, v), reply_markup=teams_keyboard(rc.id, v))
    except Exception as e:
        await reply_error(message, e)


async def _redraw_open_panels(cid: int) -> None:
    """Show or hide the ⚽ Teams button on panels already posted."""
    from handlers.lifecycle import _update_panel
    for idx, rc in enumerate(manager.get_rollcalls(cid)):
        await best_effort(_update_panel(cid, idx + 1, rc), "panel redraw", cid)


async def _is_admin(call) -> bool:
    return bool(await admin_rights(
        SimpleNamespace(chat=call.message.chat, from_user=call.from_user), manager))


def _actor(call) -> str:
    u = call.from_user
    return u.first_name or u.username or str(u.id)


@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("tm:"))
async def teams_callback(call):
    cid = call.message.chat.id
    try:
        parts = call.data.split(":", 3)
        if len(parts) < 3:
            await bot.answer_callback_query(call.id, "Invalid action")
            return
        op, arg = parts[1], (parts[3] if len(parts) == 4 else "")
        try:
            rc_id = int(parts[2])
        except ValueError:
            await bot.answer_callback_query(call.id, "Invalid action")
            return

        # A rollcall id from the button is only trusted for an OPEN rollcall
        # of THIS chat (callback data can be forged).
        if find_rollcall_index(cid, rc_id, manager) is None:
            await bot.answer_callback_query(call.id, "That rollcall has ended.", show_alert=True)
            return

        if op == "back":
            await _back_to_panel(call, cid, rc_id)
            return
        if not teams_svc.enabled(cid):
            await bot.answer_callback_query(call.id, teams_svc.OFF_MESSAGE, show_alert=True)
            return

        is_admin = await _is_admin(call) if op in ("form", "lock", "unlock") else False
        actor = _actor(call)
        toast, post = "", None
        async with manager.get_chat_write_lock(cid):
            idx = find_rollcall_index(cid, rc_id, manager)
            if idx is None:
                rc = None
            else:
                rc = manager.get_rollcall(cid, idx)
                if op == "form":
                    v = teams_svc.form(rc, arg, actor, is_admin)
                    toast = f"{v['count']} teams made"
                elif op == "shuf":
                    v = teams_svc.reshuffle(rc, actor)
                    toast = "Reshuffled"
                elif op == "mv":
                    key = teams_svc.player_by_token(teams_svc.view(rc), arg)
                    if key is None:
                        raise incorrectParameter("That player isn't IN any more.")
                    v = teams_svc.move(rc, key, None, actor)
                    toast = f"{v['moved']['name']} → Team {v['moved']['team']}"
                elif op in ("lock", "unlock"):
                    v = teams_svc.set_locked(rc, op == "lock", is_admin)
                    toast = "Teams locked" if op == "lock" else "Teams unlocked"
                elif op == "pub":
                    v, post = teams_svc.publish(rc)
                    toast = "Published"
                else:
                    v = teams_svc.view(rc)
        if rc is None:
            await bot.answer_callback_query(call.id, "That rollcall has ended.", show_alert=True)
            return

        await best_effort(bot.answer_callback_query(call.id, toast or None), "teams tap", cid)
        await safe_edit_text(cid, call.message.message_id, _sheet_text(rc, v),
                             reply_markup=teams_keyboard(rc_id, v))
        if post:
            await best_effort(bot.send_message(cid, post + f"\n\n📣 Published by {actor}"),
                              "teams publish", cid)
    except (incorrectParameter, insufficientPermissions) as e:
        await best_effort(bot.answer_callback_query(call.id, str(e), show_alert=True), "teams refusal", cid)
    except Exception:
        logging.exception("teams callback failed (%s)", call.data)
        await best_effort(bot.answer_callback_query(call.id, "⚠️ Something went wrong."), "teams error", cid)


async def _back_to_panel(call, cid: int, rc_id: int) -> None:
    from handlers.lifecycle import _build_panel_text, get_status_keyboard
    await bot.answer_callback_query(call.id)
    idx = find_rollcall_index(cid, rc_id, manager)
    if idx is None:
        return
    rc = manager.get_rollcall(cid, idx)
    await safe_edit_text(cid, call.message.message_id, _build_panel_text(rc, idx + 1),
                         reply_markup=await get_status_keyboard(idx + 1, cid))
