"""Container entry point: never listen on the interfaces shared with desktop environments."""

import logging
import os

import uvicorn

from ai_desktop_agent.server.networks import EnvironmentNetworks
from ai_desktop_agent.server.vm_pool import VmPool


def main():
    pool = VmPool()
    client = pool._docker()
    networks = EnvironmentNetworks(client, pool._project_name(client))
    address = networks.management_address()
    os.environ["AGENT_MANAGEMENT_CIDRS"] = ",".join(networks.management_subnets())
    logging.getLogger(__name__).info("管理APIの待受IP: %s", address)
    uvicorn.run(
        "ai_desktop_agent.server.app:app",
        host=address,
        port=int(os.environ.get("PORT", "8081")),
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
