# Polymarket copy bot — production image (VPS / Hostinger-style hosts)
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
# Activity feed: copy all BUYs ($1 if balance ≤$200 else 0.75%), 15m cooldown per token; hold to resolution (no TP). Position-sync: cli run
CMD ["python", "-m", "polymarket_copy_bot.activity_runner", "run", "--limit", "50"]
