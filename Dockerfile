FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    DASHBOARD_HOST=0.0.0.0

WORKDIR /app

COPY pyproject.toml README.md ./
COPY jobsearch ./jobsearch
RUN pip install --no-cache-dir .

# Your CV, profile and database live on a mounted volume, never in the image.
RUN mkdir -p /data/cv /data/output /data/logs
VOLUME ["/data"]

EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4).status == 200 else 1)"

CMD ["jobsearch", "serve"]
