"""version_info.py — what actually shipped, logged on every boot.

Motivated by a recurring shape this project keeps hitting: a deploy silently
not being what you think it is. The database-in-git incident, the SW-pinned
app.js, three PRs merging without version.json catching up — each looked fine
until someone asked "wait, is this actually running?" and there was nothing to
check against. This gives that question a one-line answer in `docker compose
logs`, and this file confirms the answer is trustworthy.

Kept a leaf module on purpose (no telebot, no db, no bot_state) so it can be
logged before anything else has had a chance to fail — these tests import it
directly rather than through conftest's mocks, and it must stay import-safe
without them.
"""
import json
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import version_info  # noqa: E402


class TestPackageVersion(unittest.TestCase):
    def test_an_installed_package_reports_its_real_version(self):
        # pytest itself is guaranteed present in this test environment.
        self.assertRegex(version_info._package_version("pytest"), r"^\d+\.\d+")

    def test_a_missing_optional_dependency_is_named_as_absent_not_an_error(self):
        """sentry-sdk is optional; a fresh environment without it must read
        as an expected state, not degrade the whole banner."""
        self.assertEqual(
            version_info._package_version("definitely-not-a-real-package-xyz"),
            "not installed",
        )


class TestBuildInfo(unittest.TestCase):
    def setUp(self):
        self._real_path = version_info._BUILD_INFO_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        os.unlink(self._tmp.name)  # start absent, as a fresh checkout would be
        version_info._BUILD_INFO_PATH = self._tmp.name

    def tearDown(self):
        version_info._BUILD_INFO_PATH = self._real_path
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_missing_file_is_unknown_not_fatal(self):
        """A local `python runner.py` outside any container, or an image
        built before this existed, must still boot cleanly."""
        info = version_info._build_info()
        self.assertEqual(info, {"commit": "unknown", "built_at": "unknown"})

    def test_corrupt_file_is_unknown_not_fatal(self):
        with open(self._tmp.name, "w") as fh:
            fh.write("{not valid json")
        info = version_info._build_info()
        self.assertEqual(info, {"commit": "unknown", "built_at": "unknown"})

    def test_a_real_build_is_read_back_correctly(self):
        with open(self._tmp.name, "w") as fh:
            json.dump({"commit": "e842e0a", "built_at": "2026-09-25T09:00:00Z"}, fh)
        info = version_info._build_info()
        self.assertEqual(info, {"commit": "e842e0a", "built_at": "2026-09-25T09:00:00Z"})

    def test_the_dockerfiles_own_unknown_default_round_trips_cleanly(self):
        """The Dockerfile writes literally the string 'unknown' when no
        --build-arg is passed — that must render the same as a missing file,
        not as a confusing 'commit unknown' vs some other placeholder."""
        with open(self._tmp.name, "w") as fh:
            json.dump({"commit": "unknown", "built_at": "unknown"}, fh)
        self.assertEqual(version_info._build_info(),
                         {"commit": "unknown", "built_at": "unknown"})

    def test_blank_fields_fall_back_to_unknown(self):
        with open(self._tmp.name, "w") as fh:
            json.dump({"commit": "", "built_at": "   "}, fh)
        self.assertEqual(version_info._build_info(),
                         {"commit": "unknown", "built_at": "unknown"})


class TestDeployedAppVersion(unittest.TestCase):
    def setUp(self):
        self._real_base = version_info._BASE_DIR
        self._dir = tempfile.mkdtemp()
        version_info._BASE_DIR = self._dir

    def tearDown(self):
        version_info._BASE_DIR = self._real_base

    def _write(self, entries):
        with open(os.path.join(self._dir, "version.json"), "w") as fh:
            json.dump(entries, fh)

    def test_reads_the_entry_flagged_deployed(self):
        self._write([
            {"Version": 10.4, "DeployedOnProd": "N"},
            {"Version": 10.5, "DeployedOnProd": "Y"},
        ])
        self.assertEqual(version_info.deployed_app_version(), "10.5")

    def test_no_entry_flagged_is_reported_not_silently_wrong(self):
        self._write([{"Version": 10.4, "DeployedOnProd": "N"}])
        self.assertIn("none marked deployed", version_info.deployed_app_version())

    def test_missing_file_is_reported_not_fatal(self):
        self.assertIn("unreadable", version_info.deployed_app_version())

    def test_corrupt_file_is_reported_not_fatal(self):
        with open(os.path.join(self._dir, "version.json"), "w") as fh:
            fh.write("[not json")
        self.assertIn("unreadable", version_info.deployed_app_version())


class TestRedactDsn(unittest.TestCase):
    def test_credentials_are_stripped(self):
        self.assertEqual(
            version_info._redact_dsn("postgresql://rollcall:s3cr3t@host:5432/rollcall"),
            "postgresql://***@host:5432/rollcall",
        )

    def test_sqlite_paths_are_untouched(self):
        """No userinfo to redact, and nothing should be removed from the path."""
        dsn = "sqlite:////app/data/rollcall.db"
        self.assertEqual(version_info._redact_dsn(dsn), dsn)

    def test_a_dsn_with_no_credentials_is_untouched(self):
        dsn = "postgresql://host:5432/rollcall"
        self.assertEqual(version_info._redact_dsn(dsn), dsn)


class TestCollectAndLog(unittest.TestCase):
    """collect() and log_startup_banner() end to end, against whatever is
    genuinely installed in this environment — no mocking of importlib.metadata,
    so a real dependency swap would actually be caught here."""

    def test_collect_returns_every_expected_field(self):
        info = version_info.collect()
        self.assertIn("app_version", info)
        self.assertIn("commit", info)
        self.assertIn("built_at", info)
        self.assertIn("python", info)
        self.assertTrue(info["python"].startswith("CPython"))
        for _, label in version_info._TRACKED_PACKAGES:
            self.assertIn(label, info["packages"])

    def test_pytelegrambotapi_is_a_real_version_not_a_placeholder(self):
        """The one dependency this whole project is built on. If this ever
        reads 'not installed' something has gone very wrong."""
        info = version_info.collect()
        self.assertRegex(info["packages"]["pyTelegramBotAPI"], r"^\d+\.\d+\.\d+$")

    def test_log_startup_banner_never_raises(self):
        logger = logging.getLogger("test_version_info")
        version_info.log_startup_banner(logger, db_type="sqlite",
                                        database_url="sqlite:///x.db",
                                        rest_api_enabled=False)
        version_info.log_startup_banner(logger, db_type="postgresql",
                                        database_url="postgresql://u:p@h/d",
                                        rest_api_enabled=True)
        version_info.log_startup_banner(logger)  # every kwarg has a default

    def test_a_totally_broken_collect_degrades_to_a_warning_not_a_crash(self):
        """This runs on every single boot before validate_environment(). It
        must never be the reason the bot fails to start."""
        logger = logging.getLogger("test_version_info")
        real_collect = version_info.collect
        version_info.collect = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            version_info.log_startup_banner(logger)  # must not raise
        finally:
            version_info.collect = real_collect

    def test_postgres_line_only_appears_for_postgres(self):
        buf = []
        logger = logging.getLogger("test_version_info_capture")
        handler = logging.Handler()
        handler.emit = lambda record: buf.append(record.getMessage())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        version_info.log_startup_banner(logger, db_type="sqlite",
                                        database_url="sqlite:///x.db")
        self.assertFalse(any("PG driver" in m for m in buf))

        buf.clear()
        version_info.log_startup_banner(logger, db_type="postgresql",
                                        database_url="postgresql://u:p@h/d")
        self.assertTrue(any("PG driver" in m for m in buf))

    def test_rest_api_line_only_appears_when_enabled(self):
        buf = []
        logger = logging.getLogger("test_version_info_capture2")
        handler = logging.Handler()
        handler.emit = lambda record: buf.append(record.getMessage())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        version_info.log_startup_banner(logger, rest_api_enabled=False)
        self.assertFalse(any("REST API" in m for m in buf))

        buf.clear()
        version_info.log_startup_banner(logger, rest_api_enabled=True)
        self.assertTrue(any("REST API" in m for m in buf))

    def test_no_credentials_ever_reach_the_logger(self):
        """The whole point of _redact_dsn — checked at the integration level,
        not just as a unit of the helper, so a future call site that bypasses
        it would be caught here too."""
        buf = []
        logger = logging.getLogger("test_version_info_capture3")
        handler = logging.Handler()
        handler.emit = lambda record: buf.append(record.getMessage())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        version_info.log_startup_banner(
            logger, db_type="postgresql",
            database_url="postgresql://rollcall:s3cr3t-value@host/db")
        self.assertFalse(any("s3cr3t-value" in m for m in buf),
                         "a database password reached the log output")


class TestWiredIntoRunner(unittest.TestCase):
    """The module existing and working is worthless if runner.py never calls
    it, or calls it before db_type is actually known."""

    def _runner_src(self):
        path = os.path.join(os.path.dirname(__file__), "..", "rollCall", "runner.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_runner_calls_the_banner(self):
        self.assertIn("version_info.log_startup_banner", self._runner_src())

    def test_a_failure_to_log_versions_does_not_block_startup(self):
        src = self._runner_src()
        block = src.split("version_info.log_startup_banner")[0][-400:]
        self.assertIn("try:", block)

    def test_db_type_is_imported_live_not_bound_stale_at_module_load(self):
        """db.py sets db_type via `global db_type` inside init_db(), called at
        db.py's own import time — so `from db import db_type` only reads the
        right value if it runs AFTER that import completes.

        Anchored on the specific TOP-LEVEL import line (the one main() closes
        over), not "db_type appears in an import somewhere in the file" — the
        file also has an unrelated local import three hundred lines down
        (`from db import get_connection, release_connection, db_type as
        _db_type`, for postgres pool stats) that a looser regex matched
        happily even after the top-level import was reverted to not importing
        db_type at all. That first version of this test passed against a
        broken runner.py, which is worse than not having it.
        """
        import re
        src = self._runner_src()
        # Column-0 (module-level, unindented) imports of `db` specifically —
        # excludes the indented local import inside a function, which is the
        # one that made the first version of this test pass falsely.
        top_level = [l for l in src.splitlines() if re.match(r"^from db import ", l)]
        self.assertEqual(len(top_level), 1,
                         f"expected exactly one top-level `from db import`, found {top_level}")
        self.assertIn("db_type", top_level[0])
        self.assertNotIn("as _db_type", top_level[0],
                         "must import the live name, not an aliased shadow")


class TestDockerBuildBakesProvenance(unittest.TestCase):
    """The other half of this: nothing in version_info.py can know the git
    commit unless the image build actually wrote it there."""

    def setUp(self):
        path = os.path.join(os.path.dirname(__file__), "..", "dockerfile")
        with open(path, encoding="utf-8") as fh:
            self.dockerfile = fh.read()

    def test_build_args_declared_with_safe_defaults(self):
        """Defaults matter: a bare `docker build` with no --build-arg (any
        developer running one by hand) must still produce a working image."""
        self.assertIn("ARG GIT_SHA=unknown", self.dockerfile)
        self.assertIn("ARG BUILD_DATE=unknown", self.dockerfile)

    def test_the_write_happens_after_the_pip_install_layer(self):
        """GIT_SHA/BUILD_DATE change on every commit. Declaring the ARG
        before the pip install would invalidate that (expensive) layer on
        every single build."""
        pip_idx = self.dockerfile.index("pip3 install")
        arg_idx = self.dockerfile.index("ARG GIT_SHA")
        self.assertLess(pip_idx, arg_idx)

    def test_output_path_matches_where_version_info_looks(self):
        expected_name = os.path.basename(version_info._BUILD_INFO_PATH)
        self.assertIn(expected_name, self.dockerfile)


class TestMakefilePassesBuildArgs(unittest.TestCase):
    def setUp(self):
        path = os.path.join(os.path.dirname(__file__), "..", "Makefile")
        with open(path, encoding="utf-8") as fh:
            self.makefile = fh.read()

    def test_git_sha_and_build_date_are_computed_and_exported(self):
        self.assertIn("GIT_SHA", self.makefile)
        self.assertIn("BUILD_DATE", self.makefile)
        export_lines = [l for l in self.makefile.splitlines() if l.startswith("export ")]
        self.assertTrue(any("GIT_SHA" in l and "BUILD_DATE" in l for l in export_lines),
                        "GIT_SHA/BUILD_DATE must be exported so docker compose "
                        "(a child process) can see them")

    def test_falls_back_when_not_in_a_git_checkout(self):
        self.assertIn("unknown", self.makefile.split("GIT_SHA")[1].split("\n")[0])


if __name__ == "__main__":
    unittest.main()
