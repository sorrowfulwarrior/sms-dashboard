# SMS dashboard (Flask + Gunicorn) and Telegram bot, one image for both.
# Dependencies come from poetry.lock so the image matches the host venv.
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1

RUN pip install "poetry==2.1.2"

WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root


FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    SENT_IDS_FILE=/data/message_ids.txt

RUN useradd --system --uid 10001 --home-dir /app app \
 && mkdir -p /data && chown app:app /data

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src/ /app/src/

USER app
VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/', timeout=4)" || exit 1

# Web by default; the bot service overrides the command.
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "--access-logfile", "-", "sms-dashboard.app:app"]
