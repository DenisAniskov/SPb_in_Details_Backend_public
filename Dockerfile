# Tested Python series; public Docker Hub manifest verified 2026-10-10.
# Update the digest and runtime lock together after testing Python security updates.
FROM python:3.14.7-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BACKEND_ENV=production \
    HOST=0.0.0.0 \
    PORT=8000 \
    WORKERS=1 \
    RELOAD=false \
    DATABASE_URL=sqlite+aiosqlite:////data/smart_navigator_spb.db

WORKDIR /app

# Linux amd64 / CPython 3.14 wheels; no compiler, test tools or reload extras.
COPY requirements.runtime.lock ./requirements.runtime.lock
RUN python -m pip --isolated install --index-url https://pypi.org/simple --no-cache-dir --only-binary=:all: --require-hashes -r requirements.runtime.lock \
    && python -m pip check \
    && mkdir /data \
    && chown 10001:10001 /data \
    && chmod 0700 /data

# Deliberate allowlist: local .env files, databases and tests never enter layers.
COPY app ./app
COPY run.py ./run.py
COPY container_entrypoint.py ./container_entrypoint.py
# Standard-library online backup; no test or development dependencies.
COPY scripts/sqlite_backup.py ./scripts/sqlite_backup.py
# Phase C7.1: the read-only pilot building dataset (OpenStreetMap, ODbL).
# Mount a newer dataset over this path to update coverage without rebuilding.
COPY data ./data
RUN chmod -R a=rX /app

USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; exec(\"try:\\n urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/ready',timeout=3).close()\\nexcept Exception:\\n sys.exit(1)\")"]
ENTRYPOINT ["python", "container_entrypoint.py"]
