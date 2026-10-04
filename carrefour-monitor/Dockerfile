FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd --create-home --uid 1000 monitor \
    && mkdir -p /app/data /app/logs \
    && chown -R monitor:monitor /app
USER monitor

# config.yaml, proxies.txt, data/ y logs/ se montan como volúmenes (ver docker-compose.yml)
CMD ["python", "main.py", "--config", "/app/config.yaml"]
