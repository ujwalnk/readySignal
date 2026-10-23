# syntax=docker/dockerfile:1

# ---- build stage: resolve and install dependencies into a venv that has no pip of its own ----
FROM python:3.12-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv --without-pip /opt/venv
COPY requirements.txt .
RUN pip --python /opt/venv/bin/python install -r requirements.txt

# ---- runtime stage: base + curl + the venv + the app, nothing else ----
FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.title="Ready Signal" org.opencontainers.image.source="ready-signal"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data HOST=0.0.0.0 PORT=8080 PATH=/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home app \
    && mkdir /data && chown 10001:10001 /data && chmod 700 /data
COPY --from=builder /opt/venv /opt/venv
WORKDIR /srv
COPY app ./app
COPY static ./static
USER 10001
VOLUME /data
EXPOSE 8080
# SECRET is intentionally NOT set here; supply it at run time.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"
CMD ["python", "-m", "app.main"]
