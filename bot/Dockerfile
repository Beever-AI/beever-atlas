FROM node:26-alpine@sha256:e71ac5e964b9201072425d59d2e876359efa25dc96bb1768cb73295728d6e4ea AS build

WORKDIR /app
COPY package*.json ./
RUN npm ci --audit-signatures
COPY tsconfig.json ./
COPY src/ src/
RUN npm run build

FROM node:26-alpine@sha256:e71ac5e964b9201072425d59d2e876359efa25dc96bb1768cb73295728d6e4ea

# Issue #39 — drop root. node:22-alpine ships a built-in `node` user (UID 1000).
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
