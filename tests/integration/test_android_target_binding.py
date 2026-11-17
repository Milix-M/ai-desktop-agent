"""Bind setup to one real Docker instance across promotion and replacement."""

import json
import os
import uuid
from contextlib import suppress

import docker
import pytest
from docker.errors import NotFound

from ai_desktop_agent.server.networks import LABEL_PROJECT
from tests.integration.test_vm_primary import GUEST

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]

PROBE = r"""import json,os,socket,sys,threading,time,uuid
from pathlib import Path
import docker
from ai_desktop_agent.server import android_adb as adb
from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.server.vm_pool import VmPool
image,project,guest,persistent=sys.argv[1:]
client=docker.from_env(timeout=5)
base=Path('/tmp/android-binding')
base.mkdir()
if persistent=='true':
    image_file=base/'android/android-data.img'
    image_file.parent.mkdir()
    block=bytearray(1024+0x78)
    block[1024+0x38:1024+0x3a]=b'\x53\xef'
    block[1024+0x68:1024+0x78]=uuid.UUID('11111111-1111-1111-1111-111111111111').bytes
    image_file.write_bytes(block)
labels={'ai-desktop-agent.project':project,'com.docker.compose.project':project,'com.docker.compose.service':'android'}
def create(marker):
    return client.containers.run(
        image,name=project+'-android-1',entrypoint=['python3','-u','-c'],
        command=[guest,marker],detach=True,network=project+'_default',labels=labels,cap_drop=['ALL'])
pool=VmPool(client=client,state_file=base/'vms.json')
setup=AndroidProvisioner(pool=pool,state_file=base/'setup.json',apks_dir=base/'apks')
original_prepare=pool.prepare_vm
primary=None
def prepare(vm_id):
    global primary
    if primary is None: primary=create('primary')
    original_prepare(vm_id)
pool.prepare_vm=prepare
configured=[]
def connect(host):
    for attempt in range(50):
        try:
            with socket.create_connection((host,5555),timeout=1) as connection:
                return connection.recv(100).decode()
        except OSError:
            if attempt==49: raise
            time.sleep(0.05)
def provision(host,**kwargs):
    configured.append(connect(host))
    return True
original_provision=setup.provision_vm
setup.provision_vm=provision
setup.ensure()
assert not configured,{'configured':configured,'wrong_completion':not setup.needs('android')}
assert setup.needs('android')
setup.ensure()
assert configured==['primary'],configured
assert not setup.needs('android')
setup._state_file.unlink()
setup.provision_vm=original_provision
command=base/'command.json'
executable=base/'adb-probe'
executable.write_text('#!'+sys.executable+'\n'+'''import json,os,socket,sys,time
from pathlib import Path
serial=sys.argv[sys.argv.index('-s')+1]
host,port=serial.rsplit(':',1)
with socket.create_connection((host,int(port)),timeout=2) as connection:
    marker=connection.recv(100).decode()
ready=Path('/tmp/android-binding/command.tmp')
ready.write_text(json.dumps({'marker':marker,'pid':os.getpid()}))
ready.replace('/tmp/android-binding/command.json')
time.sleep(20)
''')
executable.chmod(0o700)
adb._adb_bin=lambda:str(executable)
setup._wait_adb_serial=lambda host,**kwargs:host+':5555'
adb.setup_sleep=lambda serial,**kwargs:kwargs['runner'](serial,'shell','long-step')
worker=threading.Thread(target=setup.ensure)
worker.start()
try:
    deadline=time.monotonic()+5
    while not command.exists() and time.monotonic()<deadline: time.sleep(0.02)
    assert command.exists(),'the first command did not reach the original primary'
    observed=json.loads(command.read_text())
    assert observed['marker']=='primary',observed
    previous=primary.id
    primary.remove(force=True)
    primary=create('replacement')
    assert primary.id!=previous
    worker.join(5)
    assert not worker.is_alive(),'setup kept running against a replaced container'
    assert setup.needs('android'),'replacement was recorded as configured'
    try: os.kill(observed['pid'],0)
    except ProcessLookupError: pass
    else: raise AssertionError('the ADB child was not reaped')
finally:
    setup._stop_requested.set()
    worker.join(2)
    assert not worker.is_alive()
print(json.dumps({'configured':configured,'replacement_pending':setup.needs('android'),'pid_reaped':True,'persistent':persistent}))
client.close()
"""


@pytest.mark.parametrize("persistent", [False, True])
def test_setup_promotion_and_replacement_keep_the_record_bound_to_the_configured_instance(
    persistent,
):
    client = docker.from_env(timeout=30)
    project = "android-binding-" + uuid.uuid4().hex[:10]
    labels = {LABEL_PROJECT: project, "com.docker.compose.project": project}
    image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    control = client.networks.create(project + "_default", driver="bridge", labels=labels)
    try:
        client.containers.run(
            image,
            name=project + "-manual-only",
            entrypoint=["python3", "-u", "-c"],
            command=[GUEST, "manual"],
            detach=True,
            network=control.name,
            cap_drop=["ALL"],
            labels={**labels, "com.docker.compose.service": "android"},
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
            environment={"COMPOSE_PROJECT_NAME": project, "VM_HOST_DIR": "/tmp/android-binding"},
            volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
        )
        result = backend.exec_run(
            [
                "/app/.venv/bin/python",
                "-c",
                PROBE,
                image,
                project,
                GUEST,
                "true" if persistent else "false",
            ]
        )
        assert result.exit_code == 0, result.output.decode(errors="replace")
        observed = json.loads(result.output)
        assert observed["configured"] == ["primary"]
        assert observed["replacement_pending"] and observed["pid_reaped"]
    finally:
        for container in client.containers.list(
            all=True, filters={"label": f"{LABEL_PROJECT}={project}"}, sparse=True
        ):
            with suppress(NotFound):
                container.remove(force=True)
        for network in client.networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
            network.remove()
        client.close()
