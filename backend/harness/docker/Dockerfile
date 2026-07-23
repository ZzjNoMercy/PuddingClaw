FROM node:22-bookworm-slim AS node-runtime

FROM python:3.12-slim-bookworm

LABEL com.puddingclaw.runtime="python3.12-node22-chromium-v4"

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s /usr/local/lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && corepack enable

COPY validate-html-report-e2e.mjs /opt/puddingclaw/bin/validate-html-report-e2e.mjs

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

WORKDIR /workspace

CMD ["sh", "-lc", "trap : TERM INT; sleep infinity & wait"]
