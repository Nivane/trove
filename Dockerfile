# ── Backend image: FastAPI `trove serve` only ─────────────────────
# The frontend lives in its own container (frontend/Dockerfile: nginx
# serves the bundle and reverse-proxies /v1 → backend:8000); rebuilds
# and releases are independent per container.
# pip packages the committed bundle (package-data), so this image can
# still serve a UI at /ui when run standalone — the committed version,
# not the live frontend image. docker-compose.yml wires the two together.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY trove/ ./trove/
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["trove", "serve", "--host", "0.0.0.0", "--port", "8000"]
