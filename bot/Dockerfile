FROM node:22-alpine@sha256:8ea2348b068a9544dae7317b4f3aafcdc032df1647bb7d768a05a5cad1a7683f AS build

WORKDIR /app
COPY package*.json ./
RUN npm ci --audit-signatures
COPY tsconfig.json ./
COPY src/ src/
RUN npm run build

FROM node:22-alpine@sha256:8ea2348b068a9544dae7317b4f3aafcdc032df1647bb7d768a05a5cad1a7683f

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
