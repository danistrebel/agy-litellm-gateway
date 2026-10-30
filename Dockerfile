FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    LITELLM_CONFIG_PATH=/app/litellm_config.yaml

WORKDIR /app

# Install system dependencies required by cryptography/grpc cleanly
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY litellm_config.yaml /app/litellm_config.yaml
COPY gateway_middleware.py /app/gateway_middleware.py

EXPOSE 8080

CMD ["sh", "-c", "uvicorn gateway_middleware:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-keep-alive 3600"]
