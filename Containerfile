FROM docker.io/library/python:3.13-slim

LABEL maintainer="preston"
LABEL description="Codex-to-Z.ai Responses API Proxy"

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY proxy/ ./proxy/

# Non-root user for security
RUN useradd --create-home --shell /sbin/nologin proxyuser
USER proxyuser

EXPOSE 4891

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4891/health')" || exit 1

CMD ["uvicorn", "proxy.main:app", "--host", "0.0.0.0", "--port", "4891", "--log-level", "info"]
