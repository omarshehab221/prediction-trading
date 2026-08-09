#!/usr/bin/env bash
# Isolated full checkup. Fresh dir, fresh interpreter, no shared state.
set -u
RUN="$1"
SRC=/mnt/user-data/outputs
DIR=$(mktemp -d "/tmp/checkup${RUN}.XXXXXX")
cd "$DIR" || exit 1

cp "$SRC/btc_5m_predictor.py" "$SRC/test_btc_5m.py" \
   "$SRC/conformance.py" "$SRC/fuzz.py" "$SRC/mutate.py" \
   "$SRC/coherence.py" \
   "$SRC/entrypoint.sh" "$SRC/render.yaml" "$SRC/Dockerfile" \
   "$SRC/requirements.txt" "$SRC/verify.sh" "$SRC/patterns.py" .
chmod +x entrypoint.sh 2>/dev/null || true
rm -rf __pycache__ .pytest_cache
export BINANCE_API_KEY=dummy BINANCE_API_SECRET=dummy
export PYTHONDONTWRITEBYTECODE=1
export PYTHONHASHSEED=$((RUN * 7919))   # different hash seed each run

echo "### RUN $RUN  (dir=$DIR  PYTHONHASHSEED=$PYTHONHASHSEED)"

# 1. integrity of the copied sources
md5sum btc_5m_predictor.py test_btc_5m.py | sed 's/^/  sha /'

# 2. static
python3 -m py_compile btc_5m_predictor.py test_btc_5m.py \
  && echo "  compile    OK" || echo "  compile    FAIL"
python3 -m pyflakes btc_5m_predictor.py test_btc_5m.py >/dev/null 2>&1 \
  && echo "  pyflakes   clean" || { echo "  pyflakes   ISSUES"; python3 -m pyflakes ./*.py; }

# 3. endpoints vs the official connector
python3 - <<'PY'
import re, sys
sys.path.insert(0, '.')
import btc_5m_predictor as m
base = '/home/claude/probe/node_modules/@binance/w3w-prediction/dist/'
src = open(base + 'index.d.ts').read() + open(base + 'index.js').read()
real = set(re.findall(r"/sapi/v1/w3w/wallet/prediction/[a-zA-Z0-9/_-]+", src))
bad = [n for n, (verb, p) in m.DEFAULT_ENDPOINTS.items() if p not in real]
print(f"  endpoints  {len(m.DEFAULT_ENDPOINTS)-len(bad)}/{len(m.DEFAULT_ENDPOINTS)} verified"
      + (f"  MISMATCH {bad}" if bad else ""))
PY

# 3b. schema conformance against the official connector (ground truth)
CONF=$(python3 conformance.py 2>&1 | tail -1)
echo "  conformance$(printf '%*s' 1 '')${CONF}"

# 3b2. internal coherence: no artifacts left over from an earlier stage
CO=$(python3 coherence.py 2>&1 | grep -cE "^    (L[0-9]+:|Config\.|--)" || true)
python3 coherence.py >/dev/null 2>&1 && echo "  coherence  0 stale artifacts" \
  || echo "  coherence  ${CO} STALE ARTIFACT(S)"

# 3c. property-based invariants under hostile input
FZ=$(python3 fuzz.py --trials 1500 --seed "$RUN" 2>/dev/null | tail -1)
echo "  fuzz       ${FZ}"

# 4. full suite, shuffled order to expose inter-test dependencies
python3 - <<'PY'
import random, sys, unittest
sys.path.insert(0, '.')
seed = int(__import__('os').environ['PYTHONHASHSEED'])
loader = unittest.TestLoader()
loader.sortTestMethodsUsing = None
suite = loader.loadTestsFromName('test_btc_5m')

def flatten(s):
    for t in s:
        if isinstance(t, unittest.TestSuite):
            yield from flatten(t)
        else:
            yield t

tests = list(flatten(suite))
random.Random(seed).shuffle(tests)
res = unittest.TextTestRunner(verbosity=0, stream=open('/dev/null', 'w')).run(
    unittest.TestSuite(tests))
print(f"  tests      {res.testsRun} run, {len(res.failures)} failed, "
      f"{len(res.errors)} errors, {len(res.skipped)} skipped  "
      f"{'OK' if res.wasSuccessful() else 'BROKEN'}")
for t, tb in (res.failures + res.errors):
    print(f"    !! {t}")
    print("       " + tb.strip().splitlines()[-1])
PY

# 5. determinism of the pure numerics (must be bit-identical across runs)
python3 - <<'PY'
import hashlib, sys
sys.path.insert(0, '.')
import btc_5m_predictor as m
c = m.Config(api_key='k', api_secret='s', **m.PROFILES['convex'])
vals = []
for df in (None, 2.5, 4.0, 12.0):
    for s in (99_000, 99_800, 100_000, 100_400, 101_500):
        vals.append(m.digital_up_probability(s, 100_000, 0.55, 90, df))
for p in (0.05, 0.12, 0.2, 0.35, 0.6, 0.9):
    vals.append(m.breakeven_probability(p, 200))
    vals.append(m.kelly_stake(1000, min(p * 1.5, 0.99), p, c))
    vals.append(m.settle_pnl(10, p, True, 200))
print("  numerics   " + hashlib.sha256(
    ";".join(f"{v:.15e}" for v in vals).encode()).hexdigest()[:32])
PY

# 6. CLI surface
python3 btc_5m_predictor.py --help >/dev/null 2>&1 && echo "  cli --help OK"
python3 btc_5m_predictor.py --kelly 9 2>&1 | grep -q "Invalid configuration" \
  && echo "  cli reject OK"
BINANCE_API_KEY= python3 btc_5m_predictor.py 2>&1 | grep -q "Set BINANCE_API_KEY" \
  && echo "  cli nokeys OK"
for p in convex balanced; do
  python3 btc_5m_predictor.py --profile "$p" --calibration-report --db "cal_$p.db" \
    >/dev/null 2>&1 && echo "  profile $p OK"
done

# 7. per-run stderr fingerprint (must be identical across runs)
python3 - <<'PYX' 2>run_err.txt
import random, sys, unittest, os
sys.path.insert(0, '.')
l = unittest.TestLoader(); l.sortTestMethodsUsing = None
def flat(s):
    for t in s:
        if isinstance(t, unittest.TestSuite): yield from flat(t)
        else: yield t
ts = list(flat(l.loadTestsFromName('test_btc_5m')))
random.Random(int(os.environ['PYTHONHASHSEED'])).shuffle(ts)
unittest.TextTestRunner(verbosity=0, stream=open(os.devnull,'w')).run(
    unittest.TestSuite(ts))
PYX
echo "  warnings   $(sort run_err.txt | uniq -c | awk '{printf "%s:%s ", $1, substr($2,1,18)}')"

# 8. artifacts left behind in the working directory
STRAY=$(ls -A | grep -vE '^(btc_5m_predictor.py|test_btc_5m.py|conformance.py|fuzz.py|mutate.py|coherence.py|entrypoint.sh|render.yaml|Dockerfile|requirements.txt|verify.sh|patterns.py|cal_convex.db|cal_balanced.db|__pycache__|run_err.txt)$' | tr '\n' ' ')
echo "  stray      ${STRAY:-none}"

cd / && rm -rf "$DIR"
echo
