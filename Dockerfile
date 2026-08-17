FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Create non-root user
RUN groupadd -r frameart && useradd -r -g frameart -m frameart

WORKDIR /app

# Copy build metadata first (for dependency caching)
COPY pyproject.toml README.md ./

# Copy application code
COPY frameart/ frameart/
COPY config.example.yaml .

# Install the API and optional local-integration dependencies.
RUN pip install --no-cache-dir ".[api,integrations]"

# Data volume
RUN mkdir -p /data/frameart && chown -R frameart:frameart /data/frameart
VOLUME /data/frameart

# Switch to non-root user
USER frameart

ENV FRAMEART_DATA_DIR=/data/frameart

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready')"]

ENTRYPOINT ["frameart"]
CMD ["--help"]
