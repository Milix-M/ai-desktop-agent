"""
AI Desktop Agent — Entry point.

Starts the QEMU VM with VNC, launches the FastAPI backend,
and serves the web UI with embedded noVNC viewer.
"""

import argparse
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="AI Desktop Agent")
    parser.add_argument("--host", default="0.0.0.0", help="Server host")
    parser.add_argument("--port", type=int, default=8080, help="Server port")
    parser.add_argument("--vm-image", help="Path to QEMU VM image")
    parser.add_argument("--vnc-port", type=int, default=5900, help="VM VNC port")
    parser.add_argument("--no-vm", action="store_true", help="Skip VM start (external VM)")
    args = parser.parse_args()

    logger.info("AI Desktop Agent v%s starting...", __import__(__name__).__version__)

    # TODO: Start VM (QEMU)
    # TODO: Start websockify (VNC → WebSocket)
    # TODO: Start FastAPI server with uvicorn
    # TODO: Initialize agent loop

    logger.info("Server running on http://%s:%d", args.host, args.port)


if __name__ == "__main__":
    main()
