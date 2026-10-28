# The Case workbench: Next.js standalone output, built in stages and run as a non-root user.
# Build context is this folder (`docker compose build workbench`).

FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# The lockfile is the build's only source of versions.
RUN npm ci

FROM node:22-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-slim
# Upgrade first, so any OS fix Debian has published is in the image (the Trivy scan fails on
# fixable High findings).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
# The server needs node alone. The npm and corepack bundled in the base image carry High CVEs
# (brace-expansion, ip-address, pacote, picomatch, sigstore) and nothing here runs them.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
    /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
# The standalone server carries the few node_modules it needs; static files sit beside it.
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
