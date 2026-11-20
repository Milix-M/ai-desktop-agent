"""Container entry point: never listen on the interfaces shared with desktop environments."""

import logging
import os

import uvicorn

from ai_desktop_agent.server.networks import EnvironmentNetworks
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.server.websocket_io import MAX_MESSAGE_BYTES, MAX_MESSAGE_QUEUE
from ai_desktop_agent.server.websocket_protocol import AgentWebSocketProtocol


def run_server(host: str, port: int, *, reload: bool = False) -> None:
    """Keep local and container entry points on the same state and transport limits."""
    uvicorn.run(
        "ai_desktop_agent.server.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
        proxy_headers=False,
        workers=1,
        ws=AgentWebSocketProtocol,
        ws_max_size=MAX_MESSAGE_BYTES,
        ws_max_queue=MAX_MESSAGE_QUEUE,
    )


def main():
    with VmPool() as pool:
        client = pool._docker()
        networks = EnvironmentNetworks(client, pool._project_name(client))
        address = networks.management_address()
        os.environ["AGENT_MANAGEMENT_CIDRS"] = ",".join(networks.management_subnets())
    logging.getLogger(__name__).info("管理APIの待受IP: %s", address)
    run_server(address, int(os.environ.get("PORT", "8081")))


if __name__ == "__main__":
    main()
