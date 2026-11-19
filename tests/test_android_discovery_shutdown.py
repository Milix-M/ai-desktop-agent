"""Shutdown must invalidate targets returned by discovery that was already in flight."""

import asyncio
import threading
from unittest.mock import Mock

from ai_desktop_agent.server.android_provision import AndroidProvisioner
from tests.test_android_service_lifecycle import target_pool


async def test_stop_during_explicit_discovery_never_prepares_or_records_the_returned_target(
    tmp_path, monkeypatch
):
    pool = target_pool()
    targets = pool.list_vms.return_value
    entered, release = threading.Event(), threading.Event()

    def discover():
        entered.set()
        assert release.wait(3)
        return targets

    pool.list_vms.side_effect = discover
    state = tmp_path / "setup.json"
    setup = AndroidProvisioner(pool=pool, state_file=state)
    provision = Mock(return_value=True)
    monkeypatch.setattr(setup, "provision_vm", provision)
    discovering = asyncio.create_task(asyncio.to_thread(setup.ensure))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(setup.stop(), 1)
        release.set()
        await asyncio.wait_for(asyncio.shield(discovering), 1)
        pool.prepare_vm.assert_not_called()
        provision.assert_not_called()
        assert not state.exists()
        # Explicit ensure remains disabled after stop, including a second call.
        await asyncio.to_thread(setup.ensure)
        pool.list_vms.assert_called_once_with()
        pool.close.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(discovering, return_exceptions=True)
        await setup.stop()
