FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# System deps (minimal)
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    ca-certificates curl bash git \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt requirements-dev.txt ./
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install -r requirements-dev.txt

# App code
COPY src ./src
COPY configs ./configs
COPY scripts ./scripts
COPY README.md ./

RUN chmod +x scripts/*.sh || true

# Default command shows help
CMD ["python", "-m", "cryptostorm", "validate", "configs/realtime.yaml", "--no-require-env"]

