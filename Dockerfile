# Ultron Prime: one image with the API, the built web app and a small local embedding model.
#   docker compose up -d   then   docker compose logs ultron   for the URL and the passphrase

# --- web app ---------------------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npx vite build

# --- runtime ---------------------------------------------------------------------------------
FROM python:3.13-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
# CPU-only torch keeps the image near 1.5 GB instead of several GB of CUDA libraries.
RUN uv pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1 \
 && uv pip install sentence-transformers==6.1.0

COPY src/ src/
COPY data/calibration.json data/calibration.json
COPY workspace/ workspace/
RUN uv sync --frozen --no-dev --inexact
COPY --from=web /web/dist web/dist

RUN useradd --create-home --uid 10001 ultron \
 && mkdir -p /app/data /app/workspace/uploads /app/workspace/outputs \
 && chown -R ultron:ultron /app/data /app/workspace
USER ultron

# CONTAINER: bind the published port (compose maps it to the host's loopback only).
# SANDBOX_NETWORK=host: Docker's default seccomp profile blocks the user namespaces that
# network isolation needs, so the container itself is the boundary for code runs.
ENV CONTAINER=true \
    SANDBOX_NETWORK=host \
    EMBEDDING_BACKEND=local_light \
    HF_HOME=/app/data/models \
    XDG_CACHE_HOME=/tmp/cache \
    MPLCONFIGDIR=/tmp/matplotlib \
    ALLOWED_HOSTS=localhost,127.0.0.1 \
    ALLOWED_ORIGINS=http://localhost:8787,http://127.0.0.1:8787

VOLUME ["/app/data", "/app/workspace"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --start-interval=3s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/v1/health', timeout=4).status == 200 else 1)"]
CMD ["ultron", "serve"]
