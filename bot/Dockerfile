FROM node:22-alpine@sha256:8ea2348b068a9544dae7317b4f3aafcdc032df1647bb7d768a05a5cad1a7683f AS build

WORKDIR /app
COPY package*.json ./
RUN npm ci --audit-signatures
COPY tsconfig.json ./
COPY src/ src/
RUN npm run build

FROM node:22-alpine@sha256:8ea2348b068a9544dae7317b4f3aafcdc032df1647bb7d768a05a5cad1a7683f

WORKDIR /app
COPY package*.json ./
RUN npm ci --audit-signatures --omit=dev
COPY --from=build /app/dist dist/

EXPOSE 3001

CMD ["node", "dist/index.js"]
