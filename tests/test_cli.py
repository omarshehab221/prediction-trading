"""What each command does, and what the environment may override."""

from __future__ import annotations

import os
import tempfile
import unittest

import btc_5m_predictor as m
from tests.support import ROOT, _close_journals

class TestMainEntryPoint(unittest.TestCase):
    """
    Actually invoke main().

    Every other test constructs Config directly, so main() was never executed
    by the suite at all. That is how a duplicate keyword argument -- profiles
    setting kelly_fraction while main() also passed it explicitly -- shipped
    and crashed on the first real run with 384 tests green.
    """

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self._env = dict(os.environ)
        os.environ["BINANCE_API_KEY"] = "k"
        os.environ["BINANCE_API_SECRET"] = "s"

    def tearDown(self):
        _close_journals(self)
        os.environ.clear(); os.environ.update(self._env)
        if os.path.exists(self.db):
            os.unlink(self.db)

    def _run(self, *argv):
        """Run main() with stdout captured; returns (code, output)."""
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = m.main([*argv, "--db", self.db])
        return code, buf.getvalue()

    def test_every_profile_constructs_through_main(self):
        """The exact failure: Config() got two values for kelly_fraction."""
        for profile in sorted(m.PROFILES):
            code, out = self._run("--profile", profile,
                                  "--calibration-report")
            self.assertEqual(code, 0, f"{profile}: {out}")

    def test_every_profile_accepts_a_kelly_override(self):
        for profile in sorted(m.PROFILES):
            code, out = self._run("--profile", profile, "--kelly", "0.5",
                                  "--calibration-report")
            self.assertEqual(code, 0, f"{profile}: {out}")

    def test_kelly_defaults_to_the_profile_not_the_flag(self):
        """--kelly defaulted to 0.25, silently overriding every profile."""
        import inspect, re as _re
        src = inspect.getsource(m.main)
        hit = _re.search(r'"--kelly".*?default=(\w+)', src, _re.S)
        self.assertEqual(hit.group(1), "None")

    def test_all_override_flags_work_together(self):
        code, out = self._run("--profile", "buffer", "--kelly", "0.3",
                              "--min-edge", "0.02", "--fee-bps", "0",
                              "--min-buffer", "1.0", "--paper-bankroll", "50",
                              "--scale-in", "--calibration-report")
        self.assertEqual(code, 0, out)

    def test_diagnose_runs_for_every_profile(self):
        for profile in sorted(m.PROFILES):
            code, out = self._run("--profile", profile, "--diagnose")
            self.assertEqual(code, 0, f"{profile}: {out}")

    def test_reading_a_report_needs_no_credentials(self):
        """Reading the journal is not an API call, so keys are not required."""
        del os.environ["BINANCE_API_KEY"]
        code, _ = self._run("--profile", "micro", "--calibration-report")
        self.assertEqual(code, 0)

    def test_missing_credentials_exits_cleanly_for_api_commands(self):
        del os.environ["BINANCE_API_KEY"]
        code, out = self._run("--profile", "micro")
        self.assertEqual(code, 1)
        self.assertIn("BINANCE_API_KEY", out)

    def test_invalid_config_exits_cleanly_not_with_a_traceback(self):
        code, out = self._run("--profile", "micro", "--kelly", "9")
        self.assertEqual(code, 1)
        self.assertIn("Invalid configuration", out)

    def test_no_config_key_is_passed_twice(self):
        """Structural guard: one mapping, so a collision cannot recur."""
        import ast, inspect
        tree = ast.parse(inspect.getsource(m.main))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "Config"):
                continue
            explicit = [k.arg for k in node.keywords if k.arg is not None]
            starred = [k for k in node.keywords if k.arg is None]
            self.assertFalse(explicit and starred,
                             "Config() mixes explicit kwargs with **settings; "
                             "a profile field can collide with one of them")


class TestTradingModeEnv(unittest.TestCase):
    """TRADING_MODE is the environment equivalent of --live/--paper."""

    def setUp(self):
        self._env = dict(os.environ)
        os.environ["BINANCE_API_KEY"] = "k"
        os.environ["BINANCE_API_SECRET"] = "s"
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        _close_journals(self)
        os.environ.clear(); os.environ.update(self._env)
        os.unlink(self.db)

    def _run(self, *argv):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = m.main([*argv, "--db", self.db])
        return code, buf.getvalue()

    def test_invalid_mode_is_rejected(self):
        os.environ["TRADING_MODE"] = "yolo"
        code, out = self._run("--calibration-report")
        self.assertEqual(code, 1)
        self.assertIn("TRADING_MODE", out)

    def test_paper_and_live_are_accepted(self):
        for value in ("paper", "live", "PAPER", "Live"):
            os.environ["TRADING_MODE"] = value
            code, out = self._run("--calibration-report")
            self.assertEqual(code, 0, f"{value}: {out}")

    def test_flags_are_mutually_expressive(self):
        import inspect
        src = inspect.getsource(m.main)
        self.assertIn('"--paper"', src)
        self.assertIn('"--live"', src)

    def test_flag_default_is_none_so_the_file_can_govern(self):
        import inspect, re as _re
        src = inspect.getsource(m.main)
        hit = _re.search(r'"--live".*?default=(\w+)', src, _re.S)
        self.assertEqual(hit.group(1), "None")


class TestSymbolsArgParsing(unittest.TestCase):
    """The comma-separated parser shared by --symbols and SYMBOLS."""

    def test_splits_and_uppercases(self):
        self.assertEqual(m._parse_symbols_arg("btcusdt,ethusdt"),
                         ("BTCUSDT", "ETHUSDT"))

    def test_strips_whitespace_and_drops_blank_entries(self):
        self.assertEqual(m._parse_symbols_arg(" btcusdt , , ethusdt "),
                         ("BTCUSDT", "ETHUSDT"))

    def test_blank_string_means_no_restriction(self):
        self.assertEqual(m._parse_symbols_arg(""), ())
        self.assertEqual(m._parse_symbols_arg("   "), ())
        self.assertEqual(m._parse_symbols_arg(",,,"), ())


class TestSymbolsEnv(unittest.TestCase):
    """SYMBOLS is the environment equivalent of --symbols."""

    def test_env_var_is_read(self):
        import inspect
        src = inspect.getsource(m.main)
        self.assertIn('os.environ.get("SYMBOLS"', src)

    def test_cli_flag_is_checked_before_the_env_fallback(self):
        """--symbols must win when both are set."""
        import inspect
        src = inspect.getsource(m.main)
        cli_at = src.index("if args.symbols:")
        env_at = src.index("elif env_symbols:")
        self.assertLess(cli_at, env_at)

    def test_cli_and_env_share_one_parser(self):
        """A shared helper is what keeps the two paths from drifting apart."""
        import inspect
        src = inspect.getsource(m.main)
        self.assertEqual(src.count("_parse_symbols_arg(args.symbols)"), 1)
        self.assertEqual(src.count("_parse_symbols_arg(env_symbols)"), 1)


class TestNewCliSurface(unittest.TestCase):
    """The new knobs are reachable from the command line and the environment."""

    def setUp(self):
        self._env = dict(os.environ)
        os.environ["BINANCE_API_KEY"] = "k"
        os.environ["BINANCE_API_SECRET"] = "s"
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        _close_journals(self)
        os.environ.clear(); os.environ.update(self._env)
        os.unlink(self.db)

    def _run(self, *argv):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = m.main([*argv, "--db", self.db])
        return code, buf.getvalue()

    def test_auth_wait_env_must_be_a_number(self):
        os.environ["AUTH_WAIT_S"] = "soon"
        code, out = self._run("--calibration-report")
        self.assertEqual(code, 1)
        self.assertIn("AUTH_WAIT_S", out)

    def test_auth_wait_env_is_accepted(self):
        os.environ["AUTH_WAIT_S"] = "120"
        code, out = self._run("--calibration-report")
        self.assertEqual(code, 0, out)

    def test_the_flag_wins_over_the_environment(self):
        import inspect
        src = inspect.getsource(m.main)
        cli_at = src.index("if args.wait_for_auth is not None:")
        env_at = src.index("elif env_auth_wait is not None:")
        self.assertLess(cli_at, env_at)

    def test_min_return_is_settable(self):
        code, out = self._run("--min-return", "0.4", "--check-config")
        self.assertIn(code, (0, 1), out)     # no config file in this temp dir

    def test_the_new_settings_reach_the_config_document(self):
        doc = m.default_config_document()
        for name in ("min_win_return", "trend_follow", "trend_stake_multiple",
                     "auth_wait_timeout_s"):
            self.assertIn(name, doc["defaults"], name)


class TestPreflightGate(unittest.TestCase):
    """Preflight runs at boot, where real credentials and a region exist."""

    @classmethod
    def setUpClass(cls):
        import os as _os
        cls.here = ROOT

    def _entry(self):
        import os as _os
        path = _os.path.join(self.here, "entrypoint.sh")
        if not _os.path.exists(path):
            self.skipTest("entrypoint.sh not present")
        return open(path).read()

    def test_preflight_runs_before_exec(self):
        text = self._entry()
        self.assertLess(text.index("--preflight"), text.index("exec python"))

    def test_preflight_blocks_by_default(self):
        self.assertIn("PREFLIGHT_REQUIRED:-1", self._entry())

    def test_preflight_can_be_downgraded_to_a_warning(self):
        self.assertIn("PREFLIGHT_REQUIRED", self._entry())

    def test_preflight_can_be_skipped(self):
        self.assertIn("SKIP_PREFLIGHT", self._entry())

    def test_preflight_is_not_a_build_step(self):
        """Build has no real credentials and may sit in a blocked region."""
        import os as _os
        path = _os.path.join(self.here, "Dockerfile")
        if not _os.path.exists(path):
            self.skipTest("Dockerfile not present")
        self.assertNotIn("--preflight", open(path).read())
