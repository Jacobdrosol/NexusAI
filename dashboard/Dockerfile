FROM python:3.11-slim
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates docker.io docker-cli \
    && (command -v docker || (command -v docker.io && ln -s "$(command -v docker.io)" /usr/local/bin/docker)) \
    && docker --version \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONPATH=/app
EXPOSE 5000
CMD ["sh", "-lc", "exec gunicorn --bind 0.0.0.0:5000 --worker-class \"${DASHBOARD_GUNICORN_WORKER_CLASS:-gthread}\" --workers \"${DASHBOARD_GUNICORN_WORKERS:-4}\" --threads \"${DASHBOARD_GUNICORN_THREADS:-8}\" --timeout \"${DASHBOARD_GUNICORN_TIMEOUT:-0}\" --graceful-timeout \"${DASHBOARD_GUNICORN_GRACEFUL_TIMEOUT:-30}\" --keep-alive \"${DASHBOARD_GUNICORN_KEEP_ALIVE:-300}\" 'dashboard.app:create_app()'"]
