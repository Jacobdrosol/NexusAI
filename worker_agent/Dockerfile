FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONPATH=/app
EXPOSE 8001
CMD ["uvicorn", "worker_agent.main:app", "--host", "0.0.0.0", "--port", "8001"]
