"""Verify primary Android target discovery, DNS and setup identity with real Docker."""

import json
import os
import uuid
from contextlib import suppress

import docker
import pytest
from docker.errors import NotFound

from ai_desktop_agent.server.networks import LABEL_PROJECT

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]

GUEST = """import socket,sys
server=socket.socket()
server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
server.bind(('0.0.0.0',5555))
server.listen(8)
print('ready',flush=True)
while True:
    connection,_=server.accept()
    with connection: connection.sendall(sys.argv[1].encode())
"""

PROBE = """import json,socket,sys,time
from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.server.vm_pool import VmPool
pool=VmPool(state_file='/tmp/primary-probe/vms.json')
setup=AndroidProvisioner(pool=pool,state_file='/tmp/primary-probe/setup.json')
vm=pool.get_vm('android')
assert vm is not None
target=setup._target('android')
host=target.host if target else None
assert host==vm.vnc_host, (host,vm.vnc_host)
for attempt in range(50):
    try:
        with socket.create_connection((host,5555),timeout=1) as connection:
            marker=connection.recv(100).decode()
        break
    except OSError:
        if attempt==49: raise
        time.sleep(0.05)
assert marker==sys.argv[2],marker
assert setup.needs('android') is (sys.argv[1]!='shadow-replaced')
if sys.argv[1]!='shadow-replaced': setup.mark_done('android')
assert not setup.needs('android')
print(json.dumps({'host':host,'instance_id':pool.instance_id('android'),'data_id':setup._data_id('android'),'marker':marker}))
"""


@pytest.mark.parametrize("with_shadow", [False, True])
def test_android_target_remains_reachable_and_tracks_only_primary_replacement(with_shadow):
    client = docker.from_env(timeout=15)
    project = "primary-target-" + uuid.uuid4().hex[:10]
    labels = {LABEL_PROJECT: project, "com.docker.compose.project": project}
    image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    control = client.networks.create(project + "_default", driver="bridge", labels=labels)
    created = []

    def guest(name, marker):
        container = client.containers.run(
            image,
            name=name,
            entrypoint=["python3", "-u", "-c"],
            command=[GUEST, marker],
            detach=True,
            network=control.name,
            cap_drop=["ALL"],
            labels={**labels, "com.docker.compose.service": "android"},
        )
        created.append(container)
        return container

    def probe(mode, marker):
        result = backend.exec_run(["/app/.venv/bin/python", "-c", PROBE, mode, marker])
        assert result.exit_code == 0, result.output.decode(errors="replace")
        return json.loads(result.output)

    try:
        primary = guest(project + "-android-1", "primary-v1")
        shadow = guest(project + "-manual-shadow", "shadow-v1") if with_shadow else None
        backend = client.containers.run(
            image,
            name=project + "-backend",
            entrypoint=["python3", "-c"],
            command=["import time; time.sleep(300)"],
            detach=True,
            network=control.name,
            networking_config={control.name: {"GwPriority": 1}},
            cap_drop=["ALL"],
            labels={**labels, "com.docker.compose.service": "backend"},
            environment={"COMPOSE_PROJECT_NAME": project, "VM_HOST_DIR": "/tmp/no-android-data"},
            volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
        )
        created.append(backend)
        first = probe("initial", "primary-v1")
        assert first["host"] == primary.name and first["instance_id"] == primary.id
        assert first["data_id"] == "container:" + primary.id
        primary.reload()
        assert set(primary.attrs["NetworkSettings"]["Networks"]) == {project + "_env_android"}
        if shadow is not None:
            shadow.reload()
            assert set(shadow.attrs["NetworkSettings"]["Networks"]) == {control.name}
            name = shadow.name
            shadow.remove(force=True)
            shadow = guest(name, "shadow-v2")
            assert probe("shadow-replaced", "primary-v1")["instance_id"] == primary.id
        name = primary.name
        previous_id = primary.id
        primary.remove(force=True)
        primary = guest(name, "primary-v2")
        after = probe("primary-replaced", "primary-v2")
        assert after["instance_id"] == primary.id and primary.id != previous_id
        assert after["data_id"] == "container:" + primary.id
    finally:
        for container in reversed(created):
            with suppress(NotFound):
                container.remove(force=True)
        for network in client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            network.remove()
        client.close()
