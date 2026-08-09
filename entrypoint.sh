#!/usr/bin/env bash
# Seed the configuration, validate it, then hand over to the bot.
#
# THE ONE RULE HERE: never overwrite an existing config.
#
# The whole point of the external file is that you edit it and the running bot
# picks up the change. If this script regenerated the file on every boot, every
# deploy and every restart would silently discard your edits and hot reload
# would be pointless. So the file is written ONCE, on first boot, and every
# later start reuses whatever is on disk.
#
# It also has to live on the mounted disk, not in the image. Render's
# filesystem is ephemeral: a config written into the image would reset on every
# deploy, which is the same failure in slower motion.

set -euo pipefail

CONFIG_PATH="${CONFIG_PATH:-/var/data/config.json}"
DB_PATH="${DB_PATH:-/var/data/btc5m_journal.db}"
# Read the default from the bot rather than repeating it here: a literal in
# shell is one more copy to drift out of step with the Python module.
PROFILE="${PROFILE:-$(python btc_5m_predictor.py --print-default-profile)}"

CONFIG_DIR="$(dirname "$CONFIG_PATH")"

# Refuse to run against a path that will not survive a restart, rather than
# discovering it when the journal and the config vanish mid-experiment.
if ! mkdir -p "$CONFIG_DIR" 2>/dev/null; then
  echo "FATAL: cannot create $CONFIG_DIR." >&2
  echo "  CONFIG_PATH must sit on the mounted disk (Render: /var/data)." >&2
  echo "  Check that the disk's mountPath matches CONFIG_PATH." >&2
  exit 1
fi

if [ ! -w "$CONFIG_DIR" ]; then
  echo "FATAL: $CONFIG_DIR is not writable. Mount a persistent disk there." >&2
  exit 1
fi

# Verify before anything else. The image was checked at build time, but the
# runtime environment differs -- different Python, different disk, possibly a
# different image than the one that was built. Roughly ten seconds, and it is
# the difference between a broken bot that refuses to start and one that
# trades. SKIP_VERIFY=1 bypasses it.
# VERIFY_NESTED=1 skips the handful of tests that themselves re-run
# verify.sh. Those check the gate rather than the bot, they belong at build
# time, and including them here would turn a 10-second boot check into a
# 36-second one on every restart.
if [ -x ./verify.sh ] || [ -f ./verify.sh ]; then
  if ! VERIFY_NESTED=1 bash ./verify.sh; then
    echo "FATAL: verification failed; not starting." >&2
    exit 1
  fi
  echo
else
  echo "WARNING: verify.sh not found; starting unverified." >&2
fi

if [ -f "$CONFIG_PATH" ]; then
  echo "Config found at $CONFIG_PATH -- keeping it (your edits are preserved)."
else
  echo "No config at $CONFIG_PATH -- writing defaults for profile '$PROFILE'."
  python btc_5m_predictor.py \
    --config "$CONFIG_PATH" \
    --db "$DB_PATH" \
    --profile "$PROFILE" \
    --write-config
fi

# Validate before starting. A malformed file caught here fails the deploy
# loudly; caught later it would fail after the process is already live.
if ! python btc_5m_predictor.py --config "$CONFIG_PATH" --db "$DB_PATH" \
     --check-config; then
  echo "FATAL: $CONFIG_PATH is invalid. Fix it and redeploy." >&2
  exit 1
fi

# Preflight: probe the live API before trading. Build time cannot do this --
# there are no real credentials there, and the build host may sit in a region
# Binance blocks -- so it belongs here and only here.
#
# It blocks startup by default. A failure means the keys, region, wallet or
# endpoints are wrong, and none of those fix themselves by trading anyway.
# Render restarts a failed worker, so a genuinely transient network blip
# resolves on the next attempt rather than needing a human.
#
# PREFLIGHT_REQUIRED=0 downgrades it to a warning, for the case where one
# probe fails for a reason you have decided to accept -- an unfunded wallet
# in paper mode, say, where the balance check legitimately fails while
# everything the bot needs still works.
if [ "${SKIP_PREFLIGHT:-0}" = "1" ]; then
  echo "SKIP_PREFLIGHT=1 -- API not probed."
else
  echo
  if python btc_5m_predictor.py --config "$CONFIG_PATH" --db "$DB_PATH" \
       --preflight; then
    echo "Preflight OK."
  elif [ "${PREFLIGHT_REQUIRED:-1}" = "0" ]; then
    echo "WARNING: preflight failed; PREFLIGHT_REQUIRED=0, starting anyway." >&2
  else
    echo "FATAL: preflight failed. Fix the reported checks, or set" >&2
    echo "  PREFLIGHT_REQUIRED=0 to start regardless." >&2
    exit 1
  fi
fi

echo
echo "Starting: config=$CONFIG_PATH db=$DB_PATH"
echo "Edit $CONFIG_PATH at any time; changes apply within a poll interval."

# exec matters: without it the shell stays as PID 1 and receives SIGTERM,
# while Python never sees it. The bot's graceful shutdown -- stop opening
# positions, let the open one resolve -- would never run, and every redeploy
# would abandon a position mid-round.
exec python btc_5m_predictor.py \
  --config "$CONFIG_PATH" \
  --db "$DB_PATH" \
  "$@" \
  --live
