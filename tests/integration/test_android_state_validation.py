"""Damaged setup metadata cannot prepare or configure an actual owned container."""

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

PROBE = r"""
import json,sys
from pathlib import Path
import docker
from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.server.vm_pool import VmPool
mode,guest_id=sys.argv[1:]
client=docker.from_env(timeout=10)
guest=client.containers.get(guest_id)
root=Path('/tmp/android-state');root.mkdir()
state=root/'setup.json'
record={'done':False,'data_id':'pending','at':1}
records={'android':record}
if mode=='empty-identity':record['data_id']=''
elif mode=='boolean-time':record['at']=False
elif mode=='nan-time':record['at']=float('nan')
elif mode=='invalid-id':records['../saved']={'done':True,'data_id':'known'}
elif mode.startswith('extension-'):
    record['future_extension']=[{'label':'報告書 😀','values':[0,1.25,False,None,[]]}]
    if mode!='extension-valid':
        value={'extension-nan':float('nan'),'extension-unicode':'\ud800',
               'extension-overflow':'__OVERFLOW__'}[mode]
        records['and-other']={'done':True,'data_id':'known','future_extension':[{'value':value}]}
else:
    records={'android':{'done':True,'data_id':'container:'+guest_id},
             'and-legacy':{'done':False,'data_id':'known','at':0,'future_extension':{'revision':2}}}
original=json.dumps(records).replace('"__OVERFLOW__"','1e999');state.write_text(original)
commands=[];configured=[]
class ObservedPool(VmPool):
    def prepare_vm(self,vm_id):
        commands.append(vm_id)
        return super().prepare_vm(vm_id)
pool=ObservedPool(client=client,state_file=root/'vms.json')
setup=AndroidProvisioner(pool=pool,state_file=state)
def configure(host,**kwargs):
    command="from pathlib import Path;Path('/tmp/setup-marker').write_text('configured')"
    result=guest.exec_run(['python3','-c',command])
    assert result.exit_code==0,result.output
    configured.append(host)
    return True
setup.provision_vm=configure
def snapshot():
    guest.reload()
    return {'status':guest.status,'started':guest.attrs['State']['StartedAt'],
            'networks':sorted(guest.attrs['NetworkSettings']['Networks'])}
proof={'mode':mode,'before':snapshot()}
try:setup.ensure()
except Exception as error:proof.update(error=type(error).__name__)
proof.update(after=snapshot(),commands=commands,configured=configured,
             state_unchanged=state.read_text()==original,
             marker=guest.exec_run(['test','-e','/tmp/setup-marker']).exit_code==0)
proof['extension_preserved']=(json.loads(state.read_text())['android'].get('future_extension')
                              ==record.get('future_extension'))
pool.close();client.close()
print(json.dumps(proof))
"""


@pytest.mark.parametrize(
    "damage",
    [
        "empty-identity",
        "boolean-time",
        "nan-time",
        "invalid-id",
        "legacy",
        "extension-nan",
        "extension-unicode",
        "extension-overflow",
        "extension-valid",
    ],
)
def test_invalid_saved_setup_state_stops_before_real_container_preparation(tmp_path, damage):
    client = docker.from_env(timeout=30)
    project = "android-state-" + uuid.uuid4().hex[:10]
    labels = {LABEL_PROJECT: project, "com.docker.compose.project": project}
    image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    created = []
    network = None
    try:
        network = client.networks.create(project + "_default", driver="bridge", labels=labels)
        guest = client.containers.run(
            image,
            name=project + "-android-1",
            entrypoint=["sleep"],
            command=["180"],
            detach=True,
            network=network.name,
            cap_drop=["ALL"],
            labels={**labels, "com.docker.compose.service": "android"},
        )
        created.append(guest)
        control = client.containers.run(
            image,
            name=project + "-backend",
            entrypoint=["sleep"],
            command=["180"],
            detach=True,
            network=network.name,
            networking_config={network.name: {"GwPriority": 1}},
            cap_drop=["ALL"],
            labels={**labels, "com.docker.compose.service": "backend"},
            environment={"COMPOSE_PROJECT_NAME": project},
            volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
        )
        created.append(control)
        result = control.exec_run(["/app/.venv/bin/python", "-c", PROBE, damage, guest.id])
        (tmp_path / "android-state-controller.log").write_text(
            result.output.decode(errors="replace")
        )
        assert result.exit_code == 0, result.output.decode(errors="replace")
        proof = json.loads(result.output.decode().splitlines()[-1])
        (tmp_path / "android-state-proof.json").write_text(json.dumps(proof, indent=2))
        if damage == "extension-valid":
            assert "error" not in proof and proof["commands"] == ["android"], proof
            assert len(proof["configured"]) == 1 and proof["marker"], proof
            assert not proof["state_unchanged"] and proof["extension_preserved"], proof
        else:
            assert proof.get("error") == (None if damage == "legacy" else "StorageError"), proof
            assert proof["commands"] == proof["configured"] == [], proof
            assert proof["state_unchanged"] and not proof["marker"], proof
            assert proof["before"] == proof["after"], proof
    finally:
        for target in created:
            with suppress(NotFound):
                target.remove(force=True)
        for target in client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            target.remove()
        client.close()
