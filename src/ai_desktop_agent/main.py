"""
AI Desktop Agent — Entry point.

Starts the FastAPI backend server with uvicorn.
Use Docker Compose for the full stack (desktop + backend + frontend).
"""

import argparse
import logging
import os

from ai_desktop_agent.server.__main__ import run_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="AI Desktop Agent")
    parser.add_argument("--host", default="127.0.0.1", help="Server host")
    parser.add_argument("--port", type=int, default=8081, help="Server port")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev mode)")
    args = parser.parse_args()

    logger.info("AI Desktop Agent v0.1.0 starting...")
    logger.info("LLM_PROVIDER=%s", os.environ.get("LLM_PROVIDER", "openai"))
    vnc_host = os.environ.get("VNC_HOST", "localhost")
    vnc_port = os.environ.get("VNC_PORT", "5900")
    logger.info("VNC_HOST=%s VNC_PORT=%s", vnc_host, vnc_port)

    run_server(args.host, args.port, reload=args.reload)


if __name__ == "__main__":
    main()
