FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first: source changes must not invalidate the pip cache.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[postgres]"

COPY scripts ./scripts

RUN useradd -m -u 10001 aigate && chown -R aigate /app
USER aigate

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"

CMD ["uvicorn", "aigate.main:app", "--host", "0.0.0.0", "--port", "8080"]