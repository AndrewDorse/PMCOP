# Polymarket copy bot — production image (VPS / Hostinger-style hosts)
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
# activity_runner: PYTHONPATH=/app/src — module name is polymarket_copy_bot (not polymymarket_*).
# Activity feed: copy fresh source BUY deals as capped FAK orders. Position-sync: cli run
CMD ["python", "-m", "polymarket_copy_bot.activity_runner", "run", "--limit", "50"]
