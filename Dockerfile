# Zombie Hunter Dockerfile
# Multi-stage build for minimal production image

# Build stage
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy and install application
COPY pyproject.toml .
COPY zombie_hunter/ zombie_hunter/
RUN pip install --no-cache-dir .


# Production stage
FROM python:3.11-slim as production

# Labels
LABEL org.opencontainers.image.title="Zombie Hunter"
LABEL org.opencontainers.image.description="FinOps tool for finding and eliminating zombie cloud resources"
LABEL org.opencontainers.image.source="https://github.com/yourusername/zombie-hunter"

# Create non-root user
RUN groupadd --gid 1000 zombiehunter && \
    useradd --uid 1000 --gid zombiehunter --shell /bin/bash --create-home zombiehunter

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy configuration
COPY config.yaml /app/config.yaml

# Set ownership
RUN chown -R zombiehunter:zombiehunter /app

# Switch to non-root user
USER zombiehunter

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV ZOMBIE_HUNTER_CONFIG_PATH=/app/config.yaml

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import zombie_hunter; print('OK')" || exit 1

# Default command - run scan
ENTRYPOINT ["zombie-hunter"]
CMD ["scan", "--output", "json"]
