"""Ratchet: every REST path that names a rollcall by its display number has a
stable by-id twin, and the number path is marked deprecated (CLAUDE.md,
"REST: name rollcalls by id"). A new route registered on {rc_number} alone
would bring back the renumbering bug for every API client."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


class TestRestRollcallIds(unittest.TestCase):

    def test_number_paths_have_id_twins_and_are_deprecated(self):
        from api.main import create_app
        routes = [r for r in create_app().routes if hasattr(r, "methods")]
        paths = {(r.path, m) for r in routes for m in r.methods}
        by_number = [(r.path, m, r) for r in routes for m in r.methods if "{rc_number}" in r.path]
        self.assertTrue(by_number, "expected the deprecated number-based paths to still exist")
        for path, method, route in by_number:
            twin = path.replace("/rollcalls/{rc_number}", "/rollcalls/by-id/{rollcall_id}")
            self.assertIn((twin, method), paths, f"{method} {path} has no by-id twin")
            self.assertTrue(route.deprecated, f"{method} {path} isn't marked deprecated")


if __name__ == "__main__":
    unittest.main()
