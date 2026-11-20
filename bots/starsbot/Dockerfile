# StarsBot для VPS (работа 24/7). Сборка: docker build -t starsbot .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 STARSBOT_HOME=/data STARSBOT_HOST=0.0.0.0 STARSBOT_PORT=8083 TZ=Europe/Moscow
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY starsbot ./starsbot
COPY StarsBot.py config.example.yaml ./
VOLUME ["/data"]
EXPOSE 8083
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8083/api/ping',timeout=3)"
CMD ["python", "StarsBot.py", "--headless"]
