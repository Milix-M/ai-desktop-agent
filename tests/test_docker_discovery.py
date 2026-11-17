"""Exercise Docker SDK discovery over HTTP without inspecting unrelated containers."""

import copy
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import docker
import pytest
from httpx import ASGITransport

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.server.workspaces import HOME, LABEL_HELPER, LABEL_HOME, WorkspaceVolumes
from tests.http import AuthenticatedClient


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "mine")
    state = {"status": {}, "inspected": [], "listed": [], "changed": {}}
    records, volumes = {}, {}
    state["volumes"] = volumes

    def add(name, labels, network="mine_default", mounts=None):
        records[name] = {
            "Id": name,
            "Name": "/" + name,
            "Config": {"Labels": labels},
            "State": {"Status": "running", "Running": True, "Health": {"Status": "healthy"}},
            "NetworkSettings": {"Networks": {network: {"IPAddress": "172.30.0.2"}}},
            "Mounts": mounts or [],
        }

    add("mine-vm-1", {"com.docker.compose.project": "mine", "com.docker.compose.service": "vm"})
    add("other-vm-1", {"com.docker.compose.project": "other", "com.docker.compose.service": "vm"})
    for name, owner in (("desk-mine", "mine"), ("desk-other", "other")):
        add(name, {"ai-desktop-agent.vm-id": name, "ai-desktop-agent.project": owner})
    add("legacy-owned", {"ai-desktop-agent.vm-id": "and-legacy"})
    add("legacy-unallocated", {"ai-desktop-agent.vm-id": "and-unallocated"})
    add("legacy-wrong-network", {"ai-desktop-agent.vm-id": "and-away"}, "other_default")
    add("backend", {"com.docker.compose.project": "mine"})
    records["backend"]["NetworkSettings"]["Networks"]["mine_default"]["GwPriority"] = 1

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path.startswith("/v1.48/volumes/"):
                name = url.path.rsplit("/", 1)[-1]
                self.respond(200, volumes[name]) if name in volumes else self.respond(
                    404, {"message": "missing volume"}
                )
                return
            if url.path == "/v1.48/containers/json":
                filters = json.loads(parse_qs(url.query).get("filters", ["{}"])[0])
                state["listed"].append(filters)
                labels = filters.get("label", [])
                if isinstance(labels, str):
                    labels = [labels]
                rows = []
                for record in records.values():
                    if filters.get("id") and not any(
                        record["Id"].startswith(value) for value in filters["id"]
                    ):
                        continue
                    actual = record["Config"]["Labels"]
                    if any(
                        (value.split("=", 1)[0] not in actual)
                        or (
                            "=" in value
                            and actual[value.split("=", 1)[0]] != value.split("=", 1)[1]
                        )
                        for value in labels
                    ):
                        continue
                    if filters.get("volume") and not any(
                        mount.get("Name") in filters["volume"] for mount in record["Mounts"]
                    ):
                        continue
                    rows.append(
                        {
                            "Id": record["Id"],
                            "Names": [record["Name"]],
                            "Labels": actual,
                            "State": record["State"]["Status"],
                            "NetworkSettings": record["NetworkSettings"],
                            "Mounts": record["Mounts"],
                        }
                    )
                self.respond(200, rows)
                return
            if url.path.startswith("/v1.48/containers/") and url.path.endswith("/json"):
                name = url.path.split("/")[-2]
                if name == socket.gethostname():
                    name = "backend"
                state["inspected"].append(name)
                status = state["status"].get(name, 200)
                if status != 200 or name not in records:
                    self.respond(
                        status if status != 200 else 404, {"message": "controlled lookup failure"}
                    )
                    return
                self.respond(200, state["changed"].get(name, records[name]))
                return
            self.respond(404, {"message": "unexpected endpoint"})

        def do_DELETE(self):
            name = urlsplit(self.path).path.split("/")[-1]
            if name in records:
                records.pop(name)
                self.respond(204, {})
            else:
                self.respond(404, {"message": "missing container"})

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # Poll shutdown promptly; socket and client timeouts remain unchanged.
    worker = threading.Thread(
        target=upstream.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    worker.start()
    client = docker.DockerClient(
        base_url=f"http://127.0.0.1:{upstream.server_port}", version="1.48", timeout=1
    )
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    pool._save_state("and-legacy", {"name": "旧環境"})
    pool._save_state("and-away", {"name": "別ネットワーク"})
    # This upstream models discovery reads; network mutations have separate integration tests.
    monkeypatch.setattr(pool, "isolate_environments", lambda: None)
    monkeypatch.setattr(server, "get_pool", lambda: pool)
    try:
        yield state, records, client, pool
    finally:
        client.close()
        upstream.shutdown()
        upstream.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.mark.parametrize(
    "unrelated", ["other-vm-1", "desk-other", "legacy-unallocated", "legacy-wrong-network"]
)
@pytest.mark.parametrize("status", [404, 403, 500])
async def test_unrelated_lookup_failure_does_not_break_this_projects_vm_list(
    engine, unrelated, status
):
    state, _, _, _ = engine
    state["status"][unrelated] = status
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        response = await api.get("/vms")
    assert response.status_code == 200, response.text
    assert {vm["id"] for vm in response.json()} == {"vm", "desk-mine", "and-legacy"}
    assert unrelated not in state["inspected"]


def test_owned_metadata_is_refreshed_but_foreign_details_are_never_loaded(engine):
    state, _, _, pool = engine
    assert {vm.id for vm in pool.list_vms()} == {"vm", "desk-mine", "and-legacy"}
    assert set(state["inspected"]) == {"backend", "mine-vm-1", "desk-mine", "legacy-owned"}
    fixed_queries = [
        q for q in state["listed"] if "com.docker.compose.service=vm" in q.get("label", [])
    ]
    assert fixed_queries and "com.docker.compose.project=mine" in fixed_queries[0]["label"]


@pytest.mark.parametrize("owned", ["mine-vm-1", "desk-mine", "legacy-owned"])
async def test_owned_inspect_failures_still_return_503(engine, owned):
    state, _, _, _ = engine
    state["status"][owned] = 500
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        response = await api.get("/vms")
    assert response.status_code == 503, response.text


@pytest.mark.parametrize(
    "owned,missing",
    [("mine-vm-1", "vm"), ("desk-mine", "desk-mine"), ("legacy-owned", "and-legacy")],
)
async def test_owned_disappearance_between_list_and_inspect_keeps_the_other_vms_available(
    engine, owned, missing
):
    state, _, _, _ = engine
    state["status"][owned] = 404
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        response = await api.get("/vms")
    assert response.status_code == 200, response.text
    assert {vm["id"] for vm in response.json()} == {"vm", "desk-mine", "and-legacy"} - {missing}


def test_legacy_network_ownership_is_rechecked_after_inspection(engine):
    state, records, _, pool = engine
    moved = copy.deepcopy(records["legacy-owned"])
    moved["NetworkSettings"]["Networks"] = {"other_default": {}}
    state["changed"]["legacy-owned"] = moved
    assert "and-legacy" not in {vm.id for vm in pool.list_vms()}


def test_in_use_volume_checks_do_not_inspect_the_attached_container(engine):
    state, records, client, _ = engine
    records["other-vm-1"]["Mounts"] = [{"Name": "mine-home", "Type": "volume"}]
    state["status"]["other-vm-1"] = 500
    from ai_desktop_agent.server.vm_control import DockerUnavailableError

    with pytest.raises(DockerUnavailableError, match="使用中"):
        WorkspaceVolumes(client, "mine").require_unused(SimpleNamespace(name="mine-home"))
    assert not state["inspected"]


def test_creation_rollback_cleans_owned_helpers_without_inspecting_foreign_id_collisions(engine):
    state, records, client, pool = engine
    for name, project in (("broken-owned", "mine"), ("broken-other", "other")):
        record = copy.deepcopy(records["desk-mine"])
        record.update(Id=name, Name="/" + name)
        record["Config"]["Labels"] = {
            "ai-desktop-agent.vm-id": "desk-broken",
            "ai-desktop-agent.project": project,
        }
        records[name] = record
    state["status"]["broken-other"] = 500
    with (
        pytest.raises(RuntimeError, match="controlled actor failure"),
        pool._creation(client, "desk-broken", {}),
    ):
        raise RuntimeError("controlled actor failure")
    assert "broken-owned" not in records and "broken-other" in records
    assert "broken-other" not in state["inspected"]
    assert pool._load_state("desk-broken") is None


def workspace_helper(engine, name, project="mine", owner="missing-parent"):
    state, records, _, _ = engine
    record = copy.deepcopy(records["desk-mine"])
    record.update(Id=name, Name="/" + name)
    record["Config"]["Labels"] = {
        LABEL_HELPER: "true",
        "ai-desktop-agent.project": project,
        "ai-desktop-agent.helper-owner": owner,
    }
    record["HostConfig"] = {"NetworkMode": "none", "ReadonlyRootfs": True}
    record["Mounts"] = [{"Name": "mine_home_desk-helper", "Type": "volume", "Destination": HOME}]
    records[name] = record
    state["volumes"]["mine_home_desk-helper"] = {
        "Name": "mine_home_desk-helper",
        "Driver": "local",
        "Options": None,
        "Labels": {
            "ai-desktop-agent.project": "mine",
            "ai-desktop-agent.vm-id": "desk-helper",
            LABEL_HOME: "home",
        },
    }


@pytest.mark.parametrize("status", [404, 403, 500])
def test_stale_workspace_helpers_are_reaped_without_inspecting_other_projects(engine, status):
    state, records, client, _ = engine
    workspace_helper(engine, "owned-helper")
    workspace_helper(engine, "foreign-helper", project="other")
    state["status"]["foreign-helper"] = status
    WorkspaceVolumes(client, "mine").reap_helpers()
    assert "owned-helper" not in records and "foreign-helper" in records
    assert "foreign-helper" not in state["inspected"]


def test_a_helpers_live_parent_is_checked_without_loading_its_details(engine):
    state, records, client, _ = engine
    workspace_helper(engine, "owned-helper", owner="other-vm-1")
    state["status"]["other-vm-1"] = 500
    WorkspaceVolumes(client, "mine").reap_helpers()
    assert "owned-helper" in records
    assert set(state["inspected"]) == {"owned-helper"}


def test_a_workspace_helper_removed_after_listing_is_skipped(engine):
    state, _, client, _ = engine
    workspace_helper(engine, "gone-helper")
    state["status"]["gone-helper"] = 404
    WorkspaceVolumes(client, "mine").reap_helpers()
