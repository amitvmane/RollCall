"""
Id namespaces: which ids are Telegram's and which are this app's own.

Every table keys people on `tg_user_id`/`user_id` and groups on `chat_id`.
Re-keying them onto principals is a large migration against a live database.
Instead this app allocates its own people and groups ids in a range Telegram
can never issue, so every existing table, query and token works unchanged for
someone who has no Telegram at all:

    Telegram ids          |id| < 2**52   ("at most 52 significant bits" —
                                          Bot API docs, for users and chats)
    local people          2**52 + principal_id         (positive)
    local groups          -(2**52) - n                 (negative)

Both local ranges stay below 2**53, so they survive a round trip through
JavaScript numbers (the web app reads chat_id and user_id as JSON numbers).

Consequences the rest of the code relies on:
  * a local person is a "real" user everywhere (`isinstance(uid, int) and
    uid > 0`), so their votes, stats, ghosts and dues are keyed exactly like a
    Telegram member's — not a name-only guest;
  * nothing is ever sent to Telegram for a local id: chat_outbox refuses the
    request before it leaves (TelegramUnreachable), and every send site is
    already best-effort because Telegram refuses sends routinely;
  * existing rows are never touched — no Telegram id can fall in these ranges.
"""

TELEGRAM_ID_LIMIT = 2 ** 52
_JS_SAFE_LIMIT = 2 ** 53

LOCAL_USER_BASE = TELEGRAM_ID_LIMIT
LOCAL_CHAT_BASE = -TELEGRAM_ID_LIMIT


def _as_int(value):
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def is_local_user(user_id) -> bool:
    uid = _as_int(user_id)
    return uid is not None and LOCAL_USER_BASE <= uid < _JS_SAFE_LIMIT


def is_local_chat(chat_id) -> bool:
    cid = _as_int(chat_id)
    return cid is not None and -_JS_SAFE_LIMIT < cid <= LOCAL_CHAT_BASE


def is_local_id(value) -> bool:
    """Either kind — what the Telegram request guard checks."""
    return is_local_user(value) or is_local_chat(value)


def local_user_id(principal_id: int) -> int:
    """The user id a principal with no Telegram account acts under."""
    pid = int(principal_id)
    if pid <= 0:
        raise ValueError("principal id must be positive")
    uid = LOCAL_USER_BASE + pid
    if uid >= _JS_SAFE_LIMIT:
        raise ValueError("principal id out of range")
    return uid


def principal_of_local_user(user_id) -> int:
    if not is_local_user(user_id):
        raise ValueError("not a local user id")
    return int(user_id) - LOCAL_USER_BASE


def local_chat_id(n: int) -> int:
    """The n-th web-only group's chat id (n >= 1)."""
    n = int(n)
    if n <= 0:
        raise ValueError("group number must be positive")
    cid = LOCAL_CHAT_BASE - n
    if cid <= -_JS_SAFE_LIMIT:
        raise ValueError("group number out of range")
    return cid
