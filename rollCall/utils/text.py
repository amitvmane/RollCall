"""Small text-processing helpers with no dependency on bot_state/db/telebot.

Kept dependency-free (only exceptions.py, itself a leaf module) so the
services layer — which is documented as platform-agnostic — can use these
without pulling in the Telegram bot machinery.
"""

import zlib

from exceptions import incorrectParameter


def esc_md(text: str) -> str:
    """Escape Markdown v1 special characters in user-supplied strings.
    Includes `]` so display names cannot break `[name](tg://user?id=X)` links."""
    if not text:
        return text or ""
    for c in ('_', '*', '`', '[', ']'):
        text = text.replace(c, f'\\{c}')
    return text


def parse_rc_suffix(args: list[str]) -> tuple[int, list[str]]:
    """Pop optional ::N suffix and return (0-based rc_index, remaining args)."""
    if args and "::" in args[-1]:
        try:
            idx = int(args[-1].replace("::", "")) - 1
            if idx < 0:
                raise ValueError
            return idx, args[:-1]
        except (ValueError, TypeError):
            raise incorrectParameter("The rollcall number must be a positive integer (e.g. ::2).")
    return 0, args


# Telegram refuses a message longer than this with "message is too long" —
# the whole message, nothing is delivered.
TG_TEXT_LIMIT = 4096


def _u16(s: str) -> int:
    # Telegram counts UTF-16 units: an emoji is 2, Python's len() says 1.
    return len(s.encode("utf-16-le")) // 2


def split_for_telegram(text: str, limit: int = TG_TEXT_LIMIT) -> list[str]:
    """Split text into pieces Telegram will accept, preferring blank lines,
    then line breaks, so a Markdown entity (all single-line here) is never
    cut in half. Measures the raw text, which is never shorter than what
    Telegram counts after parsing the markup, so every piece is safe.
    Text that already fits comes back as a one-item list, unchanged."""
    if _u16(text) <= limit:
        return [text]
    pieces, cur = [], ""

    def _flush():
        nonlocal cur
        if cur:
            pieces.append(cur)
        cur = ""

    for block in text.split("\n\n"):
        joined = f"{cur}\n\n{block}" if cur else block
        if _u16(joined) <= limit:
            cur = joined
            continue
        _flush()
        if _u16(block) <= limit:
            cur = block
            continue
        for line in block.split("\n"):
            joined = f"{cur}\n{line}" if cur else line
            if _u16(joined) <= limit:
                cur = joined
                continue
            _flush()
            # A single line over the limit: hard-cut it (only plain data like
            # a pasted list can do this; nothing we render is that wide).
            while _u16(line) > limit:
                cut = limit
                while _u16(line[:cut]) > limit:
                    cut -= 1
                pieces.append(line[:cut])
                line = line[cut:]
            cur = line
    _flush()
    return pieces


# Telegram caps a button's callback_data at 64 BYTES and refuses the whole
# message over it. A free-text name can't go in raw: a web guest's full name
# may be 64 characters, and a Devanagari letter is 3 bytes.
TG_CALLBACK_LIMIT = 64


def callback_key(name: str) -> str:
    """Short, stable stand-in for a name in callback data ("n" + crc32 hex).
    The handler maps it back by checking the names it expects — a match is
    re-verified against real data, so a collision can't select a stranger."""
    return "n%08x" % (zlib.crc32((name or "").encode("utf-8")) & 0xFFFFFFFF)


def match_callback_name(token: str, names) -> "str | None":
    """The name a button's token stands for, among the names it could be.
    Accepts the raw name too, so buttons already posted before the switch
    to keys keep working."""
    names = list(names)
    if token in names:
        return token
    return next((n for n in names if callback_key(n) == token), None)
