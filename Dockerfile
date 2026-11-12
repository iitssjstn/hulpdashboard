# syntax=docker/dockerfile:1

# ---------- stage 1: afhankelijkheden bouwen ----------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ---------- stage 2: runtime ----------
FROM python:3.12-slim

ARG GIT_SHA=dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Amsterdam \
    APP_VERSION=${GIT_SHA}

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && mkdir -p /data /keys \
 && chown app:app /data /keys \
 && chmod 700 /keys

COPY --from=builder /install /usr/local
WORKDIR /app
COPY app ./app

LABEL org.opencontainers.image.title="hulpdashboard" \
      org.opencontainers.image.description="Discord-hulpbot met webdashboard" \
      org.opencontainers.image.revision="${GIT_SHA}"

USER app
EXPOSE 8080
VOLUME ["/data", "/keys"]

HEALTHCHECK --interval=30s --timeout=6s --start-period=40s --retries=3 \
  CMD ["python", "-m", "app.healthcheck"]

CMD ["python", "-m", "app"]
