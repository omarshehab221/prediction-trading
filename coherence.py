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


def load(path: str) -> tuple[str, ast.Module]:
    src = open(path, encoding="utf-8").read()
    return src, ast.parse(src)


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

    # Which are read anywhere as cfg.X / self._cfg.X / c.X?
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in declared:
            read.add(node.attr)
    # Fields consumed via **PROFILES entries count as used.
    for name in declared:
        if re.search(rf"\b{name}\s*=", src.split("PROFILES")[-1] if "PROFILES" in src else ""):
            read.add(name)

    internal = {"api_key", "api_secret", "endpoints", "profile_name"}
    for name in sorted(set(declared) - read - internal):
        f.error(f"Config.{name} is declared but never read -- dead setting")

    # Which strategy fields does no profile override?
    profiles = re.search(r"PROFILES:.*?\n\}", src, re.S)
    if not profiles:
        f.warn("could not locate PROFILES to audit defaults")
        return
    block = profiles.group(0)
    overridden = set(re.findall(r"(\w+)\s*=", block))

    # Only fields that shape strategy or risk need per-profile values;
    # plumbing legitimately shares one default.
    plumbing = re.compile(r"(recv_window|http_|poll_|db_path|sigma_window|"
                          r"vol_lookback|max_consecutive_errors|"
                          r"calibration_|tail_df_|round_seconds)")
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

    called: set[str] = set()
    for node in ast.walk(tree):
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
        f.error(f"L{line}: {name}() is defined but never called -- dead code")


# --------------------------------------------------------------------------
# 4. Instance attributes
# --------------------------------------------------------------------------


def check_attributes(src: str, tree: ast.Module, f: Findings) -> None:
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        assigned: dict[str, int] = {}
        read: set[str] = set()
        # Methods and class-level constants are "assigned" by definition;
        # counting self.method() as an unassigned attribute is noise.
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
                    and node.value.id == "self":
                if isinstance(node.ctx, ast.Store):
                    assigned.setdefault(node.attr, node.lineno)
                else:
                    read.add(node.attr)
        # An attribute may legitimately be read by code outside the class
        # (exception payloads, dataclass fields), so check module-wide.
        module_read = {n.attr for n in ast.walk(tree)
                       if isinstance(n, ast.Attribute)
                       and isinstance(n.ctx, ast.Load)}
        for name, line in sorted(assigned.items(), key=lambda kv: kv[1]):
            if name.startswith("__") or name in read or name in module_read:
                continue
            f.error(f"L{line}: {cls.name}.{name} is assigned but never "
                    f"read -- leftover state")
        for name in sorted(read - set(assigned)):
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
    live: set[str] = {"self", "cls"}
    for node in ast.walk(tree):
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
    module_name = _os.path.basename(SOURCE_PATH).removesuffix(".py")
    live.add(module_name)
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
            f.warn(f"L{line_no}: prose mentions '{name}', which no longer "
                   f"exists in the module")


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="btc_5m_predictor.py")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as failures")
    args = ap.parse_args()

    global SOURCE_PATH
    SOURCE_PATH = args.source
    src, tree = load(args.source)
    f = Findings()
    for check in (check_config, check_dead_functions, check_attributes,
                  check_cli, check_stale_prose, check_magic_numbers,
                  check_default_profile):
        check(src, tree, f)

    print(f"=== COHERENCE: {args.source} ===\n")
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
