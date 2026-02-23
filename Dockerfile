FROM python:3.12-slim AS base

# System deps for Pillow and websocket
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libjpeg62-turbo-dev \
        zlib1g-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r frameart && useradd -r -g frameart -m frameart

WORKDIR /app

# Copy build metadata first (for dependency caching)
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir . 2>/dev/null || true

# Copy application code
COPY frameart/ frameart/
COPY config.example.yaml .

# Install the package with API dependencies (FastAPI + uvicorn)
RUN pip install --no-cache-dir ".[api]"

# Data volume
RUN mkdir -p /data/frameart && chown -R frameart:frameart /data/frameart
VOLUME /data/frameart

# Switch to non-root user
USER frameart

ENV FRAMEART_DATA_DIR=/data/frameart

EXPOSE 8000

ENTRYPOINT ["frameart"]
CMD ["--help"]
