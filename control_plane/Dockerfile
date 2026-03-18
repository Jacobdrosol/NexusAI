FROM python:3.11-slim
ARG NEXUSAI_REPO_RUNTIME_TOOLCHAINS=node,dotnet,go,rust,cpp
ARG NEXUSAI_REPO_RUNTIME_DOTNET_CHANNEL=8.0
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/install_repo_runtime_toolchains.sh /tmp/install_repo_runtime_toolchains.sh
RUN chmod +x /tmp/install_repo_runtime_toolchains.sh \
    && NEXUSAI_REPO_RUNTIME_DOTNET_CHANNEL=${NEXUSAI_REPO_RUNTIME_DOTNET_CHANNEL} \
       /tmp/install_repo_runtime_toolchains.sh "${NEXUSAI_REPO_RUNTIME_TOOLCHAINS}" \
    && rm -f /tmp/install_repo_runtime_toolchains.sh
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8000"]
