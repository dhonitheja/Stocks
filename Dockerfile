FROM python:3.11-slim

# Security: non-root user
RUN groupadd -r stocksage && useradd -r -g stocksage stocksage

WORKDIR /app

# System deps (for torch/transformers builds)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ruff black mypy \
    && pip install --no-cache-dir -r requirements.txt

# Copy source
COPY agent/ ./agent/
COPY backtesting/ ./backtesting/
COPY config/ ./config/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY pyproject.toml pytest.ini ./

# Non-root
RUN chown -R stocksage:stocksage /app
USER stocksage

# Logs dir
RUN mkdir -p /app/logs

ENV PYTHONUNBUFFERED=1
ENV PYTHONUTF8=1
ENV LOG_FORMAT=json
ENV TRADING_MODE=paper

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["python", "-m", "agent.main"]
