"""The rules the source itself must obey, and the tools that check them."""

from __future__ import annotations

import sys
import unittest

from tests.support import ROOT, _package_tree, _package_trees

class TestNoSilentFailures(unittest.TestCase):
    """
    Meta-tests over the source. These exist because every serious bug in this
    project so far was an error being swallowed and reported as something
    benign -- a signature failure read as 'endpoint missing', an auth failure
    read as 'book too thin', a wrong-account lookup read as 'zero balance'.
    """

    @classmethod
    def setUpClass(cls):
        import ast
        files = _package_trees()
        cls.src = "\n".join(src for _, src, _ in files)
        cls.tree = ast.Module(
            body=[n for _, _, tree in files for n in tree.body],
            type_ignores=[])

    def test_no_bare_except(self):
        import ast
        bare = [n.lineno for n in ast.walk(self.tree)
                if isinstance(n, ast.ExceptHandler) and n.type is None]
        self.assertEqual(bare, [], f"bare except at lines {bare}")

    def test_no_handler_silently_passes(self):
        import ast
        bad = []
        for n in ast.walk(self.tree):
            if isinstance(n, ast.ExceptHandler) and len(n.body) == 1:
                if isinstance(n.body[0], (ast.Pass, ast.Continue)):
                    bad.append(n.lineno)
        self.assertEqual(bad, [], f"silent pass/continue at lines {bad}")

    def test_no_handler_returns_without_explaining(self):
        """
        An except that returns must log first, or re-raise.

        Exempt: functions named `*_or_none`, whose contract is explicitly to
        report absence rather than to hide a failure. The naming convention
        keeps the exemption visible instead of hidden in a test allowlist.
        """
        import ast
        bad = []
        for fn in ast.walk(self.tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name.endswith("_or_none"):
                continue
            for n in ast.walk(fn):
                if not isinstance(n, ast.ExceptHandler):
                    continue
                body = " ".join(ast.unparse(st) for st in n.body)
                returns = any(isinstance(st, ast.Return) for st in n.body)
                explains = ("LOG." in body or "raise" in body
                            or "print" in body)
                if returns and not explains:
                    bad.append((fn.name, n.lineno))
        self.assertEqual(bad, [], f"unexplained early return at {bad}")

    def test_the_exemption_is_not_abused(self):
        """Only genuinely trivial parsers may use the `_or_none` exemption."""
        import ast
        for fn in ast.walk(self.tree):
            if isinstance(fn, ast.FunctionDef) and fn.name.endswith("_or_none"):
                self.assertLessEqual(
                    len(fn.body), 3,
                    f"{fn.name} is too complex to be exempt from logging")

    def test_venue_fields_are_not_defaulted_with_or(self):
        """`x.get('f') or 5` turns a real 0 into 5 -- the fee-rate bug."""
        import re
        offenders = re.findall(
            r'\.get\(\s*["\'](feeRateBps|decimalPrecision|chainId|collateral'
            r'|startPrice|endPrice|amountOut|realizedPnl|priceImpact)["\']'
            r'\s*\)\s+or\s+[^\s)]', self.src)
        self.assertEqual(offenders, [], f"silent defaults for {offenders}")

    def test_every_apierror_handler_mentions_the_error(self):
        import ast
        bad = []
        for n in ast.walk(self.tree):
            if not isinstance(n, ast.ExceptHandler) or n.type is None:
                continue
            if "ApiError" not in ast.unparse(n.type):
                continue
            body = " ".join(ast.unparse(st) for st in n.body)
            if not ("LOG." in body or "raise" in body or "print" in body
                    or "append" in body):
                bad.append(n.lineno)
        self.assertEqual(bad, [], f"ApiError handled without a trace at {bad}")

    def test_the_rules_are_pointed_at_the_whole_bot(self):
        """
        These rules are worth exactly what they are read against.

        Read through the facade they would find no handlers, no main and no
        client, and every rule above would pass on an empty corpus. This is
        the test that fails instead.
        """
        import ast
        defined = {n.name for n in ast.walk(self.tree)
                   if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        for name in ("main", "Trader", "PredictionClient", "Journal",
                     "assess", "breakeven_probability"):
            self.assertIn(name, defined, f"{name} is not in view")
        self.assertGreater(len(defined), 200, "the corpus lost definitions")
        handlers = [n for n in ast.walk(self.tree)
                    if isinstance(n, ast.ExceptHandler)]
        self.assertGreater(len(handlers), 60,
                           f"only {len(handlers)} except handlers in view; "
                           f"the rules are reading a fraction of the bot")


class TestNoRawTracebacks(unittest.TestCase):
    """CLI commands must fail with a message, never a traceback."""

    def test_every_command_dispatch_is_wrapped(self):
        import ast
        tree = _package_tree()
        main_fn = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "main")
        src = ast.unparse(main_fn)
        self.assertIn("except ApiError", src)
        self.assertIn("except KeyboardInterrupt", src)

    def test_trader_run_is_wrapped(self):
        import ast
        tree = _package_tree()
        main_fn = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "main")
        src = ast.unparse(main_fn)
        idx = src.find(").run()")
        self.assertGreater(idx, 0, "no Trader run call found in main()")
        self.assertIn("except ApiError", src[max(0, idx - 300):idx + 400])


class TestSchemaConformance(unittest.TestCase):
    """
    Every API call must conform to the connector's schema.

    This is the check that does not depend on my model of the API: the
    connector is generated from Binance's own OpenAPI spec, so it catches
    assumptions that the unit tests share with the code under test.
    """

    def test_all_calls_conform(self):
        import subprocess, os as _os
        here = ROOT
        conf = _os.path.join(here, "conformance.py")
        if not _os.path.exists(conf):
            self.skipTest("conformance.py not present")
        r = subprocess.run(
            [sys.executable, conf, "--source",
             _os.path.join(here, "btc_5m_predictor.py")],
            capture_output=True, text=True, cwd=here)
        if r.returncode == 2:
            self.skipTest("connector not installed")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])

    def test_every_request_call_is_found_in_every_source(self):
        """
        The client is several files. A checker that reads one of them and
        reports "all calls conform" is worse than no checker.
        """
        import ast as _ast, os as _os, sys as _sys
        _sys.path.insert(0, ROOT)
        import coherence, conformance
        here = ROOT
        sources = coherence.bot_sources(here)
        calls, reads = conformance.collect(sources)

        expected = 0
        for path in sources:
            with open(path, encoding="utf-8") as fh:
                tree = _ast.parse(fh.read())
            expected += sum(
                1 for n in _ast.walk(tree)
                if isinstance(n, _ast.Call)
                and isinstance(n.func, _ast.Attribute)
                and n.func.attr == "_request"
                and n.args and isinstance(n.args[0], _ast.Constant))
        self.assertEqual(len(calls), expected)
        self.assertEqual(len(calls), 17, "the bot's venue calls changed count")
        self.assertEqual(len({c.endpoint for c in calls}), 15)
        self.assertIn("get_quote", reads)


class TestPropertyInvariants(unittest.TestCase):
    """A fast in-suite slice of the fuzzer, so CI enforces the invariants."""

    def test_fuzz_suite_passes(self):
        import subprocess, os as _os
        here = ROOT
        fz = _os.path.join(here, "fuzz.py")
        if not _os.path.exists(fz):
            self.skipTest("fuzz.py not present")
        r = subprocess.run([sys.executable, fz, "--trials", "400"],
                           capture_output=True, text=True, cwd=here)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])


class TestCoherenceCorpus(unittest.TestCase):
    """
    coherence.py must judge "used anywhere" across every analysed file.

    Single-file analysis was correct while the project was one module. A
    second module makes it actively wrong: a config field read only from
    ws_feeds.py reads as a dead setting, which is an ERROR, which fails the
    build for code that is working.
    """

    def setUp(self):
        import coherence
        self.coherence = coherence

    def _write(self, body):
        import os as _os
        import tempfile as _tf
        fd, path = _tf.mkstemp(suffix=".py")
        with _os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.addCleanup(_os.unlink, path)
        return path

    def test_set_corpus_merges_every_file(self):
        a = self._write("def alpha():\n    return 1\n")
        b = self._write("def beta():\n    return 2\n")
        loaded = self.coherence.set_corpus([a, b])
        self.assertEqual([p for p, _, _ in loaded], [a, b])
        names = {n.name for n in self.coherence.CORPUS_TREE.body}
        self.assertEqual(names, {"alpha", "beta"})
        self.assertIn("alpha", self.coherence.CORPUS_SRC)
        self.assertIn("beta", self.coherence.CORPUS_SRC)

    def test_a_function_called_only_from_another_file_is_not_dead(self):
        a = self._write("def helper():\n    return 1\n")
        b = self._write("def caller():\n    return helper()\n")
        loaded = self.coherence.set_corpus([a, b])
        f = self.coherence.Findings()
        for path, src, tree in loaded:
            self.coherence.SOURCE_PATH = path
            self.coherence.check_dead_functions(src, tree, f)
        self.assertEqual(
            [e for e in f.errors if "helper" in e], [],
            f"helper() is called from the other file; errors were {f.errors}")

    def test_findings_name_the_file_when_there_is_more_than_one(self):
        a = self._write("def only_here():\n    return 1\n")
        b = self._write("def other():\n    return 2\n")
        loaded = self.coherence.set_corpus([a, b])
        f = self.coherence.Findings()
        for path, src, tree in loaded:
            self.coherence.SOURCE_PATH = path
            self.coherence.check_dead_functions(src, tree, f)
        dead = [e for e in f.errors if "only_here" in e]
        self.assertEqual(len(dead), 1, f.errors)
        import os as _os
        self.assertIn(_os.path.basename(a), dead[0],
                      "a finding must say which file it came from")

    def test_prose_may_name_an_identifier_defined_in_another_file(self):
        a = self._write("class Config:\n    ws_stale_s: float = 5.0\n")
        b = self._write("# reads cfg.ws_stale_s to decide staleness\n"
                        "def reader():\n    return 1\n")
        loaded = self.coherence.set_corpus([a, b])
        f = self.coherence.Findings()
        for path, src, tree in loaded:
            self.coherence.SOURCE_PATH = path
            self.coherence.check_stale_prose(src, tree, f)
        self.assertEqual(
            [w for w in f.warnings if "ws_stale_s" in w], [],
            f"ws_stale_s exists in the corpus; warnings were {f.warnings}")

    def test_default_sources_are_the_whole_bot(self):
        import os as _os
        here = ROOT
        sources = self.coherence.bot_sources(here)
        self.assertTrue(sources[0].endswith("btc_5m_predictor.py"),
                        f"the facade must lead, got {sources[:1]}")
        self.assertTrue(any(s.endswith("ws_feeds.py") for s in sources))
        for path in sources:
            self.assertTrue(_os.path.exists(path), path)

    def test_the_config_checks_read_every_file(self):
        """
        Config, the CLI and the profile table may live in any file.

        These four checks used to read only the first source. That was right
        while the first source was the whole bot; with the bot in a package it
        would check a facade that declares none of them and report nothing --
        a gate that passes because it looked in an empty room.
        """
        facade = self._write("import sys\n")
        rest = self._write(
            'PROFILES: dict[str, dict] = {\n'
            '    "p": dict(min_edge=0.1),\n'
            '}\n'
            'DEFAULT_PROFILE = "p"\n'
            'class Config:\n'
            '    min_edge: float = 0.05\n'
            '    unread_setting: float = 1.0\n')
        f = self.coherence.analyse([facade, rest])
        self.assertEqual(
            [e for e in f.errors if "no Config class" in e], [],
            f"the Config audit did not find Config in the corpus: {f.errors}")
        self.assertTrue(
            any("unread_setting" in e for e in f.errors),
            f"a dead setting in the second file went unreported: {f.errors}")

    def test_a_mixin_may_read_what_its_host_assigns(self):
        """
        A mixin's methods run on the host's instance.

        Splitting a class across files must not make every attribute the host
        assigns read as one the mixin never sets -- that is 200 warnings for
        code that is working, which is how a report stops being read.
        """
        mixin = self._write("class ScalpMixin:\n"
                            "    def enter(self):\n"
                            "        return self._client\n")
        host = self._write("class Trader(ScalpMixin):\n"
                           "    def __init__(self):\n"
                           "        self._client = 1\n")
        f = self.coherence.analyse([host, mixin])
        self.assertEqual(
            [w for w in f.warnings if "_client" in w], [],
            f"_client is assigned by the host: {f.warnings}")

    def test_a_mixin_may_call_a_sibling_mixins_method(self):
        one = self._write("class OneMixin:\n"
                          "    def enter(self):\n"
                          "        return self.settle()\n")
        two = self._write("class TwoMixin:\n"
                          "    def settle(self):\n"
                          "        return 1\n")
        host = self._write("class Trader(OneMixin, TwoMixin):\n"
                           "    pass\n")
        f = self.coherence.analyse([host, one, two])
        self.assertEqual(
            [w for w in f.warnings if "settle" in w], [],
            f"settle() is a sibling mixin's method: {f.warnings}")

    def test_the_real_project_is_still_coherent(self):
        """The change must not make the actual codebase report new findings."""
        import subprocess as _sp
        import sys as _sys
        import os as _os
        here = ROOT
        proc = _sp.run([_sys.executable, "coherence.py"], cwd=here,
                       capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
