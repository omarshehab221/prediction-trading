#!/usr/bin/env python3
"""
coherence.py -- is the project internally consistent, or does it still carry
artifacts from an earlier stage?

WHY THIS EXISTS
---------------
This project changed shape several times: longshots, then favourites, then
buffers. Each pivot left residue -- a config field nobody reads, a default
tuned for a strategy that is no longer the default, a comment describing an
older design, an attribute assigned in __init__ and never used again. Every
one of those is invisible to the tests, because tests check behaviour that
exists, not behaviour that was left behind.

This finds them mechanically:

  1. config fields declared but never read
  2. config fields no profile overrides (the default silently governs)
  3. functions and methods defined but never called
  4. instance attributes assigned but never read, or read but never assigned
  5. CLI arguments parsed but never used
  6. comments and docstrings naming identifiers that no longer exist
  7. duplicated numeric constants that should be one source of truth

    python3 coherence.py [--source btc_5m_predictor.py] [--strict]

Exit code 0 when clean (warnings alone do not fail unless --strict).
"""

from __future__ import annotations

import argparse
import ast
import os
import re
from dataclasses import dataclass, field


@dataclass
class Findings:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


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
CORPUS_MODULES: set[str] = set()
MULTI_FILE = False


ROOT = os.path.dirname(os.path.abspath(__file__))


def package_sources(root: str = ROOT) -> list[str]:
    """
    Every file the bot itself is made of, the facade first.

    The facade leads because the checks that read the deployment manifests
    resolve them next to the first source, and the facade sits at the root.
    """
    out = [os.path.join(root, "btc_5m_predictor.py")]
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, "btc5m")):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        out += [os.path.join(dirpath, name)
                for name in sorted(filenames) if name.endswith(".py")]
    return [path for path in out if os.path.exists(path)]


def bot_sources(root: str = ROOT) -> list[str]:
    """The bot, plus the transport module it reads its feeds through."""
    out = package_sources(root)
    feeds = os.path.join(root, "ws_feeds.py")
    if os.path.exists(feeds):
        out.append(feeds)
    return out


def load(path: str) -> tuple[str, ast.Module]:
    src = open(path, encoding="utf-8").read()
    return src, ast.parse(src)


def set_corpus(paths: list[str]) -> list[tuple[str, str, ast.Module]]:
    """Load every path, build the merged corpus, and return the parts."""
    global CORPUS_SRC, CORPUS_TREE, CORPUS_MODULES, MULTI_FILE
    loaded = [(p, *load(p)) for p in paths]
    CORPUS_SRC = "\n".join(src for _, src, _ in loaded)
    CORPUS_TREE = ast.Module(
        body=[node for _, _, tree in loaded for node in tree.body],
        type_ignores=[])
    # Each module's own name, so prose in one may name another. Without this
    # ws_feeds.py's docstring pointing at btc_5m_predictor reads as a mention
    # of something that no longer exists.
    CORPUS_MODULES = {os.path.basename(p).removesuffix(".py") for p in paths}
    MULTI_FILE = len(loaded) > 1
    return loaded


def where(msg: str) -> str:
    """Prefix a finding with its file, but only when that is ambiguous."""
    if not MULTI_FILE:
        return msg
    return f"{os.path.basename(SOURCE_PATH)}: {msg}"


def class_def(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


# --------------------------------------------------------------------------
# 1 + 2. Config fields
# --------------------------------------------------------------------------


def check_config(src: str, tree: ast.Module, f: Findings) -> None:
    cfg = class_def(tree, "Config")
    if cfg is None:
        f.error("no Config class found")
        return

    declared: dict[str, str] = {}
    for node in cfg.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            declared[node.target.id] = (ast.unparse(node.value)
                                        if node.value else "?")

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

    internal = {"api_key", "api_secret", "endpoints", "profile_name"}
    for name in sorted(set(declared) - read - internal):
        f.error(where(f"Config.{name} is declared but never read "
                      f"-- dead setting"))

    # Which strategy fields does no profile override?
    profiles = re.search(r"^PROFILES:.*?\n\}", src, re.S | re.M)
    if not profiles:
        f.warn("could not locate PROFILES to audit defaults")
        return
    block = profiles.group(0)
    # Profiles may write kwarg style (dict(key=value)) or dict-literal style
    # ("key": value) -- both are matched so a pure style change never counts
    # as every field going unoverridden.
    overridden = (set(re.findall(r"(\w+)\s*=", block))
                  | set(re.findall(r'"(\w+)"\s*:', block)))

    # Only fields that shape strategy or risk need per-profile values;
    # plumbing legitimately shares one default.
    plumbing = re.compile(r"(recv_window|http_|poll_|db_path|sigma_window|"
                          r"vol_lookback|max_consecutive_errors|"
                          r"calibration_|tail_df_|round_seconds|ws_)")
    strategyish = re.compile(
        r"(edge|price|stake|buffer|loss|spread|impact|kelly|"
        r"liquidity|rounds_per_day|bankroll|scale_in|consecutive_losses)")
    for name in sorted(declared):
        if name in internal or plumbing.search(name):
            continue
        if not strategyish.search(name):
            continue
        if name not in overridden:
            f.warn(f"Config.{name} = {declared[name]} is never set by any "
                   f"profile; the default governs every strategy")


# --------------------------------------------------------------------------
# 3. Dead functions
# --------------------------------------------------------------------------


def check_dead_functions(src: str, tree: ast.Module, f: Findings) -> None:
    defined: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("__"):
                continue
            defined[node.name] = node.lineno

    # Asked of the corpus: a helper defined here and called from the other
    # module is called.
    called: set[str] = set()
    for node in ast.walk(CORPUS_TREE):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)
        elif isinstance(node, ast.Attribute):
            called.add(node.attr)      # method passed as a value
        elif isinstance(node, ast.Name):
            called.add(node.id)

    entry = {"main", "preflight", "discover_min", "selftest"}
    for name, line in sorted(defined.items(), key=lambda kv: kv[1]):
        if name in entry or name in called:
            continue
        f.error(where(f"L{line}: {name}() is defined but never called "
                      f"-- dead code"))


# --------------------------------------------------------------------------
# 4. Instance attributes
# --------------------------------------------------------------------------


def class_families(tree: ast.Module) -> dict[str, set[str]]:
    """
    Class name -> every class in the corpus that shares an instance with it.

    A mixin's methods run on the instance of the class that mixes them in, so
    an attribute that class assigns IS assigned as far as the mixin is
    concerned, and a sibling mixin's method IS a method of the same object.
    Without this, splitting a class across files reports every attribute it
    uses as one it never sets.
    """
    bases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases[node.name] = {b.id for b in node.bases
                                if isinstance(b, ast.Name)}

    def ancestors(name: str, seen: set[str] | None = None) -> set[str]:
        seen = set() if seen is None else seen
        if name in seen:
            return seen
        seen.add(name)
        for base in bases.get(name, ()):
            if base in bases:
                ancestors(base, seen)
        return seen

    lineage = {name: ancestors(name) for name in bases}
    return {name: set().union(*(lineage[d] for d in bases if name in lineage[d]))
            for name in bases}


def _assigned_in(cls: ast.ClassDef) -> dict[str, int]:
    """Attribute -> the line that first assigns it, methods included."""
    assigned: dict[str, int] = {}
    for item in cls.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assigned.setdefault(item.name, item.lineno)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            assigned.setdefault(item.target.id, item.lineno)
        elif isinstance(item, ast.Assign):
            for t in item.targets:
                if isinstance(t, ast.Name):
                    assigned.setdefault(t.id, item.lineno)
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "self" and isinstance(node.ctx, ast.Store):
            assigned.setdefault(node.attr, node.lineno)
    return assigned


def check_attributes(src: str, tree: ast.Module, f: Findings) -> None:
    families = class_families(CORPUS_TREE)
    corpus_assigned = {c.name: _assigned_in(c) for c in ast.walk(CORPUS_TREE)
                       if isinstance(c, ast.ClassDef)}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        assigned = _assigned_in(cls)
        read: set[str] = set()
        for node in ast.walk(cls):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "self" \
                    and not isinstance(node.ctx, ast.Store):
                read.add(node.attr)
        module_read = {n.attr for n in ast.walk(CORPUS_TREE)
                       if isinstance(n, ast.Attribute)
                       and isinstance(n.ctx, ast.Load)}
        for name, line in sorted(assigned.items(), key=lambda kv: kv[1]):
            if name.startswith("__") or name in read or name in module_read:
                continue
            f.error(where(f"L{line}: {cls.name}.{name} is assigned but never "
                          f"read -- leftover state"))
        family = set().union(*(set(corpus_assigned.get(c, {}))
                               for c in families.get(cls.name, {cls.name})))
        for name in sorted(read - set(assigned) - family):
            if name.startswith("_") and not hasattr(object, name):
                # Reading state the constructor never sets is how a test that
                # bypasses __init__ blows up at runtime.
                f.warn(f"{cls.name}.{name} is read but never assigned in "
                       f"this class")


# --------------------------------------------------------------------------
# 5. CLI arguments
# --------------------------------------------------------------------------


def check_cli(src: str, tree: ast.Module, f: Findings) -> None:
    # An option with an explicit dest is consumed under that name, so record
    # the dest rather than the flag; otherwise every --no-x reads as dead.
    declared = set()
    for hit in re.finditer(
            r'add_argument\(\s*"--([a-z0-9-]+)"(?P<rest>[^)]*)\)', src):
        dest = re.search(r'dest="(\w+)"', hit.group("rest"))
        declared.add(dest.group(1) if dest
                     else hit.group(1).replace("-", "_"))

    used = {m.group(1) for m in re.finditer(r"\bargs\.(\w+)", src)}
    for name in sorted(declared - used):
        f.error(f"--{name.replace('_', '-')} is parsed but never used")


# --------------------------------------------------------------------------
# 6. Stale identifiers in prose
# --------------------------------------------------------------------------


def check_stale_prose(src: str, tree: ast.Module, f: Findings) -> None:
    # Asked of the corpus: prose in one module may legitimately name a class
    # or field defined in the other.
    live: set[str] = {"self", "cls"}
    for node in ast.walk(CORPUS_TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            live.add(node.name)
        elif isinstance(node, ast.Name):
            live.add(node.id)
        elif isinstance(node, ast.Attribute):
            live.add(node.attr)
        elif isinstance(node, ast.arg):
            live.add(node.arg)

    # Identifiers mentioned in comments/docstrings that look like code but
    # no longer exist anywhere in the module.
    prose: list[tuple[int, str]] = []
    for i, line in enumerate(src.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            prose.append((i, stripped))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            doc = ast.get_docstring(node)
            if doc:
                prose.append((getattr(node, "lineno", 0), doc))

    import os as _os
    live.add(_os.path.basename(SOURCE_PATH).removesuffix(".py"))
    live |= CORPUS_MODULES
    pattern = re.compile(r"\b([a-z_][a-z0-9_]{4,})\b")
    english = re.compile(
        r"^(should|which|would|because|through|therefore|instead|already|"
        r"whether|between|against|without|another|matters|nothing|itself|"
        r"cannot|simply|rather|leaves|behind|really|change|changes|number|"
        r"numbers|values|value|price|prices|market|markets|trade|trades|"
        r"trading|orders|order|round|rounds|profile|profiles|balance|"
        r"account|accounts|wallet|venue|amount|amounts|result|results|"
        r"sample|samples|window|windows|strike|buffer|buffers|signal|"
        r"signals|little|larger|bigger|smaller|slower|faster|nearly|"
        r"always|during|before|after|enough|fields|field|source|target)$")
    for line_no, text in prose:
        for name in set(pattern.findall(text)):
            if name in live or english.match(name):
                continue
            if "_" not in name:
                continue        # only flag snake_case, which reads as code
            f.warn(where(f"L{line_no}: prose mentions '{name}', which no "
                         f"longer exists in the project"))


# --------------------------------------------------------------------------
# 7. Duplicated magic numbers
# --------------------------------------------------------------------------


def check_magic_numbers(src: str, tree: ast.Module, f: Findings) -> None:
    counts: dict[float, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            v = float(node.value)
            if v in (0, 1, 2, 0.0, 1.0, 2.0, -1, 100, 10, 3, 4, 5):
                continue
            counts.setdefault(v, []).append(node.lineno)
    for value, lines in sorted(counts.items()):
        if len(lines) >= 4:
            f.warn(f"literal {value} appears {len(lines)} times "
                   f"(lines {lines[:6]}...) -- consider one named constant")


def check_default_profile(src: str, tree: ast.Module, f: Findings) -> None:
    """
    The default profile is declared in Python; every manifest must agree.

    Deployment files cannot import the module, so each carries a literal.
    Without this check, changing DEFAULT_PROFILE leaves the Dockerfile and
    render.yaml quietly seeding a different strategy on first boot -- and
    since the config is written only once, that wrong default persists for
    the life of the disk.
    """
    hit = re.search(r'^DEFAULT_PROFILE\s*=\s*"(\w+)"', src, re.M)
    if not hit:
        f.error("DEFAULT_PROFILE is not declared in the module")
        return
    expected = hit.group(1)

    here = os.path.dirname(os.path.abspath(SOURCE_PATH)) or "."
    manifests = {
        "Dockerfile": r"ENV\s+PROFILE=(\w+)",
        "render.yaml": r"key:\s*PROFILE\s*\n\s*value:\s*(\w+)",
    }
    for name, pattern in manifests.items():
        path = os.path.join(here, name)
        if not os.path.exists(path):
            f.warn(f"{name} not found; cannot verify its default profile")
            continue
        found = re.search(pattern, open(path, encoding="utf-8").read())
        if not found:
            f.warn(f"{name} declares no PROFILE")
        elif found.group(1) != expected:
            f.error(f"{name} seeds profile {found.group(1)!r} but "
                    f"DEFAULT_PROFILE is {expected!r}")

    entry = os.path.join(here, "entrypoint.sh")
    if os.path.exists(entry):
        text = open(entry, encoding="utf-8").read()
        literal = re.search(r'PROFILE="\$\{PROFILE:-(\w+)\}"', text)
        if literal and literal.group(1) != expected:
            f.error(f"entrypoint.sh hardcodes profile {literal.group(1)!r} "
                    f"but DEFAULT_PROFILE is {expected!r}")


def check_mode_flags(src: str, tree: ast.Module, f: Findings) -> None:
    """
    The entrypoint must not hardcode --live or --paper.

    A command-line flag beats the TRADING_MODE environment variable, so a
    hardcoded flag makes that variable dead: a manifest can read
    TRADING_MODE=paper while the process trades real money.
    """
    here = os.path.dirname(os.path.abspath(SOURCE_PATH)) or "."
    entry = os.path.join(here, "entrypoint.sh")
    if not os.path.exists(entry):
        return
    text = open(entry, encoding="utf-8").read()
    # Only the exec line matters; a mention in a comment is fine.
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"(?<![\w-])--(live|paper)(?![\w-])", stripped):
            f.error(f"entrypoint.sh hardcodes a mode flag ({stripped[:60]}); "
                    f"this silently overrides TRADING_MODE")


def analyse(sources: list[str]) -> Findings:
    """Run every check over a corpus and return what it found."""
    global SOURCE_PATH
    loaded = set_corpus(sources)
    f = Findings()

    for path, src, tree in loaded:
        SOURCE_PATH = path
        for check in (check_dead_functions, check_attributes,
                      check_stale_prose, check_magic_numbers):
            check(src, tree, f)

    # These four describe the bot as a whole -- its one Config, its one
    # argument parser, its one profile table, its one default profile -- so
    # they read the merged corpus. They used to read the first file only,
    # which was the same thing while that file was the whole bot. With the bot
    # in a package it would mean checking a facade that declares none of them
    # and reporting nothing, which is worse than not checking at all.
    SOURCE_PATH = sources[0]
    for check in (check_config, check_cli, check_default_profile,
                  check_mode_flags):
        check(CORPUS_SRC, CORPUS_TREE, f)
    return f


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", action="append", default=None,
                    help="analyse this file; repeat for a multi-file corpus")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as failures")
    args = ap.parse_args()

    sources = args.source or bot_sources()
    f = analyse(sources)

    print(f"=== COHERENCE: {', '.join(os.path.basename(s) for s in sources)} "
          f"===\n")
    if f.errors:
        print(f"  {len(f.errors)} ERROR(S) -- stale artifacts:\n")
        for e in f.errors:
            print(f"    {e}")
        print()
    if f.warnings:
        print(f"  {len(f.warnings)} warning(s):\n")
        for w in f.warnings:
            print(f"    {w}")
        print()
    if not f.errors and not f.warnings:
        print("  No stale artifacts found.")
    elif not f.errors:
        print("  No errors; warnings above are judgement calls.")

    if f.errors or (args.strict and f.warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
