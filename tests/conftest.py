import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_desktop_agent.server import auth, task_admission
from tests.http import TEST_ACCESS_KEY


@pytest.fixture(autouse=True)
def isolated_access_control(monkeypatch):
    monkeypatch.setenv("AGENT_ACCESS_TOKEN", TEST_ACCESS_KEY)
    monkeypatch.setattr(auth, "_access", None)


def _record_module_sleeps(monkeypatch, module):
    """Skip wall-clock backoffs without patching other modules' asyncio.sleep."""

    async def yield_once(delay):
        await asyncio.sleep(0)

    sleep = AsyncMock(side_effect=yield_once)
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": sleep})))
    return sleep


@pytest.fixture
def session_retry_sleep(monkeypatch):
    from ai_desktop_agent.server import session

    return _record_module_sleeps(monkeypatch, session)


@pytest.fixture
def provider_retry_sleep(monkeypatch):
    from ai_desktop_agent.agent.llm import openai_compat_provider

    return _record_module_sleeps(monkeypatch, openai_compat_provider)


@pytest.fixture
async def advance_task_clock(monkeypatch):
    """Advance the budget and its asyncio timers together at test checkpoints."""
    from ai_desktop_agent.agent import budget

    loop = asyncio.get_running_loop()
    loop_time = loop.time
    offset = 0.0

    def now():
        return loop_time() + offset

    def advance(seconds):
        nonlocal offset
        offset += seconds

    monkeypatch.setattr(loop, "time", now)
    monkeypatch.setattr(budget, "time", SimpleNamespace(monotonic=now))
    return advance


@pytest.fixture(autouse=True)
def isolated_task_admission(monkeypatch):
    monkeypatch.setattr(task_admission, "_task_admission", task_admission.TaskAdmission())
