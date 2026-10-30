"""Ratchet: no Telegram send while holding a chat write lock.

Every vote in a chat queues on that chat's write lock. Telegram sends are
paced (chat_outbox) and can wait out a rate limit for up to a minute, so a
send made while holding the lock would make every vote in the chat wait with
it. Close/cancel flows therefore change state under the lock
(_close_panel_bookkeeping) and announce after releasing it.
"""
import ast
import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "rollCall")

BOT_SENDS = {"send_message", "edit_message_text", "edit_message_reply_markup",
             "edit_message_caption", "send_photo", "send_document", "send_media_group",
             "pin_chat_message", "unpin_chat_message", "delete_message",
             "copy_message", "forward_message"}
# answer_callback_query is deliberately absent: it has no chat_id, so it is
# never paced and can't hold the lock any longer than it always did.
# Project helpers that send. (Service-layer functions never send — see
# services/__init__.py — so same-named service calls are not listed.)
SENDING_HELPERS = {"_update_panel", "_render_panel", "show_panel_for_rollcall",
                   "_post_end_cleanup", "_post_cancel_cleanup", "_strip_panel",
                   "announce_one", "announce_promotions", "reply_error", "best_effort",
                   "safe_edit_text", "safe_edit_markup", "mirror_panel_to_telegram",
                   "send_vote_notification", "send_event_notification",
                   "send_promotion_notification", "notify_proxy_owner_wait_to_in",
                   "send_settle_nudge", "deliver"}


def _offenders():
    out = []
    for root, _, files in os.walk(ROOT):
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            tree = ast.parse(open(path).read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.AsyncWith) and any(
                        "get_chat_write_lock" in ast.unparse(i.context_expr) for i in node.items)):
                    continue
                for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                    if not isinstance(sub, ast.Call):
                        continue
                    fn = sub.func
                    if isinstance(fn, ast.Attribute) and fn.attr in BOT_SENDS \
                            and isinstance(fn.value, ast.Name) and fn.value.id == "bot":
                        out.append(f"{os.path.relpath(path, ROOT)}:{sub.lineno} bot.{fn.attr}")
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                    if name in SENDING_HELPERS:
                        out.append(f"{os.path.relpath(path, ROOT)}:{sub.lineno} {name}")
    return out


class TestNoSendUnderChatLock(unittest.TestCase):

    def test_no_telegram_send_inside_a_chat_write_lock(self):
        self.assertEqual(_offenders(), [],
                         "Telegram send inside `async with manager.get_chat_write_lock(...)`: "
                         "every vote in the chat would wait while it rides out a rate limit. "
                         "Change state under the lock, announce after it.")

    def test_the_scan_sees_a_violation(self):
        """Guard the guard: the AST pattern must actually match a real send."""
        src = ("async def f():\n"
               "    async with manager.get_chat_write_lock(cid):\n"
               "        await bot.send_message(cid, 'x')\n")
        tree = ast.parse(src)
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncWith))
        calls = [c for c in ast.walk(ast.Module(body=node.body, type_ignores=[]))
                 if isinstance(c, ast.Call)]
        self.assertTrue(any(getattr(c.func, "attr", "") == "send_message" for c in calls))


if __name__ == "__main__":
    unittest.main()
