"""
Guard: aggregates must not be keyed on a member's display name.

The monthly wrap-up card once showed a player with more games than the month
contained. The cause was a single `GROUP BY u.first_name` in
get_attendance_between: two different members who share a first name were
added together, and `users.first_name` genuinely cannot tell them apart —
models.User disambiguates the DISPLAY name for a namesake, but
_save_user_to_db persists the raw first_name.

Every other aggregate was already keyed on user_id (real members) or on
proxy name (which IS a guest's identity). This test exists so the next one
added isn't the exception, because the symptom only surfaces in a group that
happens to contain two people with the same name — which is to say, months
later, on a card everybody sees.

It reads SQL text, which is a blunt instrument. That is deliberate: the
alternative is noticing in production again.
"""
import os
import re
import unittest

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "rollCall", "db.py")

# Grouping by a proxy's name is correct — a guest has no user_id, so the name
# is the only identity they have.
_PROXY_TABLE_ALIASES = ("p", "pu", "proxy_users")

_GROUP_BY = re.compile(r"GROUP BY\s+([^\"']+?)(?:\"|'|\n)", re.IGNORECASE)


def _name_keys(clause: str):
    """Name-ish columns in a GROUP BY clause that don't belong to a proxy."""
    hits = []
    for term in clause.split(","):
        term = term.strip()
        m = re.match(r"^(?:(\w+)\.)?(first_name|name|display_name|user_name)$", term, re.I)
        if not m:
            continue
        table_alias = (m.group(1) or "").lower()
        if table_alias in _PROXY_TABLE_ALIASES:
            continue
        hits.append(term)
    return hits


class TestNoNameKeyedAggregates(unittest.TestCase):

    def test_no_aggregate_groups_real_members_by_display_name(self):
        with open(DB_PATH, encoding="utf-8") as fh:
            source = fh.read()

        offenders = []
        for m in _GROUP_BY.finditer(source):
            clause = m.group(1)
            names = _name_keys(clause)
            if not names:
                continue
            # Grouping by user_id AND the name is fine: the id is what
            # separates rows, the name just rides along for display.
            if re.search(r"\buser_id\b", clause, re.I):
                continue
            line = source[:m.start()].count("\n") + 1
            offenders.append(f"db.py:{line} — GROUP BY {clause.strip()}")

        self.assertEqual(
            offenders, [],
            "Aggregate(s) grouped on a display name. Two members sharing a "
            "first name will be summed into one row, which is how the monthly "
            "card once reported 11 games in an 8-game month. Group by user_id "
            "for real members (fold merged aliases with "
            "services.identity.get_canonical_map, as get_leaderboard_by_attendance "
            "does) and by name only for proxy_users:\n  " + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
