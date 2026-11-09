FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    ATLAS_HOME=/data \
    ATLAS_HEADLESS=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g newman \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY atlas ./atlas
COPY docker-start.sh ./

RUN pip install --no-cache-dir -e . \
    && playwright install --with-deps chromium \
    && chmod +x docker-start.sh

VOLUME /data
EXPOSE 7801

CMD ["./docker-start.sh"]
