"""Damaged submission metadata must preserve history and release retry ownership."""

import json
from contextlib import closing
from dataclasses import replace

import pytest

from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.store import TaskRecord, TaskStore
from ai_desktop_agent.server.submissions import (
    SubmissionConflictError,
    fingerprint,
    key_digest,
    task_id_for,
)


@pytest.fixture
def saved_submission(tmp_path):
    store = TaskStore(tmp_path)
    digest = key_digest("quarterly-report-submission")
    request_fp = fingerprint({"instruction": "四半期報告を保存する", "vm_id": "desktop"})
    record = TaskRecord(
        task_id_for(digest),
        "四半期報告を保存する",
        "completed",
        request_digest=digest,
        request_fingerprint=request_fp,
    )
    store.save(record)
    history = tmp_path / "tasks" / f"{record.id}.json"
    receipt = tmp_path / "submissions" / f"{digest}.json"
    return store, record, history, receipt


@pytest.mark.parametrize("receipt_present", [False, True])
def test_mismatched_history_is_preserved_and_retry_lock_is_released(
    saved_submission, receipt_present
):
    store, record, history, receipt = saved_submission
    original_history, original_receipt = history.read_bytes(), receipt.read_bytes()
    different = fingerprint({"instruction": "別の報告を保存する"})
    if receipt_present:
        damaged = json.loads(original_history)
        damaged["request_fingerprint"] = different
        history.write_text(json.dumps(damaged))
        requested = record.request_fingerprint
        expected = StorageError
    else:
        receipt.unlink()  # A crash can leave the task durable before its receipt.
        requested = different
        expected = SubmissionConflictError
    before = history.read_bytes()
    with pytest.raises(expected):
        store.claim_submission(record.request_digest, requested)
    assert history.read_bytes() == before
    if receipt_present:
        assert receipt.read_bytes() == original_receipt
        history.write_bytes(original_history)
    else:
        assert not receipt.exists()
    # A second store can claim the correct request after the rejected attempt.
    with closing(
        TaskStore(history.parent.parent).claim_submission(
            record.request_digest, record.request_fingerprint
        )
    ) as claim:
        assert claim.record.id == record.id
    assert history.read_bytes() == original_history
    assert json.loads(receipt.read_bytes()) == json.loads(original_receipt)


def test_saving_a_changed_fingerprint_cannot_replace_existing_history_or_receipt(saved_submission):
    store, record, history, receipt = saved_submission
    before = history.read_bytes(), receipt.read_bytes()
    changed = replace(record, request_fingerprint=fingerprint({"instruction": "別の内容"}))
    with pytest.raises(StorageError, match="内容が一致しません"):
        store.save(changed)
    assert (history.read_bytes(), receipt.read_bytes()) == before
    assert store.load(record.id).request_fingerprint == record.request_fingerprint


@pytest.mark.parametrize("damage", ["list", "task-id", "digest", "fingerprint"])
def test_invalid_receipt_is_not_recreated_or_used_to_accept_a_retry(saved_submission, damage):
    store, record, history, receipt = saved_submission
    original = receipt.read_bytes()
    value = json.loads(original)
    if damage == "list":
        value = [value]
    else:
        field = {
            "task-id": "task_id",
            "digest": "request_digest",
            "fingerprint": "request_fingerprint",
        }[damage]
        value[field] = "another-task" if damage == "task-id" else "invalid-digest"
    receipt.write_text(json.dumps(value))
    damaged, saved = receipt.read_bytes(), history.read_bytes()
    with pytest.raises(StorageError):
        store.claim_submission(record.request_digest, record.request_fingerprint)
    assert receipt.read_bytes() == damaged and history.read_bytes() == saved
    receipt.write_bytes(original)
    with closing(
        store.claim_submission(record.request_digest, record.request_fingerprint)
    ) as claim:
        assert claim.record.id == record.id


def test_an_unusable_lock_path_is_reported_without_rewriting_durable_data(saved_submission):
    store, record, history, receipt = saved_submission
    before = history.read_bytes(), receipt.read_bytes()
    lock = receipt.with_suffix(".lock")
    lock.mkdir()
    with pytest.raises(StorageError, match="アクセスできません") as caught:
        store.claim_submission(record.request_digest, record.request_fingerprint)
    assert isinstance(caught.value.__cause__, IsADirectoryError)
    assert (history.read_bytes(), receipt.read_bytes()) == before
    lock.rmdir()
    with closing(
        store.claim_submission(record.request_digest, record.request_fingerprint)
    ) as claim:
        assert claim.record.id == record.id


@pytest.mark.parametrize("damage", ["digest", "task-id", "subtasks"])
def test_invalid_saved_submission_fields_are_rejected_without_repairing_the_file(
    saved_submission, damage
):
    store, record, history, receipt = saved_submission
    value = json.loads(history.read_bytes())
    if damage == "digest":
        value["request_digest"] = "invalid-digest"
    elif damage == "task-id":
        value["id"] = "another-task"
    else:
        value["subtasks"] = ["invalid-subtask"]
    history.write_text(json.dumps(value))
    before = history.read_bytes(), receipt.read_bytes()
    with pytest.raises(ValueError):
        TaskRecord.from_dict(value)
    assert store.load(record.id) is None
    assert (history.read_bytes(), receipt.read_bytes()) == before


def test_invalid_submission_digest_cannot_create_paths_outside_the_receipt_directory(tmp_path):
    store = TaskStore(tmp_path)
    before = set(tmp_path.rglob("*"))
    with pytest.raises(ValueError, match="識別子が不正"):
        store.claim_submission("../outside", "a" * 64)
    assert set(tmp_path.rglob("*")) == before
