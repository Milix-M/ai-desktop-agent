"""A damaged snapshot fence must close the viewer and allow recovery after repair."""

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.store import TaskRecord
from tests.http import AUTH_HEADERS
from tests.test_websocket_snapshot import store as store


def test_unreadable_task_order_closes_with_1011_and_reconnects_after_repair(store, tmp_path):
    record = TaskRecord(
        "saved-report", "保存済みの文書", "completed", success=True, vm_id="desktop"
    )
    store.save(record)
    order = tmp_path / "task-order.json"
    order.write_text('{"last_order":false}')
    client = TestClient(server.app, headers=AUTH_HEADERS, client=("127.0.0.1", 50000))
    with client.websocket_connect("/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as disconnected:
            ws.receive_json()
        assert disconnected.value.code == 1011
    for callbacks in (
        server._ws_state_cbs,
        server._ws_action_cbs,
        server._ws_error_cbs,
        server._ws_complete_cbs,
    ):
        assert callbacks == []
    assert store.load(record.id).success is True

    order.write_text(json.dumps({"last_order": 0}))
    with client.websocket_connect("/ws") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot" and snapshot["through_session_order"] == 0
        assert len(snapshot["tasks"]) == 1
        assert snapshot["tasks"][0]["session_id"] == record.id
        assert snapshot["tasks"][0]["state"] == "completed"
        ws.send_text("ping")
        assert ws.receive_json() == {"type": "pong"}
    assert not server._ws_state_cbs and not server._ws_action_cbs
    assert not server._ws_error_cbs and not server._ws_complete_cbs
