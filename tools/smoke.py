#!/usr/bin/env python3
"""
tools/smoke.py -- a fingerprint of what the bot does, for the package split.

Runs the CLI surface and the pure numerics and prints one text block. Taken
before the split and after it, the two must be identical: the tests prove the
logic, this proves the thing a user actually invokes still answers the same.

Scaffolding for one refactor; deleted with it.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "btc_5m_predictor.py")
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ", re.M)

CASES = [
    ("help", ["--help"], {}),
    ("default-profile", ["--print-default-profile"], {}),
    ("write-config", ["--config", "cfg.json", "--db", "j.db",
                      "--profile", "convex", "--write-config"], {}),
    ("check-config", ["--config", "cfg.json", "--db", "j.db",
                      "--check-config"], {}),
    ("reject-bad-kelly", ["--kelly", "9"], {}),
    ("no-keys", [], {"BINANCE_API_KEY": ""}),
    ("calibration-convex", ["--profile", "convex", "--calibration-report",
                            "--db", "cal_convex.db"], {}),
    ("calibration-balanced", ["--profile", "balanced", "--calibration-report",
                              "--db", "cal_balanced.db"], {}),
]


def numerics() -> str:
    sys.path.insert(0, ROOT)
    import btc_5m_predictor as m
    cfg = m.Config(api_key="k", api_secret="s", **m.PROFILES["convex"])
    values = []
    for df in (None, 2.5, 4.0, 12.0):
        for spot in (99_000, 99_800, 100_000, 100_400, 101_500):
            values.append(m.digital_up_probability(spot, 100_000, 0.55, 90, df))
    for price in (0.05, 0.12, 0.2, 0.35, 0.6, 0.9):
        values.append(m.breakeven_probability(price, 200))
        values.append(m.kelly_stake(1000, min(price * 1.5, 0.99), price, cfg))
        values.append(m.settle_pnl(10, price, True, 200))
        values.append(m.win_return(price, 200))
    digest = hashlib.sha256(
        ";".join(f"{v:.15e}" for v in values).encode()).hexdigest()
    return f"## numerics\n{digest}"


def main() -> int:
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        env_base = dict(os.environ, BINANCE_API_KEY="k", BINANCE_API_SECRET="s",
                        PYTHONHASHSEED="0")
        for key in ("TRADING_MODE", "SYMBOLS", "PROFILE", "CONFIG_PATH",
                    "DB_PATH"):
            env_base.pop(key, None)
        for label, args, extra in CASES:
            proc = subprocess.run([sys.executable, BOT, *args], cwd=tmp,
                                  env={**env_base, **extra},
                                  capture_output=True, text=True, timeout=180)

            def clean(text: str) -> str:
                text = text.replace(tmp, "<TMP>")
                text = text.replace(tmp.replace("\\", "/"), "<TMP>")
                return STAMP.sub("<TS> ", text)

            out.append(f"## {label} exit={proc.returncode}\n"
                       f"{clean(proc.stdout)}\n-- stderr --\n"
                       f"{clean(proc.stderr)}")
        with open(os.path.join(tmp, "cfg.json"), encoding="utf-8") as fh:
            out.append("## cfg.json\n" + fh.read())
    out.append(numerics())
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
