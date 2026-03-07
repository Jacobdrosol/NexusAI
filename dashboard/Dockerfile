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
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "0", "--graceful-timeout", "30", "--keep-alive", "300", "dashboard.app:create_app()"]
