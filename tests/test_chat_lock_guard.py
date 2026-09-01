"""
Guard: mutations that need the per-chat write lock must actually hold it.

CLAUDE.md: "Anything that mutates a chat's rollcall state (votes, proxy adds,
set_limit, end_rollcall) should run inside `async with
manager.get_chat_write_lock(cid)`."

Some services take that lock themselves (voting, proxy) — those are safe from
any caller. The rest document "caller should hold the lock", and that
contract is invisible at the call site: nothing fails, nothing logs, and the
race only shows up as a vote that lands during an /erc and disappears. The
web routes were given the lock in the 2026-07 audit; the token-authenticated
REST routes and three bot handlers were missed and stayed unlocked for weeks.

So: assert it, rather than relying on the next person reading the docstring.
"""
import ast
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "rollCall")

# Services whose docstring says the CALLER must hold the lock. (Anything that
# locks itself — voting.vote_*, proxy.set_*_for — is deliberately absent.)
CALLER_MUST_LOCK = (
    "start_rollcall(", "end_rollcall(", "set_title(",
    "delete_user_from_rollcall(", "set_user_status(",
    "set_rollcall_limit(", "set_wait_limit(", "review_session(",
)

# Files that DEFINE these (the definition isn't a call site) plus the pure
# data layer, where locking would be the wrong altitude.
SKIP_FILES = {
    "services/rollcalls.py", "services/admin.py", "services/settings.py",
    "services/ghost.py", "db.py", "models.py", "rollcall_manager.py",
}

# Call sites that are covered by a lock in a DIFFERENT file — the checker is
# lexical, so it can't see the caller's `async with`. Each must stay true.
EXEMPT = {
    # close_game ends the rollcall as part of settling. Every one of its
    # callers (api/routes/dues.py, handlers/dues.py ×3) takes the lock first,
    # because the money write and the end have to be one atomic step.
    ("services/dues.py", "close_game"),
}


def _rel(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def _enclosing_function(src, n):
    """Name of the innermost def containing line n (1-based).

    Uses the AST rather than walking indentation: a multi-line `def` signature
    puts `) -> dict:` at column 0, which a line walk reads as "left the
    function" and then attributes the call to nothing.
    """
    best = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            if node.lineno <= n <= end:
                if best is None or node.lineno > best[0]:
                    best = (node.lineno, node.name)
    return best[1] if best else None


def _inside_lock(lines, n):
    """Is line n lexically inside an `async with ...get_chat_write_lock(...)`?"""
    ind = len(lines[n - 1]) - len(lines[n - 1].lstrip())
    for i in range(n - 2, -1, -1):
        l = lines[i]
        if not l.strip() or l.lstrip().startswith("#"):
            continue
        li = len(l) - len(l.lstrip())
        if li < ind:
            if "get_erc_lock" in l or "get_chat_write_lock" in l:
                return True
            ind = li
            if re.match(r"\s*(?:async\s+)?def\s", l):
                return False
    return False


class TestChatLockDiscipline(unittest.TestCase):

    def _offenders(self):
        out = []
        for root, _, files in os.walk(ROOT):
            if "__pycache__" in root:
                continue
            for fn in sorted(files):
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(root, fn)
                rel = _rel(path)
                if rel in SKIP_FILES:
                    continue
                src = open(path, encoding="utf-8").read()
                lines = src.split("\n")
                for i, line in enumerate(lines, 1):
                    s = line.strip()
                    if s.startswith(("def ", "async def ", "#")):
                        continue
                    for call in CALLER_MUST_LOCK:
                        if call not in line:
                            continue
                        if (rel, _enclosing_function(src, i)) in EXEMPT:
                            continue
                        if not _inside_lock(lines, i):
                            out.append(f"{rel}:{i}  {s[:70]}")
        return sorted(set(out))

    def test_mutations_hold_the_chat_write_lock(self):
        offenders = self._offenders()
        self.assertEqual(
            offenders, [],
            "These mutate chat state without holding the per-chat write lock, so "
            "they can interleave with a vote landing from Telegram:\n  "
            + "\n  ".join(offenders)
            + "\n\nWrap in `async with manager.get_chat_write_lock(chat_id):` "
              "(see CLAUDE.md → Chat mutations).",
        )

    def test_exemptions_are_not_stale(self):
        """An exemption that no longer points at real code is a hole nobody
        can see — the same failure mode this whole file exists to stop."""
        for rel, func in EXEMPT:
            path = os.path.join(ROOT, rel)
            self.assertTrue(os.path.exists(path), f"exempt file gone: {rel}")
            src = open(path, encoding="utf-8").read()
            self.assertRegex(
                src, rf"def\s+{func}\(",
                f"exempt function {rel}::{func} no longer exists — drop the exemption",
            )


if __name__ == "__main__":
    unittest.main()
