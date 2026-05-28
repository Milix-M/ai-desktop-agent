FROM python:3.12-slim

WORKDIR /app

# uv で依存解決
COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv && \
    uv sync --frozen --no-dev

COPY src/ ./src/

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "ai_desktop_agent.server.app:app", "--host", "0.0.0.0", "--port", "8080"]
