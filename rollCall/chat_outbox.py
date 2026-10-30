"""Outbound pacing for group chats: burst-safe delivery and panel coalescing.

Why this exists. Telegram limits how fast a bot may post in one group (about
one message a second, ~20 a minute). Every vote makes the bot post twice —
"X is now IN!" and a panel edit — so ten members whose scheduled /in all fire
at 9:01 ask for ~20 messages in one second, and Telegram answers most of
them with 429 "Too Many Requests: retry after N". Those messages were lost.

It applies to EVERY message the bot posts, whatever command, background job
or web action caused it: install_request_pacing() wraps telebot's single
request function, so the limit is respected at the one place all of them
pass through. (Telegram counts every message to a chat, not per command.)

Two mechanisms, both invisible in normal use:

1. ``deliver(cid, factory)`` — sends now. Only after Telegram has refused a
   send to this chat does it start pacing: later sends to that chat queue in
   FIFO order, wait until Telegram's ``retry_after`` has passed, then go out
   about one a second. A send that cannot go out within ``MAX_WAIT`` raises
   the last 429 so the caller can log it. No 429 → no waiting, no lock: the
   exact call that was made before this module existed.

2. ``coalesce(key, leading, trailing)`` — the panel. The first refresh runs
   immediately, exactly as before. Refreshes requested WHILE one is in flight
   don't each redraw: they mark the panel dirty, and one trailing redraw runs
   after the burst settles, reading the state as it is then. If anything
   changes during that redraw it runs again, so the final panel always shows
   everyone. A lone refresh never waits.

Neither touches votes. A vote and its position are saved under the chat write
lock before any of this runs; this module only decides when Telegram hears
about it.

Not persisted: a trailing redraw pending at shutdown is lost, leaving the
panel stale until the next change. Votes are unaffected (they're in the DB).
"""
import asyncio
import logging
import time

# Injectable so tests can run a burst on a fake clock instead of real seconds.
_clock = time.monotonic
_sleep = asyncio.sleep

MAX_WAIT = 60.0          # longest a message may be held back before giving up
RECOVERY_SPACING = 1.0   # pace while recovering from a 429 (Telegram: ~1/s/chat)
PANEL_SETTLE = 1.0       # how long a burst gets to settle before the final redraw
_DEFAULT_RETRY_AFTER = 3.0

_blocked_until: dict = {}   # chat_id -> monotonic time Telegram said to wait until
_chat_locks: dict = {}      # chat_id -> asyncio.Lock queueing sends during recovery
_queued: dict = {}          # chat_id -> sends currently waiting in recovery
_inflight: dict = {}        # coalesce key -> {"dirty": bool}
_trailing_tasks: set = set()


def is_rate_limited(e) -> bool:
    # Duck-typed: unit tests replace telebot with mocks, so isinstance on
    # ApiTelegramException is not available everywhere this runs.
    return getattr(e, "error_code", None) == 429


def retry_after(e) -> float:
    """Seconds Telegram asked us to wait; at least 1, so a malformed or zero
    value can't turn the retry loop into a spin."""
    params = (getattr(e, "result_json", None) or {}).get("parameters") or {}
    try:
        return max(1.0, float(params.get("retry_after", _DEFAULT_RETRY_AFTER)))
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER


class GaveUp(Exception):
    """A send could not go out within MAX_WAIT. error_code 429 so callers
    handle it exactly like Telegram's own refusal."""
    error_code = 429


def _recovering(cid) -> bool:
    return _blocked_until.get(cid, 0.0) > _clock() or _queued.get(cid, 0) > 0


def _note_refused(cid, e) -> None:
    until = _clock() + retry_after(e)
    if until > _blocked_until.get(cid, 0.0):
        _blocked_until[cid] = until


async def deliver(cid, factory, max_wait: float = MAX_WAIT, retry: bool = True):
    """Await ``factory()`` — a zero-arg callable that makes ONE fresh Telegram
    call — for chat ``cid``, waiting out Telegram's rate limit if needed.

    Returns the call's result. Non-429 errors propagate unchanged on the
    first occurrence. If the message can't go out within ``max_wait``,
    raises GaveUp (error_code 429), and the caller logs it.

    The factory is only re-invoked after a 429, i.e. after Telegram said it
    did NOT post the message — so a retry can't duplicate one. retry=False
    (file uploads, whose stream the first attempt consumed) still waits its
    turn in the queue but makes a single attempt.
    """
    deadline = _clock() + max_wait
    if not _recovering(cid):
        # Normal path — the same single call as before this module existed.
        try:
            return await factory()
        except Exception as e:
            if not is_rate_limited(e):
                raise
            _note_refused(cid, e)
            if not retry:
                raise

    # Recovery: queue in arrival order behind earlier sends to this chat.
    _queued[cid] = _queued.get(cid, 0) + 1
    lock = _chat_locks.setdefault(cid, asyncio.Lock())
    try:
        async with lock:
            while True:
                wait = _blocked_until.get(cid, 0.0) - _clock()
                if wait > 0:
                    if _clock() + wait > deadline:
                        raise GaveUp(f"chat {cid}: Telegram rate limit outlasted {max_wait:.0f}s")
                    await _sleep(wait)
                try:
                    result = await factory()
                except Exception as e:
                    if not is_rate_limited(e):
                        raise
                    _note_refused(cid, e)
                    if not retry:
                        raise
                    continue
                # Posted. Space the next queued send ~1s behind this one.
                _blocked_until[cid] = max(_blocked_until.get(cid, 0.0),
                                          _clock() + RECOVERY_SPACING)
                return result
    finally:
        _queued[cid] -= 1
        if _queued[cid] <= 0:
            # Last one out: recovery over. Drop the chat from every table so
            # they only ever hold chats being throttled right now.
            _queued.pop(cid, None)
            _chat_locks.pop(cid, None)
            _blocked_until.pop(cid, None)


# Telegram methods that post into a chat, i.e. count toward its flood limit.
# Reads (getChatMember…) and answerCallbackQuery (no chat_id) are untouched.
PACED_METHODS = frozenset({
    "sendMessage", "editMessageText", "editMessageReplyMarkup", "editMessageCaption",
    "editMessageMedia", "sendPhoto", "sendDocument", "sendAnimation", "sendVideo",
    "sendAudio", "sendVoice", "sendSticker", "sendMediaGroup", "sendLocation",
    "sendPoll", "sendDice", "sendContact", "sendVenue", "copyMessage",
    "forwardMessage", "pinChatMessage", "unpinChatMessage", "unpinAllChatMessages",
    "deleteMessage",
})

_orig_process_request = None    # telebot's real request function (tests swap in a fake)


def install_request_pacing(helper) -> bool:
    """Route every chat-posting Telegram call through deliver().

    ``helper`` is ``telebot.asyncio_helper``; every API method calls its
    module-level ``_process_request``, so wrapping that one function covers
    every command, background job and web action — present and future.
    Idempotent. Returns False (and changes nothing) when telebot is mocked,
    as in the unit tests.
    """
    global _orig_process_request
    current = getattr(helper, "_process_request", None)
    if getattr(current, "_rc_paced", False) is True:   # `is`: a mock's attribute is truthy
        return True
    if not asyncio.iscoroutinefunction(current):
        return False
    _orig_process_request = current

    async def _paced(token, url, method='get', params=None, files=None, **kwargs):
        chat = (params or {}).get("chat_id") if url in PACED_METHODS else None
        if chat is None:
            return await _orig_process_request(token, url, method, params, files, **kwargs)
        return await deliver(
            str(chat),
            lambda: _orig_process_request(token, url, method, params, files, **kwargs),
            retry=not files,
        )

    _paced._rc_paced = True
    helper._process_request = _paced
    return True


async def coalesce(key, leading, trailing) -> None:
    """Run ``leading()`` now unless a refresh for ``key`` is already in
    flight; in that case mark it dirty and return at once. A dirty key gets
    one ``trailing()`` redraw after PANEL_SETTLE, repeated until nothing
    changed during the last redraw.

    ``leading`` and ``trailing`` are zero-arg async callables. ``trailing``
    must read the CURRENT state (it runs later than the request).
    Exceptions from ``leading`` propagate to the caller, as before.
    """
    state = _inflight.get(key)
    if state is not None:
        state["dirty"] = True
        return
    state = _inflight[key] = {"dirty": False}
    try:
        await leading()
    finally:
        if state["dirty"]:
            task = asyncio.create_task(_trail(key, state, trailing))
            task.add_done_callback(_log_task_exc)
            _trailing_tasks.add(task)
            task.add_done_callback(_trailing_tasks.discard)
        else:
            _inflight.pop(key, None)


async def _trail(key, state, trailing) -> None:
    try:
        while True:
            await _sleep(PANEL_SETTLE)
            state["dirty"] = False          # anything after this is drawn next round
            try:
                await trailing()
            except Exception:
                logging.warning("Coalesced panel refresh failed (%s)", key, exc_info=True)
            if not state["dirty"]:
                return
    finally:
        _inflight.pop(key, None)


def _log_task_exc(task) -> None:
    """Surface a trailing redraw's crash in the logs (no dependency on
    bot_state, which imports this module)."""
    if not task.cancelled() and task.exception() is not None:
        logging.error("Trailing panel redraw crashed", exc_info=task.exception())


async def drain() -> None:
    """Wait for every pending trailing redraw (tests and shutdown).

    Prunes finished tasks itself rather than relying on the done-callback:
    that callback is only *scheduled* when a task finishes, and awaiting an
    already-finished task doesn't yield, so a loop waiting for the callback
    to empty the set spun forever.
    """
    while True:
        _trailing_tasks.difference_update([t for t in _trailing_tasks if t.done()])
        if not _trailing_tasks:
            return
        await asyncio.wait(list(_trailing_tasks))
