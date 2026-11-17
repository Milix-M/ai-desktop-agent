"""Keep task admission consistent with the container's health check."""


def is_ready(status: str, health: str | None) -> bool:
    # Existing custom images may have no HEALTHCHECK; VNC connection still verifies them.
    return status == "running" and health in (None, "none", "healthy")
