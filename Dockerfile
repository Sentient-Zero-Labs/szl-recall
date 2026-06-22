# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install into a prefix so runtime stage can copy just /install
RUN pip install --no-cache-dir --prefix=/install .

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copy installed package from builder — no gcc or build tools in the final image
COPY --from=builder /install /usr/local

# Persistent data directory for the SQLite database
RUN mkdir -p /data

# ── Environment variables ─────────────────────────────────────────────────────
# RECALL_DB_PATH  — SQLite database path (default: /data/recall.db)
#                   Override: -e RECALL_DB_PATH=/my/path/recall.db
# RECALL_DB_URL   — Postgres connection string (overrides RECALL_DB_PATH)
#                   Example: postgresql://user:pass@host:5432/recall
# ANTHROPIC_API_KEY — Required for memory extraction and consolidation
# RECALL_EXPORT_BUCKET — S3/R2 bucket name for Object Lock audit export (optional)
# RECALL_EXPORT_AWS_KEY / RECALL_EXPORT_AWS_SECRET — S3/R2 credentials (optional)

ENV RECALL_DB_PATH=/data/recall.db

EXPOSE 8678

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8678/.well-known/agent-card.json')" \
  || exit 1

CMD ["recall", "serve", "--host", "0.0.0.0", "--port", "8678", "--db", "/data/recall.db"]
