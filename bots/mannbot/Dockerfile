# MannBot для VPS (работа 24/7). Сборка: docker build -t mannbot .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MANNBOT_HOME=/data MANNBOT_HOST=0.0.0.0 MANNBOT_PORT=8080 TZ=Europe/Moscow
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir aiohttp beautifulsoup4 lxml PyYAML Jinja2 cryptography
COPY mannbot ./mannbot
COPY MannBot.py config.example.yaml ./
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/ping',timeout=3)"
CMD ["python", "MannBot.py", "--headless"]
