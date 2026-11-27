"""
Teams on Telegram: the panel's ⚽ Teams button swaps the panel for the
team sheet, the way 📋 Lists does, and 🔙 Back returns to the panel.
/teams [::N] posts the same sheet as a message of its own.
/teams on|off (admin) — teams are opt-in per group and off by default.

Callback data: tm:<op>:<rollcall db id>[:<arg>]
  form:<n>  make n teams          shuf      reshuffle
  sw:<team>          swap picker: pick a player from <team>
  sp:<tok>[:<team>]  ...then who to swap them with (from <team>)
  sx:<tok>:<tok>[:<team>]  swap those two (<team>: where the second one was shown)
  mt:<tok>:<team>    just move one
  mv:<tok>  move a player on (sheets posted before the picker existed)
  lock / unlock / pub / pubpic (admin)   back   return to the panel
  view      redraw

With a sport set and 2 teams, the sheet is the lineup (services/lineup):
positions are arranged on the web page (🏟 button); publish posts the
text (pub) or a picture of the pitch (pubpic).

The sheet itself carries at most six buttons; the picker shows one team's
names at a time, three to a row, so an 8v8 stays readable.

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
from services import lineup as lineup_svc
from services import teams as teams_svc
from services.common import find_rollcall_index
from utils.text import parse_rc_suffix

_NAME_LABEL = 14
_PER_ROW = 3
# Telegram refuses a keyboard of more than 100 buttons — the picker would
# fail to draw. Past this many players on one team, swaps are made on the web.
_MAX_MOVE_BUTTONS = 80


def _move_buttons_fit(v: dict) -> bool:
    return max((len(t["players"]) for t in v["teams"]), default=0) <= _MAX_MOVE_BUTTONS


def _rows(markup: InlineKeyboardMarkup, buttons: list) -> None:
    for i in range(0, len(buttons), _PER_ROW):
        markup.row(*buttons[i:i + _PER_ROW])


def _team(v: dict, no) -> dict:
    return next((t for t in v["teams"] if str(t["no"]) == str(no)), v["teams"][0])


def _team_of(v: dict, key: str) -> dict:
    return next(t for t in v["teams"] if any(p["key"] == key for p in t["players"]))


def _cb(op: str, rc_id: int, arg: str = "") -> str:
    return f"tm:{op}:{rc_id}" + (f":{arg}" if arg else "")


def _publish_buttons(rc_id: int, lv) -> list:
    if lv and lv.get("available"):
        return [InlineKeyboardButton("📣 Post text", callback_data=_cb("pub", rc_id)),
                InlineKeyboardButton("🖼 Post picture", callback_data=_cb("pubpic", rc_id))]
    return [InlineKeyboardButton("📣 Publish", callback_data=_cb("pub", rc_id))]


def teams_keyboard(rc_id: int, v: dict, lv: dict | None = None, web_url: str = "") -> InlineKeyboardMarkup:
    """lv: the lineup view (services.lineup) when the group plays a sport;
    web_url: the group's page, for the 🏟 Arrange button."""
    markup = InlineKeyboardMarkup(row_width=2)
    if not v["formed"]:
        markup.add(*[InlineKeyboardButton(f"⚽ {n} teams", callback_data=_cb("form", rc_id, str(n)))
                     for n in range(teams_svc.MIN_TEAMS, teams_svc.MAX_TEAMS + 1)])
    elif not v["locked"]:
        shuffle = InlineKeyboardButton("🔀 Reshuffle", callback_data=_cb("shuf", rc_id))
        if _move_buttons_fit(v):
            markup.row(shuffle, InlineKeyboardButton("🔁 Swap players", callback_data=_cb("sw", rc_id, "1")))
        else:
            markup.row(shuffle)
        if lv and lv.get("available"):
            row = [InlineKeyboardButton("🔒 Lock", callback_data=_cb("lock", rc_id))]
            if web_url:
                row.append(InlineKeyboardButton("🏟 Arrange on web", url=web_url))
            markup.row(*row)
            markup.row(*_publish_buttons(rc_id, lv))
        else:
            markup.row(
                InlineKeyboardButton("🔒 Lock", callback_data=_cb("lock", rc_id)),
                InlineKeyboardButton("📣 Publish", callback_data=_cb("pub", rc_id)),
            )
        nxt_count = v["count"] % teams_svc.MAX_TEAMS + 1
        if nxt_count < teams_svc.MIN_TEAMS:
            nxt_count = teams_svc.MIN_TEAMS
        markup.row(
            InlineKeyboardButton(f"🔢 {nxt_count} teams", callback_data=_cb("form", rc_id, str(nxt_count))),
            InlineKeyboardButton("🔙 Back", callback_data=_cb("back", rc_id)),
        )
        return markup
    else:
        markup.row(*_publish_buttons(rc_id, lv))
        markup.row(InlineKeyboardButton("🔓 Unlock", callback_data=_cb("unlock", rc_id)))
    markup.add(InlineKeyboardButton("🔙 Back", callback_data=_cb("back", rc_id)))
    return markup


def _sheet_text(rc, v: dict, lv: dict | None = None) -> str:
    if lv and lv.get("available"):
        text = lineup_svc.render_text(rc, lv)
        if not v["locked"]:
            text += ("\n\n🏟 Arrange positions and formations on the group's web page."
                     if lv.get("kind") == "pitch" else
                     "\n\n🏏 Give each player a role (WK, BAT, AR, BOWL) on the group's web page.")
        return text
    text = teams_svc.render_text(rc, v)
    if v["formed"] and not v["locked"]:
        text += ("\n\n🔁 Swap players trades two players, so the teams stay even." if _move_buttons_fit(v)
                 else "\n\nToo many players for buttons — move players on the group's web page (🌐 Vote on Web).")
    elif not v["formed"]:
        text += "\n\nPick how many teams — players are split at random."
    return text


def _name_buttons(players: list, data) -> list:
    """One button per player, data(p) as its callback. Two players whose
    labels read the same (namesakes, or names alike in their first 14
    letters) get a (2), (3)… in sheet order, so the picker can tell them
    apart; an empty name would make Telegram refuse the whole keyboard."""
    seen: dict = {}
    out = []
    for p in players:
        label = (p["name"] or "")[:_NAME_LABEL].strip() or "?"
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = f"{label} ({seen[label]})"
        out.append(InlineKeyboardButton(label, callback_data=data(p)))
    return out


def _cancel(rc_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton("✖ Cancel", callback_data=_cb("view", rc_id))


def _tab(t: dict, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(f"{t['emoji']} Team {t['label']} ▸", callback_data=data)


def pick_screen(rc, v: dict, team_no) -> tuple[str, InlineKeyboardMarkup]:
    """Swap, step 1: one team's names; tabs reach the other teams."""
    team = _team(v, team_no)
    markup = InlineKeyboardMarkup()
    _rows(markup, _name_buttons(team["players"],
                                lambda p: _cb("sp", rc.id, teams_svc.button_token(p["key"]))))
    _rows(markup, [_tab(t, _cb("sw", rc.id, str(t["no"]))) for t in v["teams"] if t["no"] != team["no"]]
          + [_cancel(rc.id)])
    return (teams_svc.render_text(rc, v)
            + f"\n\n🔁 Who swaps? Pick a player from {team['emoji']} Team {team['label']}."), markup


def partner_screen(rc, v: dict, key: str, team_no=None) -> tuple[str, InlineKeyboardMarkup]:
    """Swap, step 2: who to trade the picked player with — or just move them."""
    own = _team_of(v, key)
    others = [t for t in v["teams"] if t["no"] != own["no"]]
    target = next((t for t in others if str(t["no"]) == str(team_no)), None) or others[0]
    name = next(p["name"] for p in own["players"] if p["key"] == key)
    tok = teams_svc.button_token(key)
    markup = InlineKeyboardMarkup()
    # The partner's team rides along so a swap tapped after the teams
    # changed (a reshuffle, another swap) is caught, not applied backwards.
    _rows(markup, _name_buttons(target["players"], lambda p: _cb(
        "sx", rc.id, f"{tok}:{teams_svc.button_token(p['key'])}:{target['no']}")))
    _rows(markup, [_tab(t, _cb("sp", rc.id, f"{tok}:{t['no']}")) for t in others if t["no"] != target["no"]])
    markup.row(InlineKeyboardButton(f"➡ Just move {name[:_NAME_LABEL]} to {target['label']}",
                                    callback_data=_cb("mt", rc.id, f"{tok}:{target['no']}")),
               _cancel(rc.id))
    return (teams_svc.render_text(rc, v)
            + f"\n\n🔁 Swap {name} ({own['emoji']} Team {own['label']}) with… "
              f"a player from {target['emoji']} Team {target['label']}."), markup


async def show_teams(call, cid: int, rc) -> None:
    """The panel's ⚽ Teams button — show the sheet in place of the panel."""
    if not teams_svc.enabled(cid):
        await bot.answer_callback_query(call.id, teams_svc.OFF_MESSAGE, show_alert=True)
        return
    await bot.answer_callback_query(call.id)
    v = teams_svc.view(rc)
    lv = lineup_svc.view(rc, v)
    await safe_edit_text(cid, call.message.message_id, _sheet_text(rc, v, lv),
                         reply_markup=teams_keyboard(rc.id, v, lv, _web_url(cid)))


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
        lv = lineup_svc.view(rc, v)
        await bot.send_message(cid, _sheet_text(rc, v, lv), reply_markup=teams_keyboard(rc.id, v, lv, _web_url(cid)))
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


def _player(v: dict, token: str) -> str:
    """The sheet key a button stands for — refused if they've left since."""
    key = teams_svc.player_by_token(v, token)
    if key is None:
        raise incorrectParameter("That player isn't IN any more.")
    return key


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

        is_admin = await _is_admin(call) if op in ("lock", "unlock", "pub", "pubpic") else False
        actor = _actor(call)
        toast, post, screen = "", None, None
        async with manager.get_chat_write_lock(cid):
            idx = find_rollcall_index(cid, rc_id, manager)
            if idx is None:
                rc = None
            else:
                rc = manager.get_rollcall(cid, idx)
                toks = arg.split(":") + [""]
                if op in ("sw", "sp", "sx", "mt"):
                    teams_svc.check_editable(rc)
                    v = teams_svc.view(rc)
                    if op in ("sw", "sp") and not _move_buttons_fit(v):
                        # A picker posted before the team grew past the cap.
                        raise incorrectParameter("Too many players for buttons — swap players "
                                                 "on the group's web page (🌐 Vote on Web).")
                if op == "sw":
                    screen = ("pick", toks[0] or "1")
                elif op == "sp":
                    screen = ("partner", _player(v, toks[0]), toks[1])
                elif op == "sx":
                    a, b = _player(v, toks[0]), _player(v, toks[1])
                    if toks[2] and str(_team_of(v, b)["no"]) != toks[2]:
                        # Shown a stale partner list: draw the picker afresh.
                        toast = "The teams changed — pick again."
                        screen = ("pick", str(_team_of(v, a)["no"]))
                    else:
                        v = teams_svc.swap(rc, a, b, actor)
                        toast = "{} ⇄ {}".format(*v["swapped"])
                elif op == "mt":
                    v = teams_svc.move(rc, _player(v, toks[0]), toks[1] or None, actor)
                    toast = f"{v['moved']['name']} → Team {v['moved']['team']}"
                elif op == "form":
                    v = teams_svc.form(rc, arg, actor)
                    toast = f"{v['count']} teams made"
                elif op == "shuf":
                    v = teams_svc.reshuffle(rc, actor)
                    toast = "Reshuffled"
                elif op == "mv":
                    v = teams_svc.move(rc, _player(teams_svc.view(rc), arg), None, actor)
                    toast = f"{v['moved']['name']} → Team {v['moved']['team']}"
                elif op in ("lock", "unlock"):
                    v = teams_svc.set_locked(rc, op == "lock", is_admin)
                    toast = "Teams locked" if op == "lock" else "Teams unlocked"
                elif op in ("pub", "pubpic"):
                    v, post = teams_svc.publish(rc, is_admin)
                    toast = "Published"
                else:
                    v = teams_svc.view(rc)
        if rc is None:
            await bot.answer_callback_query(call.id, "That rollcall has ended.", show_alert=True)
            return

        if screen and screen[0] == "pick":
            text, markup = pick_screen(rc, v, screen[1])
        elif screen:
            text, markup = partner_screen(rc, v, screen[1], screen[2])
        else:
            lv = lineup_svc.view(rc, v)
            text, markup = _sheet_text(rc, v, lv), teams_keyboard(rc_id, v, lv, _web_url(cid))
        await best_effort(bot.answer_callback_query(call.id, toast or None), "teams tap", cid)
        await safe_edit_text(cid, call.message.message_id, text, reply_markup=markup)
        if post:
            await post_published(cid, rc, v, post, actor, as_picture=(op == "pubpic"))
    except (incorrectParameter, insufficientPermissions) as e:
        await best_effort(bot.answer_callback_query(call.id, str(e), show_alert=True), "teams refusal", cid)
    except Exception:
        logging.exception("teams callback failed (%s)", call.data)
        await best_effort(bot.answer_callback_query(call.id, "⚠️ Something went wrong."), "teams error", cid)


def _web_url(cid: int) -> str:
    from handlers.lifecycle import _group_web_url
    return _group_web_url(cid)


async def post_published(cid: int, rc, v: dict, teams_text: str, actor: str,
                         as_picture: bool = False, via: str = "") -> None:
    """Post a publish to the group — the lineup's text or picture when the
    group plays a sport, else the team sheet. Outside the chat lock."""
    content = lineup_svc.publish_content(rc, v, teams_text, as_picture)
    by = f"📣 Published by {actor}{via}"
    if content["photo"] is not None:
        await best_effort(bot.send_photo(cid, content["photo"], caption=f"{content['caption']}\n{by}"),
                          "lineup picture", cid)
    else:
        await best_effort(bot.send_message(cid, content["text"] + f"\n\n{by}"), "teams publish", cid)


@bot.message_handler(func=lambda m: bool(m.text) and m.text.split()[0].split("@")[0].lower() == "/sport")
async def sport_cmd(message):
    """/sport — show the group's sport; /sport football | off (admin)."""
    cid = message.chat.id
    try:
        args = message.text.split()[1:]
        if not args:
            cur = lineup_svc.sport(cid)
            await bot.send_message(cid, (
                f"{lineup_svc.SPORTS[cur]['emoji']} This group plays {lineup_svc.SPORTS[cur]['label']} — "
                + ("2 teams are shown on a pitch." if lineup_svc.kind(cur) == "pitch"
                   else "players in 2 teams get a role (WK, BAT, AR, BOWL).")
                + " /sport off goes back to plain team lists."
                if cur else
                "No sport set — teams are plain lists. An admin can set one: /sport football or /sport cricket"))
            return
        if not await admin_rights(message, manager):
            raise insufficientPermissions("Only an admin can set the group's sport.")
        actor = message.from_user.first_name or message.from_user.username or "Admin"
        async with manager.get_chat_write_lock(cid):
            new = lineup_svc.set_sport(cid, args[0], message.from_user.id, actor)
            if new:
                lineup_svc.autofill_open(cid, actor)
        if new:
            hint = "" if teams_svc.enabled(cid) else " Turn teams on with /teams on."
            if lineup_svc.kind(new) == "pitch":
                what = "make 2 teams and they're drawn on a pitch (/lineup)."
            else:
                what = ("cricket doesn't need a pitch or formation, so instead each player in "
                        "2 teams gets a role: 🧤 wicket-keeper, 🏏 batter, ⭐ all-rounder or 🎯 bowler "
                        "(set them on the group's web page; /lineup shows them).")
            await bot.send_message(cid, f"{lineup_svc.SPORTS[new]['emoji']} Sport set to "
                                        f"{lineup_svc.SPORTS[new]['label']} — {what}{hint}")
        else:
            await bot.send_message(cid, "Sport off — teams are plain lists again.")
    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda m: bool(m.text) and m.text.split()[0].split("@")[0].lower() == "/lineup")
async def lineup_cmd(message):
    """/lineup [pic] [::N] — post the lineup (text, or the pitch picture)."""
    cid = message.chat.id
    try:
        args = message.text.split()[1:]
        as_picture = any(a.lower() in ("pic", "picture", "photo") for a in args)
        args = [a for a in args if a.lower() not in ("pic", "picture", "photo")]
        if not teams_svc.enabled(cid):
            await bot.send_message(cid, teams_svc.OFF_MESSAGE)
            return
        if lineup_svc.sport(cid) is None:
            await bot.send_message(cid, "No sport set for this group — an admin can set one with "
                                        "/sport football (a pitch with positions) or /sport cricket "
                                        "(roles: WK, BAT, AR, BOWL).")
            return
        idx, _rest = parse_rc_suffix(args)
        rollcalls = manager.get_rollcalls(cid)
        if not rollcalls:
            raise rollCallNotStarted("No rollcall is open — the lineup is made from an open game's teams.")
        if idx >= len(rollcalls):
            raise incorrectParameter(f"There is no rollcall ::{idx + 1}.")
        rc = rollcalls[idx]
        v = teams_svc.view(rc)
        lv = lineup_svc.view(rc, v)
        if not lv["available"]:
            await bot.send_message(cid, "The lineup needs exactly 2 teams — make them with /teams.")
            return
        url = _web_url(cid)
        markup = None
        if url and not lv["locked"]:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🏟 Arrange on web", url=url))
        if as_picture:   # showing it, not publishing it — no webhook, no publish stamp
            await bot.send_photo(cid, lineup_svc.render_png(rc, lv),
                                 caption=lineup_svc.credit_caption(
                                     f"{lineup_svc.SPORTS[lv['sport']]['emoji']} "
                                     f"{'Lineup' if lv.get('kind') == 'pitch' else 'Teams'} — {rc.title}"),
                                 reply_markup=markup)
            return
        await bot.send_message(cid, lineup_svc.render_text(rc, lv), reply_markup=markup)
    except Exception as e:
        await reply_error(message, e)


async def _back_to_panel(call, cid: int, rc_id: int) -> None:
    from handlers.lifecycle import _build_panel_text, get_status_keyboard
    await bot.answer_callback_query(call.id)
    idx = find_rollcall_index(cid, rc_id, manager)
    if idx is None:
        return
    rc = manager.get_rollcall(cid, idx)
    await safe_edit_text(cid, call.message.message_id, _build_panel_text(rc, idx + 1),
                         reply_markup=await get_status_keyboard(idx + 1, cid))
