"""Ratchet: every REST path that names a rollcall by its display number has a
stable by-id twin, and the number path is marked deprecated (CLAUDE.md,
"REST: name rollcalls by id"). A new route registered on {rc_number} alone
would bring back the renumbering bug for every API client."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


def _api_routes(app):
    """(full_path, route) for every endpoint. Starlette 1.x leaves an
    include_router() container in app.routes, whose children store their path
    WITHOUT the prefix — the same walk as tests/test_web_routes._all_route_paths
    (a naive `app.routes` scan finds nothing there and would pass vacuously)."""
    out = []

    def walk(routes, prefix=""):
        for r in routes:
            original = getattr(r, "original_router", None)
            if original is not None:
                ctx = getattr(r, "include_context", None)
                walk(original.routes, prefix + (getattr(ctx, "prefix", "") or ""))
                continue
            if getattr(r, "methods", None) and getattr(r, "path", None) is not None:
                out.append((prefix + r.path, r))
            child = getattr(r, "routes", None)
            if child:
                walk(child, prefix)

    walk(app.routes)
    return out


class TestRestRollcallIds(unittest.TestCase):

    def test_number_paths_have_id_twins_and_are_deprecated(self):
        from api.main import create_app
        routes = _api_routes(create_app())
        paths = {(p, m) for p, r in routes for m in r.methods}
        by_number = [(p, m, r) for p, r in routes for m in r.methods if "{rc_number}" in p]
        self.assertTrue(by_number, "expected the deprecated number-based paths to still exist")
        for path, method, route in by_number:
            twin = path.replace("/rollcalls/{rc_number}", "/rollcalls/by-id/{rollcall_id}")
            self.assertIn((twin, method), paths, f"{method} {path} has no by-id twin")
            self.assertTrue(route.deprecated, f"{method} {path} isn't marked deprecated")


if __name__ == "__main__":
    unittest.main()
