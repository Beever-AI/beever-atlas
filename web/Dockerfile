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

FROM nginx:alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de
COPY --from=build /app/dist /usr/share/nginx/html
COPY <<'EOF' /etc/nginx/conf.d/default.conf
server {
    listen 80;
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
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
