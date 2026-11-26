import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_desktop_agent.vm.cua import CuaDesktop, DriverError


@pytest.fixture
def desktop(monkeypatch):
    driver = CuaDesktop("a" * 64)
    monkeypatch.setattr(driver, "_start", AsyncMock())
    monkeypatch.setattr(driver, "_active", AsyncMock(return_value=12))
    monkeypatch.setattr(
        driver,
        "_windows",
        AsyncMock(
            return_value=[
                {"pid": 4, "window_id": 12, "title": "Save As"},
                {"pid": 9, "window_id": 15, "title": "Orders"},
            ]
        ),
    )
    button = {
        "element_token": "private-token",
        "label": "Save",
        "role": "push button",
        "frame": {"x": 1, "y": 1, "w": 32, "h": 16},
        "actions": ["click"],
    }
    monkeypatch.setattr(
        driver,
        "_tool",
        AsyncMock(
            return_value={
                "elements": [button, {**button, "is_password": True}, {**button, "enabled": False}]
            }
        ),
    )
    return driver


@pytest.mark.parametrize("effect", ["confirmed", "unverifiable"])
async def test_ref_binds_window_and_only_works_once(desktop, effect):
    window, button = await desktop.observe()
    assert window["label"] == "Orders" and button["label"] == "Save"
    assert "private-token" not in str(button)
    desktop._tool.reset_mock()
    desktop._tool.return_value = {"effect": effect}
    await desktop.perform("element_click", button["ref"])
    with pytest.raises(DriverError):
        await desktop.perform("element_click", button["ref"])
    desktop._tool.assert_awaited_once_with(
        "click", pid=4, window_id=12, element_token="private-token"
    )


async def test_changed_window_or_generation_cannot_receive_click(desktop):
    _, old = await desktop.observe()
    _, current = await desktop.observe()
    desktop._tool.reset_mock()
    with pytest.raises(DriverError):
        await desktop.perform("element_click", old["ref"])
    _, current = await desktop.observe()
    desktop._active.return_value = 100
    desktop._tool.reset_mock()
    with pytest.raises(DriverError):
        await desktop.perform("element_click", current["ref"])
    desktop._tool.assert_not_called()


async def test_focus_requires_same_window_and_confirms_activation(desktop):
    window, _ = await desktop.observe()
    desktop._tool.reset_mock()
    with pytest.raises(DriverError):
        await desktop.perform("focus_window", window["ref"])
    desktop._tool.assert_awaited_once_with("bring_to_front", pid=9, window_id=15)
    window, _ = await desktop.observe()
    desktop._windows.return_value = [{"pid": 17, "window_id": 15, "title": "Orders"}]
    desktop._tool.reset_mock()
    with pytest.raises(DriverError):
        await desktop.perform("focus_window", window["ref"])
    desktop._tool.assert_not_called()


async def test_failed_transport_falls_back_once_and_never_resends(desktop):
    _, button = await desktop.observe()
    desktop._tool.reset_mock()
    desktop._tool.side_effect = TimeoutError()
    with pytest.raises(DriverError):
        await desktop.perform("element_click", button["ref"])
    assert await desktop.observe() == []
    desktop._tool.assert_awaited_once_with(
        "click", pid=4, window_id=12, element_token="private-token"
    )


async def test_calc_batch_requires_unique_sheet_in_foreground_calc(desktop):
    desktop._windows.return_value[0]["title"] = "Work.ods — LibreOffice Calc"
    sheet = {"role": "table", "label": "Sheet Sales", "visible": True}
    desktop._tool.return_value = {"elements": [sheet]}
    rows = await desktop.observe()
    ref = next(r["ref"] for r in rows if r["action"] == "calc_write")
    desktop._tool.return_value = {"elements": [sheet, sheet]}
    assert not any(r["action"] == "calc_write" for r in await desktop.observe())
    with pytest.raises(DriverError):
        await desktop.write_cells(ref, [{"address": "B2", "value": "hi"}])
    desktop._tool.return_value = {"elements": [sheet]}
    desktop._windows.return_value[0]["title"] = "Firefox"
    assert not any(r["action"] == "calc_write" for r in await desktop.observe())


async def test_slow_accessibility_tree_keeps_window_switch_available(desktop):
    desktop._tool.side_effect = TimeoutError()
    rows = await desktop.observe()
    assert [row["action"] for row in rows] == ["focus_window"]
    assert not desktop.disabled
    desktop._tool.reset_mock()
    assert [row["label"] for row in await desktop.observe()] == ["Orders"]
    desktop._tool.assert_not_called()


async def test_cancelled_batch_drains_current_cell_and_never_sends_the_next(desktop, monkeypatch):
    sheet = {"role": "table", "label": "Sheet Sales"}
    desktop._windows.return_value[0]["title"] = "Work.ods — LibreOffice Calc"
    desktop._tool.return_value = {"elements": [sheet]}
    ref = next(r["ref"] for r in await desktop.observe() if r["action"] == "calc_write")
    writing = asyncio.Event()
    closing = asyncio.Event()
    closed = asyncio.Event()
    process = MagicMock()
    process.stdin.drain = AsyncMock()

    async def reply():
        if process.stdin.write.call_count == 1:
            return b"ready\n"
        writing.set()
        await asyncio.Future()

    async def finish():
        closing.set()
        await closed.wait()

    process.stdout.readline = reply
    process.communicate = finish
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(
        desktop.write_cells(
            ref, [{"address": "A1", "value": "first"}, {"address": "A2", "value": "next"}]
        )
    )
    await asyncio.wait_for(writing.wait(), 1)
    task.cancel()
    await asyncio.wait_for(closing.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and process.stdin.write.call_count == 2
    process.stdin.close.assert_called_once()
    closed.set()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_verifier_credits_readback_until_another_edit():
    from types import SimpleNamespace

    from ai_desktop_agent.actions.primitives import Action, ActionType
    from ai_desktop_agent.agent.state import ActionRecord
    from ai_desktop_agent.server.session import TaskSession

    batch = Action(
        ActionType.CALC_WRITE, {"ref": "2.1", "cells": [{"address": "C2", "value": "=A2*B2"}]}
    )
    history = [
        ActionRecord(batch, True),
        ActionRecord(Action(ActionType.KEY_COMBO, {"keys": ["ctrl", "s"]}), True),
        ActionRecord(Action(ActionType.SUBTASK_COMPLETE), True),
    ]
    session = SimpleNamespace(loop=SimpleNamespace(context=SimpleNamespace(action_history=history)))
    receipt = TaskSession._calc_readback_evidence(session)
    assert "Ctrl+S" in receipt and "=A2*B2" in receipt
    saved_batch = Action(ActionType.CALC_WRITE, {**batch.params, "save": True})
    history[0] = ActionRecord(saved_batch, True)
    assert "同じ既存文書" in TaskSession._calc_readback_evidence(session)
    history.insert(-1, ActionRecord(Action(ActionType.KEY_COMBO, {"keys": ["ctrl", "z"]}), True))
    assert TaskSession._calc_readback_evidence(session) == ""


async def test_fill_chunks_share_target_and_stop_after_first_unconfirmed_batch(
    desktop, monkeypatch
):
    desktop.table_memory.remember("Jobs", [[["Hours"], *[[str(i)] for i in range(70)]]])
    desktop._windows.return_value[0]["title"] = "Work.ods — LibreOffice Calc"
    desktop._tool.return_value = {"elements": [{"role": "table", "label": "Work", "visible": True}]}
    ref = next(r["ref"] for r in await desktop.observe() if r["action"] == "table_fill")
    batch = AsyncMock(side_effect=["", DriverError("window changed")])
    monkeypatch.setattr(desktop, "_write_batch", batch)
    with pytest.raises(DriverError, match="window changed"):
        await desktop.fill_query(ref, "select c1 from t1 order by rowid", "A2", [], True)
    assert batch.await_count == 2
    assert batch.await_args_list[0].args[1][0]["address"] == "A2"
    assert batch.await_args_list[1].args[1][0]["address"] == "A34"
    assert batch.await_args_list[0].args[2] is True
    with pytest.raises(DriverError):
        await desktop.fill_query(ref, "select c1 from t1", "A2", [], True)


async def test_text_report_rejects_switched_window(desktop):
    desktop.table_memory.remember("Jobs", [[["Site", "Hours"], ["Depot", "5"]]])
    desktop._windows.return_value[0]["title"] = "Report.txt - Mousepad"
    ref = next(r["ref"] for r in await desktop.observe() if r["action"] == "table_type")
    assert await desktop.render_table(ref, "select * from t1", " | ") == "Depot | 5"
    ref = next(r["ref"] for r in await desktop.observe() if r["action"] == "table_type")
    desktop._active.return_value = 17
    with pytest.raises(DriverError):
        await desktop.render_table(ref, "select * from t1", " | ")
