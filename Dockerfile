FROM python:3.12-slim

WORKDIR /app

# OCR用（tesseract + 英語データ）
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# uv で依存解決
COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv && \
    uv sync --frozen --no-dev

COPY src/ ./src/

ENV PYTHONPATH=/app/src

EXPOSE 8081
CMD ["/app/.venv/bin/uvicorn", "ai_desktop_agent.server.app:app", "--host", "0.0.0.0", "--port", "8081"]
