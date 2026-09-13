"""
The deploy path: the entrypoint, the gate, and the manifests that
have to agree with the module.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest

import btc_5m_predictor as m
from tests.support import ROOT, _stage_bot

class TestDeploymentEntrypoint(unittest.TestCase):
    """
    The deploy path, tested as a unit.

    These are the behaviours that cannot be checked by reading the file: a
    regenerated config would silently discard the user's edits on every
    deploy, and a missing `exec` would stop SIGTERM ever reaching Python.
    """

    @classmethod
    def setUpClass(cls):
        import os as _os
        here = ROOT
        cls.script = _os.path.join(here, "entrypoint.sh")
        cls.bot = _os.path.join(here, "btc_5m_predictor.py")
        cls.text = (open(cls.script).read()
                    if _os.path.exists(cls.script) else "")

    def setUp(self):
        if not self.text:
            self.skipTest("entrypoint.sh not present")
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, config_path, profile=None):
        import subprocess, shutil as _sh, os as _os
        _stage_bot(self.tmp)
        _sh.copy(self.script, self.tmp)
        _os.chmod(_os.path.join(self.tmp, "entrypoint.sh"), 0o755)
        # These exercise config seeding, not the venue: preflight would try
        # to reach Binance with placeholder keys and fail for reasons that
        # have nothing to do with what is being tested.
        env = dict(_os.environ,
                   BINANCE_API_KEY="k", BINANCE_API_SECRET="s",
                   CONFIG_PATH=config_path,
                   PROFILE=profile or m.DEFAULT_PROFILE,
                   SKIP_PREFLIGHT="1", VERIFY_NESTED="1",
                   DB_PATH=_os.path.join(self.tmp, "j.db"))
        return subprocess.run(["./entrypoint.sh", "--check-config"],
                              cwd=self.tmp, env=env, capture_output=True,
                              text=True, timeout=120)

    def test_uses_exec_so_sigterm_reaches_python(self):
        """Without exec the shell keeps PID 1 and Python never shuts down."""
        self.assertRegex(self.text, r"\nexec python")

    def test_first_boot_creates_the_config(self):
        import os as _os
        path = _os.path.join(self.tmp, "data", "config.json")
        r = self._run(path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(_os.path.exists(path))

    def test_written_config_uses_the_requested_profile(self):
        import os as _os, json as _j
        path = _os.path.join(self.tmp, "data", "config.json")
        self._run(path, profile="convex")
        self.assertEqual(_j.load(open(path))["active_profile"], "convex")

    def test_redeploy_preserves_user_edits(self):
        """The reason the script must never regenerate an existing file."""
        import os as _os, json as _j
        path = _os.path.join(self.tmp, "data", "config.json")
        self._run(path)
        doc = _j.load(open(path))
        doc["profiles"][m.DEFAULT_PROFILE]["min_buffer_sigmas"] = 2.75
        doc["overrides"]["max_rounds_per_day"] = 42
        _j.dump(doc, open(path, "w"), indent=2)

        r = self._run(path)                      # simulate a redeploy
        self.assertEqual(r.returncode, 0, r.stderr)
        after = _j.load(open(path))
        self.assertEqual(
            after["profiles"][m.DEFAULT_PROFILE]["min_buffer_sigmas"], 2.75)
        self.assertEqual(after["overrides"]["max_rounds_per_day"], 42)

    def test_invalid_config_fails_before_starting(self):
        import os as _os, json as _j
        path = _os.path.join(self.tmp, "data", "config.json")
        self._run(path)
        doc = _j.load(open(path))
        doc["profiles"][m.DEFAULT_PROFILE]["max_stake_pct"] = 9
        _j.dump(doc, open(path, "w"))
        r = self._run(path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("invalid", r.stderr.lower())

    def test_unwritable_config_dir_fails_with_a_clear_message(self):
        r = self._run("/proc/nope/config.json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("mounted disk", r.stderr)

    def test_config_and_journal_default_to_the_same_disk(self):
        self.assertIn("/var/data/config.json", self.text)
        self.assertIn("/var/data", self.text)


class TestDefaultProfileCoherence(unittest.TestCase):
    """
    One default, four files.

    The manifests cannot import the module, so each carries a literal. Because
    the config is written only once, a manifest disagreeing with the module
    seeds the wrong strategy for the life of the disk.
    """

    @classmethod
    def setUpClass(cls):
        import os as _os
        cls.here = ROOT

    def _read(self, name):
        import os as _os
        path = _os.path.join(self.here, name)
        if not _os.path.exists(path):
            self.skipTest(f"{name} not present")
        return open(path).read()

    def test_dockerfile_agrees_with_the_module(self):
        import re as _re
        hit = _re.search(r"ENV\s+PROFILE=(\w+)", self._read("Dockerfile"))
        self.assertEqual(hit.group(1), m.DEFAULT_PROFILE)

    def test_render_agrees_with_the_module(self):
        import re as _re
        hit = _re.search(r"key:\s*PROFILE\s*\n\s*value:\s*(\w+)",
                         self._read("render.yaml"))
        self.assertEqual(hit.group(1), m.DEFAULT_PROFILE)

    def test_entrypoint_derives_rather_than_hardcodes(self):
        text = self._read("entrypoint.sh")
        self.assertIn("--print-default-profile", text)

    def test_print_default_profile_matches_the_constant(self):
        import subprocess, os as _os
        env = dict(_os.environ, BINANCE_API_KEY="k", BINANCE_API_SECRET="s")
        r = subprocess.run(
            [sys.executable, _os.path.join(self.here, "btc_5m_predictor.py"),
             "--print-default-profile"], capture_output=True, text=True,
            env=env, timeout=60)
        self.assertEqual(r.stdout.strip(), m.DEFAULT_PROFILE)


class TestVerificationGate(unittest.TestCase):
    """
    Broken code must not become a trading process.

    verify.sh runs at build time (failing the build) and again at boot
    (failing the start). These check the gate actually gates.
    """

    @classmethod
    def setUpClass(cls):
        import os as _os
        cls.here = ROOT
        cls.script = _os.path.join(cls.here, "verify.sh")

    def setUp(self):
        import os as _os
        if not _os.path.exists(self.script):
            self.skipTest("verify.sh not present")
        if _os.environ.get("VERIFY_NESTED") == "1":
            # verify.sh runs this suite, and this suite runs verify.sh.
            # Without a depth guard that recurses forever, so the inner run
            # skips these particular tests. The outer run still exercises them.
            self.skipTest("nested inside verify.sh")
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stage(self, mutate=None, target="btc_5m_predictor.py"):
        import shutil as _sh, os as _os
        _stage_bot(self.tmp)
        _sh.copy(self.script, self.tmp)
        if mutate:
            path = _os.path.join(self.tmp, target)
            text = open(path, encoding="utf-8").read()
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(mutate(text))

    def _home_of(self, needle):
        """The file a definition lives in -- it moves during the split."""
        import coherence, os as _os
        for path in coherence.package_sources(self.here):
            with open(path, encoding="utf-8") as fh:
                if needle in fh.read():
                    return _os.path.relpath(path, self.here)
        self.fail(f"nothing defines {needle!r}")

    def _run(self, env_extra=None):
        import subprocess, os as _os
        env = dict(_os.environ, BINANCE_API_KEY="k", BINANCE_API_SECRET="s",
                   VERIFY_NESTED="1")
        env.update(env_extra or {})
        return subprocess.run(["bash", "verify.sh"], cwd=self.tmp, env=env,
                              capture_output=True, text=True, timeout=600)

    def test_healthy_code_passes(self):
        self._stage()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout[-1500:])

    def test_syntax_error_is_caught(self):
        self._stage(lambda t: t + "\ndef broken(:\n")
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("byte-compile", r.stdout)

    def test_broken_logic_is_caught(self):
        """A function that returns a plausible constant still fails."""
        signature = ("def breakeven_probability(price: float, fee_bps: int) "
                     "-> float:")
        self._stage(lambda t: t.replace(
            signature, signature + "\n    return 0.5"),
            target=self._home_of(signature))
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED", r.stdout)

    def test_refusal_message_is_explicit(self):
        self._stage(lambda t: t + "\ndef broken(:\n")
        r = self._run()
        self.assertIn("must not trade", r.stdout)

    def test_skip_verify_bypasses_everything(self):
        self._stage(lambda t: t + "\ndef broken(:\n")
        r = self._run({"SKIP_VERIFY": "1"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("NOT been checked", r.stdout)

    def test_missing_connector_skips_rather_than_fails(self):
        """The runtime image has no Node connector; that is not a violation."""
        import os as _os
        self._stage()
        text = open(_os.path.join(self.tmp, "verify.sh")).read()
        text = text.replace('"$PY" conformance.py 2>&1',
                            '"$PY" conformance.py --connector /nonexistent 2>&1')
        open(_os.path.join(self.tmp, "verify.sh"), "w").write(text)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout[-1000:])
        self.assertIn("SKIP", r.stdout)

    def test_mutation_testing_is_not_a_deploy_gate(self):
        """20+ minutes of test-quality measurement must not block a restart."""
        self.assertNotIn("mutate.py", open(self.script).read())

    def test_boot_verification_skips_only_the_self_referential_tests(self):
        """Boot must stay fast; the gate's own tests belong at build time."""
        import os as _os
        path = _os.path.join(self.here, "entrypoint.sh")
        if not _os.path.exists(path):
            self.skipTest("entrypoint.sh not present")
        self.assertIn("VERIFY_NESTED=1 bash ./verify.sh", open(path).read())

    def test_build_verification_is_the_full_run(self):
        import os as _os
        path = _os.path.join(self.here, "Dockerfile")
        if not _os.path.exists(path):
            self.skipTest("Dockerfile not present")
        text = open(path).read()
        build_line = [ln for ln in text.split("\n")
                      if ln.startswith("RUN BINANCE_API_KEY=build")][0]
        self.assertNotIn("VERIFY_NESTED", build_line)

    def test_entrypoint_runs_verification_before_exec(self):
        import os as _os
        path = _os.path.join(self.here, "entrypoint.sh")
        if not _os.path.exists(path):
            self.skipTest("entrypoint.sh not present")
        text = open(path).read()
        self.assertLess(text.index("verify.sh"), text.index("exec python"))

    def test_dockerfile_verifies_at_build_time(self):
        import os as _os
        path = _os.path.join(self.here, "Dockerfile")
        if not _os.path.exists(path):
            self.skipTest("Dockerfile not present")
        text = open(path).read()
        self.assertIn("RUN BINANCE_API_KEY=build", text)
        self.assertLess(text.index("verify.sh"), text.index("ENTRYPOINT"))

    def test_render_verifies_at_build_time(self):
        import os as _os
        path = _os.path.join(self.here, "render.yaml")
        if not _os.path.exists(path):
            self.skipTest("render.yaml not present")
        text = open(path).read()
        build = text[text.index("buildCommand"):text.index("startCommand")]
        self.assertIn("verify.sh", build)

    def test_the_gate_compiles_every_file(self):
        """A syntax error inside the package must fail the gate as a syntax
        error, not arrive later dressed as a broken test."""
        text = open(self.script).read()
        self.assertIn("btc5m", text)


class TestModeIsNotHardcoded(unittest.TestCase):
    """
    The entrypoint must not pin the mode.

    A command-line flag beats TRADING_MODE, so a hardcoded --live makes that
    variable dead: render.yaml could read TRADING_MODE=paper while the process
    spent real money.
    """

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

    def test_entrypoint_has_no_mode_flag(self):
        import re as _re
        for line in self._entry().split("\n"):
            if line.strip().startswith("#"):
                continue
            self.assertIsNone(
                _re.search(r"(?<![\w-])--(live|paper)(?![\w-])", line),
                f"entrypoint pins the mode: {line.strip()}")

    def test_cli_flag_would_beat_the_environment(self):
        """Why the above matters, asserted rather than assumed."""
        import json as _j, tempfile as _t, os as _os
        fd, path = _t.mkstemp(suffix=".json"); _os.close(fd)
        try:
            doc = m.default_config_document()
            doc["defaults"]["live"] = False   # explicit paper baseline
            _j.dump(doc, open(path, "w"))
            doc = _j.load(open(path))
            pinned = m.build_config(doc, api_key="k", api_secret="s",
                                    live=True, db_path="d")
            unpinned = m.build_config(doc, api_key="k", api_secret="s",
                                      live=None, db_path="d")
            self.assertTrue(pinned.live)
            self.assertFalse(unpinned.live)
        finally:
            _os.unlink(path)

    def test_render_declares_a_trading_mode(self):
        import os as _os, re as _re
        path = _os.path.join(self.here, "render.yaml")
        if not _os.path.exists(path):
            self.skipTest("render.yaml not present")
        # The key and its live value may be separated by commented-out
        # alternatives (`# value: paper`). Matching only the line immediately
        # after the key made the test fail on a perfectly valid manifest, so
        # scan forward past comments and blank lines to the first real value.
        lines = open(path).read().split("\n")
        value = None
        for i, line in enumerate(lines):
            if not _re.match(r"\s*-?\s*key:\s*TRADING_MODE\s*$", line):
                continue
            for follow in lines[i + 1:]:
                if not follow.strip() or follow.strip().startswith("#"):
                    continue
                hit = _re.match(r"\s*value:\s*(\w+)\s*$", follow)
                value = hit.group(1) if hit else None
                break
            break
        self.assertIsNotNone(
            value, "render.yaml declares no TRADING_MODE value")
        self.assertIn(value, ("paper", "live"))


class TestDeploymentManifests(unittest.TestCase):
    """render.yaml and the Dockerfile must agree with the entrypoint."""

    @classmethod
    def setUpClass(cls):
        import os as _os
        cls.here = ROOT

    def _read(self, name):
        import os as _os
        path = _os.path.join(self.here, name)
        if not _os.path.exists(path):
            self.skipTest(f"{name} not present")
        return open(path).read()

    def test_render_region_is_not_in_the_us(self):
        import re as _re
        text = self._read("render.yaml")
        region = _re.search(r"region:\s*(\w+)", text).group(1)
        self.assertNotIn(region, ("oregon", "ohio", "virginia"))

    def test_render_paths_sit_on_the_mounted_disk(self):
        import re as _re
        text = self._read("render.yaml")
        mount = _re.search(r"mountPath:\s*(\S+)", text).group(1)
        for key in ("CONFIG_PATH", "DB_PATH"):
            value = _re.search(rf"{key}\n\s*value:\s*(\S+)", text).group(1)
            self.assertTrue(value.startswith(mount), f"{key}={value}")

    def test_render_uses_the_entrypoint(self):
        self.assertIn("entrypoint.sh", self._read("render.yaml"))

    def test_render_states_autodeploy_explicitly(self):
        """
        Whichever way autoDeploy is set, it must be set on purpose.

        This test used to demand `false`. It was not updated when the setting
        was deliberately flipped to `true`, so it sat red and took the build
        gate down with it -- a test asserting a decision that had already been
        revisited. What is worth pinning is not the value but that the value
        is chosen: an absent autoDeploy silently inherits Render's default,
        and a `true` with no explanation is how a push lands mid-round with
        nobody having decided that it should.
        """
        import re as _re
        text = self._read("render.yaml")
        match = _re.search(r"autoDeploy:\s*(true|false)", text)
        self.assertIsNotNone(match, "render.yaml does not set autoDeploy")
        if match.group(1) == "true":
            self.assertIn("AUTODEPLOY IS ON", text,
                          "autoDeploy: true must carry the note explaining "
                          "that a push can restart the worker mid-round")

    def test_dockerfile_uses_entrypoint_not_cmd_python(self):
        text = self._read("Dockerfile")
        self.assertIn("ENTRYPOINT", text)
        self.assertIn("entrypoint.sh", text)

    def test_dockerfile_paths_are_on_the_disk_not_the_image(self):
        text = self._read("Dockerfile")
        self.assertIn("CONFIG_PATH=/var/data/", text)
        self.assertIn("DB_PATH=/var/data/", text)

    def test_the_shell_gate_is_pinned_to_lf(self):
        """
        A Windows clone with core.autocrlf=true rewrites LF to CRLF, and bash
        reads the carriage return as part of the command:

            verify.sh: line 22: $'\\r': command not found

        entrypoint.sh is the container ENTRYPOINT and verify.sh is the gate
        that runs before the bot is allowed to trade, so both break on a
        fresh Windows clone -- and Linux never sees it, which is what makes
        it worth pinning rather than remembering.
        """
        import os as _os
        if not _os.path.exists(_os.path.join(self.here, ".git")):
            # Line endings are a property of the repository. The image and
            # the gates' staging directories are copies with no checkout in
            # them, so there is nothing here for this rule to govern -- and
            # failing there is what kept every Docker build red.
            self.skipTest("not a git checkout; .gitattributes governs clones")
        path = _os.path.join(self.here, ".gitattributes")
        if not _os.path.exists(path):
            self.fail(".gitattributes is missing; shell scripts are then at "
                      "the mercy of whoever cloned the repo")
        with open(path) as fh:
            rules = fh.read()
        for name in ("*.sh", "entrypoint.sh", "verify.sh", "Dockerfile"):
            # MULTILINE, or the anchor only ever matches the top of
            # the file and every rule below the first reads as absent.
            self.assertRegex(
                rules,
                re.compile(rf"^{re.escape(name)}\s+.*eol=lf", re.M),
                msg=f"{name} is not pinned to LF")

    def test_the_scripts_on_disk_actually_have_lf_endings(self):
        """
        The rule and the bytes are two different claims. This checks the
        second one, because a rule added after the files were checked out
        governs the next clone and not this working copy.
        """
        import os as _os
        for name in ("verify.sh", "entrypoint.sh", "checkup.sh", "Dockerfile"):
            path = _os.path.join(self.here, name)
            if not _os.path.exists(path):
                continue
            with open(path, "rb") as fh:
                raw = fh.read()
            self.assertEqual(
                raw.count(b"\r\n"), 0,
                f"{name} has CRLF line endings; bash and Docker both read "
                f"the carriage return as content")

    def test_the_image_ships_the_suite(self):
        """
        The image runs the suite at build AND at boot. Half a suite copied in
        is a gate that passes because most of it was not there.
        """
        import re as _re
        self.assertRegex(
            self._read("Dockerfile"), _re.compile("^COPY[ ]+tests/", _re.M),
            "the Dockerfile must COPY the tests package")

    def test_the_image_ships_the_package(self):
        """
        An image with the entry point and not the package has no bot in it,
        and says so as an ImportError at boot rather than as a build failure.
        """
        import re as _re
        self.assertRegex(
            self._read("Dockerfile"), _re.compile(r"^COPY\s+btc5m/", _re.M),
            "the Dockerfile must COPY the btc5m package -- matching the word "
            "anywhere would pass on the journal filename alone")
