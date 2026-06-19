"""
Template handlers: /templates, /set_template, /start_template, /delete_template,
/schedules, /schedule_template, and schedules_toggle_callback.
"""
import asyncio
import html
import logging
from datetime import datetime

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot_state import bot, _sched_selection, _log_task_exc, _esc_md, reply_error
from exceptions import insufficientPermissions, incorrectParameter, parameterMissing
from functions import admin_rights, weekly_minutes, WEEKDAY_MAP
from rollcall_manager import manager
from services import templates as templates_svc


def _ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


@bot.message_handler(func=lambda message: message.text.split(" ")[0].split("@")[0].lower() == "/templates")
async def list_templates(message):
    cid = message.chat.id
    if not await admin_rights(message, manager):
        await bot.send_message(cid, "You don't have permission to use this command.")
        return
    templates = templates_svc.list_templates(cid)

    if not templates:
        await bot.send_message(cid, "No templates defined for this chat.")
        return

    lines = []
    for t in templates:
        t_title = t.get("title") or "(no title)"
        sched_enabled = t.get("schedule_enabled")
        sched_day = t.get("schedule_day")
        sched_time = t.get("schedule_time")
        event_day = t.get("event_day")
        event_time = t.get("event_time")
        last_run = t.get("last_scheduled_date")

        if sched_enabled and sched_day and sched_time:
            sched_info = f"  🗓 Opens {sched_day.capitalize()} {sched_time}"
            if event_day and event_time:
                sched_info += f" → closes {event_day.capitalize()} {event_time}"
            if last_run:
                sched_info += f"  (last: {last_run})"
        elif event_day and event_time:
            sched_info = f"  📅 Event: {event_day.capitalize()} {event_time}"
        else:
            sched_info = ""

        lines.append(f"- {t['name']}: {t_title}{sched_info}")

    await bot.send_message(cid, "Templates:\n" + "\n".join(lines))


def _fmt_schedule_entry(t: dict) -> str:
    name = t.get("name", "?")
    title = t.get("title") or name
    enabled = bool(t.get("schedule_enabled"))
    sched_day = t.get("schedule_day") or ""
    sched_time = t.get("schedule_time") or ""
    event_day = t.get("event_day") or ""
    event_time = t.get("event_time") or ""
    recurrence = t.get("recurrence_type") or "weekly"
    last_run = t.get("last_scheduled_date")

    status = "🟢" if enabled else "🔴"
    paused_tag = "" if enabled else "  <i>(paused)</i>"

    rec_label = {"weekly": "weekly", "biweekly": "every 2 weeks", "monthly": "monthly"}.get(recurrence, recurrence)
    if recurrence == "monthly":
        timing = f"day {html.escape(sched_day)} at {html.escape(sched_time)}"
        if event_day and event_time:
            timing += f" → closes day {html.escape(event_day)} at {html.escape(event_time)}"
    else:
        timing = f"{html.escape(sched_day.capitalize())} {html.escape(sched_time)}"
        if event_day and event_time:
            timing += f" → {html.escape(event_day.capitalize())} {html.escape(event_time)}"

    last = f"last run: {html.escape(last_run)}" if last_run else "never run"
    return (
        f"{status} <b>{html.escape(title)}</b> <code>[{html.escape(name)}]</code>{paused_tag}\n"
        f"   {timing}  ·  {rec_label}  ·  {last}"
    )


def _build_schedules_keyboard(templates: list, cid: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    selected = _sched_selection.get(cid, set())

    for t in templates:
        name = t.get("name", "")
        title = t.get("title") or name
        status = "🟢" if t.get("schedule_enabled") else "🔴"
        check = "☑️" if name in selected else "⬜"
        markup.add(InlineKeyboardButton(
            f"{check} {status}  {title}",
            callback_data=f"sched_sel_{name}"
        ))

    if selected:
        markup.row(
            InlineKeyboardButton("⏸ Pause Selected", callback_data="sched_apply_off"),
            InlineKeyboardButton("▶️ Resume Selected", callback_data="sched_apply_on"),
        )

    all_names = {t.get("name", "") for t in templates}
    if selected >= all_names:
        markup.row(InlineKeyboardButton("☐ Clear selection", callback_data="sched_selclear"))
    else:
        markup.row(InlineKeyboardButton("✅ Select all", callback_data="sched_selall"))

    return markup


async def _send_schedules(cid: int, edit_msg_id: int = None):
    templates = templates_svc.list_templates(cid)
    scheduled = [t for t in templates if t.get("schedule_day") and t.get("schedule_time")]

    if not scheduled:
        text = "📅 No scheduled templates yet.\nUse /schedule_template to set up auto-start."
        if edit_msg_id:
            try:
                await bot.edit_message_text(text, cid, edit_msg_id)
            except Exception:
                pass
        else:
            await bot.send_message(cid, text)
        return

    active = sum(1 for t in scheduled if t.get("schedule_enabled"))
    selected_count = len(_sched_selection.get(cid, set()))
    sel_hint = f"  ·  <i>{selected_count} selected</i>" if selected_count else ""
    header = f"<b>📅 Scheduled Templates</b>  ({active} active / {len(scheduled)} total{sel_hint})\n"
    body = "\n\n".join([_fmt_schedule_entry(t) for t in scheduled])
    text = f"{header}\n{body}"
    markup = _build_schedules_keyboard(scheduled, cid)

    if edit_msg_id:
        try:
            await bot.edit_message_text(text, cid, edit_msg_id, parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            if "message is not modified" not in str(e):
                raise
    else:
        await bot.send_message(cid, text, parse_mode="HTML", reply_markup=markup)


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/schedules")
async def schedules_command(message):
    cid = message.chat.id
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        await _send_schedules(cid)
    except (insufficientPermissions,) as e:
        await reply_error(cid, e)
    except Exception:
        logging.exception("Error in /schedules")
        await bot.send_message(cid, "Error fetching schedule info.")


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/schedule_template")
async def schedule_template_cmd(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        cid = message.chat.id
        parts = message.text.strip().split()

        if len(parts) < 2:
            await bot.send_message(
                cid,
                "Usage:\n"
                "/schedule_template <name> <weekday> <HH:MM>           — weekly auto-start\n"
                "/schedule_template <name> <weekday> <HH:MM> biweekly  — every 2 weeks\n"
                "/schedule_template <name> monthly <day> <HH:MM>       — monthly on day N\n"
                "/schedule_template <name> off                          — disable\n"
                "/schedule_template <name>                              — show current schedule\n\n"
                "Examples:\n"
                "/schedule_template sunday_game friday 09:00\n"
                "/schedule_template sunday_game friday 09:00 biweekly\n"
                "/schedule_template sunday_game monthly 15 09:00"
            )
            return

        name = parts[1]
        try:
            tmpl = templates_svc.get_one_template(cid, name)
        except (incorrectParameter, parameterMissing):
            await bot.send_message(cid, f"Template '{name}' not found. Use /templates to list available templates.")
            return

        if len(parts) == 2:
            sched_enabled = tmpl.get("schedule_enabled")
            sched_day = tmpl.get("schedule_day")
            sched_time = tmpl.get("schedule_time")
            event_day = tmpl.get("event_day")
            event_time = tmpl.get("event_time")
            last_run = tmpl.get("last_scheduled_date")
            recurrence_type = tmpl.get("recurrence_type") or "weekly"
            if sched_enabled and sched_day and sched_time:
                recurrence_label = {"weekly": "weekly", "biweekly": "every 2 weeks", "monthly": "monthly"}.get(recurrence_type, recurrence_type)
                if recurrence_type == "monthly":
                    opens_str = f"day {sched_day} of each month at {sched_time}"
                else:
                    opens_str = f"{sched_day.capitalize()} {sched_time} ({recurrence_label})"
                status = f"🗓 *{_esc_md(name)}* schedule: 🟢 enabled\nOpens: {opens_str}\n"
                if event_day and event_time:
                    status += f"Closes: {event_day.capitalize()} {event_time}\n"
                if last_run:
                    status += f"Last auto-started: {last_run}"
            else:
                status = f"🗓 *{_esc_md(name)}* schedule: 🔴 disabled"
            await bot.send_message(cid, status, parse_mode="Markdown")
            return

        if parts[2].lower() == "off":
            try:
                templates_svc.disable_schedule(cid, name, message.from_user.id, message.from_user.first_name)
                await bot.send_message(cid, f"🔴 Schedule disabled for template '{name}'.")
            except Exception:
                await bot.send_message(cid, f"Failed to update template '{name}'.")
            return

        if len(parts) < 4:
            await bot.send_message(
                cid,
                "To enable scheduling provide a weekday and time.\n"
                "Example: /schedule_template sunday_game friday 09:00\n"
                "         /schedule_template sunday_game monthly 15 09:00"
            )
            return

        recurrence_type = "weekly"
        sched_day = parts[2].lower()

        if sched_day == "monthly":
            if len(parts) < 5:
                await bot.send_message(cid, "For monthly scheduling: /schedule_template <name> monthly <day_number> <HH:MM>\nExample: /schedule_template sunday_game monthly 15 09:00")
                return
            try:
                day_num = int(parts[3])
                if not 1 <= day_num <= 31:
                    raise ValueError
            except ValueError:
                await bot.send_message(cid, f"'{parts[3]}' is not a valid day number (1–31).")
                return
            sched_day = str(day_num)
            sched_time = parts[4]
            recurrence_type = "monthly"
        else:
            sched_time = parts[3]
            if len(parts) > 4 and parts[4].lower() == "biweekly":
                recurrence_type = "biweekly"

            if sched_day not in WEEKDAY_MAP:
                await bot.send_message(
                    cid,
                    f"'{sched_day}' is not a valid weekday.\n"
                    "Use: monday, tuesday, wednesday, thursday, friday, saturday, sunday"
                )
                return

        try:
            sh, sm = map(int, sched_time.split(":"))
            if not (0 <= sh < 24 and 0 <= sm < 60):
                raise ValueError
        except ValueError:
            await bot.send_message(cid, f"'{sched_time}' is not a valid time. Use HH:MM (e.g. 09:00).")
            return

        event_day = tmpl.get("event_day")
        event_time = tmpl.get("event_time")
        if not event_day or not event_time:
            await bot.send_message(
                cid,
                f"Template '{name}' has no event_day/event_time set.\n"
                "Set them first so the auto-started rollcall knows when to close:\n"
                f"/set_template {name} event_day=sunday event_time=17:00"
            )
            return

        if recurrence_type in ("weekly", "biweekly"):
            sched_mins = weekly_minutes(sched_day, sched_time)
            event_mins = weekly_minutes(event_day, event_time)
            if sched_mins is None or event_mins is None:
                await bot.send_message(cid, "Could not validate schedule vs event time. Check day/time formats.")
                return
            if sched_mins >= event_mins:
                await bot.send_message(
                    cid,
                    f"Schedule time ({sched_day.capitalize()} {sched_time}) must be "
                    f"before event time ({event_day.capitalize()} {event_time}) in the weekly cycle."
                )
                return

        try:
            templates_svc.set_schedule(
                cid, name, message.from_user.id, message.from_user.first_name,
                recurrence_type=recurrence_type,
                schedule_day=sched_day if recurrence_type != "monthly" else None,
                schedule_time=sched_time,
                monthly_day=day_num if recurrence_type == "monthly" else None,
            )
            recurrence_label = {"weekly": "weekly", "biweekly": "every 2 weeks", "monthly": "monthly"}.get(recurrence_type, recurrence_type)
            if recurrence_type == "monthly":
                opens_str = f"day {sched_day} of each month at {sched_time}"
            else:
                opens_str = f"{sched_day.capitalize()} at {sched_time} ({recurrence_label})"
            await bot.send_message(
                cid,
                f"🟢 Schedule set for template *{_esc_md(name)}*:\n"
                f"Opens: {opens_str}\n"
                f"Closes: {event_day.capitalize()} at {event_time}",
                parse_mode="Markdown"
            )
        except Exception:
            await bot.send_message(cid, f"Failed to save schedule for '{name}'.")

    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: message.text.split(" ")[0].split("@")[0].lower() == "/start_template")
async def start_template(message):
    cid = message.chat.id
    if not await admin_rights(message, manager):
        await bot.send_message(cid, "You don't have permission to use this command.")
        return
    parts = message.text.split(" ", 2)

    if len(parts) < 2:
        await bot.send_message(
            cid,
            "Usage:\n"
            "/start_template template_name [optional extra title]\n"
            'Example: /start_template sunday "With guests"'
        )
        return

    template_name = parts[1]
    extra = parts[2].strip() if len(parts) > 2 else ""

    try:
        result = await templates_svc.start_template(
            cid, template_name,
            message.from_user.id, message.from_user.first_name,
            extra_title=extra or None,
        )
    except (incorrectParameter, parameterMissing) as e:
        await reply_error(message, e)
        return

    from handlers.lifecycle import show_panel_for_rollcall
    await show_panel_for_rollcall(cid, result["number"])

    if result.get("finalize_date"):
        from check_reminders import start
        rollcalls = manager.get_rollcalls(cid)
        asyncio.create_task(start(rollcalls, result["timezone"], cid)).add_done_callback(_log_task_exc)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_template")
async def set_template(message):
    try:
        cid = message.chat.id

        if await admin_rights(message, manager) is False:
            await bot.send_message(cid, "Error - User does not have sufficient permissions for this operation")
            return

        msg = message.text.strip()
        msg = msg.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")

        parts = msg.split(" ", 2)
        if len(parts) < 2:
            await bot.send_message(
                cid,
                "Format:\n"
                "/set_template name \"Title\" [limit=N] [location=Place] [fee=\"Amount\"] "
                "[offset_days=D] [offset_hours=H] [offset_minutes=M] [event_day=weekday] [event_time=HH:MM]"
            )
            return

        name = parts[1]

        if len(name) > 50:
            await bot.send_message(cid, f"⚠️ Template name is too long (max 50 characters). Got {len(name)}.")
            return

        title = None
        tail = ""

        if len(parts) == 2:
            tail = ""
        else:
            tail = parts[2].strip()

        try:
            existing = templates_svc.get_one_template(cid, name)
        except Exception:
            existing = {}
        title       = existing.get('title')
        inlistlimit = existing.get('limit')
        location    = existing.get('location')
        eventfee    = existing.get('fee')
        offsetdays  = existing.get('offset_days')
        offsethours = existing.get('offset_hours')
        offsetminutes = existing.get('offset_minutes')
        event_day   = existing.get('event_day')
        event_time  = existing.get('event_time')

        if tail.startswith('"'):
            end_quote = tail.find('"', 1)
            if end_quote != -1:
                title = tail[1:end_quote]
                tail = tail[end_quote + 1:].strip()
            else:
                title = tail[1:]
                tail = ""
        else:
            first_space = tail.find(" ")
            first_token = tail[:first_space] if first_space > 0 else tail
            if tail and '=' not in first_token:
                title = first_token
                tail = tail[first_space + 1:].strip() if first_space > 0 else ""

        tokens = tail.split()
        bad_values = []
        for tok in tokens:
            if "=" not in tok:
                continue
            key, val = tok.split("=", 1)
            key = key.strip().lower()
            val = val.strip().strip('"').strip("'")

            def _try_int(field_name, current):
                try:
                    return int(val)
                except ValueError:
                    bad_values.append(f"{field_name}={val!r}")
                    return current

            if key == "limit":
                inlistlimit = _try_int("limit", inlistlimit)
            elif key == "location":
                location = val
            elif key == "fee":
                eventfee = val
            elif key == "offset_days":
                offsetdays = _try_int("offset_days", offsetdays)
            elif key == "offset_hours":
                offsethours = _try_int("offset_hours", offsethours)
            elif key == "offset_minutes":
                offsetminutes = _try_int("offset_minutes", offsetminutes)
            elif key == "event_day":
                event_day = val.lower()
            elif key == "event_time":
                event_time = val

        if bad_values:
            await bot.send_message(
                cid,
                f"⚠️ Ignored non-integer value(s): {', '.join(bad_values)}. Existing values preserved.",
            )

        try:
            templates_svc.upsert_template(
                cid, name, message.from_user.id, message.from_user.first_name,
                title=title, limit=inlistlimit, location=location, fee=eventfee,
                offset_days=offsetdays, offset_hours=offsethours, offset_minutes=offsetminutes,
                event_day=event_day, event_time=event_time,
            )
            if not manager.get_shh_mode(cid):
                await bot.send_message(
                    cid,
                    f"Template '{name}' saved for this chat.\n"
                    f"Title: {title or 'none'}\n"
                    f"Limit: {inlistlimit if inlistlimit is not None else 'none'}\n"
                    f"Location: {location or 'none'}\n"
                    f"Fee: {eventfee or 'none'}\n"
                    f"Offsets: days={offsetdays}, hours={offsethours}, minutes={offsetminutes}\n"
                    f"Event_Day={event_day}, Event_Time={event_time}"
                )
        except Exception:
            await bot.send_message(cid, "Failed to save template. Please try again.")

    except Exception as e:
        from bot_state import _USER_FACING_EXCEPTIONS
        if not isinstance(e, _USER_FACING_EXCEPTIONS):
            logging.exception("[set_template] Unexpected error")
        await reply_error(message, e)


@bot.message_handler(func=lambda message: message.text.split(" ")[0].split("@")[0].lower() == "/delete_template")
async def delete_template_command(message):
    cid = message.chat.id

    if await admin_rights(message, manager) is False:
        await bot.send_message(cid, "You don't have permissions to use this command :(")
        return

    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await bot.send_message(cid, "Usage:\n/delete_template name\nExample: /delete_template sunday")
        return

    name = parts[1].strip()
    try:
        templates_svc.delete_one_template(cid, name, message.from_user.id, message.from_user.first_name)
        if not manager.get_shh_mode(cid):
            await bot.send_message(cid, f"Template '{name}' deleted.")
    except (incorrectParameter, parameterMissing) as e:
        await reply_error(message, e)


@bot.callback_query_handler(func=lambda call: call.data and (
    call.data.startswith("sched_sel_")
    or call.data.startswith("sched_apply_")
    or call.data in ("sched_selall", "sched_selclear")
))
async def schedules_toggle_callback(call):
    try:
        cid = call.message.chat.id
        data = call.data

        if data.startswith("sched_sel_"):
            name = data[len("sched_sel_"):]
            sel = _sched_selection.setdefault(cid, set())
            if name in sel:
                sel.discard(name)
                await bot.answer_callback_query(call.id, "⬜ Deselected")
            else:
                sel.add(name)
                await bot.answer_callback_query(call.id, "☑️ Selected")
            await _send_schedules(cid, edit_msg_id=call.message.message_id)
            return

        if data == "sched_selall":
            templates = templates_svc.list_templates(cid)
            scheduled = [t for t in templates if t.get("schedule_day") and t.get("schedule_time")]
            _sched_selection[cid] = {t.get("name", "") for t in scheduled}
            await bot.answer_callback_query(call.id, "✅ All selected")
            await _send_schedules(cid, edit_msg_id=call.message.message_id)
            return

        if data == "sched_selclear":
            _sched_selection.pop(cid, None)
            await bot.answer_callback_query(call.id, "☐ Selection cleared")
            await _send_schedules(cid, edit_msg_id=call.message.message_id)
            return

        if data in ("sched_apply_on", "sched_apply_off"):
            sel = _sched_selection.pop(cid, set())
            if not sel:
                await bot.answer_callback_query(call.id, "Nothing selected")
                return
            uid = call.from_user.id
            fname = call.from_user.first_name

            def _toggle(name):
                try:
                    if data == "sched_apply_on":
                        templates_svc.enable_schedule(cid, name, uid, fname)
                    else:
                        templates_svc.disable_schedule(cid, name, uid, fname)
                    return True
                except Exception:
                    return False

            done = sum(1 for name in sel if _toggle(name))
            verb = "▶️ Resumed" if data == "sched_apply_on" else "⏸ Paused"
            await bot.answer_callback_query(call.id, f"{verb} {done} template(s)")
            await _send_schedules(cid, edit_msg_id=call.message.message_id)
            return

    except Exception as e:
        err_str = str(e)
        if "query is too old" in err_str or "query ID is invalid" in err_str:
            logging.warning(f"[{_ts()}] Stale schedule toggle callback ignored")
            return
        logging.exception("Error in schedules_toggle_callback")
        try:
            await bot.answer_callback_query(call.id, "Error updating schedule")
        except Exception:
            pass
