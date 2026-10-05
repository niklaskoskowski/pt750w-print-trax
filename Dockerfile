# Debian's own Pillow: no compiler needed, same image on arm64, armhf (32-bit Pi OS) and amd64.
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pil fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PTB_DATA_DIR=/data \
    PTB_PORT=8750

WORKDIR /app
COPY ptbridge ./ptbridge
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

EXPOSE 8750
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8750/health', timeout=4)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "-m", "ptbridge", "serve"]
