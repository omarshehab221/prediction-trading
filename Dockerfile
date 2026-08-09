FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The verification suite ships with the bot: it runs at build time (below) and
# again on every boot, so a broken image never becomes a trading process.
COPY btc_5m_predictor.py test_btc_5m.py coherence.py fuzz.py \
     conformance.py verify.sh entrypoint.sh ./
RUN chmod +x verify.sh entrypoint.sh

# Fail the BUILD on broken code, so a bad image is never produced rather than
# produced and then refusing to start. This is the FULL run, including the
# tests that exercise verify.sh itself; the boot-time run skips those, since
# they check the gate rather than the bot and would triple every restart.
#
# Schema conformance skips automatically here: it needs the Node connector as
# ground truth, which is not in this image. Run it in CI, where npm exists.
RUN BINANCE_API_KEY=build BINANCE_API_SECRET=build ./verify.sh

ENV PYTHONUNBUFFERED=1

# Both live on the mounted disk, never in the image: anything written into the
# image resets on every deploy, wiping the config and splitting the journal.
ENV CONFIG_PATH=/var/data/config.json
ENV DB_PATH=/var/data/btc5m_journal.db
# Only used when the config is first created; the file governs after that.
# Must match DEFAULT_PROFILE in btc_5m_predictor.py -- coherence.py asserts it.
ENV PROFILE=buffer
# paper | live. Pins the mode; unset it to let config.json govern, which makes
# the mode hot-reloadable.
# ENV TRADING_MODE=paper
ENV TRADING_MODE=live
# Seconds the boot-time preflight check waits for a signed request to be
# ACCEPTED before giving up. On shared egress the outbound address is not knowable until the process
# is running and can change on any restart, so it cannot be added to Binance's
# key allowlist in advance. Preflight prints the address and keeps knocking,
# which turns a one-second race into a window long enough to paste it in.
# Bounded rather than infinite: a worker waiting forever on a key that is
# simply wrong costs money and reports nothing. "0" fails on first refusal.
ENV AUTH_WAIT_S=600

ENTRYPOINT ["./entrypoint.sh"]
