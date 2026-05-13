FROM node:24-alpine@sha256:d1b3b4da11eefd5941e7f0b9cf17783fc99d9c6fc34884a665f40a06dbdfc94f AS build

WORKDIR /app
COPY package*.json ./
RUN npm ci --audit-signatures
COPY tsconfig.json ./
COPY src/ src/
RUN npm run build

FROM node:24-alpine@sha256:d1b3b4da11eefd5941e7f0b9cf17783fc99d9c6fc34884a665f40a06dbdfc94f

# Issue #39 — drop root. node:24-alpine ships a built-in `node` user (UID 1000).
# Keep WORKDIR /app for path stability — anything hard-coding /app/dist/index.js
# (volume mounts, log/process monitoring) continues to work.
RUN mkdir -p /app && chown node:node /app
WORKDIR /app
USER node
COPY --chown=node:node package*.json ./
RUN npm ci --audit-signatures --omit=dev
COPY --chown=node:node --from=build /app/dist dist/

EXPOSE 3001

CMD ["node", "dist/index.js"]
