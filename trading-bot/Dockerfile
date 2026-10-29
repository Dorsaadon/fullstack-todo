# tradebot: one container = one bot. Build from trading-bot/:
#   docker compose build && docker compose up -d
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Pinned dependencies first, so code changes don't reinstall them.
COPY requirements.lock .
RUN pip install -r requirements.lock

# Run from source (PYTHONPATH): no build step, so nothing unpinned is fetched.
COPY src ./src
RUN useradd --create-home --uid 1000 tradebot \
    && mkdir -p /app/data /app/config && chown tradebot:tradebot /app/data

USER tradebot
# Recordings, journal, saved state and the instance lock live here: mount a volume.
VOLUME ["/app/data"]

ENV TRADEBOT_CONFIG=config/local.toml
# Healthy = the running bot saved its state in the last 2 minutes.
HEALTHCHECK --interval=60s --timeout=15s --start-period=120s --retries=3 \
    CMD python -m tradebot health -c "$TRADEBOT_CONFIG" || exit 1

# Default: paper trading. For live, override the command (see docker-compose.yml).
CMD ["sh", "-c", "exec python -m tradebot run -c \"$TRADEBOT_CONFIG\""]
