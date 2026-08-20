# Sidecar : CLI Bitwarden officielle exposant la Vault Management API.
FROM node:20-bookworm-slim

ENV BITWARDENCLI_APPDATA_DIR=/data \
    NODE_ENV=production

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @bitwarden/cli@2024.12.0 \
 && npm cache clean --force

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

VOLUME ["/data"]
EXPOSE 8087

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
