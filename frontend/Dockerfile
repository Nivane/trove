# ── Frontend image: nginx serving the SPA + proxying /v1 → backend ─
# Independent from the backend image: frontend changes only rebuild
# this container (docker compose build frontend).

# ── Stage 1: build the bundle ─────────────────────────────────────
# Deps first (lockfile rarely changes → npm ci layer stays cached).
FROM node:22-bookworm-slim AS build
WORKDIR /build/frontend
COPY package.json package-lock.json ./
RUN npm ci
COPY . ./
# vite outDir is frontend/dist (decoupled frontend); the bundle lands
# at /build/frontend/dist
RUN npm run build

# ── Stage 2: nginx runtime ────────────────────────────────────────
FROM nginx:alpine
COPY --from=build /build/frontend/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
