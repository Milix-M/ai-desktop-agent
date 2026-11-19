"""The Android worker must fail closed if its active-task reference disappears."""

import asyncio
import builtins
import json
import threading

import httpx

from ai_desktop_agent.server import app as server
from tests.http import AuthenticatedClient
from tests.test_android_task_handover import backend as backend


async def test_completed_task_deletion_during_worker_lookup_defers_setup_and_recovers(
    backend, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    watched = None

    def observed_getattr(obj, name, *default):
        result = builtins.getattr(obj, name, *default)
        if (
            threading.get_ident() != main_thread
            and watched is not None
            and obj is watched
            and name == "vm_id"
            and not entered.is_set()
        ):
            # Preserve the actual attribute value; only pause its return to
            # model this worker being descheduled between two global reads.
            entered.set()
            assert release.wait(5)
        return result

    async with server.lifespan(server.app):
        setup = backend.services[0]
        async with AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as api:
            accepted = await api.post(
                "/tasks", json={"instruction": "inspect the saved report", "vm_id": "and-first"}
            )
            assert accepted.status_code == 200
            watched = backend.sessions[-1]
            await asyncio.wait_for(watched.llm.entered.wait(), 2)
            stopped = await api.post(
                "/tasks/current/stop",
                params={"vm_id": "and-first", "session_id": watched.id},
            )
            assert stopped.status_code == 200
            await asyncio.gather(watched._task, return_exceptions=True)
            await asyncio.sleep(0)
            assert watched.id not in server._sessions and server._active_session is watched
            monkeypatch.setattr(server, "getattr", observed_getattr, raising=False)
            worker = asyncio.create_task(asyncio.to_thread(setup.ensure))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                removed = await api.delete("/tasks/" + watched.id)
                assert removed.status_code == 200 and server._active_session is None
                release.set()
                await asyncio.wait_for(asyncio.shield(worker), 3)
                assert backend.attempts == ["and-other"]
                assert not backend.setup_file.exists()
                assert backend.store.load(watched.id) is None
                backend.attempts.clear()
                setup.complete = True
                await asyncio.to_thread(setup.ensure)
                assert backend.attempts == ["and-first", "and-other"]
                saved = json.loads(backend.setup_file.read_text())
                assert set(saved) == {"and-first", "and-other"}
                assert all(item["done"] for item in saved.values())
            finally:
                release.set()
                await asyncio.gather(worker, return_exceptions=True)
