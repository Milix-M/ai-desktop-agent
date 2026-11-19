"""Pending admission remains owned through concurrent HTTP deletion on real VNC."""

import json
import os
import uuid

import docker
import pytest

from tests.integration.test_desktop_smoke import desktop as desktop
from tests.integration.test_desktop_smoke import vnc_runtime as vnc_runtime

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real desktop opt-in"),
]

PROGRAM = r"""
import asyncio
import hashlib
import json
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.vnc_client import VNCClient

async def main():
    made = []
    queued, release_start = asyncio.Event(), asyncio.Event()
    model_entered, finish_model = asyncio.Event(), asyncio.Event()
    requests = []
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    store = TaskStore(Path(tempfile.mkdtemp()) / "records")
    server._store = store
    server._resolve_vm = lambda vm: SimpleNamespace(id=vm, vnc_host="127.0.0.1")

    class WaitingModel(MockLLMProvider):
        async def decide_next_action(self, *args, **kwargs):
            requests.append("decide")
            model_entered.set()
            await finish_model.wait()
            return await super().decide_next_action(*args, **kwargs)

    def create(display):
        session = TaskSession(llm=WaitingModel(), display=display, store=store, decision=None)
        made.append(session)
        return session

    server._create_session = create

    def factory(loop, coro, **kwargs):
        if getattr(coro, "cr_code", None) is TaskSession.start_async.__code__:
            async def observed_start():
                queued.set()
                await release_start.wait()
                return await coro
            scheduled = observed_start()
        else:
            scheduled = coro
        if previous_factory is not None:
            return previous_factory(loop, scheduled, **kwargs)
        return asyncio.Task(scheduled, loop=loop, **kwargs)

    loop.set_task_factory(factory)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    api = uvicorn.Server(uvicorn.Config(server.app, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(api.serve(sockets=[listener]))
    post = None
    proof = {"app_sha256": hashlib.sha256(Path(server.__file__).read_bytes()).hexdigest()}
    try:
        async with asyncio.timeout(10):
            while not api.started:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
            headers={"Authorization": "Bearer admission-delete-local-test-key-0123456789"},
            timeout=10,
        ) as client:
            headers = {"Idempotency-Key": "native-admission-delete-0123456789"}
            body = {"instruction": "保存済み文書を確認する", "vm_id": "desktop"}
            post = asyncio.create_task(client.post("/tasks", json=body, headers=headers))
            await asyncio.wait_for(queued.wait(), 10)
            pending = (await client.get("/tasks/current")).json()
            task_id = pending["session_id"]
            assert task_id and pending["state"] == "idle"
            refused = await client.delete("/tasks/" + task_id)
            proof.update(
                pending_delete=refused.status_code,
                retry_after=refused.headers.get("Retry-After"),
            )
            release_start.set()
            admitted = await asyncio.wait_for(asyncio.shield(post), 10)
            proof["admitted"] = admitted.status_code
            assert admitted.status_code == 200, admitted.text
            await asyncio.wait_for(model_entered.wait(), 10)
            frame = await asyncio.to_thread(made[0].display.capture_raw)
            assert frame.image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
            current = (await client.get("/tasks/current", params={"vm_id":"desktop"})).json()
            pause = await client.post(
                "/tasks/current/pause", params={"vm_id":"desktop", "session_id":task_id}
            )
            proof.update(
                live_current=current["is_running"],
                actual_owner_running=made[0].is_running,
                pause=pause.status_code,
                vnc_connected=made[0].display.is_connected,
                frame_size=[frame.width, frame.height],
                frame_sha256=hashlib.sha256(frame.image_bytes).hexdigest(),
            )
            print("ADMISSION_DELETE_PROOF=" + json.dumps(proof), flush=True)
            assert proof["pending_delete"] == 409 and proof["retry_after"] == "1", proof
            assert (
                proof["live_current"] and proof["actual_owner_running"]
                and proof["pause"] == 200
            ), proof
            deleted = await client.delete("/tasks/" + task_id)
            replay = await client.post("/tasks", json=body, headers=headers)
            absent = await client.get("/tasks/" + task_id)
            proof.update(
                final_delete=deleted.status_code,
                replay=replay.status_code,
                detail=absent.status_code,
                drained=not made[0].is_running,
                vnc_closed=not made[0].display.is_connected,
                model_requests=requests,
                history_count=len(store.list(limit=None)),
            )
            print("ADMISSION_DELETE_PROOF=" + json.dumps(proof), flush=True)
            assert (
                proof["final_delete"] == 200 and proof["detail"] == 404
                and proof["replay"] == 410
            ), proof
            assert proof["drained"] and proof["vnc_closed"] and proof["history_count"] == 0, proof
            assert len(made) == 1 and requests == ["decide"], proof
    finally:
        release_start.set()
        finish_model.set()
        if post is not None:
            await asyncio.gather(post, return_exceptions=True)
        for session in made:
            session.stop()
        await asyncio.gather(*(session.aclose() for session in made))
        loop.set_task_factory(previous_factory)
        api.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()

try:
    asyncio.run(main())
finally:
    VNCClient.shutdown_runtime()
"""


def test_pending_http_delete_preserves_control_and_eventual_deletion_on_real_vnc(desktop, tmp_path):
    client = docker.from_env(timeout=30)
    container = None
    owner = uuid.uuid4().hex
    try:
        container = client.containers.run(
            os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke"),
            ["/app/.venv/bin/python", "-c", PROGRAM],
            detach=True,
            network_mode="container:" + desktop.id,
            environment={"AGENT_ACCESS_TOKEN": "admission-delete-local-test-key-0123456789"},
            labels={
                "ai-desktop-agent.smoke": owner,
                "com.docker.compose.service": "backend",
            },
        )
        status = container.wait(timeout=90)
        output = container.logs().decode()
        (tmp_path / "admission-delete-container.log").write_text(output)
        proofs = [
            json.loads(line.removeprefix("ADMISSION_DELETE_PROOF="))
            for line in output.splitlines()
            if line.startswith("ADMISSION_DELETE_PROOF=")
        ]
        (tmp_path / "admission-delete-container-proof.json").write_text(
            json.dumps({"image": container.image.id, "observations": proofs}, indent=2)
        )
        assert status["StatusCode"] == 0, output
        assert len(proofs) == 2 and proofs[-1]["drained"] and proofs[-1]["vnc_closed"]
    finally:
        try:
            owned = (
                [container]
                if container is not None
                else client.containers.list(
                    all=True, filters={"label": "ai-desktop-agent.smoke=" + owner}
                )
            )
            for item in owned:
                item.remove(force=True)
        finally:
            client.close()
