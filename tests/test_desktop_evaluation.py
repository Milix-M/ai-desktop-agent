import asyncio
import io
import json
import tarfile
import threading
import uuid
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.evaluation import desktop
from ai_desktop_agent.evaluation.cases import (
    CASES,
    MAX_ARTIFACT_BYTES,
    check_case,
    initial_spreadsheet,
    read_artifact,
)
from ai_desktop_agent.vm.fake import FakeDisplayBackend


def archive_file(data, *, link=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        info = tarfile.TarInfo("file")
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        else:
            info.size = len(data)
        archive.addfile(info, None if link else io.BytesIO(data))
    return output.getvalue()


def completed_spreadsheet(*, formula="of:=SUM([.B2:.B3])", amount="2000", change_seed=False):
    with zipfile.ZipFile(io.BytesIO(initial_spreadsheet())) as source:
        content = source.read("content.xml").decode()
    extra = (
        '<table:table-row><table:table-cell office:value-type="string"><text:p>合計</text:p>'
        "</table:table-cell>"
        f'<table:table-cell table:formula="{formula}" office:value-type="float"'
        f' office:value="{amount}"><text:p>{amount}</text:p></table:table-cell></table:table-row>'
    )
    content = content.replace("</table:table>", extra + "</table:table>")
    if change_seed:
        content = content.replace('office:value="1200"', 'office:value="1"')
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        target.writestr("content.xml", content)
    return output.getvalue()


@pytest.mark.parametrize(
    "data",
    [b"bad zip", initial_spreadsheet(), completed_spreadsheet(amount="abc")],
    ids=["invalid-zip", "unfinished-spreadsheet", "invalid-total-value"],
)
def test_invalid_or_unfinished_spreadsheets_never_pass(data):
    assert not check_case(CASES[2], {CASES[2].filename: data})[0]


def test_spreadsheet_requires_the_formula_result_and_unchanged_source_cells():
    case = CASES[2]
    assert check_case(case, {case.filename: completed_spreadsheet()})[0]
    assert not check_case(case, {case.filename: completed_spreadsheet(formula="")})[0]
    assert not check_case(case, {case.filename: completed_spreadsheet(amount="1999")})[0]
    assert not check_case(case, {case.filename: completed_spreadsheet(change_seed=True)})[0]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
@pytest.mark.parametrize("declaration", [None, "internal", "external"])
def test_spreadsheet_rejects_dtds_in_every_supported_encoding(encoding, declaration):
    with zipfile.ZipFile(io.BytesIO(completed_spreadsheet())) as archive:
        xml = archive.read("content.xml").decode()
    if declaration == "internal":
        xml = xml.replace('office:value="2000"', 'office:value="&total;"')
        xml = '<!DOCTYPE office:document-content [<!ENTITY total "2000">]>' + xml
    elif declaration == "external":
        xml = '<!DOCTYPE office:document-content SYSTEM "file:///not-read.dtd">' + xml
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("content.xml", xml.encode(encoding))
    assert check_case(CASES[2], {CASES[2].filename: output.getvalue()})[0] is (declaration is None)


def test_rename_requires_old_name_absent_and_other_file_unchanged():
    case = CASES[1]
    files = {
        case.renamed: case.expected.encode(),
        case.filename: None,
        "変更しない.txt": b"keep this file\n",
    }
    assert check_case(case, files)[0]
    assert not check_case(case, {**files, case.filename: case.initial.encode()})[0]
    assert not check_case(case, {**files, "変更しない.txt": b"modified"})[0]


def test_unicode_replacement_with_the_original_text_left_over_is_rejected():
    case = CASES[0]
    assert check_case(case, {case.filename: case.expected.encode()})[0]
    assert not check_case(case, {case.filename: (case.expected + case.initial).encode()})[0]


@pytest.mark.parametrize("link", [False, True])
def test_artifact_reader_never_extracts_guest_paths_or_accepts_links(link, tmp_path):
    container = Mock()
    container.get_archive.return_value = ([archive_file(b"content", link=link)], {})
    if link:
        with pytest.raises(ValueError, match="通常ファイル"):
            read_artifact(container, "/guest/file")
    else:
        assert read_artifact(container, "/guest/file") == b"content"
    assert not list(tmp_path.iterdir())


def test_oversized_artifacts_are_rejected_before_unbounded_reads():
    container = Mock()
    container.get_archive.return_value = (iter([b"x" * (MAX_ARTIFACT_BYTES + 65537)]), {})
    with pytest.raises(ValueError, match="上限"):
        read_artifact(container, "/guest/file")


def test_the_regular_file_limit_also_applies_inside_a_bounded_tar_stream():
    data = archive_file(b"x" * (MAX_ARTIFACT_BYTES + 1))
    assert len(data) < MAX_ARTIFACT_BYTES + 65536
    container = Mock(get_archive=Mock(return_value=(iter([data]), {})))
    with pytest.raises(ValueError, match="上限"):
        read_artifact(container, "/guest/file")


@pytest.mark.parametrize(
    "fault",
    [
        "oversized-xml",
        "unrelated-xml",
        "zero-rows",
        "zero-columns",
        "missing-total-column",
        "missing-header",
        "missing-seed-value",
        "unexpected-tag",
    ],
)
def test_spreadsheet_structure_and_size_are_checked_before_accepting_values(fault):
    with zipfile.ZipFile(io.BytesIO(completed_spreadsheet())) as archive:
        xml = archive.read("content.xml").decode()
    if fault == "oversized-xml":
        xml = "x" * (MAX_ARTIFACT_BYTES + 1)
    elif fault == "unrelated-xml":
        xml = "<root/>"
    elif fault == "zero-rows":
        xml = xml.replace(
            "<table:table-row>", '<table:table-row table:number-rows-repeated="0">', 1
        )
    elif fault == "zero-columns":
        xml = xml.replace(
            "<table:table-cell ", '<table:table-cell table:number-columns-repeated="0" ', 1
        )
    elif fault == "missing-total-column":
        start = xml.index("<table:table-cell table:formula=")
        end = xml.index("</table:table-cell>", start) + len("</table:table-cell>")
        xml = xml[:start] + xml[end:]
    elif fault == "missing-header":
        start = xml.index("<table:table-row>")
        end = xml.index("</table:table-row>", start)
        xml = xml[:start] + "<table:table-row>" + xml[end:]
    elif fault == "missing-seed-value":
        start = xml.index('<table:table-cell office:value-type="float" office:value="1200">')
        end = xml.index("</table:table-cell>", start) + len("</table:table-cell>")
        xml = xml[:start] + xml[end:]
    else:
        xml = xml.replace(
            "<table:table-row>", "<table:table-row><text:p>ignored metadata</text:p>", 1
        )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("content.xml", xml)
    assert check_case(CASES[2], {CASES[2].filename: output.getvalue()})[0] is (
        fault == "unexpected-tag"
    )


def test_a_missing_output_is_a_task_failure_with_a_useful_reason():
    assert check_case(CASES[0], {}) == (False, "保存先にファイルがありません")


def test_infrastructure_errors_are_visible_in_the_denominator():
    result = desktop.summarize(
        [
            {"status": "evaluated", "success": True, "artifact_success": True},
            {"status": "evaluated", "success": False, "artifact_success": True},
            {"status": "setup_error"},
        ]
    )
    assert result["attempts"] == 3 and result["evaluated_attempts"] == 2
    assert result["successes"] == 1 and result["task_success_rate"] == 0.5
    assert result["artifact_success_rate"] == 1 and result["infrastructure_errors"] == 1
    assert desktop.summarize([])["task_success_rate"] is None


def test_interrupted_attempts_still_count_their_model_usage():
    result = desktop.summarize(
        [
            {
                "status": "interrupted",
                "usage": {
                    "requests": 2,
                    "input_tokens": 30,
                    "output_tokens": 10,
                    "usage_complete": False,
                    "estimated_cost_usd": "0.01",
                },
            }
        ]
    )
    assert result["model_requests"] == 2 and result["input_tokens"] == 30
    assert result["output_tokens"] == 10
    assert not result["usage_complete"] and result["estimated_cost_usd"] is None


@pytest.fixture
def environment(monkeypatch):
    case = CASES[0]
    container = Mock()
    container.id = "owned-evaluation-container"
    container.attrs = {"NetworkSettings": {"Ports": {"5900/tcp": [{"HostPort": "5900"}]}}}
    container.get_archive.side_effect = lambda _: ([archive_file(case.initial.encode())], {})
    client = Mock()
    client.containers.run.return_value = container
    client.containers.list.return_value = [container]
    network = SimpleNamespace(id="owned-evaluation-network", remove=Mock())
    client.networks.create.return_value = network
    client.networks.list.return_value = [network]
    monkeypatch.setattr(desktop, "VNCClient", FakeDisplayBackend)
    monkeypatch.setattr(desktop, "wait_desktop", lambda _: None)
    monkeypatch.setattr(
        desktop, "prepare_case", lambda *args: {case.filename: case.initial.encode()}
    )
    return client, container


async def test_agent_claim_does_not_count_as_correct_output(environment, tmp_path):
    client, container = environment
    result = {"attempt_id": "attempt", "status": "running"}
    await desktop.run_attempt(
        client, "image-id", CASES[0], MockLLMProvider(), ExecutionLimits(), tmp_path, result
    )
    assert result["agent_success"] and not result["artifact_success"] and not result["success"]
    assert result["status"] == "evaluated"
    container.remove.assert_called_once_with(force=True)
    assert json.loads((tmp_path / "result.json").read_text())["success"] is False
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in tmp_path.rglob("*") if p.is_file())
    config = client.containers.run.call_args.kwargs
    assert "volumes" not in config and "OPENAI_API_KEY" not in config["environment"]
    assert config["network"] == "owned-evaluation-network"
    assert config["mem_limit"] == config["memswap_limit"] == 2 * 1024**3
    assert config["pids_limit"] == 1024


async def test_provider_cleanup_failure_does_not_skip_removing_the_desktop(
    environment, tmp_path, monkeypatch
):
    client, container = environment
    model = MockLLMProvider()
    model.aclose = AsyncMock(side_effect=RuntimeError("close failed"))
    monkeypatch.setattr(desktop, "prepare_case", Mock(side_effect=RuntimeError("setup failed")))
    result = {"attempt_id": "attempt", "status": "running"}
    await desktop.run_attempt(
        client, "image-id", CASES[0], model, ExecutionLimits(), tmp_path, result
    )
    assert result["status"] == "setup_error" and "setup failed" in result["errors"][0]
    assert "close failed" in result["cleanup_errors"][0]
    container.remove.assert_called_once_with(force=True)


async def test_manual_input_ends_noninteractive_evaluation_without_a_final_screen(
    environment, tmp_path
):
    client, container = environment
    model = MockLLMProvider(
        decide_result=ActionDecision(
            Action(ActionType.REQUEST_USER_INPUT, {"reason": "手動入力してください"}),
            "待つ",
            1,
            "入力",
        )
    )
    result = {"attempt_id": "attempt", "status": "running"}
    await asyncio.wait_for(
        desktop.run_attempt(
            client, "image-id", CASES[0], model, ExecutionLimits(), tmp_path, result
        ),
        3,
    )
    assert result["manual_input_required"] and not result["success"]
    assert not (tmp_path / "final.png").exists()
    container.remove.assert_called_once_with(force=True)


async def test_repeated_cancellation_still_finishes_all_cleanup_steps():
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    async def first():
        entered.set()
        await release.wait()
        order.append("first")

    async def second():
        order.append("second")

    task = asyncio.create_task(desktop.cleanup_all([("first", first), ("second", second)], {}))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.wait_for(task, 1)
    assert order == ["first", "second"]


async def test_cancelling_during_allocation_cleans_up_the_container_created_afterwards(
    environment, tmp_path
):
    client, container = environment
    entered, release = threading.Event(), threading.Event()

    def allocate(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return container

    client.containers.run.side_effect = allocate
    result = {"attempt_id": "cancelled-attempt", "status": "running"}
    task = asyncio.create_task(
        desktop.run_attempt(
            client, "image-id", CASES[0], MockLLMProvider(), ExecutionLimits(), tmp_path, result
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert result["status"] == "interrupted"
    client.containers.list.assert_called_once_with(
        all=True, filters={"label": "ai-desktop-agent.evaluation=cancelled-attempt"}
    )
    container.remove.assert_called_once_with(force=True)
    client.networks.create.return_value.remove.assert_called_once()
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "interrupted"


def test_missing_model_configuration_fails_before_starting_docker(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(desktop.VNCClient, "shutdown_runtime", Mock())
    docker_client = Mock(side_effect=AssertionError("must not allocate a desktop"))
    monkeypatch.setattr(desktop.docker, "from_env", docker_client)
    assert desktop.main(["--provider", "openai", "--out", str(tmp_path / "new")]) == 2
    docker_client.assert_not_called()
    assert not (tmp_path / "new").exists()


def test_existing_results_are_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.VNCClient, "shutdown_runtime", Mock())
    marker = tmp_path / "marker"
    marker.write_text("previous run")
    assert desktop.main(["--mode", "scripted", "--out", str(tmp_path)]) == 2
    assert marker.read_text() == "previous run"


def test_a_model_endpoint_is_recorded_without_its_url_credential(tmp_path, monkeypatch):
    secret = "endpoint-token-" + uuid.uuid4().hex
    monkeypatch.setenv("OPENAI_API_KEY", "configured-evaluation-api-key")
    monkeypatch.setattr(desktop.VNCClient, "shutdown_runtime", Mock())
    client = Mock()
    client.images.get.side_effect = RuntimeError("image unavailable")
    monkeypatch.setattr(desktop.docker, "from_env", Mock(return_value=client))
    output = tmp_path / "model-evaluation"
    assert (
        desktop.main(
            [
                "--provider",
                "openai",
                "--base-url",
                "https://example.test/v1?key=" + secret,
                "--out",
                str(output),
            ]
        )
        == 2
    )
    text = (output / "report.json").read_text()
    report = json.loads(text)
    assert report["status"] == "incomplete"
    assert report["summary"]["infrastructure_errors"] == 1
    assert secret not in text and "[REDACTED]" in report["endpoint"]
    client.close.assert_called_once()


def test_evaluation_defaults_preserve_lower_configured_limits(monkeypatch):
    monkeypatch.setenv("TASK_MAX_SECONDS", "20")
    monkeypatch.setenv("TASK_MAX_REQUESTS", "5")
    monkeypatch.setenv("TASK_MAX_TOKENS", "100")
    args = SimpleNamespace(max_seconds=None, max_requests=None, max_tokens=None)
    limits = desktop.evaluation_limits(args)
    assert limits.max_seconds == 20 and limits.max_requests == 5 and limits.max_tokens == 100
    args.max_requests = 10
    assert desktop.evaluation_limits(args).max_requests == 10
