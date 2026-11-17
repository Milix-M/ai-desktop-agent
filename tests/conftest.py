import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_desktop_agent.server import auth
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
