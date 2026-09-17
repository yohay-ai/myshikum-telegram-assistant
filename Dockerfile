FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HEADLESS=true \
    STATE_DIR=/app/state

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && groupadd --system --gid 10001 bot \
    && useradd --system --uid 10001 --gid bot --home-dir /app --shell /usr/sbin/nologin bot \
    && mkdir -p /app/state \
    && chown -R bot:bot /app

COPY --chown=bot:bot rehab_checker_bot.py .env.example ./
COPY --chown=bot:bot tests ./tests

USER bot
VOLUME ["/app/state"]

CMD ["python", "rehab_checker_bot.py"]
