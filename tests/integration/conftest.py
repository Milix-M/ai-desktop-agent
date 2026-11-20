"""Optional coverage for Python executed inside backend test containers."""

import os

import docker
import pytest
from docker.models.containers import ContainerCollection


@pytest.fixture(scope="session", autouse=True)
def container_coverage():
    directory = os.environ.get("DESKTOP_CONTAINER_COVERAGE_DIR")
    if not directory or os.environ.get("RUN_DESKTOP_SMOKE") != "1":
        yield None
        return
    from tests.container_coverage import ContainerCoverage

    client = docker.from_env(timeout=30)
    try:
        measurement = ContainerCoverage(
            client,
            os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke"),
            directory,
        )
    finally:
        client.close()
    create = ContainerCollection.create

    def measured_create(collection, image, command=None, **kwargs):
        options = measurement.options(image, kwargs)
        container = create(collection, image, command, **options)
        if options is not kwargs and container.attrs["Image"] != measurement.identity["image"]:
            container.remove(force=True)
            raise AssertionError("backend image changed while tests ran")
        return container

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ContainerCollection, "create", measured_create)
        yield measurement
    measurement.finish()
