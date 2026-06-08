FROM node:24-alpine@sha256:d1b3b4da11eefd5941e7f0b9cf17783fc99d9c6fc34884a665f40a06dbdfc94f AS build

WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .

# Vite inlines env vars at build time, so EVERY ``VITE_*`` we want in the
# bundle must be threaded through ARG → ENV here AND added to
# ``docker-compose.yml`` web.build.args. Single source of truth = root
# ``.env`` for the whole stack; no need for a ``web/.env.local`` file.
ARG VITE_API_URL=http://localhost:8000
ENV VITE_API_URL=${VITE_API_URL}
ARG VITE_BEEVER_API_KEY=
ENV VITE_BEEVER_API_KEY=${VITE_BEEVER_API_KEY}
ARG VITE_BEEVER_ADMIN_TOKEN=
ENV VITE_BEEVER_ADMIN_TOKEN=${VITE_BEEVER_ADMIN_TOKEN}

RUN npm run build

FROM nginxinc/nginx-unprivileged:alpine@sha256:85bcbc6b2edd325462560c597d784ecee415024f1c6a004e53ac5f202b8ca561
# Issue #39 — drop root. The `nginxinc/nginx-unprivileged` image is the official
# upstream nginx variant configured to run as the `nginx` user (UID 101) and
# listen on port 8080 by default. Avoids fragile manual chown of /var/cache/nginx,
# /var/run, /etc/nginx/conf.d that breaks across nginx version bumps. The host-side
# port mapping in docker-compose is updated atomically (3000:80 → 3000:8080).
COPY --from=build /app/dist /usr/share/nginx/html
COPY <<'EOF' /etc/nginx/conf.d/default.conf
server {
    listen 8080;
    root /usr/share/nginx/html;
    location / {
        try_files $uri $uri/ /index.html;
    }
    # Proxy API calls to the backend (avoids CORS issues in production)
    location /api/ {
        proxy_pass http://beever-atlas:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
