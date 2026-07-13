"""Small text-processing helpers with no dependency on bot_state/db/telebot.

Kept dependency-free (only exceptions.py, itself a leaf module) so the
services layer — which is documented as platform-agnostic — can use these
without pulling in the Telegram bot machinery.
"""

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
