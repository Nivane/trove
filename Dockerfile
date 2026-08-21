# ── Stage 1: build the frontend bundle ────────────────────────────
# Deps first (lockfile rarely changes → npm ci layer stays cached).
FROM node:22-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# → /build/frontend/trove/api/static
RUN npm run build

# ── Stage 2: python runtime, ships the freshly built bundle ───────
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY trove/ ./trove/
# pip install packages the committed bundle (package-data); the COPY
# below replaces it with the fresh build — deterministic, no stale UI.
RUN pip install --no-cache-dir .
COPY --from=frontend /build/frontend/trove/api/static ./trove/api/static/
EXPOSE 8000
CMD ["trove", "serve", "--host", "0.0.0.0", "--port", "8000"]
