"""Real bridges and endpoints survive refused ownership and ineffective SDK calls."""

import json
import os
import uuid
from contextlib import suppress

import docker
import pytest
from docker.errors import NotFound

from ai_desktop_agent.server.networks import LABEL_ISOLATED, LABEL_PROJECT, LABEL_VM_ID

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]

PROBE = r"""import json,os
from unittest.mock import patch
import docker
from docker.errors import NotFound
from docker.models.networks import Network
from ai_desktop_agent.server.networks import EnvironmentNetworks
from ai_desktop_agent.server.vm_control import DockerUnavailableError

project,mode,target_id=os.environ['TEST_PROJECT'],os.environ['TEST_MODE'],os.environ['TARGET_ID']
client=docker.from_env(timeout=15)
manager=EnvironmentNetworks(client,project)
target=client.containers.get(target_id)
backend=manager.backend()
private_name=manager.name_for('desktop')
management=manager.management_name
suppressed=[]

def snapshot():
    target.reload();backend.reload()
    return dict(target_networks=sorted(target.attrs['NetworkSettings']['Networks']),
                backend_networks=sorted(backend.attrs['NetworkSettings']['Networks']),
                target_started=target.attrs['State']['StartedAt'],target_status=target.status)

def marker():
    value=target.exec_run(['/app/.venv/bin/python','-c',
        "from pathlib import Path; print(Path('/tmp/saved-report').read_text())"])
    assert value.exit_code==0,value.output
    return value.output.decode().strip()

try:
    assert marker()=='quarterly report'
    if mode in {'foreign-owner','in-use'}:
        private=client.networks.get(private_name)
        private.connect(backend)
        private.reload()
        members_before=sorted(private.attrs['Containers'])
        before=snapshot()
        try:manager.remove('desktop')
        except DockerUnavailableError as error:reason=str(error)
        else:raise AssertionError('unverified bridge removed')
        private.reload()
        failed=snapshot()
        assert failed==before
        assert sorted(private.attrs['Containers'])==members_before
        assert reason==('削除対象ネットワークの所属が一致しません' if mode=='foreign-owner'
                        else '使用中の専用ネットワークは削除できません')
        if mode=='in-use':
            private.disconnect(target)
            manager.remove('desktop')
            try:client.networks.get(private_name)
            except NotFound:pass
            else:raise AssertionError('released bridge retained')
        after=snapshot()
    else:
        before=snapshot()
        original_connect,original_disconnect=Network.connect,Network.disconnect
        def connect(network,container,*args,**kwargs):
            if mode=='join-no-effect' and network.name==private_name and container.id==target_id:
                suppressed.append('connect');return
            return original_connect(network,container,*args,**kwargs)
        def disconnect(network,container,*args,**kwargs):
            if mode=='detach-no-effect' and network.name==management and container.id==target_id:
                suppressed.append('disconnect');return
            return original_disconnect(network,container,*args,**kwargs)
        with patch.object(Network,'connect',connect),patch.object(Network,'disconnect',disconnect):
            try:manager.isolate('desktop',target)
            except DockerUnavailableError as error:reason=str(error)
            else:raise AssertionError('ineffective isolation reported success')
        failed=snapshot()
        assert suppressed==(['connect'] if mode=='join-no-effect' else ['disconnect'])
        assert failed['target_networks']==([management] if mode=='join-no-effect'
                                          else sorted([management,private_name]))
        assert reason==('専用ネットワークへ接続できません: desktop' if mode=='join-no-effect'
                        else '環境のネットワークを分離できません: desktop')
        assert manager.isolate('desktop',target)==private_name
        after=snapshot()
        assert after['target_networks']==[private_name]
    assert management in after['backend_networks']
    assert before['target_started']==after['target_started']
    assert after['target_status']=='running' and marker()=='quarterly report'
    print(json.dumps(dict(mode=mode,reason=reason,before=before,failed=failed,after=after,
                         suppressed=suppressed,marker='quarterly report')))
finally:client.close()
"""


@pytest.mark.parametrize("mode", ["foreign-owner", "in-use", "join-no-effect", "detach-no-effect"])
def test_failed_isolation_and_refused_removal_preserve_actual_endpoints_and_saved_data(
    tmp_path, mode
):
    client = docker.from_env(timeout=15)
    project = "network-guards-" + uuid.uuid4().hex[:10]
    ownership = {"ai-desktop-agent.network-guard-test": project}
    labels = {**ownership, LABEL_PROJECT: project}
    image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    try:
        management = client.networks.create(project + "_default", driver="bridge", labels=labels)
        removing = mode in {"foreign-owner", "in-use"}
        if removing:
            initial = client.networks.create(
                project + "_env_desktop",
                driver="bridge",
                labels={
                    **ownership,
                    LABEL_PROJECT: project + "-other" if mode == "foreign-owner" else project,
                    LABEL_VM_ID: "desktop",
                    LABEL_ISOLATED: "true",
                },
            )
        else:
            initial = management
        target = client.containers.run(
            image,
            name=project + "-target",
            detach=True,
            entrypoint=["/app/.venv/bin/python", "-c"],
            command=[
                "from pathlib import Path; import time; "
                "report=Path('/tmp/saved-report.pending'); "
                "report.write_text('quarterly report'); report.replace('/tmp/saved-report'); "
                "time.sleep(180)"
            ],
            network=initial.name,
            cap_drop=["ALL"],
            labels={**labels, "com.docker.compose.service": "guard-target"},
        )
        # exec waits for the entrypoint to publish its initial artifact.
        readiness = target.exec_run(
            [
                "/app/.venv/bin/python",
                "-c",
                "from pathlib import Path; import time; "
                "deadline=time.monotonic()+5\n"
                "while not Path('/tmp/saved-report').exists():\n"
                " assert time.monotonic()<deadline; time.sleep(0.01)",
            ]
        )
        assert readiness.exit_code == 0, readiness.output
        backend = client.containers.run(
            image,
            name=project + "-backend",
            detach=True,
            entrypoint=["/app/.venv/bin/python", "-c"],
            command=[PROBE],
            network=management.name,
            networking_config={management.name: {"GwPriority": 1}},
            cap_drop=["ALL"],
            labels={**labels, "com.docker.compose.service": "backend"},
            environment={"TEST_PROJECT": project, "TEST_MODE": mode, "TARGET_ID": target.id},
            volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
        )
        assert backend.wait(timeout=45)["StatusCode"] == 0, backend.logs().decode()
        proof = json.loads(backend.logs())
        (tmp_path / "network-guard-proof.json").write_text(json.dumps(proof, indent=2))
        assert proof["mode"] == mode and proof["marker"] == "quarterly report"
        assert proof["after"]["target_started"] == proof["before"]["target_started"]
        assert proof["after"]["target_status"] == "running"
    finally:
        for container in client.containers.list(
            all=True, filters={"label": "ai-desktop-agent.network-guard-test=" + project}
        ):
            with suppress(NotFound):
                container.remove(force=True)
        networks = {
            network.id: network
            for label in [
                "ai-desktop-agent.network-guard-test=" + project,
                LABEL_PROJECT + "=" + project,
            ]
            for network in client.networks.list(filters={"label": label})
        }
        for network in networks.values():
            network.remove()
        client.close()
