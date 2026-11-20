# Центр перепродажи для VPS 24/7: центр + LisBot (Node) + MannBot (Python) в одном контейнере.
# Сборка: docker build -t resale-center .   Запуск: docker compose up -d (см. docker-compose.yml)
FROM node:22-bookworm-slim AS lisdeps
# компилятор — на случай, если готовая сборка better-sqlite3 не скачается
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ && rm -rf /var/lib/apt/lists/*
WORKDIR /app/bots/lisbot
COPY bots/lisbot/package.json bots/lisbot/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CENTER_HOME=/data CENTER_BOT_HOST=0.0.0.0 TZ=Europe/Moscow
COPY --from=lisdeps /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY requirements.txt ./
COPY bots/mannbot/requirements.txt bots/mannbot/
COPY bots/starsbot/requirements.txt bots/starsbot/
RUN pip install --no-cache-dir -r requirements.txt
COPY center ./center
COPY bots ./bots
COPY --from=lisdeps /app/bots/lisbot/node_modules ./bots/lisbot/node_modules
VOLUME ["/data"]
EXPOSE 8800 8801 8802 8803
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8800/api/ping',timeout=3)"
# SIGTERM от docker stop → центр мягко останавливает обоих ботов (stop_grace_period в compose — 40 с)
CMD ["python", "-m", "center", "--no-browser", "--host", "0.0.0.0"]
