# ============================================
# MEXC Futures Trading Bot — Multi-stage Dockerfile
# ============================================

# ── Stage 1: Builder ──────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Runtime ─────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Create non-root user
RUN groupadd -r botuser && useradd -r -g botuser -d /app -s /sbin/nologin botuser

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY src/ ./src/
COPY .env.example .env

# Create data directories for persistence
RUN mkdir -p data/state data/logs && chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Environment defaults
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

ENTRYPOINT ["python", "-m", "src.main"]
