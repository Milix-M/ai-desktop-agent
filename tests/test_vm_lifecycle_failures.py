"""A lifecycle error must not trigger a retry or affect another environment."""

import pytest
from docker.errors import NotFound
from requests.exceptions import ReadTimeout

from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import LABEL_KIND, LABEL_PROJECT, LABEL_VM_ID, VmPool
from tests.test_vm_pool import _FakeContainer, _FakeDocker


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ai-desktop-agent")
    monkeypatch.setenv("USE_KVM", "false")
    monkeypatch.setenv("ALLOW_TCG_VM", "false")
    client = _FakeDocker()
    pool = VmPool(
        client=client, state_file=tmp_path / "vms.json", overlays_dir=str(tmp_path / "disks")
    )

    def add(vm_id="desk-owned", project="ai-desktop-agent"):
        target = _FakeContainer(
            "primary-" + str(len(client.containers.items)),
            labels={
                LABEL_VM_ID: vm_id,
                LABEL_KIND: "container",
                LABEL_PROJECT: project,
            },
        )
        client.containers.items.append(target)
        return target

    return pool, client, add


@pytest.mark.parametrize("vm_id", ["..", "../other", "/"])
@pytest.mark.parametrize("action", ["start_vm", "stop_vm", "restart_vm", "remove_vm"])
def test_invalid_ids_are_rejected_before_any_container_command(environment, vm_id, action):
    pool, _, add = environment
    target = add(vm_id)
    other = add(vm_id, project="other-project")
    with pytest.raises(ValueError):
        getattr(pool, action)(vm_id)
    assert not any((target.started, target.stopped, target.restarted, target.removed))
    assert not any((other.started, other.stopped, other.restarted, other.removed))


@pytest.mark.parametrize(
    "action,method", [("start_vm", "start"), ("stop_vm", "stop"), ("restart_vm", "restart")]
)
@pytest.mark.parametrize("fault", ["lost-response", "disappeared"])
def test_a_command_that_was_accepted_is_not_automatically_repeated(
    environment, action, method, fault
):
    pool, _, add = environment
    target = add()
    other = add(project="other-project")
    original = getattr(target, method)
    calls = []

    def accepted(*args, **kwargs):
        calls.append(target.id)
        original(*args, **kwargs)
        if fault == "lost-response":
            raise ReadTimeout("response lost after the command was accepted")
        target.removed = True

    setattr(target, method, accepted)
    error = DockerUnavailableError if fault == "lost-response" else ValueError
    with pytest.raises(error):
        getattr(pool, action)("desk-owned")
    assert calls == [target.id]
    assert not any((other.started, other.stopped, other.restarted, other.removed))


@pytest.mark.parametrize("reader", ["get_vm", "get_instance", "list_vms"])
@pytest.mark.parametrize("fault", ["disappeared", "inspect-failure"])
def test_connection_metadata_is_not_returned_after_the_second_inspection_fails(
    environment, reader, fault
):
    pool, _, add = environment
    target = add()
    calls = 0

    def reload():
        nonlocal calls
        calls += 1
        if calls == 2:
            if fault == "disappeared":
                raise NotFound("removed after discovery")
            raise ReadTimeout("inspection reply lost")

    target.reload = reload
    args = [] if reader == "list_vms" else ["desk-owned"]
    if fault == "inspect-failure":
        with pytest.raises(DockerUnavailableError):
            getattr(pool, reader)(*args)
    else:
        result = getattr(pool, reader)(*args)
        assert result == ([] if reader == "list_vms" else None)
    assert calls == 2


def test_preparing_a_missing_environment_does_not_allocate_storage_or_networks(environment):
    pool, client, _ = environment
    before = set(client.networks.items)
    assert pool.prepare_vm("desk-missing") is None
    assert set(client.networks.items) == before
    assert not pool._state_file.exists()


@pytest.mark.parametrize("allocation", ["container", "ledger"])
def test_a_requested_creation_id_cannot_replace_an_existing_allocation(environment, allocation):
    pool, client, add = environment
    vm_id = "desk-" + "a" * 24
    if allocation == "container":
        target = add(vm_id)
    else:
        pool._save_state(vm_id, {"name": "preserve allocation"})
    before = pool._state_file.read_bytes() if pool._state_file.exists() else None
    with pytest.raises(ValueError):
        pool.create_vm(kind="container", vm_id=vm_id)
    assert client.containers.runs == []
    if allocation == "container":
        assert not target.removed
    after = pool._state_file.read_bytes() if pool._state_file.exists() else None
    assert before == after


@pytest.mark.parametrize("action", ["delete", "stop", "start"])
@pytest.mark.parametrize("vm_id", ["%2e%2e", "a.invalid"])
async def test_http_rejects_invalid_ids_before_stopping_tasks_or_calling_docker(
    environment, monkeypatch, action, vm_id
):
    from unittest.mock import AsyncMock, Mock

    from httpx import ASGITransport

    from ai_desktop_agent.server import app as server
    from tests.http import AuthenticatedClient

    pool, _, _ = environment
    stopping = AsyncMock()
    lookup = Mock(return_value=pool)
    monkeypatch.setattr(server, "_stop_sessions_for_vm", stopping)
    monkeypatch.setattr(server, "get_pool", lookup)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app, raise_app_exceptions=False), base_url="http://test"
    ) as api:
        response = await (
            api.delete(f"/vms/{vm_id}")
            if action == "delete"
            else api.post(f"/vms/{vm_id}/{action}")
        )
    assert response.status_code == 422, response.text
    stopping.assert_not_awaited()
    lookup.assert_not_called()


def test_missing_instance_cannot_return_stale_connection_metadata(environment):
    pool, client, _ = environment
    assert pool.get_instance("desk-missing") is None
    assert pool.instance_id("desk-missing") is None
    assert client.containers.runs == []
    assert not pool._state_file.exists()
