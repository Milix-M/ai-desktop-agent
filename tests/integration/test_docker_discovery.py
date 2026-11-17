"""Audit real SDK reads while other projects share the Docker daemon."""

import json
import os
import uuid
from contextlib import suppress

import docker
import pytest
from docker.errors import NotFound

from ai_desktop_agent.server.networks import LABEL_PROJECT
from ai_desktop_agent.server.workspaces import HOME, LABEL_HELPER, LABEL_HOME

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]

PROBE = """import json,socket,sys
import docker
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.server.workspaces import WorkspaceVolumes
allowed=set(json.loads(sys.argv[1])); foreign=set(json.loads(sys.argv[2])); project=sys.argv[3]
client=docker.from_env(timeout=5)
original=client.api.inspect_container
inspected=[]
def inspect(reference):
    reference=str(reference)
    if reference!=socket.gethostname() and reference not in allowed:
        raise AssertionError('unrelated inspect attempted: '+reference)
    inspected.append(reference)
    return original(reference)
client.api.inspect_container=inspect
pool=VmPool(client=client,state_file='/tmp/discovery-probe/vms.json',overlays_dir='/tmp/discovery-probe/disks')
pool._save_state('desk-legacy',{'name':'legacy'})
assert {vm.id for vm in pool.list_vms()}=={'vm','desk-query','desk-legacy'}
assert pool.stop_vm('vm',timeout=1).status=='exited'
assert pool.start_vm('vm').id=='vm'
try:
    with pool._creation(client,'desk-broken',{}):
        raise RuntimeError('controlled actor failure')
except RuntimeError as error:
    assert str(error)=='controlled actor failure'
assert pool._load_state('desk-broken') is None
volumes=WorkspaceVolumes(client,project)
volume=client.volumes.get(project+'_home_desk-helper')
try:
    volumes.require_unused(volume)
except DockerUnavailableError as error:
    assert '使用中' in str(error)
else: raise AssertionError('an in-use volume was accepted')
volumes.reap_helpers()
for identifier in foreign:
    assert client.api.containers(all=True,filters={'id':identifier}),identifier
assert not foreign.intersection(inspected)
print(json.dumps({'inspected':sorted(set(inspected)),'foreign_count':len(foreign),'owned_vms':3}))
client.close()
"""


def test_discovery_and_cleanup_never_inspect_the_other_projects_containers():
    client = docker.from_env(timeout=15)
    project = "discovery-scope-" + uuid.uuid4().hex[:10]
    other = project + "-other"
    image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    labels = {LABEL_PROJECT: project, "com.docker.compose.project": project}
    control = client.networks.create(project + "_default", driver="bridge", labels=labels)
    volume = client.volumes.create(
        project + "_home_desk-helper",
        labels={**labels, "ai-desktop-agent.vm-id": "desk-helper", LABEL_HOME: "home"},
    )
    created, owned, foreign, cleanup = [], [], [], []

    def container(name, extra, *, foreign_project=False, legacy=False, helper=False):
        owner = other if foreign_project else project
        tags = {**labels, **extra, "com.docker.compose.project": owner, LABEL_PROJECT: owner}
        if legacy:
            tags.pop(LABEL_PROJECT)
        guest = client.containers.run(
            image,
            name=project + "-" + name,
            entrypoint=["sleep"],
            command=["300"],
            detach=True,
            labels=tags,
            cap_drop=["ALL"],
            read_only=helper,
            **({"network_mode": "none"} if helper else {"network": control.name}),
            **({"volumes": {volume.name: {"bind": HOME, "mode": "ro"}}} if helper else {}),
        )
        created.append(guest)
        (foreign if foreign_project else owned).append(guest.id)
        if not foreign_project and (helper or "ai-desktop-agent.disk-copy-for" in extra):
            cleanup.append(guest.id)
        return guest

    try:
        container("vm-1", {"com.docker.compose.service": "vm"})
        container("foreign-vm", {"com.docker.compose.service": "vm"}, foreign_project=True)
        for is_foreign in (False, True):
            container(
                "dynamic-" + str(is_foreign),
                {"ai-desktop-agent.vm-id": "desk-query", "ai-desktop-agent.kind": "container"},
                foreign_project=is_foreign,
            )
        container("legacy", {"ai-desktop-agent.vm-id": "desk-legacy"}, legacy=True)
        container(
            "unallocated",
            {"ai-desktop-agent.vm-id": "desk-unallocated"},
            foreign_project=True,
            legacy=True,
        )
        for is_foreign in (False, True):
            container(
                "copy-" + str(is_foreign),
                {
                    "ai-desktop-agent.vm-id": "desk-broken",
                    "ai-desktop-agent.disk-copy-for": "desk-broken",
                },
                foreign_project=is_foreign,
            )
            container(
                "workspace-" + str(is_foreign),
                {LABEL_HELPER: "true", "ai-desktop-agent.helper-owner": "0" * 64},
                foreign_project=is_foreign,
                helper=True,
            )
        backend = client.containers.run(
            image,
            entrypoint=["sleep"],
            command=["300"],
            detach=True,
            network=control.name,
            networking_config={control.name: {"GwPriority": 1}},
            cap_drop=["ALL"],
            labels=labels,
            environment={"COMPOSE_PROJECT_NAME": project, "ALLOW_TCG_VM": "true"},
            volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
        )
        created.append(backend)
        owned.append(backend.id)
        result = backend.exec_run(
            ["/app/.venv/bin/python", "-c", PROBE, json.dumps(owned), json.dumps(foreign), project]
        )
        assert result.exit_code == 0, result.output.decode(errors="replace")
        audit = json.loads(result.output)
        assert audit["foreign_count"] == 5 and audit["owned_vms"] == 3
        assert not set(audit["inspected"]) & set(foreign)
        assert all(
            not client.api.containers(all=True, filters={"id": identifier})
            for identifier in cleanup
        )
    finally:
        for guest in reversed(created):
            with suppress(NotFound):
                guest.remove(force=True)
        for network in client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            network.remove()
        volume.remove()
        client.close()
