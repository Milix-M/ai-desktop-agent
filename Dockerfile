FROM python:3.12-slim

WORKDIR /app

# Android操作用（adb）
ARG PACKAGE_REFRESH=manual
RUN : "$PACKAGE_REFRESH" && apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    docker-cli \
    android-tools-adb \
    && rm -rf /var/lib/apt/lists/*

# uv で依存解決
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN pip install --no-cache-dir uv && \
    uv sync --frozen --no-dev --no-cache && \
    pip uninstall -y uv

COPY src/ ./src/

ENV PYTHONPATH=/app/src

EXPOSE 8081
CMD ["/app/.venv/bin/python", "-m", "ai_desktop_agent.server"]
