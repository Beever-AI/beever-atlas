FROM node:22-alpine@sha256:8ea2348b068a9544dae7317b4f3aafcdc032df1647bb7d768a05a5cad1a7683f AS build

WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .

# VITE_API_URL must be set at build time (Vite bakes env vars into the bundle)
ARG VITE_API_URL=http://localhost:8000
ENV VITE_API_URL=${VITE_API_URL}
ARG VITE_BEEVER_API_KEY=
ENV VITE_BEEVER_API_KEY=${VITE_BEEVER_API_KEY}

RUN npm run build

FROM nginxinc/nginx-unprivileged:alpine@sha256:f35982400455b4f359083ba9d1ef573fbbe4221a6a8a194dc7a14beb4e2cd3dc
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
