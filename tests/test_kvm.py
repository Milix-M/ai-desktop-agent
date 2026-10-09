"""KVM利用可否判定のテスト。"""

import os

import pytest

from ai_desktop_agent.server.kvm import (
    KvmUnavailableError,
    is_kvm_available,
    is_tcg_allowed,
    resolve_use_kvm,
)


class _FakeContainer:
    def __init__(self, name="vm-1"):
        self.name = name
        self.status = "running"


class _FakeContainers:
    def __init__(self, items):
        self._items = items

    def list(self, all=True, filters=None):
        return list(self._items)


class _FakeDocker:
    def __init__(self, items):
        self.containers = _FakeContainers(items)

    def ping(self):
        return True


def test_resolve_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("USE_KVM", raising=False)
    assert resolve_use_kvm() == "auto"


def test_resolve_explicit(monkeypatch):
    monkeypatch.setenv("USE_KVM", "TRUE")
    assert resolve_use_kvm() == "true"
    monkeypatch.setenv("USE_KVM", "false")
    assert resolve_use_kvm() == "false"


def test_explicit_true_always_available(monkeypatch):
    monkeypatch.setenv("USE_KVM", "true")
    assert is_kvm_available(_FakeDocker([])) is True


def test_explicit_false_never_available(monkeypatch):
    monkeypatch.setenv("USE_KVM", "false")
    assert is_kvm_available(_FakeDocker([_FakeContainer()])) is False


def test_auto_with_device(monkeypatch):
    monkeypatch.setenv("USE_KVM", "auto")
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    assert is_kvm_available(_FakeDocker([])) is True


def test_auto_with_vm_container_present(monkeypatch):
    monkeypatch.setenv("USE_KVM", "auto")
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    assert is_kvm_available(_FakeDocker([_FakeContainer()])) is True


def test_auto_without_kvm(monkeypatch):
    monkeypatch.setenv("USE_KVM", "auto")
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    assert is_kvm_available(_FakeDocker([])) is False


def test_tcg_override(monkeypatch):
    monkeypatch.delenv("ALLOW_TCG_VM", raising=False)
    assert is_tcg_allowed() is False
    monkeypatch.setenv("ALLOW_TCG_VM", "true")
    assert is_tcg_allowed() is True


def test_error_message_mentions_desktop():
    with pytest.raises(KvmUnavailableError, match="desktop"):
        raise KvmUnavailableError()
