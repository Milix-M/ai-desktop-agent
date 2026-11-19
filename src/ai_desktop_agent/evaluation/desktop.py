"""Run isolated desktop tasks and report artifact correctness separately from agent claims."""

import argparse
import asyncio
import dataclasses
import hashlib
import inspect
import io
import json
import logging
import os
import platform
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path

import docker
from docker.errors import NotFound

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.factory import create_llm_provider
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.evaluation.cases import (
    CASES,
    SUITE_VERSION,
    check_case,
    filenames,
    initial_spreadsheet,
    read_artifact,
)
from ai_desktop_agent.privacy import install_log_redaction, redact, redact_data
from ai_desktop_agent.server.desktop_resources import DesktopResources
from ai_desktop_agent.server.persistence import atomic_json
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.async_io import run_display_call
from ai_desktop_agent.vm.vnc_client import VNCClient

ENV = {"DISPLAY": ":0", "XAUTHORITY": "/home/agent/.Xauthority", "LANG": "C.UTF-8"}
DESKTOP_RESOURCES = DesktopResources()
DESKTOP_SHM_BYTES = 1024**3


def private_bytes(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)


async def close_provider(provider):
    close = getattr(provider, "aclose", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result


async def cleanup_all(steps, result):
    async def perform():
        for name, operation in steps:
            try:
                await operation()
            except (Exception, asyncio.CancelledError) as error:
                result.setdefault("cleanup_errors", []).append(redact(f"{name}: {error}"))

    closing = asyncio.create_task(perform())
    cancelled = False
    while True:
        try:
            await asyncio.shield(closing)
            return cancelled
        except asyncio.CancelledError:
            cancelled = True


def remove_resources(resources, *, force=False):
    failures = []
    for resource in resources:
        try:
            resource.remove(**({"force": True} if force else {}))
        except NotFound:
            continue
        except Exception as error:
            failures.append((resource.id, error))
    if failures:
        message = "; ".join(f"{identity}: {error}" for identity, error in failures)
        raise RuntimeError(message) from ExceptionGroup(
            "evaluation resource cleanup failed", [error for _, error in failures]
        )


class RecordingDisplay:
    def __init__(self, backend, directory):
        self.backend = backend
        self.directory = directory
        self.frames = []
        self.dropped = 0

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def _record(self, screenshot, overlay):
        if len(self.frames) < 64:
            name = f"frame-{len(self.frames):04d}.png"
            private_bytes(self.directory / name, screenshot.image_bytes)
            self.frames.append(
                {"file": name, "timestamp": screenshot.timestamp, "overlay": overlay}
            )
        else:
            self.dropped += 1
        return screenshot

    def capture_screen(self, *, with_overlay=True):
        return self._record(self.backend.capture_screen(with_overlay=with_overlay), with_overlay)

    def capture_raw(self):
        return self._record(self.backend.capture_raw(), False)


class ScriptedProvider(MockLLMProvider):
    """Exercise the harness with known GUI actions; never label this as model accuracy."""

    def __init__(self, case):
        super().__init__(model="scripted-v2")

        def combo(*keys):
            return Action(ActionType.KEY_COMBO, {"keys": list(keys)})

        def key(name):
            return Action(ActionType.KEY_PRESS, {"key": name})

        def text(value):
            return Action(ActionType.TYPE, {"text": value})

        if case.id == "text-edit":
            actions = [
                combo("ctrl", "a"),
                key("backspace"),
                text(case.expected),
                combo("ctrl", "s"),
            ]
        elif case.id == "text-append":
            actions = [
                combo("ctrl", "end"),
                text(case.expected.removeprefix(case.initial)),
                combo("ctrl", "s"),
            ]
        elif case.id == "file-rename":
            actions = [
                key("home"),
                key("f2"),
                combo("ctrl", "a"),
                key("backspace"),
                text(case.renamed),
                key("enter"),
            ]
        else:
            actions = [
                key("escape"),
                combo("ctrl", "home"),
                ActionDecision(key("down"), "3行下へ移動", 1.0, "評価基盤の動作確認", repeats=3),
                text("合計"),
                key("tab"),
                text("=SUM(B2:B3)"),
                key("enter"),
                combo("ctrl", "s"),
            ]
        self.actions = iter([*actions, Action(ActionType.SUBTASK_COMPLETE)])

    async def decide_next_action(self, *args, **kwargs):
        action = next(self.actions, Action(ActionType.SUBTASK_COMPLETE))
        if isinstance(action, ActionDecision):
            return action
        return ActionDecision(action, "定型操作を実行する", 1.0, "評価基盤の動作確認")


def exec_guest(container, command, *, detach=False):
    result = container.exec_run(command, user="agent", environment=ENV, detach=detach)
    if not detach and result.exit_code != 0:
        raise RuntimeError(
            "評価環境の準備に失敗しました: " + result.output.decode(errors="replace")
        )
    return result


def prepare_case(container, case, directory):
    exec_guest(container, ["mkdir", "-p", "/home/agent/Documents"])
    exec_guest(container, ["mkdir", directory])
    uid = int(exec_guest(container, ["id", "-u"]).output)
    gid = int(exec_guest(container, ["id", "-g"]).output)
    content = initial_spreadsheet() if case.id == "spreadsheet-total" else case.initial.encode()
    files = {case.filename: content}
    if case.renamed:
        files["変更しない.txt"] = b"keep this file\n"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as output:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.uid, info.gid, info.mode = len(data), uid, gid, 0o600
            output.addfile(info, io.BytesIO(data))
    if not container.put_archive(directory, archive.getvalue()):
        raise RuntimeError("評価環境の初期ファイルを配置できませんでした")
    path = directory + "/" + case.filename
    if case.id in ("text-edit", "text-append"):
        command = ["mousepad", "--disable-server", path]
    elif case.id == "file-rename":
        command = ["thunar", directory]
    else:
        command = ["libreoffice", "--nologo", "--norestore", "--calc", path]
    exec_guest(container, command, detach=True)
    # The window must exist before keyboard input. All cases start from a fresh guest.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        windows = exec_guest(container, ["wmctrl", "-l"]).output.decode(errors="replace")
        needle = Path(directory).name if case.renamed else case.filename
        match = next((line.split()[0] for line in windows.splitlines() if needle in line), None)
        if match:
            exec_guest(container, ["xdotool", "windowactivate", "--sync", match])
            return files
        time.sleep(0.1)
    raise RuntimeError("作業アプリのウィンドウが開きませんでした")


def wait_desktop(container):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        container.reload()
        if container.status in {"exited", "dead", "removing"}:
            raise RuntimeError("評価デスクトップが起動しませんでした")
        state = container.attrs.get("State", {})
        if container.status == "running" and state.get("Health", {}).get("Status") == "healthy":
            return
        time.sleep(0.2)
    raise RuntimeError("評価デスクトップの起動待ちが上限に達しました")


def collect_files(container, case, directory, output=None):
    files = {}
    for name in filenames(case):
        data = read_artifact(container, directory + "/" + name)
        files[name] = data
        if data is not None and output is not None:
            private_bytes(output / name, data)
    return files


def summarize(results):
    evaluated = [r for r in results if r["status"] == "evaluated"]
    successes = sum(bool(r.get("success")) for r in evaluated)
    artifacts = sum(bool(r.get("artifact_success")) for r in evaluated)
    usage = [r["usage"] for r in results if "usage" in r]
    complete = bool(usage) and all(item.get("usage_complete", False) for item in usage)
    costs = [item.get("estimated_cost_usd") for item in usage]
    return {
        "attempts": len(results),
        "evaluated_attempts": len(evaluated),
        "successes": successes,
        "artifact_successes": artifacts,
        "task_success_rate": successes / len(evaluated) if evaluated else None,
        "artifact_success_rate": artifacts / len(evaluated) if evaluated else None,
        "infrastructure_errors": sum(
            r["status"] not in {"evaluated", "interrupted", "running"}
            or bool(r.get("cleanup_errors"))
            or bool(r.get("errors"))
            for r in results
        ),
        "model_requests": sum(item.get("requests", 0) for item in usage),
        "input_tokens": sum(item.get("input_tokens", 0) for item in usage),
        "output_tokens": sum(item.get("output_tokens", 0) for item in usage),
        "usage_complete": complete,
        "estimated_cost_usd": str(sum((Decimal(cost) for cost in costs), Decimal(0)))
        if complete and all(cost is not None for cost in costs)
        else None,
        "agent_seconds": sum(r.get("agent_seconds", 0) for r in results),
    }


async def run_attempt(client, image, case, provider, limits, output, result):
    container = session = network = None
    display = VNCClient()
    recorded = RecordingDisplay(display, output)
    started = time.monotonic()
    labels = {"ai-desktop-agent.evaluation": result["attempt_id"]}
    try:
        network = await run_display_call(
            client.networks.create,
            "agent-eval-" + result["attempt_id"],
            driver="bridge",
            labels=labels,
        )
        result["network_id"] = network.id
        container = await run_display_call(
            client.containers.run,
            image,
            detach=True,
            network=network.id,
            ports={"5900/tcp": ("127.0.0.1", None)},
            shm_size=DESKTOP_SHM_BYTES,
            **DESKTOP_RESOURCES.docker_options(),
            cap_drop=["NET_RAW"],
            environment={"LANG": "C.UTF-8"},
            labels=labels,
        )
        result["container_id"] = container.id
        await run_display_call(wait_desktop, container)
        container.reload()
        port = int(container.attrs["NetworkSettings"]["Ports"]["5900/tcp"][0]["HostPort"])
        await run_display_call(display.connect, "127.0.0.1", port)
        directory = "/home/agent/Documents/evaluation-" + result["attempt_id"]
        result["instruction"] = case.instruction.format(directory=directory)
        expected = await run_display_call(prepare_case, container, case, directory)
        baseline = await run_display_call(collect_files, container, case, directory)
        if baseline != {name: expected.get(name) for name in filenames(case)}:
            raise RuntimeError("初期状態のファイルが準備結果と一致しません")
        if check_case(case, baseline)[0]:
            raise RuntimeError("操作前から完成しているため、評価を開始できません")
        await run_display_call(recorded.capture_raw)
        session = TaskSession(
            llm=provider, display=recorded, store=TaskStore(output), decision=None, limits=limits
        )
        result["task_id"] = session.id
        result["model_errors"] = []
        session.on_error(lambda message: result["model_errors"].append(redact(message)))

        def state_changed(state, context):
            if state.value == "awaiting_user":
                result["manual_input_required"] = True
                asyncio.get_running_loop().call_soon(session.stop)

        session.on_state_change(state_changed)
        agent_started = time.monotonic()
        await session.start_async(result["instruction"])
        try:
            result["agent_success"] = bool(await session._task)
        except asyncio.CancelledError:
            if not result.get("manual_input_required") or asyncio.current_task().cancelling():
                raise
            result["agent_success"] = False
        result["agent_seconds"] = time.monotonic() - agent_started
        record = session.snapshot()
        result.update(
            state=record.state, stop_reason=record.stop_reason, usage=record.execution_usage
        )
        result["status"] = "evaluation_error"
        try:
            files = await run_display_call(collect_files, container, case, directory, output)
            passed, evidence = check_case(case, files)
        except ValueError as error:
            passed, evidence = False, str(error)
        result.update(
            status="evaluated",
            artifact_success=passed,
            evidence=evidence,
            success=bool(passed and result["agent_success"]),
        )
        if not result.get("manual_input_required"):
            await run_display_call(display.connect, "127.0.0.1", port)
            final = await run_display_call(display.capture_raw)
            private_bytes(output / "final.png", final.image_bytes)
    except asyncio.CancelledError:
        result["status"] = "interrupted"
        raise
    except Exception as error:
        result.setdefault("errors", []).append(redact(f"{type(error).__name__}: {error}"))
        if result["status"] == "running":
            result["status"] = "setup_error" if session is None else "evaluation_error"
        if display.is_connected and not result.get("manual_input_required"):
            try:
                frame = await run_display_call(display.capture_raw)
                private_bytes(output / "error.png", frame.image_bytes)
            except Exception as diagnostic:
                result["diagnostic_error"] = redact(str(diagnostic))
    finally:

        async def close_session():
            if session is not None:
                session.stop()
                if session._task is not None:
                    await asyncio.gather(session._task, return_exceptions=True)
                await session.aclose()
            else:
                await close_provider(provider)

        steps = [
            ("session", close_session),
            ("display", lambda: run_display_call(display.disconnect)),
        ]

        def remove_containers():
            # A cancelled allocation may finish after the awaiting assignment was cancelled.
            owned = (
                [container]
                if container is not None
                else client.containers.list(
                    all=True,
                    filters={"label": "ai-desktop-agent.evaluation=" + result["attempt_id"]},
                )
            )
            remove_resources(owned, force=True)

        def remove_networks():
            owned = (
                [network]
                if network is not None
                else client.networks.list(
                    filters={"label": "ai-desktop-agent.evaluation=" + result["attempt_id"]}
                )
            )
            remove_resources(owned)

        steps.extend(
            [
                ("container", lambda: run_display_call(remove_containers)),
                ("network", lambda: run_display_call(remove_networks)),
            ]
        )
        cancelled = await cleanup_all(steps, result)
        if cancelled:
            result["status"] = "interrupted"
        if session is not None:
            record = session.snapshot()
            result.update(
                state=record.state,
                stop_reason=record.stop_reason,
                usage=record.execution_usage,
                agent_seconds=record.execution_usage["elapsed_seconds"],
            )
        result["attempt_seconds"] = time.monotonic() - started
        atomic_json(
            output / "frames.json", {"frames": recorded.frames, "dropped": recorded.dropped}
        )
        atomic_json(output / "result.json", redact_data(result))
        if cancelled:
            raise asyncio.CancelledError


def source_metadata():
    package = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for file in sorted(package.rglob("*.py")):
        digest.update(str(file.relative_to(package)).encode())
        digest.update(file.read_bytes())
    revision = None
    with suppress(OSError, subprocess.SubprocessError):
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=package, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            revision = result.stdout.strip()
    return {
        "revision": revision,
        "source_sha256": digest.hexdigest(),
        "python": platform.python_version(),
        "dependencies": {
            name: version(name) for name in ("openai", "docker", "vncdotool", "Pillow")
        },
    }


def evaluation_limits(args):
    configured = ExecutionLimits.from_env()
    return dataclasses.replace(
        configured,
        max_seconds=args.max_seconds
        if args.max_seconds is not None
        else min(configured.max_seconds, 300),
        max_requests=args.max_requests
        if args.max_requests is not None
        else min(configured.max_requests, 60),
        max_tokens=args.max_tokens
        if args.max_tokens is not None
        else min(configured.max_tokens, 30_000),
    )


async def run_suite(args):
    selected = [case for case in CASES if case.id in args.cases]
    limits = evaluation_limits(args)
    if args.repeats < 1:
        raise ValueError("repeatsは正の整数にしてください")
    provider_name = args.provider or os.environ.get("LLM_PROVIDER", "openai")
    if args.mode == "model" and provider_name == "mock":
        raise ValueError(
            "モデル評価ではmockを使えません。動作確認は--mode scriptedを指定してください"
        )

    def make_provider(case):
        return (
            ScriptedProvider(case)
            if args.mode == "scripted"
            else create_llm_provider(provider_name, args.model, base_url=args.base_url)
        )

    provider = make_provider(selected[0])  # Check configuration before allocating a desktop.
    output = Path(args.out)
    try:
        output.mkdir(parents=True, mode=0o700, exist_ok=False)
    except BaseException:
        with suppress(Exception):
            await close_provider(provider)
        raise
    suite_json = json.dumps(
        [case.manifest() for case in selected], ensure_ascii=False, sort_keys=True
    )
    report = {
        "suite_version": SUITE_VERSION,
        "suite_sha256": hashlib.sha256(suite_json.encode()).hexdigest(),
        "mode": args.mode,
        "provider": "scripted" if args.mode == "scripted" else provider_name,
        "model": provider.model_name,
        "endpoint": redact(str(provider._client.base_url))
        if hasattr(provider, "_client")
        else None,
        "decision_provider": "none",
        "created_at": datetime.now(UTC).isoformat(),
        "requested_attempts": len(selected) * args.repeats,
        "limits_per_attempt": limits.to_dict(),
        "desktop_resources": {
            "memory_bytes": DESKTOP_RESOURCES.memory_bytes,
            "memory_swap_bytes": DESKTOP_RESOURCES.memory_bytes,
            "nano_cpus": DESKTOP_RESOURCES.nano_cpus,
            "pids_limit": DESKTOP_RESOURCES.pids_limit,
            "shm_bytes": DESKTOP_SHM_BYTES,
        },
        "status": "running",
        "results": [],
    }

    def save():
        report["summary"] = summarize(report["results"])
        if report.get("error") and not report["summary"]["infrastructure_errors"]:
            report["summary"]["infrastructure_errors"] += 1
        if report.get("cleanup_errors"):
            report["summary"]["infrastructure_errors"] += 1
        atomic_json(output / "report.json", redact_data(report))

    client = None
    owner = asyncio.current_task()
    loop = asyncio.get_running_loop()
    with suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGTERM, owner.cancel)
    try:
        report["source"] = source_metadata()
        save()

        def connect_docker():
            nonlocal client
            # A cancelled await drains the worker but discards its return value.
            # Retain ownership before returning so the client is still closed.
            client = docker.from_env(timeout=15)

        await run_display_call(connect_docker)
        image = await run_display_call(client.images.get, args.image)
        report["desktop_image"] = image.id
        for repetition in range(1, args.repeats + 1):
            for case in selected:
                result = {
                    "attempt_id": uuid.uuid4().hex[:16],
                    "case_id": case.id,
                    "repetition": repetition,
                    "status": "running",
                }
                report["results"].append(result)
                attempt_output = output / result["attempt_id"]
                attempt_output.mkdir(mode=0o700)
                save()
                if provider is None:
                    provider = make_provider(case)
                current_provider = provider
                provider = None
                await run_attempt(
                    client, image.id, case, current_provider, limits, attempt_output, result
                )
                save()
        report["status"] = (
            "incomplete" if summarize(report["results"])["infrastructure_errors"] else "completed"
        )
    except asyncio.CancelledError:
        report["status"] = "interrupted"
        for result in report["results"]:
            if result["status"] == "running":
                result["status"] = "interrupted"
        raise
    except Exception as error:
        report["status"] = "incomplete"
        report["error"] = redact(f"{type(error).__name__}: {error}")
        for result in report["results"]:
            if result["status"] == "running":
                result["status"] = "setup_error"
    finally:
        steps = []
        if provider is not None:
            steps.append(("provider", lambda: close_provider(provider)))
        if client is not None:
            steps.append(("docker", lambda: run_display_call(client.close)))
        cancelled = await cleanup_all(steps, report)
        if cancelled:
            report["status"] = "interrupted"
        elif report.get("cleanup_errors") and report["status"] == "completed":
            report["status"] = "incomplete"
        with suppress(NotImplementedError):
            loop.remove_signal_handler(signal.SIGTERM)
        save()
        if cancelled:
            raise asyncio.CancelledError
    summary = report["summary"]
    print(
        json.dumps({"mode": args.mode, "status": report["status"], **summary}, ensure_ascii=False)
    )
    if report["status"] != "completed" or summary["infrastructure_errors"]:
        return 2
    return 0 if summary["successes"] == report["requested_attempts"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="保存成果物で判定するデスクトップ評価")
    parser.add_argument("--mode", choices=["model", "scripted"], default="model")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--image", default="ai-desktop-agent-desktop-eval")
    parser.add_argument(
        "--cases", choices=[case.id for case in CASES], nargs="+", default=[c.id for c in CASES]
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--out", default="data/evaluations/" + uuid.uuid4().hex[:12])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("twisted").setLevel(logging.WARNING)
    install_log_redaction()
    try:
        return asyncio.run(run_suite(args))
    except KeyboardInterrupt:
        return 130
    except asyncio.CancelledError:
        return 130
    except Exception as error:
        print(redact(str(error)), file=sys.stderr)
        return 2
    finally:
        VNCClient.shutdown_runtime()


if __name__ == "__main__":
    raise SystemExit(main())
