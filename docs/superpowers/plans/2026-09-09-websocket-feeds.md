# WebSocket Feeds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three hot REST polls in the trading loop with supervised, long-lived WebSocket connections, keeping REST as the fallback on any staleness.

**Architecture:** A new module `ws_feeds.py` holds two supervised connections (public spot streams, signed prediction-orderbook stream) behind a single `MarketData` facade. The facade exposes the three calls that are hot today with today's signatures, so the trading code changes only in where it reads from. Freshness is gated on connection health, never on per-market timestamps.

**Tech Stack:** Python 3.12, `requests`, `websocket-client`, `unittest`, SQLite.

**Spec:** `docs/superpowers/specs/2026-09-09-websocket-feeds-design.md`

## Global Constraints

- Commit straight to `master`. No feature branches on this repo.
- **Never run the full test suite locally** — it takes 10+ minutes here versus 23s on Render. Run only the named test class in each task. The Docker build is the gate.
- `autoDeploy: true` in `render.yaml`. Every push to `master` restarts the live worker. Push between rounds if the bot is holding.
- Every new `Config` field must be read somewhere, or `coherence.py` fails the build with "declared but never read — dead setting". Same for every function defined in `ws_feeds.py`.
- Prose style in this repo: comments explain *why*, name the failure the code prevents, and use `--` rather than an em dash. Match it.
- Every commit message ends with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  ```
- Environment for any test run: `BINANCE_API_KEY=build BINANCE_API_SECRET=build`. `Config.__post_init__` raises without them.
- No network in any test. Ever.

---

### Task 0: Unbreak the deploy gate

`test_render_disables_autodeploy` asserts `autoDeploy: false`, but commit `0c345a2` ("Let a push deploy itself") deliberately set it to `true` and never updated the test. The suite is red on `master` right now, which means `verify.sh` fails, which means the Docker build fails and nothing in this plan can ship. Fix this first.

The test's *intent* is still worth keeping: autoDeploy has a real consequence, documented in note 4 of `render.yaml`. So the test is rewritten to assert the setting is explicit and its consequence is documented, rather than asserting a value that is no longer the chosen one.

**Files:**
- Modify: `test_btc_5m.py:5057-5059`

**Interfaces:**
- Consumes: nothing.
- Produces: a green `TestDeploymentManifests`, which every later task's verify run depends on.

- [ ] **Step 1: Run the failing test to see the current state**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestDeploymentManifests -v
```

Expected: FAIL on `test_render_disables_autodeploy` with `'autoDeploy: false' not found`.

- [ ] **Step 2: Replace the test**

In `test_btc_5m.py`, replace this:

```python
    def test_render_disables_autodeploy(self):
        self.assertIn("autoDeploy: false", self._read("render.yaml"))
```

with this:

```python
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
```

- [ ] **Step 3: Run the test to verify it passes**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestDeploymentManifests -v
```

Expected: 6 tests, all PASS.

- [ ] **Step 4: Run the safe parts of the gate**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python coherence.py --source btc_5m_predictor.py && BINANCE_API_KEY=build BINANCE_API_SECRET=build python fuzz.py --trials 400
```

Expected: both exit 0.

**Do NOT run `./verify.sh` or the full unit suite on this machine.** It was tried once on 2026-09-09 and took 22 minutes to report 148 errors and 2 failures, every one of them a Windows artifact:

- `PermissionError [WinError 32]` unlinking an open sqlite file in `tearDown` — legal on POSIX, impossible on Windows. That is the 148.
- `TestVerificationGate.test_missing_connector_skips_rather_than_fails` — the test rewrites `verify.sh` through Python text mode, turning all 101 LF into CRLF; bash then dies with a syntax error and returns 2.
- `TestVerificationGate.test_skip_verify_bypasses_everything` — `bash` on this box resolves to WSL, and Windows environment variables do not cross into WSL without `WSLENV`, so `SKIP_VERIFY=1` never arrives and the script runs the checks it was told to skip.

`verify.sh` is also self-recursive here: it runs the unit suite, which contains `TestVerificationGate`, which spawns nested `verify.sh` runs that each run the whole suite again. That is where the 22 minutes goes.

The real gate is the Render build, or a local `docker build` when Docker Desktop's daemon is actually running — it usually is not on this box.

- [ ] **Step 5: Commit**

```bash
git add test_btc_5m.py
git commit -m "$(cat <<'EOF'
Stop a test from asserting a decision that was already revisited

autoDeploy was deliberately turned on in 0c345a2 and the test demanding
it be off was never updated, so master has been failing its own gate
since. What is worth pinning is not the value but that the value is
chosen: an absent setting inherits Render's default, and a true with no
explanation is how a push lands mid-round with nobody having decided it
should.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 1: Teach coherence.py to analyse a corpus

Every later task depends on this. `coherence.py` walks one file: a `Config` field read only from `ws_feeds.py` would be reported as "declared but never read — dead setting", which is an *error* and fails the build. Same for any function in `ws_feeds.py` called only from `btc_5m_predictor.py`.

The fix keeps definition-side analysis per-file (so line numbers stay correct and get a filename prefix) and moves only the usage-side lookups — "is this read anywhere", "is this called anywhere", "does this identifier still exist" — onto a merged corpus.

**Files:**
- Modify: `coherence.py:38-57` (Findings, SOURCE_PATH, load), `coherence.py:71-129` (check_config), `coherence.py:132-157` (check_dead_functions), `coherence.py:230-247` (check_stale_prose), `coherence.py:365-378` (main)
- Modify: `verify.sh:63` (pass both sources)
- Test: `test_btc_5m.py` (new class `TestCoherenceCorpus`)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `coherence.CORPUS_SRC: str` — every analysed file's source, concatenated.
  - `coherence.CORPUS_TREE: ast.Module` — merged AST of every analysed file.
  - `coherence.set_corpus(paths: list[str]) -> list[tuple[str, str, ast.Module]]` — loads each path, sets the two globals, returns `[(path, src, tree), ...]`.
  - `coherence.main()` accepts `--source` repeatably.

- [ ] **Step 1: Write the failing test**

Append to `test_btc_5m.py`:

```python
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
        b = self._write("import x\n\ndef caller():\n    return helper()\n")
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

    def test_the_real_project_is_still_coherent(self):
        """The change must not make the actual codebase report new findings."""
        import subprocess as _sp
        import sys as _sys
        import os as _os
        here = _os.path.dirname(_os.path.abspath(__file__))
        sources = ["btc_5m_predictor.py"]
        if _os.path.exists(_os.path.join(here, "ws_feeds.py")):
            sources.append("ws_feeds.py")
        argv = [_sys.executable, "coherence.py"]
        for s in sources:
            argv += ["--source", s]
        proc = _sp.run(argv, cwd=here, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
```

- [ ] **Step 2: Run it to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestCoherenceCorpus -v
```

Expected: FAIL with `AttributeError: module 'coherence' has no attribute 'set_corpus'`.

- [ ] **Step 3: Add the corpus globals and loader**

In `coherence.py`, replace:

```python
SOURCE_PATH = "btc_5m_predictor.py"


def load(path: str) -> tuple[str, ast.Module]:
    src = open(path, encoding="utf-8").read()
    return src, ast.parse(src)
```

with:

```python
SOURCE_PATH = "btc_5m_predictor.py"

# Every analysed file, concatenated and merged. Definitions are still judged
# one file at a time -- that is what keeps line numbers meaningful -- but
# "is this read anywhere", "is this called anywhere" and "does this name
# still exist" are questions about the project, not about a file. Asking
# them per-file is what turns a config field read from the other module into
# a reported dead setting, which is an ERROR and fails the build for code
# that is working.
CORPUS_SRC = ""
CORPUS_TREE: ast.Module = ast.Module(body=[], type_ignores=[])
MULTI_FILE = False


def load(path: str) -> tuple[str, ast.Module]:
    src = open(path, encoding="utf-8").read()
    return src, ast.parse(src)


def set_corpus(paths: list[str]) -> list[tuple[str, str, ast.Module]]:
    """Load every path, build the merged corpus, and return the parts."""
    global CORPUS_SRC, CORPUS_TREE, MULTI_FILE
    loaded = [(p, *load(p)) for p in paths]
    CORPUS_SRC = "\n".join(src for _, src, _ in loaded)
    CORPUS_TREE = ast.Module(
        body=[node for _, _, tree in loaded for node in tree.body],
        type_ignores=[])
    MULTI_FILE = len(loaded) > 1
    return loaded


def where(msg: str) -> str:
    """Prefix a finding with its file, but only when that is ambiguous."""
    if not MULTI_FILE:
        return msg
    return f"{os.path.basename(SOURCE_PATH)}: {msg}"
```

`os` is already imported at the top of `coherence.py`.

- [ ] **Step 4: Point the usage-side lookups at the corpus**

In `check_config`, replace:

```python
    # Which are read anywhere as cfg.X / self._cfg.X / c.X?
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in declared:
            read.add(node.attr)
    # Fields consumed via **PROFILES entries count as used.
    for name in declared:
        if re.search(rf"\b{name}\s*=", src.split("PROFILES")[-1] if "PROFILES" in src else ""):
            read.add(name)
```

with:

```python
    # Which are read anywhere as cfg.X / self._cfg.X / c.X? Asked of the
    # whole corpus: a setting read only from the transport module is read.
    read: set[str] = set()
    for node in ast.walk(CORPUS_TREE):
        if isinstance(node, ast.Attribute) and node.attr in declared:
            read.add(node.attr)
    # Fields consumed via **PROFILES entries count as used.
    for name in declared:
        if re.search(rf"\b{name}\s*=", CORPUS_SRC.split("PROFILES")[-1]
                     if "PROFILES" in CORPUS_SRC else ""):
            read.add(name)
```

and replace the error line:

```python
        f.error(f"Config.{name} is declared but never read -- dead setting")
```

with:

```python
        f.error(where(f"Config.{name} is declared but never read "
                      f"-- dead setting"))
```

In the same function, extend the plumbing regex so the WebSocket settings are exempted deliberately rather than by accident of not matching `strategyish`:

```python
    plumbing = re.compile(r"(recv_window|http_|poll_|db_path|sigma_window|"
                          r"vol_lookback|max_consecutive_errors|"
                          r"calibration_|tail_df_|round_seconds|ws_)")
```

- [ ] **Step 5: Point dead-function and stale-prose analysis at the corpus**

In `check_dead_functions`, replace `for node in ast.walk(tree):` in the **`called`** loop (the second loop, not the `defined` loop) with `for node in ast.walk(CORPUS_TREE):`, and replace the error line with:

```python
        f.error(where(f"L{line}: {name}() is defined but never called "
                      f"-- dead code"))
```

In `check_stale_prose`, replace `for node in ast.walk(tree):` in the **`live`** loop (the first loop) with `for node in ast.walk(CORPUS_TREE):`, and replace the warning line with:

```python
            f.warn(where(f"L{line_no}: prose mentions '{name}', which no "
                         f"longer exists in the project"))
```

Leave `check_attributes`, `check_magic_numbers`, `check_cli`, `check_default_profile` and `check_mode_flags` per-file. Attributes and magic numbers are genuinely file-local questions, and the last three only describe the main module.

- [ ] **Step 6: Make main() accept several sources**

Replace the body of `main()` from `ap.add_argument("--source", ...)` through the `for check in (...)` loop with:

```python
    ap.add_argument("--source", action="append", default=None,
                    help="analyse this file; repeat for a multi-file corpus")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as failures")
    args = ap.parse_args()

    sources = args.source or ["btc_5m_predictor.py"]
    loaded = set_corpus(sources)
    f = Findings()

    global SOURCE_PATH
    # check_cli, check_default_profile and check_mode_flags describe the main
    # module specifically; running them over the transport module would
    # report its lack of an argument parser as a finding.
    main_only = (check_cli, check_default_profile, check_mode_flags)
    for path, src, tree in loaded:
        SOURCE_PATH = path
        for check in (check_config, check_dead_functions, check_attributes,
                      check_stale_prose, check_magic_numbers):
            check(src, tree, f)
        if path == sources[0]:
            for check in main_only:
                check(src, tree, f)

    print(f"=== COHERENCE: {', '.join(sources)} ===\n")
```

and delete the now-duplicated `print(f"=== COHERENCE: {args.source} ===\n")` line that followed the old loop.

- [ ] **Step 7: Run the tests**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestCoherenceCorpus -v
```

Expected: 5 tests PASS. `test_the_real_project_is_still_coherent` proves the change reports nothing new on the actual codebase.

- [ ] **Step 8: Update verify.sh**

In `verify.sh`, replace:

```bash
  run "coherence" "$PY" coherence.py
```

with:

```bash
  run "coherence" "$PY" coherence.py --source btc_5m_predictor.py \
      --source ws_feeds.py
```

Guard it so the gate still works before `ws_feeds.py` exists — replace the whole coherence block with:

```bash
if [ -f coherence.py ]; then
  # Both modules are analysed as one corpus. A config field read only from
  # ws_feeds.py is not a dead setting, and single-file analysis would call it
  # one and fail the build for working code.
  coh_args=(--source btc_5m_predictor.py)
  [ -f ws_feeds.py ] && coh_args+=(--source ws_feeds.py)
  run "coherence" "$PY" coherence.py "${coh_args[@]}"
else
  printf '  %-22s SKIP\n' "coherence"; skipped=$((skipped + 1))
fi
```

- [ ] **Step 9: Verify the gate still passes**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python coherence.py --source btc_5m_predictor.py
```

Expected: exit 0, "No stale artifacts found" or warnings only.

- [ ] **Step 10: Commit**

```bash
git add coherence.py verify.sh test_btc_5m.py
git commit -m "$(cat <<'EOF'
Ask whether a name is used by the project, not by one file

coherence.py walks a single module, which was right while there was one.
A second module makes it wrong in the expensive direction: a config field
read only from the transport layer reads as a dead setting, and that is
an error, so the build fails for code that works.

Definitions stay per-file, because that is what keeps line numbers
meaningful and now names the file they came from. Only the usage-side
questions -- read anywhere, called anywhere, does this name still exist
-- move to the merged corpus.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Config fields and the dependency

Six settings, the `websocket-client` dependency, and the `Dockerfile` COPY entry. Nothing reads the settings yet, so they are wired to `MarketData` in Task 6; until then `coherence.py` would call them dead. To avoid a knowingly-red intermediate commit, this task lands the fields *and* the trivial accessor that reads them, which is `Config.ws_url`.

**Files:**
- Modify: `btc_5m_predictor.py:586-591` (Config fields), `btc_5m_predictor.py:808-810` (next to `ep`)
- Modify: `requirements.txt`
- Modify: `Dockerfile:29-30` (COPY list)
- Test: `test_btc_5m.py` (new class `TestWsConfig`)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Config.ws_enabled: bool`, `Config.ws_spot_url: str`, `Config.ws_book_url: str`, `Config.ws_stale_s: float`, `Config.ws_reconnect_max_s: float`, `Config.ws_recycle_s: float`
  - `Config.ws_url(self, which: str) -> str` where `which` is `"spot"` or `"book"`.
  - Module constant `WS_BOOK_VALIDATE_TOL: float = 0.02`.

- [ ] **Step 1: Write the failing test**

Append to `test_btc_5m.py`:

```python
class TestWsConfig(unittest.TestCase):
    """The WebSocket settings, and the invariants that keep them honest."""

    def test_defaults_are_present_and_sane(self):
        c = cfg()
        self.assertTrue(c.ws_enabled)
        self.assertTrue(c.ws_spot_url.startswith("wss://"))
        self.assertTrue(c.ws_book_url.startswith("wss://"))
        self.assertGreater(c.ws_stale_s, 0)
        self.assertGreater(c.ws_reconnect_max_s, 0)
        self.assertGreater(c.ws_recycle_s, 0)

    def test_recycle_lands_before_the_venue_closes_the_socket(self):
        """
        The venue drops the connection at 24h. Recycling at or after that
        means the drop always arrives as a surprise disconnect rather than a
        planned handover, which is a gap in the feed on a fixed schedule.
        """
        self.assertLess(cfg().ws_recycle_s, 24 * 3600)

    def test_a_non_positive_stale_budget_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(cfg(), ws_stale_s=0.0)

    def test_a_non_positive_reconnect_ceiling_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(cfg(), ws_reconnect_max_s=-1.0)

    def test_a_recycle_at_or_past_the_venue_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(cfg(), ws_recycle_s=24 * 3600)

    def test_ws_url_resolves_both_feeds(self):
        c = cfg()
        self.assertEqual(c.ws_url("spot"), c.ws_spot_url)
        self.assertEqual(c.ws_url("book"), c.ws_book_url)

    def test_ws_url_refuses_a_name_it_does_not_know(self):
        with self.assertRaises(KeyError):
            cfg().ws_url("orderbook")

    def test_the_validation_tolerance_is_tighter_than_a_transposed_side(self):
        """
        A transposed side mapping shows up as |1 - 2p| of price error, which
        is 0.20 at p=0.40 and grows toward the ends. The tolerance has to sit
        well below that or the check it exists to perform cannot fail.
        """
        self.assertLess(m.WS_BOOK_VALIDATE_TOL, 0.10)
        self.assertGreater(m.WS_BOOK_VALIDATE_TOL, 0.0)

    def test_websocket_client_is_declared(self):
        import os as _os
        here = _os.path.dirname(_os.path.abspath(__file__))
        with open(_os.path.join(here, "requirements.txt")) as fh:
            self.assertIn("websocket-client", fh.read())

    def test_the_image_ships_the_transport_module(self):
        import os as _os
        here = _os.path.dirname(_os.path.abspath(__file__))
        path = _os.path.join(here, "Dockerfile")
        if not _os.path.exists(path):
            self.skipTest("Dockerfile not present")
        with open(path) as fh:
            text = fh.read()
        self.assertIn("ws_feeds.py", text,
                      "ws_feeds.py must be COPYed or the image runs without "
                      "the transport layer and silently falls back to REST")
```

Note: `cfg()` and `replace` are already defined/imported at the top of `test_btc_5m.py`. `m` is the imported module.

- [ ] **Step 2: Run it to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestWsConfig -v
```

Expected: FAIL — `Config` has no attribute `ws_enabled`.

- [ ] **Step 3: Add the module constant**

In `btc_5m_predictor.py`, immediately after `DEFAULT_FEE_BPS = 200`:

```python
# How far the WebSocket-derived ask ladder may sit from the REST ladder it is
# validated against, at top of book, in absolute price. Loose enough to
# survive the few hundred milliseconds between the push and the REST reply on
# a live book; far tighter than the |1 - 2p| error a transposed side mapping
# would produce anywhere away from 0.50, which is the mistake this check
# exists to catch.
WS_BOOK_VALIDATE_TOL = 0.02
```

- [ ] **Step 4: Add the Config fields**

In `btc_5m_predictor.py`, immediately after `http_timeout_s: float = 10.0`:

```python
    # -- WebSocket feeds ----------------------------------------------------
    # False drops the bot to REST-only, which is exactly the behaviour it had
    # before these existed. Resolves through ConfigStore, so it is the
    # rollback path: edit the file, and the running bot stops using the
    # sockets on its next reload without a redeploy.
    ws_enabled: bool = True
    ws_spot_url: str = "wss://stream.binance.com:9443/stream"
    ws_book_url: str = "wss://api.binance.com/sapi/wss"
    # Silence on a socket for longer than this marks the feed unhealthy and
    # sends every read back to REST. Measured on the CONNECTION, never per
    # market: a book that is not changing sends nothing, so per-market
    # silence cannot tell a quiet market from a dead socket.
    ws_stale_s: float = 5.0
    ws_reconnect_max_s: float = 30.0
    # The venue closes the connection at 24h. Handing over early turns a
    # scheduled surprise into a planned one.
    ws_recycle_s: float = 82800.0        # 23h
```

- [ ] **Step 5: Add validation**

In `Config.__post_init__`, add to the existing positive-number loop by extending its tuple:

```python
        for name in ("clock_resync_s", "settle_grace_s", "settle_timeout_s",
                     "drain_timeout_s", "drain_poll_s", "prune_after_s",
                     "vol_cache_s", "error_backoff_max_s", "auth_wait_poll_s",
                     "ws_stale_s", "ws_reconnect_max_s", "ws_recycle_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
```

and add immediately after that loop:

```python
        if self.ws_recycle_s >= 24 * 3600:
            raise ValueError(
                "ws_recycle_s must be under 24h: the venue closes the "
                "connection at 24h, and recycling at or after that point "
                "guarantees the drop arrives as an unplanned gap")
```

- [ ] **Step 6: Add the accessor**

In `btc_5m_predictor.py`, immediately after `Config.ep`:

```python
    def ws_url(self, which: str) -> str:
        """
        Socket URL by feed name. Mirrors `ep` deliberately.

        Named lookup rather than two attribute reads for the same reason the
        endpoint table is one: a typo becomes a KeyError here instead of a
        connection to whatever the misspelled attribute happened to hold.
        """
        return {"spot": self.ws_spot_url, "book": self.ws_book_url}[which]
```

- [ ] **Step 7: Add the dependency**

Replace the contents of `requirements.txt` with:

```
requests>=2.31,<3
websocket-client>=1.7,<2
```

- [ ] **Step 8: Ship the module in the image**

In `Dockerfile`, replace:

```dockerfile
COPY btc_5m_predictor.py test_btc_5m.py coherence.py fuzz.py \
     conformance.py verify.sh entrypoint.sh ./
```

with:

```dockerfile
COPY btc_5m_predictor.py ws_feeds.py test_btc_5m.py coherence.py fuzz.py \
     conformance.py verify.sh entrypoint.sh ./
```

- [ ] **Step 9: Create a placeholder so the COPY does not break the build**

`ws_feeds.py` does not exist until Task 3, and a `COPY` naming a missing file fails the Docker build. Create it now with real content — the module docstring, which Task 3 fills in beneath:

```python
#!/usr/bin/env python3
"""
ws_feeds.py -- persistent WebSocket feeds for btc_5m_predictor.

Three reads sit inside the poll loop, and every one of them was a blocking
round trip: the prediction order book (2 x N candidate rounds per tick), spot
price (N per tick), and the kline window behind the volatility estimate. All
three have streaming equivalents. Everything signed and mutating -- quotes,
orders, redemptions -- stays on REST, where request/response is the right
shape for a call that moves money.

THE RULE THAT SHAPES EVERYTHING HERE
------------------------------------
A book that is not changing sends no messages. So a quiet market and a dead
socket are indistinguishable if freshness is measured per market: a socket
that died at 14:02 would keep serving 14:02 ladders to the sizing model,
reporting itself healthy the whole time.

Freshness is therefore measured on the CONNECTION -- time since any frame,
plus pong receipt -- and that is the only thing that decides WebSocket versus
REST. Per-market updateTimestampMs is used to discard out-of-order updates
and for nothing else.

See docs/superpowers/specs/2026-09-09-websocket-feeds-design.md.
"""

from __future__ import annotations
```

- [ ] **Step 10: Run the tests**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestWsConfig -v
```

Expected: 10 tests PASS.

- [ ] **Step 11: Confirm coherence still passes with the new fields**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python coherence.py --source btc_5m_predictor.py --source ws_feeds.py
```

Expected: exit 0. The six `ws_*` fields are read by `ws_url` and by `__post_init__`, so none is reported dead. If any *is* reported, do not silence it — that means it is genuinely unwired and Task 6 has to reach it.

- [ ] **Step 12: Install the dependency locally and commit**

```bash
pip install -r requirements.txt
git add requirements.txt Dockerfile btc_5m_predictor.py ws_feeds.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Give the sockets their settings before giving them their code

Six fields, one accessor and the dependency. ws_enabled is the one that
matters operationally: it resolves through ConfigStore, so dropping a
running bot back to REST-only is a file edit and a reload, not a
redeploy.

ws_recycle_s is validated under 24h rather than merely positive. The
venue closes the connection at 24h, so a recycle at or past that point
does not schedule a handover -- it schedules a surprise.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: WsConnection — one supervised socket

The lifecycle, with no knowledge of what it carries. Connect, ping, receive, reconnect with backoff, recycle before the venue's 24h close, and — the part that carries real operational weight — distinguish an authentication refusal from a transport failure.

**Files:**
- Modify: `ws_feeds.py`
- Test: `test_btc_5m.py` (new class `TestWsConnection`)

**Interfaces:**
- Consumes: `Config.ws_stale_s`, `Config.ws_reconnect_max_s`, `Config.ws_recycle_s`.
- Produces:
  - `ws_feeds.LOG` — `logging.getLogger("btc5m.ws")`
  - `ws_feeds.WsAuthRefused(Exception)`
  - `ws_feeds.is_auth_refusal(exc: BaseException) -> bool`
  - `ws_feeds.WsConnection(name: str, url_factory: Callable[[], str], on_message: Callable[[str], None], cfg_source, on_open: Callable[[WsConnection], None] | None = None)`
    - `.start() -> None`, `.stop() -> None`
    - `.healthy -> bool` (property), `.recycle_due -> bool` (property)
    - `.send(payload: str) -> bool`
    - `.dispatch(raw: str) -> None`
    - `.mark_frame() -> None`, `.note_open() -> None`, `.note_failure(exc) -> None`
    - `.backoff_for(attempt: int) -> float`
    - Attributes: `.last_frame_ts: float`, `.opened_ts: float`, `.disabled: bool`, `.should_retry: bool`, `.generation: int`, `._lock: threading.Lock`
  - Module-level imports `json`, `threading`, `time` — later tasks build on them.

- [ ] **Step 1: Write the failing test**

Append to `test_btc_5m.py`:

```python
class TestWsConnection(unittest.TestCase):
    """Lifecycle only. Nothing here knows what the frames mean."""

    def setUp(self):
        import ws_feeds
        self.ws_feeds = ws_feeds
        self.cfg = cfg()

    def _conn(self, **kw):
        kw.setdefault("name", "test")
        kw.setdefault("url_factory", lambda: "wss://example.invalid/x")
        kw.setdefault("on_message", lambda _raw: None)
        kw.setdefault("cfg_source", self.cfg)
        return self.ws_feeds.WsConnection(**kw)

    def test_a_fresh_connection_is_not_healthy_until_a_frame_arrives(self):
        """
        Health is evidence, not optimism. A socket that opened and then said
        nothing has proved nothing, and treating it as healthy is how a dead
        feed gets to price an order.
        """
        c = self._conn()
        self.assertFalse(c.healthy)

    def test_a_recent_frame_makes_it_healthy(self):
        c = self._conn()
        c.mark_frame()
        self.assertTrue(c.healthy)

    def test_silence_past_the_budget_makes_it_unhealthy(self):
        c = self._conn()
        c.mark_frame()
        c.last_frame_ts = time.time() - (self.cfg.ws_stale_s + 1.0)
        self.assertFalse(c.healthy)

    def test_a_disabled_connection_is_never_healthy(self):
        c = self._conn()
        c.mark_frame()
        c.disabled = True
        self.assertFalse(c.healthy)

    def test_backoff_grows_and_is_capped(self):
        c = self._conn()
        delays = [c.backoff_for(n) for n in range(1, 12)]
        self.assertEqual(delays, sorted(delays), "backoff must not shrink")
        self.assertLessEqual(max(delays), self.cfg.ws_reconnect_max_s)
        self.assertGreater(delays[-1], delays[0])

    def test_an_auth_refusal_disables_the_feed_and_does_not_retry(self):
        """
        A 401 means the key is wrong or the egress IP is not allowlisted.
        Neither heals on its own, and this venue rate-limits by IP, so
        retrying turns a configuration error into a second problem. On a
        Render starter the egress address is not knowable in advance, which
        is the whole reason AUTH_WAIT_S exists -- so this refusal has to be
        legible rather than buried under a reconnect loop.
        """
        c = self._conn()
        c.note_failure(self.ws_feeds.WsAuthRefused("HTTP 401"))
        self.assertTrue(c.disabled)
        self.assertFalse(c.should_retry)

    def test_a_transport_failure_does_retry(self):
        c = self._conn()
        c.note_failure(OSError("connection reset"))
        self.assertFalse(c.disabled)
        self.assertTrue(c.should_retry)

    def test_a_handshake_401_is_read_as_an_auth_refusal(self):
        self.assertTrue(self.ws_feeds.is_auth_refusal(
            Exception("Handshake status 401 Unauthorized")))
        self.assertTrue(self.ws_feeds.is_auth_refusal(
            Exception("Handshake status 403 Forbidden")))
        self.assertFalse(self.ws_feeds.is_auth_refusal(
            Exception("Connection to remote host was lost")))
        self.assertFalse(self.ws_feeds.is_auth_refusal(
            Exception("Handshake status 503 Service Unavailable")))

    def test_recycle_is_due_before_the_venue_would_close_it(self):
        c = self._conn()
        c.opened_ts = time.time()
        self.assertFalse(c.recycle_due)
        c.opened_ts = time.time() - (self.cfg.ws_recycle_s + 1.0)
        self.assertTrue(c.recycle_due)

    def test_a_reopen_bumps_the_generation(self):
        """
        Consumers cache state keyed to a connection. The venue forbids
        carrying a book across a reconnect, so they need to see that the
        socket underneath them is a different one -- a boolean that flickers
        false and true between two polls would be missed.
        """
        c = self._conn()
        first = c.generation
        c.note_open()
        self.assertEqual(c.generation, first + 1)
        c.note_open()
        self.assertEqual(c.generation, first + 2)

    def test_a_raising_handler_does_not_take_the_reader_down(self):
        """
        Mirrors the claim worker: a bug in one frame's handling must not
        strand the feed. It is logged and the next frame is read.
        """
        seen = []

        def boom(raw):
            seen.append(raw)
            raise ValueError("bad frame")

        c = self._conn(on_message=boom)
        c.dispatch("{}")
        c.dispatch("{}")
        self.assertEqual(len(seen), 2, "the second frame must still arrive")

    def test_a_frame_is_marked_even_when_the_handler_raises(self):
        """
        The frame proves the socket is alive regardless of whether we could
        make sense of its contents. Marking only on success would let a run
        of unparseable frames read as a dead connection.
        """
        c = self._conn(on_message=lambda _raw: (_ for _ in ()).throw(
            ValueError("bad")))
        c.last_frame_ts = 0.0
        c.dispatch("{}")
        self.assertGreater(c.last_frame_ts, 0.0)

    def test_send_on_a_closed_socket_reports_failure_rather_than_raising(self):
        c = self._conn()
        self.assertFalse(c.send('{"method":"SUBSCRIBE"}'))
```

- [ ] **Step 2: Run it to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestWsConnection -v
```

Expected: FAIL — `ws_feeds` has no attribute `WsConnection`.

- [ ] **Step 3: Implement**

Append to `ws_feeds.py`:

```python
import json
import logging
import re
import threading
import time
from collections.abc import Callable

import websocket

LOG = logging.getLogger("btc5m.ws")

# Handshake statuses that mean the credentials or the source address are
# wrong. 401 and 403 only: 429 and every 5xx are the venue having a moment,
# which is exactly what the backoff loop is for.
_AUTH_STATUS = re.compile(r"\b(401|403)\b")
_HANDSHAKE = re.compile(r"handshake status", re.I)


class WsAuthRefused(Exception):
    """The venue refused the credentials or the source address."""


def is_auth_refusal(exc: BaseException) -> bool:
    """
    Whether a failure means "your key is wrong", not "try again".

    websocket-client reports a rejected handshake as a generic exception
    carrying the status in its text, so the status has to be read back out.
    Narrow on purpose: a 503 in the same shape is transient, and treating it
    as an auth refusal would disable a working feed for the life of the
    process.
    """
    if isinstance(exc, WsAuthRefused):
        return True
    text = str(exc)
    return bool(_HANDSHAKE.search(text) and _AUTH_STATUS.search(text))


class WsConnection:
    """
    One supervised socket on one daemon thread.

    Deliberately ignorant of what it carries: it owns connect, keepalive,
    reconnect and recycle, and hands every frame to `on_message`. What the
    frames mean is the caller's problem, which is what lets the same class
    serve the public spot stream and the signed order-book stream.
    """

    def __init__(self, name: str,
                 url_factory: Callable[[], str],
                 on_message: Callable[[str], None],
                 cfg_source,
                 on_open: Callable[["WsConnection"], None] | None = None
                 ) -> None:
        self.name = name
        self._url_factory = url_factory
        self._on_message = on_message
        self._on_open = on_open
        # Either a Config or a ConfigStore, matching PredictionClient, so a
        # hot reload reaches the transport without rebuilding it. Which one
        # it is never needs asking: `_cfg` duck-types on `.current`.
        self._cfg_source = cfg_source
        self.last_frame_ts = 0.0
        self.opened_ts = 0.0
        self.disabled = False
        self.should_retry = True
        self.generation = 0
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._lock = threading.Lock()

    @property
    def _cfg(self):
        current = getattr(self._cfg_source, "current", None)
        return self._cfg_source if current is None else current

    # -- health -------------------------------------------------------------

    @property
    def healthy(self) -> bool:
        """
        Whether reads may trust this feed.

        Silence is the only signal, and it is measured on the connection.
        Per-topic timestamps cannot serve here: a book that is not changing
        sends nothing, so a quiet market and a dead socket look identical
        from inside a topic.
        """
        if self.disabled or self.last_frame_ts <= 0.0:
            return False
        return (time.time() - self.last_frame_ts) <= self._cfg.ws_stale_s

    @property
    def recycle_due(self) -> bool:
        if self.opened_ts <= 0.0:
            return False
        return (time.time() - self.opened_ts) >= self._cfg.ws_recycle_s

    def backoff_for(self, attempt: int) -> float:
        return min(self._cfg.ws_reconnect_max_s, 2.0 ** min(attempt, 6))

    def mark_frame(self) -> None:
        self.last_frame_ts = time.time()

    def note_open(self) -> None:
        self.opened_ts = time.time()
        self.generation += 1

    def note_failure(self, exc: BaseException) -> None:
        if is_auth_refusal(exc):
            self.disabled = True
            self.should_retry = False
            LOG.error("%s feed refused: %s. This does not heal on its own -- "
                      "the key is wrong or this worker's egress address is "
                      "not on the allowlist. Falling back to REST for the "
                      "life of the process; check the address preflight "
                      "printed at boot.", self.name, exc)
        else:
            self.should_retry = True
            LOG.warning("%s feed dropped (%s); reconnecting", self.name, exc)

    # -- frames -------------------------------------------------------------

    def dispatch(self, raw: str) -> None:
        """
        Hand one frame to the consumer, surviving whatever it does.

        The frame is marked BEFORE the handler runs, and marked regardless of
        the outcome: its arrival proves the socket is alive whether or not we
        could make sense of it. Marking only on success would let a run of
        unparseable frames read as a dead connection and send every price
        back to REST for no reason.
        """
        self.mark_frame()
        try:
            self._on_message(raw)
        except Exception:                    # noqa: BLE001
            # Same reasoning as the claim worker: a bug handling one frame
            # must not strand the feed. Loud, then carry on.
            LOG.exception("%s feed: handler failed on a frame", self.name)

    def send(self, payload: str) -> bool:
        """True if it went out. A closed socket is a False, never a raise."""
        ws = self._ws
        if ws is None:
            return False
        try:
            ws.send(payload)
            return True
        except Exception as exc:             # noqa: BLE001
            LOG.debug("%s feed: send failed (%s)", self.name, exc)
            return False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run_forever, name=f"ws-{self.name}", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:                # noqa: BLE001
                LOG.debug("%s feed: close failed", self.name)

    def _run_forever(self) -> None:
        attempt = 0
        while not self._stopping:
            if self.disabled:
                return
            try:
                self._connect_once()
                attempt = 0
            except Exception as exc:         # noqa: BLE001
                self.note_failure(exc)
                if not self.should_retry:
                    return
                attempt += 1
            finally:
                self.last_frame_ts = 0.0     # never serve across a reconnect
            if self._stopping or self.disabled:
                return
            time.sleep(self.backoff_for(max(attempt, 1)))

    def _connect_once(self) -> None:
        """One connection, from open to close. Returns when it drops."""
        opened = threading.Event()

        def _on_open(_ws):
            self.note_open()
            opened.set()
            if self._on_open is not None:
                try:
                    self._on_open(self)
                except Exception:            # noqa: BLE001
                    LOG.exception("%s feed: on_open failed", self.name)

        self._ws = websocket.WebSocketApp(
            self._url_factory(),
            on_open=_on_open,
            on_message=lambda _ws, raw: self.dispatch(raw),
            on_error=lambda _ws, exc: LOG.debug("%s feed error: %s",
                                                self.name, exc))
        # 30s ping is the venue's stated keepalive requirement on the signed
        # socket, and harmless on the public one.
        self._ws.run_forever(ping_interval=30, ping_timeout=10)
        if not opened.is_set():
            # run_forever returned without ever opening: the handshake was
            # refused, and its status is the only thing that says whether
            # retrying is pointless.
            raise WsAuthRefused(f"{self.name}: handshake never completed")
```

- [ ] **Step 4: Run the tests**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestWsConnection -v
```

Expected: 13 tests PASS.

- [ ] **Step 5: Run coherence on the corpus**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python coherence.py --source btc_5m_predictor.py --source ws_feeds.py
```

Expected: exit 0. If any helper is reported as dead, delete it rather than inventing a reference to it — a call added only to quiet the checker is exactly the residue `coherence.py` exists to find.

- [ ] **Step 6: Commit**

```bash
git add ws_feeds.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Hold a socket open, and know the difference between the two ways it dies

WsConnection owns connect, keepalive, backoff and the recycle ahead of
the venue's 24h close, and knows nothing about what it carries -- which
is what lets one class serve both the public stream and the signed one.

Two decisions carry the weight. Health is measured on the connection and
on nothing else, because a book that is not changing sends no messages
and a quiet market would otherwise be indistinguishable from a dead
socket. And a 401 is not a dropped connection: it means the key is wrong
or this worker's egress address is not allowlisted, neither of which
heals by trying again on a venue that rate-limits by IP.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: BookFeed — the signed order-book stream

One socket on the aggregated topic replaces 2N REST calls per tick. Two things carry the risk: the signed URL must be byte-identical to what `_signed_query` produces, and the market-level book must be mapped to per-side ask ladders correctly.

**Files:**
- Modify: `ws_feeds.py`
- Modify: `fuzz.py` (new invariant)
- Test: `test_btc_5m.py` (new class `TestBookFeed`)

**Interfaces:**
- Consumes: `WsConnection`, `WS_BOOK_VALIDATE_TOL`, `Config.ws_url`, `PredictionClient._signed_query`, `PredictionClient.asks_for`, `Round`, `Side`.
- Produces:
  - `ws_feeds.derive_asks(asks, bids, side) -> list[tuple[float, float]]`
  - `ws_feeds.parse_book_frame(raw: str) -> tuple[int, int, list, list] | None` returning `(market_id, update_ts_ms, asks, bids)`
  - `ws_feeds.BookFeed(client, cfg_source)` with `.start()`, `.stop()`, `.healthy`, `.asks(rnd, side) -> list[tuple[float, float]] | None`

- [ ] **Step 1: Write the failing test**

Append to `test_btc_5m.py`:

```python
class TestBookFeed(unittest.TestCase):
    """The signed order-book stream, and the mapping it has to prove."""

    def setUp(self):
        import ws_feeds
        self.ws_feeds = ws_feeds
        self.cfg = cfg()

    def _frame(self, market_id=8859231, ts=1717420800123,
               asks=None, bids=None):
        payload = {"msgType": "orderbook", "marketId": market_id,
                   "updateTimestampMs": ts,
                   "asks": asks if asks is not None else [["0.32", "500"],
                                                          ["0.33", "1200"]],
                   "bids": bids if bids is not None else [["0.31", "800"],
                                                          ["0.30", "2000"]]}
        return json.dumps({"type": "TOPIC",
                           "topic": f"web3_prediction_orderbook_{market_id}",
                           "data": json.dumps(payload)})

    # -- parsing ------------------------------------------------------------

    def test_the_payload_is_a_json_string_inside_the_envelope(self):
        """
        `data` is not a nested object, it is a string holding JSON. Parsing
        the envelope once and reading `data["asks"]` gets a TypeError on
        every frame.
        """
        parsed = self.ws_feeds.parse_book_frame(self._frame())
        self.assertIsNotNone(parsed)
        market_id, ts, asks, bids = parsed
        self.assertEqual(market_id, 8859231)
        self.assertEqual(ts, 1717420800123)
        self.assertEqual(asks[0], (0.32, 500.0))
        self.assertEqual(bids[0], (0.31, 800.0))

    def test_a_non_orderbook_frame_is_ignored(self):
        raw = json.dumps({"type": "PONG"})
        self.assertIsNone(self.ws_feeds.parse_book_frame(raw))

    def test_malformed_json_returns_none_rather_than_raising(self):
        self.assertIsNone(self.ws_feeds.parse_book_frame("not json{"))

    def test_unparseable_levels_are_dropped_not_fatal(self):
        parsed = self.ws_feeds.parse_book_frame(
            self._frame(asks=[["0.32", "500"], ["oops", "1"], ["1.5", "2"]]))
        _, _, asks, _ = parsed
        self.assertEqual(asks, [(0.32, 500.0)])

    # -- the mapping --------------------------------------------------------

    def test_the_near_side_asks_come_through_unchanged(self):
        asks = [(0.32, 500.0), (0.33, 1200.0)]
        bids = [(0.31, 800.0), (0.30, 2000.0)]
        self.assertEqual(
            self.ws_feeds.derive_asks(asks, bids, m.Side.UP), asks)

    def test_the_far_side_asks_are_the_bids_mirrored(self):
        """
        A share paying 1 on UP and a share paying 1 on DOWN sum to 1, so a
        bid of 0.31 for UP is an offer of 0.69 for DOWN. Getting this
        backwards prices every DOWN trade at its complement, which is why
        the mapping is checked against REST before it is trusted.
        """
        asks = [(0.32, 500.0), (0.33, 1200.0)]
        bids = [(0.31, 800.0), (0.30, 2000.0)]
        got = self.ws_feeds.derive_asks(asks, bids, m.Side.DOWN)
        self.assertEqual(got, [(0.69, 800.0), (0.70, 2000.0)])

    def test_the_far_side_is_sorted_ascending(self):
        bids = [(0.31, 800.0), (0.30, 2000.0), (0.29, 10.0)]
        got = self.ws_feeds.derive_asks([(0.32, 1.0)], bids, m.Side.DOWN)
        self.assertEqual(got, sorted(got))

    def test_deriving_twice_returns_the_original(self):
        asks = [(0.32, 500.0), (0.33, 1200.0)]
        bids = [(0.31, 800.0), (0.30, 2000.0)]
        once = self.ws_feeds.derive_asks(asks, bids, m.Side.DOWN)
        twice = self.ws_feeds.derive_asks(once, once, m.Side.DOWN)
        for (p1, s1), (p2, s2) in zip(sorted(bids), twice):
            self.assertAlmostEqual(p1, p2)
            self.assertAlmostEqual(s1, s2)

    # -- freshness and ordering --------------------------------------------

    def test_an_older_update_is_discarded(self):
        feed = self._feed()
        feed._conn.dispatch(self._frame(ts=2000, asks=[["0.40", "1"]]))
        feed._conn.dispatch(self._frame(ts=1000, asks=[["0.90", "1"]]))
        self.assertEqual(feed._books[8859231].asks[0][0], 0.40)

    def test_an_equal_timestamp_is_discarded(self):
        feed = self._feed()
        feed._conn.dispatch(self._frame(ts=2000, asks=[["0.40", "1"]]))
        feed._conn.dispatch(self._frame(ts=2000, asks=[["0.90", "1"]]))
        self.assertEqual(feed._books[8859231].asks[0][0], 0.40)

    def test_silence_beats_a_recent_per_market_timestamp(self):
        """
        THE trap. Every book in the cache can carry a timestamp from ten
        seconds ago and still be correct, because a quiet market sends
        nothing. Only the connection can say the feed is alive, and this
        test is what stops a future edit from "simplifying" the health check
        into a per-market one.
        """
        feed = self._feed()
        feed._conn.dispatch(self._frame(ts=int(time.time() * 1000)))
        self.assertTrue(feed.healthy)
        feed._conn.last_frame_ts = time.time() - (self.cfg.ws_stale_s + 1.0)
        self.assertFalse(
            feed.healthy,
            "a silent connection is unhealthy no matter how recent the "
            "per-market timestamps look")

    def test_a_reconnect_purges_every_cached_book(self):
        """
        The venue forbids carrying a book across a reconnect and requires a
        fresh REST snapshot. A book that survived the gap is a book with an
        unknown number of missed updates in it.
        """
        feed = self._feed()
        feed._conn.dispatch(self._frame())
        self.assertIn(8859231, feed._books)
        feed._conn.note_open()
        feed.on_reconnect(feed._conn)
        self.assertEqual(feed._books, {})
        self.assertEqual(feed._validated, set())

    # -- validation ---------------------------------------------------------

    def test_a_market_is_not_served_until_it_is_validated(self):
        feed = self._feed()
        feed._conn.dispatch(self._frame())
        rnd = self._round()
        feed._validated = set()
        feed._rejected = set()
        self.assertIsNone(feed.asks(rnd, m.Side.UP),
                          "an unvalidated market must fall through to REST")

    def test_a_matching_rest_ladder_validates_the_market(self):
        feed = self._feed(rest_asks={m.Side.UP: [(0.32, 500.0)],
                                     m.Side.DOWN: [(0.69, 800.0)]})
        feed._conn.dispatch(self._frame())
        rnd = self._round()
        self.assertTrue(feed.validate(rnd))
        self.assertIn(rnd.market_id, feed._validated)
        self.assertEqual(feed.asks(rnd, m.Side.UP)[0], (0.32, 500.0))

    def test_a_transposed_mapping_is_caught_and_pins_that_market_to_rest(self):
        feed = self._feed(rest_asks={m.Side.UP: [(0.32, 500.0)],
                                     m.Side.DOWN: [(0.31, 800.0)]})
        feed._conn.dispatch(self._frame())
        rnd = self._round()
        self.assertFalse(feed.validate(rnd))
        self.assertIn(rnd.market_id, feed._rejected)
        self.assertIsNone(feed.asks(rnd, m.Side.UP))

    def test_drift_inside_the_tolerance_still_validates(self):
        feed = self._feed(rest_asks={m.Side.UP: [(0.325, 500.0)],
                                     m.Side.DOWN: [(0.685, 800.0)]})
        feed._conn.dispatch(self._frame())
        self.assertTrue(feed.validate(self._round()))

    def test_one_rejected_market_does_not_pin_the_others(self):
        """
        Validation is per market, because the mapping could be right for one
        listing's shape and wrong for another's. A single disagreement must
        not cost every other market its stream.
        """
        feed = self._feed(rest_asks={m.Side.UP: [(0.32, 500.0)],
                                     m.Side.DOWN: [(0.69, 800.0)]})
        feed._conn.dispatch(self._frame(market_id=8859231))
        feed._conn.dispatch(self._frame(market_id=7000000))
        good = self._round()
        with feed._lock:
            feed._rejected.add(7000000)
        self.assertIsNotNone(feed.asks(good, m.Side.UP))
        self.assertIn(8859231, feed._validated)

    # -- the signed URL -----------------------------------------------------

    def test_the_socket_url_is_signed_by_the_same_code_as_every_request(self):
        """
        Binance recomputes the HMAC over the query string it RECEIVES, so
        the signed bytes and the sent bytes must be identical or it answers
        -1022. A second signing path for the socket is how that bug gets
        earned twice, so the URL has to come from _signed_query verbatim.
        """
        client = build_client(cfg())
        feed = self.ws_feeds.BookFeed(client, self.cfg)
        url = feed._url()
        base, _, query = url.partition("?")
        self.assertEqual(base, self.cfg.ws_book_url)
        self.assertIn(f"topic={self.ws_feeds.BookFeed.TOPIC}", query)
        self.assertIn("signature=", query)
        self.assertIn("random=", query)

    def test_the_socket_url_signature_verifies(self):
        """
        Recompute the HMAC over everything before `&signature=` and check it
        matches. This is the check that would have caught signing a sorted
        dict and then sending it in insertion order.
        """
        import hashlib as _hashlib
        import hmac as _hmac
        c = cfg()
        client = build_client(c)
        feed = self.ws_feeds.BookFeed(client, c)
        _, _, query = feed._url().partition("?")
        signed, _, signature = query.rpartition("&signature=")
        expected = _hmac.new(c.api_secret.encode(), signed.encode(),
                             _hashlib.sha256).hexdigest()
        self.assertEqual(signature, expected)

    def test_a_rejected_market_is_not_revalidated_every_tick(self):
        feed = self._feed(rest_asks={m.Side.UP: [(0.99, 1.0)],
                                     m.Side.DOWN: [(0.99, 1.0)]})
        feed._conn.dispatch(self._frame())
        rnd = self._round()
        feed.validate(rnd)
        calls = feed._client.calls
        feed.asks(rnd, m.Side.UP)
        feed.asks(rnd, m.Side.UP)
        self.assertEqual(feed._client.calls, calls,
                         "a rejected market must not re-probe REST on "
                         "every read")

    # -- helpers ------------------------------------------------------------

    def _round(self):
        return m.Round(
            topic_id=1, market_id=8859231, vendor="v", slug="btc-up-down",
            symbol="BTCUSDT", start_ms=0, end_ms=300_000,
            up_token_id="up", down_token_id="down", up_quote=0.5,
            down_quote=0.5, fee_bps=200, chain_id="1", collateral="USDT",
            venue_slippage_bps=100, decimal_precision=2, liquidity=1000.0,
            strike=50_000.0, feed_symbol="BTC/USD")

    def _feed(self, rest_asks=None):
        class FakeClient:
            calls = 0

            def __init__(self, table):
                self._table = table or {}

            def asks_for(self, rnd, side):
                type(self).calls += 1
                return self._table.get(side)

            def _signed_query(self, params):
                return "topic=x&signature=deadbeef"

        feed = self.ws_feeds.BookFeed(FakeClient(rest_asks), self.cfg)
        FakeClient.calls = 0
        return feed
```

`test_btc_5m.py` does **not** currently import `json`. Add it to the imports block, after `import math`:

```python
import json
```

`build_client` is already defined near the top of `test_btc_5m.py` and constructs a `PredictionClient` without opening a session — the two signing tests use it because they need the real `_signed_query`, not the fake.

- [ ] **Step 2: Run it to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestBookFeed -v
```

Expected: FAIL — `ws_feeds` has no attribute `parse_book_frame`.

- [ ] **Step 3: Implement parsing and the mapping**

Append to `ws_feeds.py`:

```python
def _levels(raw) -> list[tuple[float, float]]:
    """
    Parse ["<price>", "<size>"] pairs, dropping whatever will not parse.

    Same posture as PredictionClient._parse_asks: a malformed level is
    dropped rather than allowed to raise, because one bad entry in a frame
    must not cost the whole book. Prices are strictly inside (0, 1) -- a
    prediction share outside that range is not a price, it is a parse error
    wearing one.
    """
    out: list[tuple[float, float]] = []
    for lvl in raw or ():
        try:
            price, size = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
        if 0.0 < price < 1.0 and size > 0 and price == price:
            out.append((price, size))
    return sorted(out)


def parse_book_frame(raw: str
                     ) -> tuple[int, int, list, list] | None:
    """
    (market_id, update_ts_ms, asks, bids) from one push, or None.

    The envelope's `data` is a JSON STRING, not a nested object, so it needs
    a second parse. Reading it as an object gets a TypeError on every single
    frame, which is a whole feed lost to one wrong assumption.
    """
    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or envelope.get("type") != "TOPIC":
            return None
        body = json.loads(envelope.get("data") or "null")
        if not isinstance(body, dict) or body.get("msgType") != "orderbook":
            return None
        market_id = int(body["marketId"])
        ts = int(body["updateTimestampMs"])
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    return market_id, ts, _levels(body.get("asks")), _levels(body.get("bids"))


def derive_asks(asks: list[tuple[float, float]],
                bids: list[tuple[float, float]],
                side) -> list[tuple[float, float]]:
    """
    Per-side ask ladder from one market-level book.

    The push carries one book keyed on marketId with no tokenId, while
    asks_for is per-token, so one of the two sides has to be derived. UP and
    DOWN shares each pay 1 and are mutually exclusive, so their prices sum
    to 1: a bid of 0.31 for UP is an offer of 0.69 for DOWN.

    This is an inference. The venue documents the frame but not which token
    the book belongs to, which is exactly why BookFeed.validate checks it
    against REST once per market before any of it is allowed to price a
    trade.
    """
    from btc_5m_predictor import Side
    if side is Side.UP:
        return sorted(asks)
    return sorted((round(1.0 - price, 10), size) for price, size in bids)
```

- [ ] **Step 4: Implement the feed**

Append to `ws_feeds.py`:

```python
class _Book:
    """One market's last known ladder, with the stamp that orders updates."""

    __slots__ = ("asks", "bids", "ts")

    def __init__(self, asks, bids, ts) -> None:
        self.asks = asks
        self.bids = bids
        self.ts = ts


class BookFeed:
    """
    The prediction order book, on one socket for every market.

    Subscribes to the aggregated topic rather than one per market: N markets
    on one connection instead of N connections, and it means a newly listed
    round is already covered before the bot has discovered it.
    """

    TOPIC = "web3_prediction_orderbook_data"

    def __init__(self, client, cfg_source) -> None:
        self._client = client
        self._cfg_source = cfg_source
        self._books: dict[int, _Book] = {}
        self._validated: set[int] = set()
        self._rejected: set[int] = set()
        self._lock = threading.Lock()
        self._conn = WsConnection(
            name="book", url_factory=self._url, on_message=self._on_frame,
            cfg_source=cfg_source, on_open=self.on_reconnect)

    @property
    def _cfg(self):
        current = getattr(self._cfg_source, "current", None)
        return self._cfg_source if current is None else current

    def _url(self) -> str:
        """
        Signed socket URL, signed by the same code that signs every request.

        Reusing _signed_query is not tidiness. It exists because the signed
        bytes and the sent bytes have to be identical or the venue answers
        -1022, and a second signing path is how that bug gets earned twice.
        The pipe between topics percent-encodes to %7C; the signature is
        computed over the encoded string and the encoded string is what is
        sent, so the two agree.
        """
        query = self._client._signed_query({
            "random": f"{time.time_ns():x}", "topic": self.TOPIC})
        return f"{self._cfg.ws_url('book')}?{query}"

    def start(self) -> None:
        self._conn.start()

    def stop(self) -> None:
        self._conn.stop()

    @property
    def healthy(self) -> bool:
        return self._conn.healthy

    def on_reconnect(self, _conn) -> None:
        """
        Throw away everything the previous connection knew.

        The venue forbids caching a book across a reconnect and requires a
        fresh REST snapshot, and it is right to: a book that survived the gap
        carries an unknown number of missed updates and looks exactly like a
        book that did not. Validation goes with it, because the socket that
        proved the mapping is not this socket.
        """
        with self._lock:
            self._books.clear()
            self._validated.clear()
            self._rejected.clear()

    def _on_frame(self, raw: str) -> None:
        parsed = parse_book_frame(raw)
        if parsed is None:
            return
        market_id, ts, asks, bids = parsed
        with self._lock:
            known = self._books.get(market_id)
            # Strictly newer. The venue's delivery is at-most-once and out of
            # order, and an equal stamp is a duplicate, not an update.
            if known is not None and ts <= known.ts:
                return
            self._books[market_id] = _Book(asks, bids, ts)

    def validate(self, rnd) -> bool:
        """
        Prove the side mapping for one market against REST, once.

        Costs one REST call per market per connection, against the 2N per
        tick it replaces. Top of book only, and price only: depth and size
        move faster than the round trip and would reject a correct mapping.
        """
        from btc_5m_predictor import LOG as _LOG, Side, WS_BOOK_VALIDATE_TOL
        with self._lock:
            book = self._books.get(rnd.market_id)
            if book is None:
                return False
        for side in Side:
            rest = self._client.asks_for(rnd, side)
            derived = derive_asks(book.asks, book.bids, side)
            if not rest or not derived:
                return False
            if abs(rest[0][0] - derived[0][0]) > WS_BOOK_VALIDATE_TOL:
                with self._lock:
                    self._rejected.add(rnd.market_id)
                _LOG.error(
                    "Order-book stream disagrees with REST on %s %s: stream "
                    "%.4f, REST %.4f. The market-level book is not mapping "
                    "to per-token ladders the way this code assumes, so %s "
                    "stays on REST.", rnd.slug, side.name,
                    derived[0][0], rest[0][0], rnd.slug)
                return False
        with self._lock:
            self._validated.add(rnd.market_id)
        return True

    def asks(self, rnd, side) -> list[tuple[float, float]] | None:
        """
        The ask ladder, or None to say "ask REST".

        None covers three different situations on purpose -- unhealthy
        connection, unvalidated market, rejected market -- because the caller
        does the same thing in all three, and distinguishing them here would
        only invite a caller to treat one of them as fatal.
        """
        if not self._conn.healthy:
            return None
        with self._lock:
            if rnd.market_id in self._rejected:
                return None
            validated = rnd.market_id in self._validated
            book = self._books.get(rnd.market_id)
        if book is None:
            return None
        if not validated:
            if not self.validate(rnd):
                return None
        return derive_asks(book.asks, book.bids, side) or None
```

- [ ] **Step 5: Run the tests**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestBookFeed -v
```

Expected: 20 tests PASS.

- [ ] **Step 6: Add the fuzz invariant**

`fuzz.py` suites take `(rng, trials)` and report through the module-level `check(name, condition, detail)` helper rather than raising — a raised assertion would stop the run at the first counterexample instead of collecting them. Match that shape.

Add `import ws_feeds` to `fuzz.py`'s imports, then add this next to `fuzz_walk_book`:

```python
def fuzz_derived_ladder(rng: random.Random, trials: int) -> None:
    """
    A derived ladder must be well-formed, and the mirror its own inverse.

    Well-formedness alone would pass a mapping that quietly dropped a level
    or clamped a price into range. The round trip is what actually pins the
    transformation: mirroring twice has to land back where it started.
    """
    for _ in range(trials):
        prices = sorted({round(rng.uniform(0.01, 0.98), 4)
                         for _ in range(rng.randint(1, 8))})
        book_bids = [(p, round(rng.uniform(0.1, 5000.0), 4)) for p in prices]
        book_asks = [(min(0.99, p + 0.01), s) for p, s in book_bids]

        for side in m.Side:
            got = ws_feeds.derive_asks(book_asks, book_bids, side)
            check("derived ladder ascends", got == sorted(got), str(got))
            check("derived prices are in (0,1)",
                  all(0.0 < p < 1.0 for p, _ in got), str(got))
            check("derived sizes are positive",
                  all(s > 0 for _, s in got), str(got))

        once = ws_feeds.derive_asks(book_asks, book_bids, m.Side.DOWN)
        twice = ws_feeds.derive_asks(once, once, m.Side.DOWN)
        check("mirror is an involution",
              len(twice) == len(book_bids)
              and all(abs(p1 - p2) < 1e-9 and abs(s1 - s2) < 1e-9
                      for (p1, s1), (p2, s2) in zip(sorted(book_bids), twice)),
              f"{sorted(book_bids)} -> {twice}")
```

Register it in `main()`'s `suites` list, after `("order book walking", fuzz_walk_book),`:

```python
        ("derived ladder", fuzz_derived_ladder),
```

- [ ] **Step 7: Run the fuzzer**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python fuzz.py --trials 400
```

Expected: exit 0.

- [ ] **Step 8: Commit**

```bash
git add ws_feeds.py fuzz.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Take the whole order book on one socket, and prove the mapping first

The aggregated topic covers every market, so 2N REST calls per tick
become one connection -- and a newly listed round is already covered
before the bot has discovered it.

The push carries one book per marketId with no tokenId, while asks_for
is per-token, so one side has to be derived from the other. UP and DOWN
sum to 1, so a bid of 0.31 for UP is an offer of 0.69 for DOWN. The
venue documents the frame but never says which token the book belongs
to, so that is an inference, and an inference is not something to price
a trade off. Each market is checked against one REST fetch before its
stream is trusted; a market that disagrees stays on REST and says so.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: SpotFeed — price and the kline window

Public streams, no signing. Two subtleties: subscriptions are dynamic because `Config.symbols` defaults to empty (discover everything), and the 500-close volatility window is seeded from REST rather than accumulated, because accumulating it takes 8.3 hours.

**Files:**
- Modify: `ws_feeds.py`
- Test: `test_btc_5m.py` (new class `TestSpotFeed`)

**Interfaces:**
- Consumes: `WsConnection`, `Config.ws_url`, `Config.vol_lookback_min`, `PredictionClient.session`, `PredictionClient.spot_price`.
- Produces:
  - `ws_feeds.parse_spot_frame(raw: str) -> tuple[str, str, dict] | None` returning `(stream_kind, symbol, payload)` where `stream_kind` is `"trade"` or `"kline"`.
  - `ws_feeds.SpotFeed(client, cfg_source)` with `.start()`, `.stop()`, `.healthy`, `.track(symbols: Iterable[str])`, `.price(symbol) -> float | None`, `.closes(symbol) -> list[float] | None`

- [ ] **Step 1: Write the failing test**

Append to `test_btc_5m.py`:

```python
class TestSpotFeed(unittest.TestCase):
    """Public price and kline streams."""

    def setUp(self):
        import ws_feeds
        self.ws_feeds = ws_feeds
        self.cfg = replace(cfg(), vol_lookback_min=10, sigma_window_min=5)

    def _trade(self, symbol="BTCUSDT", price="50123.45"):
        return json.dumps({"stream": f"{symbol.lower()}@trade",
                           "data": {"e": "trade", "s": symbol,
                                    "p": price, "T": 1717420800123}})

    def _kline(self, symbol="BTCUSDT", close="50123.45", closed=True,
               open_time=1717420800000):
        return json.dumps({"stream": f"{symbol.lower()}@kline_1m",
                           "data": {"e": "kline", "s": symbol,
                                    "k": {"t": open_time, "c": close,
                                          "x": closed}}})

    def _feed(self, seed=None):
        class FakeClient:
            def __init__(self, closes):
                self._closes = closes
                self.seed_calls = 0

            def kline_closes(self, symbol, limit):
                self.seed_calls += 1
                return list(self._closes)

            def spot_price(self, symbol=None):
                return 1.0

        return self.ws_feeds.SpotFeed(
            FakeClient(seed if seed is not None else
                       [100.0 + i for i in range(10)]), self.cfg)

    # -- parsing ------------------------------------------------------------

    def test_a_trade_frame_yields_the_last_price(self):
        kind, symbol, payload = self.ws_feeds.parse_spot_frame(self._trade())
        self.assertEqual(kind, "trade")
        self.assertEqual(symbol, "BTCUSDT")
        self.assertAlmostEqual(float(payload["p"]), 50123.45)

    def test_a_kline_frame_is_recognised(self):
        kind, symbol, _ = self.ws_feeds.parse_spot_frame(self._kline())
        self.assertEqual(kind, "kline")
        self.assertEqual(symbol, "BTCUSDT")

    def test_a_control_reply_is_ignored(self):
        self.assertIsNone(self.ws_feeds.parse_spot_frame(
            json.dumps({"result": None, "id": 1})))

    def test_malformed_json_returns_none(self):
        self.assertIsNone(self.ws_feeds.parse_spot_frame("{{"))

    # -- price --------------------------------------------------------------

    def test_a_trade_updates_the_price(self):
        feed = self._feed()
        feed.track(["BTCUSDT"])
        feed._conn.dispatch(self._trade(price="50123.45"))
        self.assertAlmostEqual(feed.price("BTCUSDT"), 50123.45)

    def test_a_symbol_never_seen_has_no_price(self):
        feed = self._feed()
        feed.track(["BTCUSDT"])
        self.assertIsNone(feed.price("ETHUSDT"))

    def test_a_silent_connection_serves_no_price(self):
        feed = self._feed()
        feed.track(["BTCUSDT"])
        feed._conn.dispatch(self._trade())
        self.assertIsNotNone(feed.price("BTCUSDT"))
        feed._conn.last_frame_ts = time.time() - (self.cfg.ws_stale_s + 1.0)
        self.assertIsNone(feed.price("BTCUSDT"))

    # -- the kline window ---------------------------------------------------

    def test_the_window_is_seeded_from_rest_not_accumulated(self):
        """
        vol_lookback_min is 500 in production -- 8.3 hours of one-minute
        closes. A window built from the stream alone leaves the volatility
        estimate unusable for a third of a day after every start, which on a
        worker that redeploys on every push is most of the time.
        """
        feed = self._feed(seed=[float(i) for i in range(10)])
        feed.track(["BTCUSDT"])
        self.assertEqual(feed._client.seed_calls, 1)
        self.assertEqual(len(feed.closes("BTCUSDT")), 10)

    def test_a_closed_candle_appends(self):
        feed = self._feed(seed=[float(i) for i in range(10)])
        feed.track(["BTCUSDT"])
        feed._conn.dispatch(self._kline(close="999.0", closed=True,
                                        open_time=2_000_000))
        self.assertEqual(feed.closes("BTCUSDT")[-1], 999.0)

    def test_an_unclosed_candle_does_not_append(self):
        """
        An in-progress candle's close is the current price, not a close. It
        would be re-appended on every tick of the same minute and turn one
        minute into a hundred samples of itself, collapsing the measured
        volatility.
        """
        feed = self._feed(seed=[float(i) for i in range(10)])
        feed.track(["BTCUSDT"])
        before = list(feed.closes("BTCUSDT"))
        feed._conn.dispatch(self._kline(close="999.0", closed=False,
                                        open_time=2_000_000))
        self.assertEqual(feed.closes("BTCUSDT"), before)

    def test_the_same_candle_twice_appends_once(self):
        feed = self._feed(seed=[float(i) for i in range(10)])
        feed.track(["BTCUSDT"])
        feed._conn.dispatch(self._kline(close="999.0", open_time=2_000_000))
        feed._conn.dispatch(self._kline(close="999.0", open_time=2_000_000))
        self.assertEqual(feed.closes("BTCUSDT").count(999.0), 1)

    def test_the_window_is_bounded_by_the_lookback(self):
        feed = self._feed(seed=[float(i) for i in range(10)])
        feed.track(["BTCUSDT"])
        for n in range(20):
            feed._conn.dispatch(self._kline(close=str(1000.0 + n),
                                            open_time=2_000_000 + n * 60_000))
        self.assertEqual(len(feed.closes("BTCUSDT")),
                         self.cfg.vol_lookback_min)

    def test_a_reconnect_reseeds_rather_than_carrying_a_gap(self):
        """
        A drop of any length leaves a hole. An append-only window carries it
        forward invisibly, and every sigma computed off it is measuring a
        series with a jump in it that the market never made.
        """
        feed = self._feed(seed=[float(i) for i in range(10)])
        feed.track(["BTCUSDT"])
        self.assertEqual(feed._client.seed_calls, 1)
        feed.on_reconnect(feed._conn)
        self.assertEqual(feed._client.seed_calls, 2)

    # -- subscriptions ------------------------------------------------------

    def test_tracking_a_new_symbol_subscribes_to_both_streams(self):
        feed = self._feed()
        sent = []
        feed._conn.send = lambda payload: sent.append(payload) or True
        feed.track(["ETHUSDT"])
        self.assertEqual(len(sent), 1)
        body = json.loads(sent[0])
        self.assertEqual(body["method"], "SUBSCRIBE")
        self.assertIn("ethusdt@trade", body["params"])
        self.assertIn("ethusdt@kline_1m", body["params"])

    def test_tracking_the_same_symbol_twice_does_not_resubscribe(self):
        feed = self._feed()
        feed.track(["BTCUSDT"])
        sent = []
        feed._conn.send = lambda payload: sent.append(payload) or True
        feed.track(["BTCUSDT"])
        self.assertEqual(sent, [])

    def test_dropping_a_symbol_unsubscribes(self):
        feed = self._feed()
        feed.track(["BTCUSDT", "ETHUSDT"])
        sent = []
        feed._conn.send = lambda payload: sent.append(payload) or True
        feed.track(["BTCUSDT"])
        bodies = [json.loads(s) for s in sent]
        methods = {b["method"] for b in bodies}
        self.assertIn("UNSUBSCRIBE", methods)
        unsub = next(b for b in bodies if b["method"] == "UNSUBSCRIBE")
        self.assertIn("ethusdt@trade", unsub["params"])
```

- [ ] **Step 2: Run it to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestSpotFeed -v
```

Expected: FAIL — `ws_feeds` has no attribute `parse_spot_frame`.

- [ ] **Step 3: Add `kline_closes` to PredictionClient**

`VolatilityEstimator` currently fetches klines itself. The seed needs the same fetch from a second place, so it moves onto the client where both can reach it. In `btc_5m_predictor.py`, immediately after `PredictionClient.spot_price`:

```python
    def kline_closes(self, symbol: str, limit: int) -> list[float]:
        """
        Recent 1m closes, most recent last.

        Lives here rather than in the volatility estimator because two
        callers need it now: the estimator, and the socket feed seeding its
        window. One fetch, one parse, one place to fix.
        """
        r = self._session.get(
            BASE + "/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": limit},
            timeout=self._cfg.http_timeout_s)
        r.raise_for_status()
        return [float(k[4]) for k in r.json()]
```

- [ ] **Step 4: Implement the parser and feed**

Append to `ws_feeds.py`:

```python
def parse_spot_frame(raw: str) -> tuple[str, str, dict] | None:
    """
    (kind, symbol, payload) from one combined-stream frame, or None.

    Combined streams wrap everything as {"stream": ..., "data": ...}, and
    control replies to SUBSCRIBE arrive on the same socket as
    {"result": ..., "id": ...} with no stream at all.
    """
    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict):
            return None
        data = envelope.get("data")
        if not isinstance(data, dict):
            return None
        symbol = str(data.get("s") or "").upper()
        if not symbol:
            return None
        event = data.get("e")
    except ValueError:
        return None
    if event == "trade":
        return "trade", symbol, data
    if event == "kline":
        return "kline", symbol, data
    return None


class SpotFeed:
    """
    Public last price and the 1m close window, over one combined stream.

    Subscriptions are dynamic because Config.symbols defaults to empty,
    meaning "discover and trade every 5m market" -- the traded set is not
    known until market_list answers. The alternative, !miniTicker@arr,
    needs no subscription management but delivers the whole exchange, which
    is most of a megabyte a second of JSON on a half-CPU worker, nearly all
    of it for symbols this bot will never trade.
    """

    def __init__(self, client, cfg_source) -> None:
        self._client = client
        self._cfg_source = cfg_source
        self._prices: dict[str, float] = {}
        self._closes: dict[str, list[float]] = {}
        self._last_candle: dict[str, int] = {}
        self._tracked: set[str] = set()
        self._lock = threading.Lock()
        self._conn = WsConnection(
            name="spot", url_factory=self._url, on_message=self._on_frame,
            cfg_source=cfg_source, on_open=self.on_reconnect)

    @property
    def _cfg(self):
        current = getattr(self._cfg_source, "current", None)
        return self._cfg_source if current is None else current

    def _url(self) -> str:
        return self._cfg.ws_url("spot")

    def start(self) -> None:
        self._conn.start()

    def stop(self) -> None:
        self._conn.stop()

    @property
    def healthy(self) -> bool:
        return self._conn.healthy

    @staticmethod
    def _streams(symbol: str) -> list[str]:
        low = symbol.lower()
        # @trade, not @bookTicker: ticker/price is the last traded price, and
        # best bid/ask is a different quantity. Substituting it would change
        # what the model is fed without changing any line that reads it.
        return [f"{low}@trade", f"{low}@kline_1m"]

    def track(self, symbols) -> None:
        """Bring the subscription set in line with what is being traded."""
        wanted = {s.upper() for s in symbols if s}
        with self._lock:
            added = wanted - self._tracked
            dropped = self._tracked - wanted
            self._tracked = set(wanted)
        for symbol in sorted(added):
            self._seed(symbol)
        if added:
            self._control("SUBSCRIBE", added)
        if dropped:
            self._control("UNSUBSCRIBE", dropped)
            with self._lock:
                for symbol in dropped:
                    self._prices.pop(symbol, None)
                    self._closes.pop(symbol, None)
                    self._last_candle.pop(symbol, None)

    def _control(self, method: str, symbols) -> None:
        params = [s for symbol in sorted(symbols)
                  for s in self._streams(symbol)]
        self._conn.send(json.dumps({"method": method, "params": params,
                                    "id": int(time.time() * 1000) % 1_000_000}))

    def _seed(self, symbol: str) -> None:
        """
        Fill the close window from REST before the stream contributes.

        vol_lookback_min is 500 -- 8.3 hours of one-minute closes. Waiting
        for the stream to supply that would leave the volatility estimate
        unusable for a third of a day after every start, on a worker that
        restarts on every push.
        """
        try:
            closes = self._client.kline_closes(
                symbol, self._cfg.vol_lookback_min)
        except Exception as exc:             # noqa: BLE001
            # Not fatal: closes() returns None and the estimator falls back
            # to its own REST fetch, which is what it did before this feed
            # existed.
            LOG.warning("Could not seed the kline window for %s (%s); "
                        "volatility stays on REST until the next reconnect",
                        symbol, exc)
            return
        with self._lock:
            self._closes[symbol] = list(closes)[-self._cfg.vol_lookback_min:]
            self._last_candle[symbol] = 0

    def on_reconnect(self, _conn) -> None:
        """
        Resubscribe and re-seed. A gap is not a thing to append across.

        A drop of any length leaves a hole in the close window, and an
        append-only window carries it forward invisibly -- every sigma
        computed off it then describes a series containing a jump the market
        never made.
        """
        with self._lock:
            tracked = sorted(self._tracked)
        for symbol in tracked:
            self._seed(symbol)
        if tracked:
            self._control("SUBSCRIBE", tracked)

    def _on_frame(self, raw: str) -> None:
        parsed = parse_spot_frame(raw)
        if parsed is None:
            return
        kind, symbol, data = parsed
        if kind == "trade":
            try:
                price = float(data["p"])
            except (KeyError, TypeError, ValueError):
                return
            if price > 0:
                with self._lock:
                    self._prices[symbol] = price
            return
        candle = data.get("k") or {}
        try:
            # An in-progress candle's close is the current price, not a
            # close. Appending it would re-append the same minute on every
            # tick and collapse the measured volatility toward zero.
            if not candle.get("x"):
                return
            open_time = int(candle["t"])
            close = float(candle["c"])
        except (KeyError, TypeError, ValueError):
            return
        if close <= 0:
            return
        with self._lock:
            if self._last_candle.get(symbol) == open_time:
                return
            window = self._closes.setdefault(symbol, [])
            window.append(close)
            del window[:-self._cfg.vol_lookback_min]
            self._last_candle[symbol] = open_time

    def price(self, symbol: str) -> float | None:
        if not self._conn.healthy:
            return None
        with self._lock:
            return self._prices.get(symbol.upper())

    def closes(self, symbol: str) -> list[float] | None:
        if not self._conn.healthy:
            return None
        with self._lock:
            window = self._closes.get(symbol.upper())
            return list(window) if window else None
```

- [ ] **Step 5: Run the tests**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestSpotFeed -v
```

Expected: 16 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add ws_feeds.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Stream the price, and seed the window rather than waiting out the day

@trade, not @bookTicker: ticker/price is the last traded price and best
bid/ask is a different quantity, so substituting it would change what
the model is fed without changing a line that reads it.

The close window is seeded from one REST fetch and appended from the
stream, and re-seeded on every reconnect. vol_lookback_min is 500, so
building it from the stream alone leaves volatility unusable for 8.3
hours after every start, on a worker that restarts on every push -- and
appending across a reconnect would carry the gap forward invisibly, so
every sigma after it describes a jump the market never made.

Only closed candles append. An in-progress candle's close is the current
price, and re-appending it every tick turns one minute into a hundred
samples of itself.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: MarketData — the facade, and the switch-over

The seam. After this task the trading loop reads from the sockets, and REST is the fallback rather than the path.

**Files:**
- Modify: `ws_feeds.py`
- Modify: `btc_5m_predictor.py:2099-2125` (VolatilityEstimator), `btc_5m_predictor.py:5522-5524`, `btc_5m_predictor.py:5930-5932`, `Trader.__init__`, `preflight`
- Test: `test_btc_5m.py` (new class `TestMarketData`)

**Interfaces:**
- Consumes: `SpotFeed`, `BookFeed`, `Config.ws_enabled`, `PredictionClient`.
- Produces:
  - `ws_feeds.MarketData(client, cfg_source)` with `.start()`, `.stop()`, `.track(symbols)`, `.spot(symbol) -> float`, `.closes(symbol) -> list[float]`, `.asks(rnd, side) -> list | None`, `.status() -> dict[str, str]`

- [ ] **Step 1: Write the failing test**

Append to `test_btc_5m.py`:

```python
class TestMarketData(unittest.TestCase):
    """The one seam the trading code sees."""

    def setUp(self):
        import ws_feeds
        self.ws_feeds = ws_feeds

    class FakeClient:
        def __init__(self):
            self.spot_calls = 0
            self.kline_calls = 0
            self.asks_calls = 0

        def spot_price(self, symbol=None):
            self.spot_calls += 1
            return 111.0

        def kline_closes(self, symbol, limit):
            self.kline_calls += 1
            return [1.0, 2.0, 3.0]

        def asks_for(self, rnd, side):
            self.asks_calls += 1
            return [(0.55, 10.0)]

        def _signed_query(self, params):
            return "topic=x&signature=deadbeef"

    def _md(self, enabled=True):
        client = self.FakeClient()
        md = self.ws_feeds.MarketData(client, replace(cfg(),
                                                      ws_enabled=enabled))
        return md, client

    def test_disabled_means_every_read_is_rest(self):
        """
        ws_enabled=false must reproduce the behaviour the bot had before any
        of this existed. It is the rollback path, so it has to be exact.
        """
        md, client = self._md(enabled=False)
        md.start()
        self.assertEqual(md.spot("BTCUSDT"), 111.0)
        self.assertEqual(md.closes("BTCUSDT"), [1.0, 2.0, 3.0])
        self.assertEqual(md.asks(self._round(), m.Side.UP), [(0.55, 10.0)])
        self.assertEqual(client.spot_calls, 1)
        self.assertEqual(client.asks_calls, 1)

    def test_disabled_starts_no_threads(self):
        md, _ = self._md(enabled=False)
        before = threading.active_count()
        md.start()
        self.assertEqual(threading.active_count(), before)

    def test_an_unhealthy_feed_falls_back_to_rest(self):
        md, client = self._md()
        self.assertEqual(md.spot("BTCUSDT"), 111.0)
        self.assertEqual(client.spot_calls, 1)

    def test_a_healthy_feed_is_preferred_over_rest(self):
        md, client = self._md()
        md._spot._conn.mark_frame()
        with md._spot._lock:
            md._spot._prices["BTCUSDT"] = 222.0
        self.assertEqual(md.spot("BTCUSDT"), 222.0)
        self.assertEqual(client.spot_calls, 0)

    def test_a_healthy_feed_missing_this_symbol_still_falls_back(self):
        """
        A live connection is not evidence about a symbol it has never
        carried. Serving nothing here, or raising, would both be worse than
        making the REST call the bot would have made anyway.
        """
        md, client = self._md()
        md._spot._conn.mark_frame()
        self.assertEqual(md.spot("ETHUSDT"), 111.0)
        self.assertEqual(client.spot_calls, 1)

    def test_closes_prefers_the_window_when_it_is_healthy(self):
        md, client = self._md()
        md._spot._conn.mark_frame()
        with md._spot._lock:
            md._spot._closes["BTCUSDT"] = [7.0, 8.0, 9.0]
        self.assertEqual(md.closes("BTCUSDT"), [7.0, 8.0, 9.0])
        self.assertEqual(client.kline_calls, 0)

    def test_asks_falls_back_when_the_book_feed_says_none(self):
        md, client = self._md()
        self.assertEqual(md.asks(self._round(), m.Side.UP), [(0.55, 10.0)])
        self.assertEqual(client.asks_calls, 1)

    def test_when_the_socket_is_stale_and_rest_fails_too_there_is_no_book(self):
        """
        The end of the fallback chain. Nothing invents a ladder here: asks
        returns None, _maybe_enter skips the round, and no trade is priced
        off a book nobody could fetch.
        """
        class BlindClient(self.FakeClient):
            def asks_for(self, rnd, side):
                self.asks_calls += 1
                return None

        client = BlindClient()
        md = self.ws_feeds.MarketData(client, cfg())
        self.assertIsNone(md.asks(self._round(), m.Side.UP))
        self.assertEqual(client.asks_calls, 1)

    def test_status_names_every_feed(self):
        md, _ = self._md()
        status = md.status()
        self.assertEqual(set(status), {"spot", "book"})
        for value in status.values():
            self.assertIn(value, ("live", "connecting", "disabled", "off"))

    def test_status_says_off_when_disabled(self):
        md, _ = self._md(enabled=False)
        self.assertEqual(set(md.status().values()), {"off"})

    def _round(self):
        return m.Round(
            topic_id=1, market_id=8859231, vendor="v", slug="btc-up-down",
            symbol="BTCUSDT", start_ms=0, end_ms=300_000,
            up_token_id="up", down_token_id="down", up_quote=0.5,
            down_quote=0.5, fee_bps=200, chain_id="1", collateral="USDT",
            venue_slippage_bps=100, decimal_precision=2, liquidity=1000.0,
            strike=50_000.0, feed_symbol="BTC/USD")


class TestVolatilityReadsMarketData(unittest.TestCase):
    """The estimator must take its closes from the seam, not its own fetch."""

    def test_sigma_uses_the_closes_market_data_supplies(self):
        closes = [100.0 * (1.0 + 0.0001 * i) for i in range(120)]

        class FakeMarketData:
            calls = 0

            def closes(self, symbol):
                type(self).calls += 1
                return list(closes)

        md = FakeMarketData()
        est = m.VolatilityEstimator(replace(cfg(), vol_lookback_min=120,
                                            sigma_window_min=60), md)
        sigma = est.sigma_annual("BTCUSDT")
        self.assertGreater(sigma, 0.0)
        self.assertEqual(FakeMarketData.calls, 1)

    def test_too_few_closes_is_an_api_error_not_a_crash(self):
        class ThinMarketData:
            def closes(self, symbol):
                return [100.0, 100.1]

        est = m.VolatilityEstimator(cfg(), ThinMarketData())
        with self.assertRaises(m.ApiError):
            est.sigma_annual("BTCUSDT")
```

- [ ] **Step 2: Run it to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestMarketData test_btc_5m.TestVolatilityReadsMarketData -v
```

Expected: FAIL — `ws_feeds` has no attribute `MarketData`.

- [ ] **Step 3: Implement the facade**

Append to `ws_feeds.py`:

```python
class MarketData:
    """
    The only thing the trading code knows about any of this.

    Three methods, matching the three REST calls that used to sit in the
    poll loop, with the same signatures. Each prefers its socket and falls
    back to the call that was there before, which is what keeps the REST
    path continuously exercised instead of turning it into untested code
    that gets discovered broken at the moment it is first needed.
    """

    def __init__(self, client, cfg_source) -> None:
        self._client = client
        self._cfg_source = cfg_source
        self._spot = SpotFeed(client, cfg_source)
        self._book = BookFeed(client, cfg_source)

    @property
    def _cfg(self):
        current = getattr(self._cfg_source, "current", None)
        return self._cfg_source if current is None else current

    def start(self) -> None:
        """
        Bring the feeds up, if they are wanted.

        A failure here is never fatal. REST-only is exactly what this bot did
        before the sockets existed, and refusing to trade because an
        accelerator is unavailable is worse than trading at the old speed.
        """
        if not self._cfg.ws_enabled:
            LOG.info("WebSocket feeds are off; every read goes to REST")
            return
        self._spot.start()
        self._book.start()

    def stop(self) -> None:
        self._spot.stop()
        self._book.stop()

    def track(self, symbols) -> None:
        if self._cfg.ws_enabled:
            self._spot.track(symbols)

    def spot(self, symbol: str) -> float:
        if self._cfg.ws_enabled:
            price = self._spot.price(symbol)
            if price is not None:
                return price
        return self._client.spot_price(symbol)

    def closes(self, symbol: str) -> list[float]:
        if self._cfg.ws_enabled:
            window = self._spot.closes(symbol)
            if window:
                return window
        return self._client.kline_closes(symbol, self._cfg.vol_lookback_min)

    def asks(self, rnd, side) -> list[tuple[float, float]] | None:
        if self._cfg.ws_enabled:
            levels = self._book.asks(rnd, side)
            if levels:
                return levels
        return self._client.asks_for(rnd, side)

    def status(self) -> dict[str, str]:
        """One word per feed, for preflight and the log line at startup."""
        if not self._cfg.ws_enabled:
            return {"spot": "off", "book": "off"}
        out = {}
        for name, feed in (("spot", self._spot), ("book", self._book)):
            if feed._conn.disabled:
                out[name] = "disabled"
            elif feed.healthy:
                out[name] = "live"
            else:
                out[name] = "connecting"
        return out
```

- [ ] **Step 4: Move VolatilityEstimator onto the seam**

In `btc_5m_predictor.py`, change the constructor signature:

```python
    def __init__(self, cfg: Config | ConfigStore, market_data) -> None:
        self._store = None if isinstance(cfg, Config) else cfg
        self._static_cfg = cfg if isinstance(cfg, Config) else None
        self._market_data = market_data
```

(delete the `self._session = session` line) and replace the fetch inside `sigma_annual`:

```python
        r = self._session.get(
            BASE + "/api/v3/klines",
            params={"symbol": symbol, "interval": "1m",
                    "limit": self._cfg.vol_lookback_min},
            timeout=self._cfg.http_timeout_s)
        r.raise_for_status()
        closes = [float(k[4]) for k in r.json()]
        if len(closes) < 10:
            raise ApiError("insufficient kline history for volatility")
```

with:

```python
        # Through the seam rather than fetched here: the socket keeps a
        # window that is already current, and the REST fetch behind it is
        # the same one this used to make.
        closes = self._market_data.closes(symbol)
        if len(closes) < 10:
            raise ApiError("insufficient kline history for volatility")
```

- [ ] **Step 5: Wire the Trader**

In `Trader.__init__`, after `self._client` is set, add:

```python
        self._market_data = ws_feeds.MarketData(self._client,
                                                self._store or self._cfg)
```

and change the `VolatilityEstimator` construction to pass `self._market_data` instead of `self._client.session`.

Add `import ws_feeds` to the imports block in `btc_5m_predictor.py`, after `import requests`.

At `btc_5m_predictor.py:5522-5524`, replace:

```python
            symbol = self._client.market_symbol(rnd.feed_symbol)
            spot = self._client.spot_price(symbol)
```

with:

```python
            symbol = self._client.market_symbol(rnd.feed_symbol)
            spot = self._market_data.spot(symbol)
```

At `btc_5m_predictor.py:5930-5931`, make the same substitution.

Replace every `self._client.asks_for(` inside `Trader` with `self._market_data.asks(`. There are two sites, in `_maybe_enter` and `_maybe_scale_in`; leave `PredictionClient.asks_for` itself alone, and leave `BookFeed`'s calls to it alone — those are the fallback and the validation.

In `Trader.run`, immediately after `self._client.sync_clock()`, add:

```python
            self._market_data.start()
            status = self._market_data.status()
            LOG.info("Feeds: spot %s, book %s", status["spot"],
                     status["book"])
```

and inside the poll loop, immediately after the `self._settle_open()` call, add:

```python
                    # Subscriptions follow what is actually being traded.
                    # Config.symbols defaults to empty, so the set is not
                    # known until the venue has been asked.
                    self._market_data.track(
                        self._cfg.symbols or [p.rnd.symbol
                                              for p in self._positions.values()])
```

- [ ] **Step 5a: Migrate preflight's own estimator**

Found during execution, not during planning. `preflight` builds its own `VolatilityEstimator` at `btc_5m_predictor.py:6383` and the signature just changed under it. Replace:

```python
        est = VolatilityEstimator(cfg, client.session)
```

with:

```python
        est = VolatilityEstimator(cfg, ws_feeds.MarketData(client, cfg))
```

A `MarketData` with `ws_enabled` true starts no feeds until `.start()` is called, so this reads straight through to `client.kline_closes` -- which is the same REST fetch preflight was making before.

- [ ] **Step 5b: Migrate the four estimator test fakes**

Also found during execution. Four tests build the estimator with a fake `requests.Session` returning kline rows, and the second argument is no longer a session. `test_btc_5m.py:1625` passes `None` and only calls `_estimate_df`, so it is unaffected.

At `test_btc_5m.py:1326` (`test_separate_symbols_do_not_share_a_cache`), replace the `class S` fake and its use:

```python
        class Feed:
            def closes(self, symbol):
                seen.append(symbol)
                # Distinct, small-amplitude series so neither hits the
                # volatility ceiling and gets clamped to the same value.
                amp = 0.0005 if symbol == "BTCUSDT" else 0.0020
                return [100.0 * (1 + amp * (i % 2)) for i in range(30)]

        v = m.VolatilityEstimator(cfg(), Feed())
```

At `test_btc_5m.py:1951` (`test_malformed_closes_do_not_crash`):

```python
        closes = [float(v) for v in
                  ["100", "0", "101", "-5", "102", "103"] * 40]

        class Feed:
            def closes(self, symbol):
                return list(closes)

        est = m.VolatilityEstimator(cfg(), Feed())
```

At `test_btc_5m.py:1967` (`TestClampedSigmaGuard._est`) and `test_btc_5m.py:2114` (`TestRawSigmaDiagnostic._est`), both build a random walk into `rows`. Replace each `rows.append([0, 0, 0, 0, str(px)])` with `rows.append(px)` and each `class S` fake with:

```python
        class Feed:
            def closes(self, symbol):
                return list(rows)
        return m.VolatilityEstimator(cfg(), Feed())
```

Then run them:

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestSigmaCache test_btc_5m.TestClampedSigmaGuard test_btc_5m.TestRawSigmaDiagnostic test_btc_5m.TestTailEstimation -v
```

Expected: all PASS. Confirm the exact class names with `grep -n "^class Test" test_btc_5m.py` around those line numbers before running.

- [ ] **Step 6: Add the preflight check**

In `preflight`, after the existing `check("public spot", ...)` line:

```python
    md = ws_feeds.MarketData(client, cfg)
    md.start()
    # A moment for the handshakes. Not a health gate: REST-only is a
    # supported running mode, and PREFLIGHT_REQUIRED=1 must not start
    # refusing boots because an accelerator was slow.
    time.sleep(2.0)
    status = md.status()
    check("websocket feeds",
          lambda: f"spot {status['spot']}, book {status['book']}")
    md.stop()
```

- [ ] **Step 7: Run the tests**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestMarketData test_btc_5m.TestVolatilityReadsMarketData -v
```

Expected: 12 tests PASS.

- [ ] **Step 8: Run the tests that touch the changed call sites**

The `VolatilityEstimator` signature changed and the Trader now reads through the seam, so the existing suites that drive those paths have to still pass. `build_trader` already fakes `_vol` with a `SimpleNamespace`, so most are unaffected — but it does not set `_market_data`, and the two substituted call sites will now reach for it.

Add `_market_data` to `build_trader` in `test_btc_5m.py`, alongside the existing `_vol` fake:

```python
    t._market_data = types.SimpleNamespace(
        spot=lambda symbol: client.spot_price(symbol),
        closes=lambda symbol: [],
        asks=lambda rnd, side: client.asks_for(rnd, side),
        track=lambda symbols: None,
        start=lambda: None,
        stop=lambda: None,
        status=lambda: {"spot": "off", "book": "off"})
```

Then run the suites that exercise entry, scale-in and the loop:

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestMultiMarket test_btc_5m.TestScaleInSizing test_btc_5m.TestOnlyBufferScalesIn test_btc_5m.TestLastMinuteEntry test_btc_5m.TestStraddleEntry test_btc_5m.TestLoopSurvivesUnexpectedFailures -v
```

Expected: all PASS. If any fail with `AttributeError: _market_data`, the fake above is missing a method that site needs — add it rather than reverting the call site.

- [ ] **Step 9: Run coherence and the fuzzer**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python coherence.py --source btc_5m_predictor.py --source ws_feeds.py && BINANCE_API_KEY=build BINANCE_API_SECRET=build python fuzz.py --trials 400
```

Expected: both exit 0.

- [ ] **Step 10: Commit**

```bash
git add btc_5m_predictor.py ws_feeds.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Read the market through one seam, and let REST be what it falls back to

MarketData is the only thing the trading code learns about any of this:
three methods matching the three REST calls that used to sit in the poll
loop, with the same signatures. assess, the estimator and both entry
paths change in where they read, not in shape.

Each method prefers its socket and falls back to the call that was
already there, which is deliberate -- it keeps the REST path exercised
on every gap instead of letting it rot into untested code discovered
broken at the moment it is first needed.

ws_enabled=false takes the whole thing out of the picture and starts no
threads at all. That is the rollback, so it is tested for exactness
rather than for approximate equivalence.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Ship it

Documentation, the boot-time gate, and the deploy.

**Files:**
- Modify: `verify.sh` (byte-compile the new module)
- Modify: `README.md`
- Modify: `btc_5m_predictor.py` module docstring (REQUIREMENTS section)

**Interfaces:**
- Consumes: everything above.
- Produces: a deployable tree.

- [ ] **Step 1: Byte-compile the new module in the gate**

In `verify.sh`, replace:

```bash
run "byte-compile" "$PY" -m py_compile btc_5m_predictor.py
```

with:

```bash
if [ -f ws_feeds.py ]; then
  run "byte-compile" "$PY" -m py_compile btc_5m_predictor.py ws_feeds.py
else
  run "byte-compile" "$PY" -m py_compile btc_5m_predictor.py
fi
```

- [ ] **Step 2: Update the module docstring**

In `btc_5m_predictor.py`, replace:

```
REQUIREMENTS
------------
    pip install requests
```

with:

```
REQUIREMENTS
------------
    pip install requests websocket-client

FEEDS
-----
Order book, spot price and the volatility window arrive on persistent
sockets; see ws_feeds.py. Everything signed and mutating stays on REST.
Set `ws_enabled` to false in the config to go back to REST for everything,
which is what this did before the sockets existed.
```

- [ ] **Step 3: Document it in the README**

Add this section to `README.md`. Place it after the section describing the trading loop, and match the surrounding heading style — if the file underlines its headings with `---`, do that instead of `##`.

```markdown
## Feeds

Three reads used to sit inside the poll loop as blocking round trips: the
order book (twice per candidate round, every tick), spot price (once per
candidate round), and the 1m klines behind the volatility estimate. All
three now arrive on persistent sockets.

  * Order book -- one signed connection on the venue's aggregated topic,
    which carries every market. Replaces 2N REST calls per tick with one
    socket, and covers a newly listed round before the bot discovers it.
  * Spot price -- the public `@trade` stream. Not `@bookTicker`: the model
    was always fed the last traded price, and best bid/ask is a different
    quantity.
  * Klines -- the public `@kline_1m` stream, appended to a window seeded
    from one REST fetch. The window is 500 closes, so building it from the
    stream alone would leave volatility unusable for 8.3 hours after every
    restart.

Everything signed and mutating -- quotes, orders, redemptions, balances,
market discovery -- stays on REST. That is not a gap to be closed later;
request/response is the right shape for a call that moves money.

### How the bot decides a feed is dead

An order book that is not changing sends no messages. So a quiet market and
a dead socket look identical if you measure freshness per market: a socket
that died at 14:02 would keep handing 14:02 ladders to the sizing model and
report itself healthy the whole time.

Freshness is therefore measured on the CONNECTION -- time since any frame
arrived, plus pong receipt -- and that is the only thing that decides
socket-versus-REST. The per-market `updateTimestampMs` is used to throw away
out-of-order updates and for nothing else.

Every read falls back to the REST call that was already there. That keeps
the REST path exercised on every gap, rather than letting it rot into
untested code discovered broken the first time it is needed.

### The mapping the venue does not document

The order-book push carries one book per market with no token id, while the
REST endpoint is per token. One side therefore has to be derived from the
other: UP and DOWN each pay 1 and are mutually exclusive, so their prices
sum to 1, and a bid of 0.31 for UP is an offer of 0.69 for DOWN.

That is an inference, not a documented fact, so it is checked rather than
trusted. The first push for each market is compared against one REST fetch
of both sides; agreement inside 0.02 at top of book trusts that market's
stream from then on, and disagreement pins that market to REST and says so
in the log. One REST call per market, against the 2N per tick it replaces.

### Turning it off

Set `ws_enabled` to `false` in the config on the mounted disk. It resolves
through the config store, so the running bot drops to REST-only on its next
hot reload -- no redeploy, no restart, no interrupted round. That is the
rollback path, and it restores exactly the behaviour the bot had before any
of this existed.
```

- [ ] **Step 4: Run every safe local check**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m py_compile btc_5m_predictor.py ws_feeds.py && BINANCE_API_KEY=build BINANCE_API_SECRET=build python coherence.py --source btc_5m_predictor.py --source ws_feeds.py && BINANCE_API_KEY=build BINANCE_API_SECRET=build python fuzz.py --trials 400
```

Then every test class this plan created or touched:

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestDeploymentManifests test_btc_5m.TestCoherenceCorpus \
  test_btc_5m.TestWsConfig test_btc_5m.TestWsConnection \
  test_btc_5m.TestBookFeed test_btc_5m.TestSpotFeed \
  test_btc_5m.TestMarketData test_btc_5m.TestVolatilityReadsMarketData \
  test_btc_5m.TestNoSilentFailures
```

Expected: all PASS. `TestNoSilentFailures` is included deliberately — its AST meta-rules over the source catch things ordinary tests do not, such as an `except` that returns without logging or re-raising in the handler itself. The new module has several broad handlers and this is what checks each one explains itself.

**Not** `./verify.sh` — see Task 0 Step 4 for why it is unusable on this machine.

- [ ] **Step 5: Build the image, if the daemon is up**

```bash
docker build -t btc5m:ws .
```

Expected: success. The build runs `verify.sh` on Linux, where none of the Windows artifacts exist, so this is the real gate.

If `docker info` reports the daemon is unreachable, say so and stop rather than pushing blind. The Render build will run the same gate, but discovering a failure there means discovering it after the worker has already restarted.

- [ ] **Step 6: Commit and push**

`autoDeploy: true` — this push restarts the live worker. Push between rounds if the bot is holding.

```bash
git add verify.sh README.md btc_5m_predictor.py
git commit -m "$(cat <<'EOF'
Say in the docs what the bot now listens to rather than asks for

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
git push origin master
```

- [ ] **Step 7: Watch the first live round**

After the deploy, confirm in the Render logs:

1. `Feeds: spot live, book live` — or `connecting`, which should become live within seconds. `disabled` on the book feed means the egress address is not allowlisted; the preflight output above it prints the address.
2. No `Order-book stream disagrees with REST` lines. One of those means the side mapping is wrong for that market and it has correctly pinned itself to REST — the trade still prices correctly, but the derivation in `derive_asks` needs revisiting.
3. Entry latency: the gap between a round opening and an order landing should drop by roughly one round trip.

If anything looks wrong, set `ws_enabled: false` in the config on the mounted disk. The next hot reload drops to REST-only without a redeploy.

---

## Notes for whoever executes this

**Task 0 is not optional and not cosmetic.** `master` is red right now. Every later task's verification depends on the gate being green, and the Docker build runs the same suite.

**Two full-suite runs, both deliberate:** Task 0 Step 4 and Task 7 Step 4. Everywhere else, run only the named class. The suite takes 10+ minutes on this machine against 23s on Render.

**The one test that must never be "simplified":** `TestBookFeed.test_silence_beats_a_recent_per_market_timestamp`. It looks redundant next to the other freshness tests. It is the only thing standing between this design and a dead socket pricing live orders.
