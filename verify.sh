#!/usr/bin/env bash
# Run the checks that must pass before this bot is allowed to trade.
#
# WHAT IS AND IS NOT HERE
# -----------------------
# Included: byte-compile, the unit suite, coherence, and property-based
# fuzzing. Together roughly 10 seconds, which is cheap enough to run on every
# boot.
#
# Excluded: mutation testing. It takes 20+ minutes and measures test QUALITY,
# not correctness -- useful in development, actively harmful as a deploy gate
# where it would stall recovery from a crash for a third of an hour.
#
# Conditionally skipped: schema conformance, which needs the npm connector
# (@binance/w3w-prediction) as its ground truth. That is a Node package and is
# not in the runtime image, so its absence is reported as SKIP rather than
# failing the deploy. Run it locally or in CI, where the connector exists.
#
# A failure here stops the bot from starting. That is deliberate: broken code
# should not trade. SKIP_VERIFY=1 bypasses everything if you ever need to get
# a process up while you investigate.

set -uo pipefail

if [ "${SKIP_VERIFY:-0}" = "1" ]; then
  echo "SKIP_VERIFY=1 -- verification bypassed. The bot has NOT been checked."
  exit 0
fi

PY="${PYTHON:-python}"
command -v "$PY" >/dev/null 2>&1 || PY=python3

failed=0
skipped=0

run() {
  label="$1"; shift
  output=$("$@" 2>&1)
  status=$?
  if [ $status -eq 0 ]; then
    printf '  %-22s OK\n' "$label"
  else
    printf '  %-22s FAILED (exit %d)\n' "$label" "$status"
    printf '%s\n' "$output" | tail -20 | sed 's/^/      /'
    failed=$((failed + 1))
  fi
}

echo "=== Verification ==="

run "byte-compile" "$PY" -m py_compile btc_5m_predictor.py

if [ -f test_btc_5m.py ]; then
  run "unit tests" "$PY" -m unittest test_btc_5m
else
  printf '  %-22s SKIP (test_btc_5m.py not present)\n' "unit tests"
  skipped=$((skipped + 1))
fi

if [ -f coherence.py ]; then
  run "coherence" "$PY" coherence.py
else
  printf '  %-22s SKIP\n' "coherence"; skipped=$((skipped + 1))
fi

if [ -f fuzz.py ]; then
  run "fuzz invariants" "$PY" fuzz.py --trials 400
else
  printf '  %-22s SKIP\n' "fuzz invariants"; skipped=$((skipped + 1))
fi

# Conformance needs the Node connector as ground truth. Exit code 2 means
# "connector not installed", which is expected in the runtime image and must
# not be read as a schema violation.
if [ -f conformance.py ]; then
  conf_output=$("$PY" conformance.py 2>&1)
  case $? in
    0) printf '  %-22s OK\n' "schema conformance" ;;
    2) printf '  %-22s SKIP (connector not installed; run in CI)\n' \
         "schema conformance"; skipped=$((skipped + 1)) ;;
    *) printf '  %-22s FAILED\n' "schema conformance"
       printf '%s\n' "$conf_output" | tail -15 | sed 's/^/      /'
       failed=$((failed + 1)) ;;
  esac
else
  printf '  %-22s SKIP\n' "schema conformance"; skipped=$((skipped + 1))
fi

echo
if [ $failed -gt 0 ]; then
  echo "$failed check(s) FAILED. Refusing to start: broken code must not trade."
  echo "Set SKIP_VERIFY=1 to override, understanding what that means."
  exit 1
fi

if [ $skipped -gt 0 ]; then
  echo "All checks passed ($skipped skipped)."
else
  echo "All checks passed."
fi
exit 0
