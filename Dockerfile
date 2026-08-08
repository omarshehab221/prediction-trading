# Optional: use this instead of the native Python runtime to pin the version.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY btc_5m_predictor.py .

# Logs stream immediately rather than buffering, so Render's log view is live.
ENV PYTHONUNBUFFERED=1

# The journal lives on the mounted disk, not in the image.
CMD ["python", "btc_5m_predictor.py", "--db", "/var/data/btc5m_journal.db", "--live"]
