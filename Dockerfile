# syntax=docker/dockerfile:1
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends fping sshpass openssh-client iproute2 curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pyyaml==6.0.2
WORKDIR /app
# Vendor database (OUI) to name device makers; optional: without it, the maker comes from OPNsense only
RUN curl -sfL -o manuf https://www.wireshark.org/download/automated/data/manuf || true
COPY app/ ./
ENV DATA=/data CONFIG=/config/config.yaml PORT=8080 PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8080'), timeout=4)"
CMD ["python", "app.py"]
