# ── Backend image: FastAPI `trove serve` only ─────────────────────
# Pure JSON API — every route lives under /v1, never serves the UI.
# The frontend lives in its own container (frontend/Dockerfile: nginx
# serves the built SPA and reverse-proxies /v1 → backend:8000); rebuilds
# and releases are fully independent per container.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY trove/ ./trove/
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["trove", "serve", "--host", "0.0.0.0", "--port", "8000"]
