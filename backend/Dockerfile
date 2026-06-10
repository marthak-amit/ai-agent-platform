# ─────────────────────────────────────────────────────────────────────────────
# Backend — Python 3.12 / FastAPI / uvicorn
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim

WORKDIR /app

# gcc + libpq-dev are required to compile asyncpg from source.
# Using --no-install-recommends keeps the image lean.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (own layer — cached unless requirements change).
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Copy source.
# In development the app/ directory is overridden by a volume mount, so this
# layer is mainly used for the production / CI build path.
COPY . .

# Make the entrypoint executable inside the image.
RUN chmod +x docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
