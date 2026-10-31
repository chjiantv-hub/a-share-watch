# A股盯盘 — cloud image (FastAPI + in-process background poller)
FROM python:3.13-slim
LABEL org.opencontainers.image.source=https://github.com/chjiantv-hub/a-share-watch
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Asia/Shanghai \
    PORT=8080 DATA_DIR=/data SEED_DIR=/srv/seed
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app/ /srv/app/
COPY seed/ /srv/seed/
# writable data dir (rules.json, alert_log.json, watchlist.md, quote cache). Mount a volume here if the platform offers one.
RUN useradd -m -u 1000 app && mkdir -p /data && chown -R app:app /data /srv
USER app
WORKDIR /srv/app
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/healthz',timeout=8)"
# single worker on purpose: the poller thread lives inside the uvicorn process (starts on FastAPI startup)
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --log-level warning --proxy-headers --forwarded-allow-ips='*'"]
