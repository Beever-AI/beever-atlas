FROM node:25-alpine@sha256:bdf2cca6fe3dabd014ea60163eca3f0f7015fbd5c7ee1b0e9ccb4ced6eb02ef4 AS build

WORKDIR /app
COPY package*.json ./
RUN npm ci --audit-signatures
COPY tsconfig.json ./
COPY src/ src/
RUN npm run build

FROM node:25-alpine@sha256:bdf2cca6fe3dabd014ea60163eca3f0f7015fbd5c7ee1b0e9ccb4ced6eb02ef4

WORKDIR /app
COPY package*.json ./
RUN npm ci --audit-signatures --omit=dev
COPY --from=build /app/dist dist/

EXPOSE 3001

CMD ["node", "dist/index.js"]
