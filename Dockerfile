# Gateway image.
#
# Build:   docker build -t aigate-gateway:0.3.0 .
# Run:     docker run --env-file .env.production -p 8080:8080 aigate-gateway:0.3.0
#
# Four decisions worth keeping:
#
# 1. **Install from uv.lock** (`uv pip install --frozen`) instead of resolving at build time.
#    `pip install .` picks whatever is newest that day, so two builds of the same commit
#    produce different images — and "works on my machine" becomes unfixable.
# 2. **/app/data exists in the image, owned by the runtime user.** The compose file mounts a
#    named volume there for the metering spool. Docker seeds a named volume from the image's
#    directory *and its ownership*; if the directory didn't exist, the volume arrives
#    root-owned and the non-root process cannot write the spool — a silent accounting hole.
# 3. **Alembic is installed here**, not in a "dev" image: a production image that cannot run
#    `alembic upgrade head` has a schema that only moves by hand.
# 4. **Non-root (uid 10001)**. The process only needs /app.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# uv is the project's own resolver; installing with it keeps the image and the lockfile honest.
RUN pip install --no-cache-dir "uv>=0.5"

# Dependency layer: only the manifests, so a source edit does not reinstall the world.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv pip install --system --frozen ".[postgres,redis]" \
 && uv pip install --system "alembic>=1.20,<2"

COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 aigate \
 && mkdir -p /app/data \
 && chown -R aigate:aigate /app

USER aigate

EXPOSE 8080

# timeout 写在 urlopen 里：没有它会等默认超时，健康检查变成"永远 pending"。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"

# WEB_CONCURRENCY 由 uvicorn 自己读取：>1 时即多副本，必须配 redis 后端（见 DEPLOY.md）。
CMD ["uvicorn", "aigate.main:app", "--host", "0.0.0.0", "--port", "8080"]