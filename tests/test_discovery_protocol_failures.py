"""Untrusted Docker metadata must not identify or mutate an ambiguous environment."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import docker
import pytest

from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import LABEL_PROJECT, LABEL_VM_ID, VmPool


@pytest.fixture
def discovery_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "metadata-test")
    physical_id = "f" * 64
    labels = {
        "com.docker.compose.project": "metadata-test",
        "com.docker.compose.service": "vm",
    }
    state = {
        "phase": "healthy",
        "invalid_id": None,
        "inspections": 0,
        "mutations": [],
        "labels": labels,
        "dynamic": False,
    }

    class Engine(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, value):
            payload = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path.removeprefix("/v1.48")
            if path == "/_ping":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")
                return
            if path == "/containers/json":
                filters = json.loads(parse_qs(parsed.query).get("filters", ["{}"])[0])
                requested = filters.get("label", [])
                selected = (
                    LABEL_VM_ID in requested
                    if state["dynamic"]
                    else "com.docker.compose.service=vm" in requested
                )
                if not selected:
                    self.respond(200, [])
                    return
                result = {
                    "Id": physical_id,
                    "Names": ["/metadata-test-vm-1"],
                    "Labels": state["labels"],
                    "State": "running",
                }
                if state["phase"] == "malformed-listing":
                    result["Config"] = ["invalid configuration metadata"]
                self.respond(200, [result])
                return
            if path == f"/containers/{physical_id}/json":
                state["inspections"] += 1
                if state["phase"] == "removed-after-discovery" and state["inspections"] == 2:
                    self.respond(404, {"message": "No such container"})
                    return
                result = {
                    "Id": physical_id,
                    "Name": "/metadata-test-vm-1",
                    "Config": {"Labels": state["labels"]},
                    "State": {"Status": "running", "Health": {"Status": "healthy"}},
                }
                if state["phase"] == "invalid-inspect-id" and state["inspections"] == 2:
                    result["Id"] = state["invalid_id"]
                self.respond(200, result)
                return
            self.respond(404, {"message": "No such container"})

        def do_POST(self):
            state["mutations"].append(("POST", self.path))
            self.respond(500, {"message": "unexpected mutation"})

        def do_DELETE(self):
            state["mutations"].append(("DELETE", self.path))
            self.respond(500, {"message": "unexpected mutation"})

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
    thread = threading.Thread(
        target=upstream.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    client = docker.DockerClient(
        base_url=f"http://127.0.0.1:{upstream.server_port}", version="1.48", timeout=1
    )
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    try:
        yield pool, state, physical_id
    finally:
        pool.close()
        client.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("invalid_id", [None, "", 123], ids=["null", "empty", "number"])
def test_a_lost_physical_id_in_the_final_inspection_cannot_be_used_for_handover(
    discovery_endpoint, invalid_id
):
    pool, state, physical_id = discovery_endpoint
    state.update(phase="invalid-inspect-id", invalid_id=invalid_id)
    with pytest.raises(DockerUnavailableError, match="環境の識別子"):
        pool.get_instance("vm")
    assert state["inspections"] == 2 and state["mutations"] == []
    state.update(phase="healthy", inspections=0)
    recovered = pool.get_instance("vm")
    assert recovered.container_id == physical_id
    assert recovered.info.id == "vm" and recovered.info.status == "running"
    assert state["inspections"] == 2 and state["mutations"] == []


def test_malformed_list_configuration_is_not_inspected_or_deleted_and_can_recover(
    discovery_endpoint,
):
    pool, state, physical_id = discovery_endpoint
    state["phase"] = "malformed-listing"
    assert pool.get_instance("vm") is None
    assert not pool.remove_vm("vm")
    assert state["inspections"] == 0 and state["mutations"] == []
    before = copy.deepcopy(state)
    state["phase"] = "healthy"
    recovered = pool.get_instance("vm")
    assert recovered.container_id == physical_id and recovered.info.kind == "qemu"
    assert state["mutations"] == before["mutations"] == []


@pytest.mark.parametrize("dynamic", [False, True], ids=["compose-service", "allocated-environment"])
def test_a_container_removed_after_discovery_is_omitted_and_a_later_list_can_recover(
    discovery_endpoint, dynamic
):
    pool, state, _ = discovery_endpoint
    if dynamic:
        state["labels"] = {LABEL_PROJECT: "metadata-test", LABEL_VM_ID: "vm-saved"}
    state.update(phase="removed-after-discovery", dynamic=dynamic)
    assert pool.list_vms() == []
    assert state["inspections"] == 2 and state["mutations"] == []
    state.update(phase="healthy", inspections=0)
    recovered = pool.list_vms()
    assert len(recovered) == 1
    assert recovered[0].id == ("vm-saved" if dynamic else "vm")
    assert recovered[0].status == "running" and recovered[0].managed is dynamic
    assert state["inspections"] == 2 and state["mutations"] == []


@pytest.mark.parametrize("vm_id", ["", "vm"], ids=["empty-id", "reserved-fixed-id"])
def test_dynamic_labels_cannot_introduce_an_empty_or_reserved_fixed_environment_id(
    discovery_endpoint, vm_id
):
    pool, state, _ = discovery_endpoint
    state.update(dynamic=True, labels={LABEL_PROJECT: "metadata-test", LABEL_VM_ID: vm_id})
    assert pool.list_vms() == []
    assert state["inspections"] == 1 and state["mutations"] == []
    state.update(inspections=0, labels={LABEL_PROJECT: "metadata-test", LABEL_VM_ID: "vm-saved"})
    recovered = pool.list_vms()
    assert len(recovered) == 1 and recovered[0].id == "vm-saved" and recovered[0].managed
    assert state["inspections"] == 2 and state["mutations"] == []
