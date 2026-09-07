"""
Guard: every always-on compose service must be in the Makefile's SERVICES.

`make down` stops everything, but `make up` / `make build` only start what
SERVICES lists. A service present in docker-compose.yml and absent from that
line gets stopped by the next deploy and silently never comes back — and the
deploy reports success either way.

This is not hypothetical twice over. db-backup died on 2026-08-03 and was not
noticed until 2026-08-24. Then on 2026-09-06 the watchdog was added to
docker-compose.yml and left out of SERVICES, which would have shipped an
alerting sidecar that never ran — strictly worse than no alerting, because you
believe you are covered.

Profile-gated services are exempt: they are opt-in by design and `up` starts
them conditionally.

Added 2026-09-06.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_COMPOSE = os.path.join(_ROOT, "docker-compose.yml")
_MAKEFILE = os.path.join(_ROOT, "Makefile")


class TestDeployServicesWired(unittest.TestCase):
    def test_every_default_service_is_started_by_make(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml not installed")

        with open(_COMPOSE, encoding="utf-8") as fh:
            compose = yaml.safe_load(fh)

        # Services behind a profile are opt-in; `up` handles them separately.
        default_services = {
            name for name, spec in (compose.get("services") or {}).items()
            if not (spec or {}).get("profiles")
        }

        makefile = open(_MAKEFILE, encoding="utf-8").read()

        # Resolve SERVICES := $(BOT) $(BACKUP) $(WATCHDOG) through its vars.
        m = re.search(r"^SERVICES\s*:?=\s*(.+)$", makefile, re.M)
        self.assertIsNotNone(m, "SERVICES not found in Makefile")
        services_line = m.group(1)

        for var in re.findall(r"\$\((\w+)\)", services_line):
            vm = re.search(rf"^{var}\s*:?=\s*(\S+)", makefile, re.M)
            self.assertIsNotNone(vm, f"Makefile variable {var} referenced but not defined")
            services_line = services_line.replace(f"$({var})", vm.group(1))

        started = set(services_line.split())

        # postgres is started by its own target (up-postgres) and gated by the
        # DATABASE_URL the operator chose, so it is not part of the default set.
        missing = default_services - started - {"postgres"}
        self.assertEqual(
            set(), missing,
            f"These compose services are never started by `make up`/`make build`: "
            f"{sorted(missing)}. `make down` will stop them and nothing will bring "
            f"them back. Add each to SERVICES in the Makefile, or give it a profile "
            f"if it is genuinely opt-in.",
        )


if __name__ == "__main__":
    unittest.main()
