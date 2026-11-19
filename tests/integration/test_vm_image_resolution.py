"""Image selection remains correct under real Docker tag and discovery changes."""

import io
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

PROBE = """
import json,sys,time
from pathlib import Path
from unittest.mock import patch
import docker
from requests.exceptions import ReadTimeout
from ai_desktop_agent.server.vm_pool import VmPool,COMPOSE_SERVICE_LABEL

mode,expected,image_b,repository=sys.argv[1:]
client=docker.from_env(timeout=15)
attempts=[]
accepted=[]
class ProbePool(VmPool):
    def _fixed_containers(self,client,service):
        found=super()._fixed_containers(client,service)
        if mode=='shadow':
            found.sort(key=lambda c:c.name.endswith('manual-shadow'),reverse=True)
        return found
    def _desktop_image_ref(self,client):
        if mode=='lookup-failure':
            listing=type(client.containers).list
            def fail(collection,*args,**kwargs):
                labels=(kwargs.get('filters') or {}).get('label',[])
                if f'{COMPOSE_SERVICE_LABEL}=desktop' in labels:
                    raise ReadTimeout('injected source lookup failure')
                return listing(collection,*args,**kwargs)
            with patch.object(type(client.containers),'list',fail):
                return super()._desktop_image_ref(client)
        selected=super()._desktop_image_ref(client)
        if mode=='retag':
            client.api.tag(image_b,repository=repository,tag='live',force=True)
        return selected

pool=ProbePool(client=client,state_file='/tmp/image-probe/vms.json')
visible=pool.get_vm('desktop')
run=type(client.containers).run
def launch(collection,image,*args,**kwargs):
    attempts.append(image)
    if mode=='lookup-failure':
        raise RuntimeError('blocked fallback launch in the test; never pull an unknown image')
    created=run(collection,image,*args,**kwargs)
    accepted.append(created.id)
    if mode=='launch-response-lost':
        raise ReadTimeout('injected lost acknowledgement after actual container start')
    return created
result={'visible_primary':visible.vnc_host,'expected_image':expected}
try:
    with patch.object(type(client.containers),'run',launch):
        created=pool.create_vm(kind='container')
    container=client.containers.get(created.vnc_host)
    container.reload()
    marker=''
    for _ in range(60):
        contents=container.exec_run(['cat','/tmp/selected-image'])
        if contents.exit_code==0:
            marker=contents.output.decode()
            break
        time.sleep(0.05)
    result.update(image=container.attrs['Image'],marker=marker)
except Exception as error:
    result.update(error_type=type(error).__name__,error=str(error))
finally:
    result['launch_attempts']=attempts
    result['accepted_containers']=accepted
    result['state']=json.loads(Path('/tmp/image-probe/vms.json').read_text())
    pool.close()
    client.close()
print(json.dumps(result))
"""


@pytest.mark.parametrize("mode", ["retag", "shadow", "lookup-failure", "launch-response-lost"])
def test_creation_keeps_the_primary_image_and_rolls_back_unknown_source_state(tmp_path, mode):
    client = docker.from_env(timeout=15)
    project = "image-source-" + uuid.uuid4().hex[:10]
    labels = {LABEL_PROJECT: project, "com.docker.compose.project": project}
    repository = "ai-desktop-audit-01a1ea83-" + project
    base = client.images.get(
        os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    ).id
    images, control = [], None

    def image(marker, tag):
        command = [
            "/app/.venv/bin/python",
            "-c",
            "import os,time;from pathlib import Path;"
            "pending=Path('/tmp/selected-image.pending');"
            "pending.write_text(os.environ['IMAGE_MARKER']);"
            "pending.replace('/tmp/selected-image');time.sleep(180)",
        ]
        dockerfile = (
            f"FROM {base}\nLABEL {LABEL_PROJECT}={project}\n"
            f"ENV IMAGE_MARKER={marker}\nCMD {json.dumps(command)}\n"
        )
        built, _ = client.images.build(
            fileobj=io.BytesIO(dockerfile.encode()), tag=tag, rm=True, pull=False
        )
        assert built.id != base
        images.append(built.id)
        return built.id

    try:
        primary_image = image("primary", repository + ":live")
        shadow_image = image("shadow", repository + ":other")
        assert primary_image != shadow_image
        network = client.networks.create(project + "_default", driver="bridge", labels=labels)
        primary = client.containers.run(
            primary_image,
            name=project + "-desktop-1",
            detach=True,
            network=network.name,
            cap_drop=["ALL"],
            labels={**labels, "com.docker.compose.service": "desktop"},
        )
        if mode == "shadow":
            client.containers.run(
                shadow_image,
                name=project + "-manual-shadow",
                detach=True,
                network=network.name,
                cap_drop=["ALL"],
                labels={**labels, "com.docker.compose.service": "desktop"},
            )
        control = client.containers.run(
            base,
            name=project + "-backend",
            entrypoint=["python3", "-c"],
            command=["import time;time.sleep(180)"],
            detach=True,
            network=network.name,
            networking_config={network.name: {"GwPriority": 1}},
            cap_drop=["ALL"],
            labels={**labels, "com.docker.compose.service": "backend"},
            environment={"COMPOSE_PROJECT_NAME": project},
            volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
        )
        execution = control.exec_run(
            [
                "/app/.venv/bin/python",
                "-c",
                PROBE,
                mode,
                primary_image,
                shadow_image,
                repository,
            ]
        )
        assert execution.exit_code == 0, execution.output.decode(errors="replace")
        result = json.loads(execution.output.decode().splitlines()[-1])
        (tmp_path / "image-resolution-proof.json").write_text(json.dumps(result, indent=2))
        assert result["visible_primary"] == primary.name
        if mode in {"lookup-failure", "launch-response-lost"}:
            assert result["error_type"] == "DockerUnavailableError"
            assert result["state"] == {}
            if mode == "lookup-failure":
                assert result["launch_attempts"] == [] and result["accepted_containers"] == []
            else:
                assert len(result["launch_attempts"]) == len(result["accepted_containers"]) == 1
            assert not client.containers.list(
                all=True,
                filters={"label": [f"{LABEL_PROJECT}={project}", "ai-desktop-agent.vm-id"]},
            )
            assert not client.volumes.list(filters={"label": f"{LABEL_PROJECT}={project}"})
            assert {
                item.name
                for item in client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"})
            } == {project + "_default"}
        else:
            assert result["image"] == primary_image and result["marker"] == "primary"
    finally:
        for container in client.containers.list(
            all=True, filters={"label": f"{LABEL_PROJECT}={project}"}
        ):
            with suppress(NotFound):
                container.remove(force=True)
        for network in client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            network.remove()
        for volume in client.volumes.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            volume.remove(force=True)
        for identifier in images:
            with suppress(NotFound):
                owned = client.images.get(identifier)
                assert owned.attrs["Config"]["Labels"][LABEL_PROJECT] == project
                client.images.remove(identifier, force=True, noprune=True)
        client.close()
