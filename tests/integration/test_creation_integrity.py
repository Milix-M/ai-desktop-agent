"""Actual Docker creations must not follow ambiguous durable recovery records."""

import json
import os
import uuid
from contextlib import suppress

import docker
import pytest
from docker.errors import NotFound

from ai_desktop_agent.server.networks import LABEL_PROJECT, LABEL_VM_ID

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]

PROBE = """
import asyncio,hashlib,json,sys,threading,time
from pathlib import Path
from unittest.mock import patch
import docker
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.vm_creations import (
    CreationRecord,CreationStore,VmCreationService,_fingerprint)
from ai_desktop_agent.server.vm_pool import VmPool,LABEL_VM_ID

mode=sys.argv[1]
client=docker.from_env(timeout=15)
root=Path('/tmp/creation-integrity');root.mkdir()
store=CreationStore(root/'requests')
entered,release=threading.Event(),threading.Event()
accepted=[]
launch=type(client.containers).run
def marked(collection,image,*args,**kwargs):
    vm_id=kwargs.get('labels',{}).get(LABEL_VM_ID)
    if vm_id:
        kwargs['entrypoint']=['/app/.venv/bin/python','-c']
        kwargs['command']=["from pathlib import Path;import os,time;"
            "Path('/tmp/created-marker').write_text(os.environ['CREATED_VM_ID']);time.sleep(180)"]
        kwargs['environment']={**kwargs.get('environment',{}),'CREATED_VM_ID':vm_id}
    result=launch(collection,image,*args,**kwargs)
    if vm_id:accepted.append({'vm_id':vm_id,'container_id':result.id})
    return result
class ControlledPool(VmPool):
    uncertain=True
    def create_vm(self,*args,**kwargs):
        result=super().create_vm(*args,**kwargs)
        if mode=='uncertain' and self.uncertain:
            self.uncertain=False
            entered.set()
            if not release.wait(15):raise RuntimeError('test did not release accepted creation')
            raise StorageError('injected uncertainty after an actual accepted container')
        return result
pool=ControlledPool(client=client,state_file=root/'vms.json')
key='creation-integrity-primary-0123456789'
digest=hashlib.sha256(key.encode()).hexdigest()
record=CreationRecord(id='creation-'+digest,fingerprint=_fingerprint('primary','container'),
    vm_id='desk-'+digest[:24],name='primary',kind='container',state='queued',created_at=1.,updated_at=1.)
proof={'mode':mode}
async def exercise():
    service=VmCreationService(pool,store)
    recovered=None
    try:
        if mode=='invalid-name':
            try:pool.create_vm(name='bad\\ud800name',kind='container')
            except Exception as error:
                proof.update(error_type=type(error).__name__,error=str(error))
        elif mode=='uncertain':
            first=await service.submit(key,'primary','container')
            if not await asyncio.to_thread(entered.wait,10):
                raise RuntimeError('creation did not enter test barrier')
            second=await service.submit(key+'-second','second','container')
            release.set()
            for job in [first,second]:
                try:await service.wait(job.id)
                except StorageError:pass
                else:raise RuntimeError('uncertain storage failure was hidden')
            proof['before_recovery']={job.id:store.read(job.id).state for job in [first,second]}
            await service.close()
            recovered=VmCreationService(pool,CreationStore(store.directory))
            await recovered.start()
            proof['states']=[(await recovered.wait(job.id)).state for job in [first,second]]
        else:
            store.write(record)
            if mode=='receipt':
                path=store._path(record.id)
                proof['damaged']='{\"state\":\"failed\",'+path.read_text()[1:]
            elif mode=='allocations':
                path=root/'vms.json'
                proof['damaged']='{\"desk-other\":{\"name\":\"original\"},\"desk-other\":{\"name\":\"replacement\"}}'
            if mode in {'receipt','allocations'}:path.write_text(proof['damaged'])
            try:
                await service.start()
                result=await service.wait(record.id)
                proof['states']=[result.state]
            except Exception as error:
                proof.update(error_type=type(error).__name__,error=str(error))
            if mode in {'receipt','allocations'}:proof['saved']=path.read_text()
        proof['accepted']=accepted
        markers={}
        for item in accepted:
            target=client.containers.get(item['container_id'])
            for _ in range(100):
                out=target.exec_run(['cat','/tmp/created-marker'])
                if out.exit_code==0:markers[target.id]=out.output.decode();break
                await asyncio.sleep(.05)
        proof['markers']=markers
    finally:
        release.set()
        await service.close()
        if recovered is not None:await recovered.close()
        pool.close()
with patch.object(type(client.containers),'run',marked):asyncio.run(exercise())
client.close()
print(json.dumps(proof))
"""


@pytest.mark.parametrize("mode", ["receipt", "allocations", "valid", "uncertain", "invalid-name"])
def test_recovery_rejects_ambiguous_records_and_preserves_actual_accepted_environments(
    tmp_path, mode
):
    client = docker.from_env(timeout=15)
    project = "creation-integrity-" + uuid.uuid4().hex[:10]
    labels = {LABEL_PROJECT: project, "com.docker.compose.project": project}
    image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    try:
        management = client.networks.create(project + "_default", labels=labels)
        control = client.containers.run(
            image,
            detach=True,
            name=project + "-backend",
            network=management.name,
            networking_config={management.name: {"GwPriority": 1}},
            cap_drop=["ALL"],
            entrypoint=["python3", "-c"],
            command=["import time;time.sleep(180)"],
            labels={**labels, "com.docker.compose.service": "backend"},
            volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
            environment={"COMPOSE_PROJECT_NAME": project, "DESKTOP_IMAGE_REF": image},
        )
        execution = control.exec_run(["/app/.venv/bin/python", "-c", PROBE, mode])
        assert execution.exit_code == 0, execution.output.decode(errors="replace")
        proof = json.loads(execution.output.decode().splitlines()[-1])
        (tmp_path / "creation-integrity-proof.json").write_text(json.dumps(proof, indent=2))
        targets = client.containers.list(
            all=True, filters={"label": [f"{LABEL_PROJECT}={project}", LABEL_VM_ID]}
        )
        if mode == "invalid-name":
            assert proof.get("error_type") == "ValueError", proof
            assert proof["accepted"] == [] and targets == []
        elif mode in {"receipt", "allocations"}:
            assert proof.get("error_type") == "StorageError", proof
            assert proof["saved"] == proof["damaged"]
            assert proof["accepted"] == [] and targets == []
            assert client.volumes.list(filters={"label": f"{LABEL_PROJECT}={project}"}) == []
        else:
            count = 2 if mode == "uncertain" else 1
            assert proof["states"] == ["succeeded"] * count
            assert len(proof["accepted"]) == len(targets) == count
            assert proof["markers"] == {
                item["container_id"]: item["vm_id"] for item in proof["accepted"]
            }
            if mode == "uncertain":
                assert sorted(proof["before_recovery"].values()) == ["queued", "running"]
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
        client.close()
