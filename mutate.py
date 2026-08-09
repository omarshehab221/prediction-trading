#!/usr/bin/env python3
"""
mutate.py -- mutation testing.

Injects small, plausible bugs into the source and runs the test suite against
each. A mutation that SURVIVES (tests still pass) marks a line the suite does
not really check. This measures test quality objectively rather than relying on
my opinion of my own tests.

    python3 mutate.py [--source btc_5m_predictor.py] [--limit N]
"""

from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

# (description, pattern, replacement). Chosen to mimic real mistakes:
# off-by-one, flipped comparison, dropped guard, wrong constant.
MUTATIONS: list[tuple[str, str, str]] = [
    ("comparison >= -> >", r"(?<![<>=!])>=(?!=)", ">"),
    ("comparison <= -> <", r"(?<![<>=!])<=(?!=)", "<"),
    ("comparison < -> <=", r"(?<![<>=!])<(?![=<])", "<="),
    ("comparison > -> >=", r"(?<![<>=!-])>(?![=>])", ">="),
    ("equality == -> !=", r"==", "!="),
    ("boolean and -> or", r"\band\b", "or"),
    ("boolean or -> and", r"\bor\b", "and"),
    ("negation removed", r"\bnot\b ", ""),
    ("addition -> subtraction", r"(?<![+=<>!*/-])\+(?![=+])", "-"),
    ("subtraction -> addition", r"(?<![+=<>!*/-])-(?![=>-])", "+"),
    ("multiply -> divide", r"(?<![*/])\*(?![*=])", "/"),
    ("divide -> multiply", r"(?<![*/])/(?![/=])", "*"),
    ("constant 0.0 -> 1.0", r"\b0\.0\b", "1.0"),
    ("constant 1.0 -> 0.0", r"\b1\.0\b", "0.0"),
    ("constant 2.0 -> 1.0", r"\b2\.0\b", "1.0"),
    ("return None -> return 0.0", r"return None", "return 0.0"),
    ("True -> False", r"\bTrue\b", "False"),
    ("False -> True", r"\bFalse\b", "True"),
]

# Lines we do not mutate: docstrings, comments, imports, logging.
SKIP_LINE = re.compile(
    r"^\s*(#|\"\"\"|'''|import |from |LOG\.|print\(|@|\.\.\.)")


def candidate_sites(lines: list[str]) -> list[tuple[int, str, str, str]]:
    """Every (line_no, description, pattern, replacement) we could apply."""
    sites = []
    in_doc = False
    for i, line in enumerate(lines):
        triple = line.count('"""') + line.count("'''")
        if in_doc:
            if triple % 2 == 1:
                in_doc = False
            continue
        if triple % 2 == 1:
            in_doc = True
            continue
        if SKIP_LINE.match(line) or not line.strip():
            continue
        code = line.split("#")[0]
        for desc, pattern, repl in MUTATIONS:
            if re.search(pattern, code):
                sites.append((i, desc, pattern, repl))
    return sites


def run_suite(workdir: str, timeout: int = 120) -> bool:
    """True if the test suite passes."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "unittest", "test_btc_5m"],
            cwd=workdir, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "BINANCE_API_KEY": "d",
                 "BINANCE_API_SECRET": "d", "PYTHONDONTWRITEBYTECODE": "1"})
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False        # a hang is a caught mutation, not a survivor


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="btc_5m_predictor.py")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--seed", type=int, default=17)
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(args.source)) or "."
    original = open(args.source, encoding="utf-8").read()
    lines = original.split("\n")

    sites = candidate_sites(lines)
    rng = random.Random(args.seed)
    rng.shuffle(sites)
    sites = sites[:args.limit]
    print(f"Testing {len(sites)} mutation(s)\n")

    workdir = tempfile.mkdtemp(prefix="mutate.")
    for name in ("btc_5m_predictor.py", "test_btc_5m.py", "conformance.py"):
        src = os.path.join(here, name)
        if os.path.exists(src):
            shutil.copy(src, workdir)

    baseline = run_suite(workdir)
    if not baseline:
        print("Baseline suite FAILS; fix that before mutating.")
        return 2

    survived: list[tuple[int, str, str]] = []
    killed = 0
    target = os.path.join(workdir, os.path.basename(args.source))

    for n, (idx, desc, pattern, repl) in enumerate(sites, 1):
        mutated = list(lines)
        code = mutated[idx].split("#")[0]
        mutated[idx] = re.sub(pattern, repl, code, count=1)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("\n".join(mutated))

        try:
            compile("\n".join(mutated), target, "exec")
        except SyntaxError:
            continue                       # not a valid mutation

        if run_suite(workdir):
            survived.append((idx + 1, desc, lines[idx].strip()))
            print(f"  [{n}/{len(sites)}] SURVIVED  L{idx+1}: {desc}")
        else:
            killed += 1

    with open(target, "w", encoding="utf-8") as fh:
        fh.write(original)

    total = killed + len(survived)
    print("\n=== RESULT ===")
    print(f"  killed   : {killed}/{total}")
    print(f"  survived : {len(survived)}/{total}")
    if total:
        print(f"  score    : {killed/total:.1%}")
    if survived:
        print("\n  Surviving mutations (untested logic):")
        for line_no, desc, text in survived:
            print(f"    L{line_no:<5} {desc:<28} {text[:66]}")
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
