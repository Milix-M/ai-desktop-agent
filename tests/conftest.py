import pytest

from ai_desktop_agent.server import auth, task_admission
from tests.http import TEST_ACCESS_KEY


@pytest.fixture(autouse=True)
def isolated_access_control(monkeypatch):
    monkeypatch.setenv("AGENT_ACCESS_TOKEN", TEST_ACCESS_KEY)
    monkeypatch.setattr(auth, "_access", None)


@pytest.fixture(autouse=True)
def isolated_task_admission(monkeypatch):
    monkeypatch.setattr(task_admission, "_task_admission", task_admission.TaskAdmission())
