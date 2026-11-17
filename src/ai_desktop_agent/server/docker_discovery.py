"""Read list metadata before inspecting containers whose ownership was established."""

from collections.abc import Iterable, Iterator
from typing import Any

from docker.errors import NotFound


def container_labels(container: Any) -> dict:
    try:
        attrs = container.attrs
        if "Config" in attrs:
            return (attrs.get("Config") or {}).get("Labels") or {}
        return attrs.get("Labels") or {}
    except (AttributeError, TypeError):
        return {}


def refresh_existing(containers: Iterable[Any]) -> Iterator[Any]:
    for container in containers:
        try:
            container.reload()
        except NotFound:
            continue
        yield container
