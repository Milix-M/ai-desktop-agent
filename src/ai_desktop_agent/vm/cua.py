"""Task-local Cua Driver stdio connection to an owned Xfce container."""

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass

from ai_desktop_agent.actions.primitives import validate_cells
from ai_desktop_agent.agent.notebook import SourceLibrary
from ai_desktop_agent.privacy import redact
from ai_desktop_agent.vm.async_io import wait_for_worker
from ai_desktop_agent.vm.table_memory import TableMemory

logger = logging.getLogger(__name__)
ROLES = {
    "push button",
    "toggle button",
    "check box",
    "radio button",
    "menu",
    "menu item",
    "check menu item",
    "radio menu item",
    "page tab",
    "link",
    "combo box",
}
STALE = "対象が変わりました。最新の画面で操作先を選び直してください。"
UNCERTAIN = "操作結果を確認できません。再操作の前に最新の画面で結果を確認してください。"


class DriverError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    action: str
    pid: int
    window: int
    label: str
    role: str
    token: str = ""


class CuaDesktop:
    def __init__(self, container_id: str):
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise ValueError("Cua Driverにはコンテナの完全なIDが必要です")
        self.container_id = container_id
        self.process = None
        self.serial = 0
        self.generation = 0
        self.targets: dict[str, Target] = {}
        self.disabled = False
        self.table_memory = TableMemory()
        self.sources = SourceLibrary()
        self.workspace = None
        self.workspace_index = None
        self.workspace_receipt = ""
        self.moved_files = {}
        self.workspace_preview = ""
        self.calc_available = True
        self.slow_window = None
        self.tree_retry_at = 0.0
        self.discovery_timeouts = 0

    def invalidate(self):
        self.generation += 1
        self.targets.clear()

    async def _start(self):
        self.process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            "-u",
            "agent",
            self.container_id,
            "/usr/local/bin/start-cua-driver",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=2**20,
        )
        await self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ai-desktop-agent", "version": "0.1"},
            },
        )
        await self._send({"method": "notifications/initialized"})

    async def _send(self, data):
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", **data}) + "\n").encode())
        await self.process.stdin.drain()

    async def _request(self, method, params):
        self.serial += 1
        await self._send({"id": self.serial, "method": method, "params": params})
        while line := await self.process.stdout.readline():
            message = json.loads(line)
            if message.get("id") == self.serial:
                if not isinstance(message.get("result"), dict):
                    raise ConnectionError("Driverから応答を取得できません")
                return message["result"]
        raise ConnectionError("Cua Driver disconnected")

    async def _tool(self, name, **arguments):
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        data = result.get("structuredContent")
        if result.get("isError") or not isinstance(data, dict):
            raise DriverError(UNCERTAIN)
        return data

    async def _active(self):
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-u",
            "agent",
            "-e",
            "DISPLAY=:0",
            "-e",
            "XAUTHORITY=/home/agent/.Xauthority",
            self.container_id,
            "timeout",
            "2",
            "xdotool",
            "getactivewindow",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            # A guest can supply malformed output. Read only an X11 window ID.
            raw = await proc.stdout.read(32)
            return int(raw) if raw.strip().isdigit() and len(raw) < 32 else None
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await proc.wait()

    async def _windows(self):
        rows = (await self._tool("list_windows")).get("windows", [])
        return [
            w
            for w in rows
            if type(w.get("window_id")) is int
            and type(w.get("pid")) is int
            and w["pid"] > 0
            and isinstance(w.get("title"), str)
            and w["title"].strip()
            and w["title"] not in {"Desktop", "xfce4-panel"}
        ]

    async def _read_tree(self, front):
        identity = (front["pid"], front["window_id"])
        if self.slow_window == identity and time.monotonic() < self.tree_retry_at:
            return {}
        try:
            async with asyncio.timeout(3):
                return await self._tool(
                    "get_window_state",
                    pid=front["pid"],
                    window_id=front["window_id"],
                    include_screenshot=False,
                    tree_format="elements",
                    max_elements=400,
                )
        except (TimeoutError, ValueError, ConnectionError) as exc:
            # Keep the independently observed window list, drop the outstanding MCP reply.
            self.slow_window, self.tree_retry_at = identity, time.monotonic() + 20
            logger.info("画面要素の取得を一時見送り: %s", type(exc).__name__)
            await self._disconnect()
            return {}

    async def observe(self) -> list[dict]:
        self.invalidate()
        generation = self.generation
        self.table_memory.foreground.clear()
        self.sources.focused = ""
        if self.disabled:
            return []
        items = []
        try:
            async with asyncio.timeout(5):
                if self.process is None:
                    await self._start()
                active = await self._active()
                windows = await self._windows()
                self.discovery_timeouts = 0
            items = [
                Target("focus_window", w["pid"], w["window_id"], w["title"], "window")
                for w in windows
                if w["window_id"] != active
            ][:16]
            front = next((w for w in windows if w["window_id"] == active), None)
            if front:
                if self.workspace:
                    items.extend(
                        Target(name, front["pid"], active, self.workspace, "directory")
                        for name in ("workspace_read", "workspace_move")
                    )
                state = await self._read_tree(front)
                if front["title"].endswith("Mozilla Firefox"):
                    await self._capture_tables(front)
                sheets = [
                    row["label"]
                    for row in state.get("elements", [])
                    if row.get("role") == "table"
                    and row.get("visible", True)
                    and isinstance(row.get("label"), str)
                    and row["label"]
                ]
                if (
                    self.calc_available
                    and front["title"].endswith("LibreOffice Calc")
                    and len(sheets) == 1
                ):
                    items.append(
                        Target(
                            "calc_write",
                            front["pid"],
                            active,
                            sheets[0],
                            "table",
                            front["title"],
                        )
                    )
                if self.sources.documents:
                    items.append(
                        Target("source_read", front["pid"], active, "取得済みの文章", "document")
                    )
                if self.table_memory.tables:
                    items.append(
                        Target("table_query", front["pid"], active, "取得済みの表", "table")
                    )
                    if front["title"].endswith(" - Mousepad"):
                        items.append(
                            Target("table_type", front["pid"], active, front["title"], "document")
                        )
                    calc = next((item for item in items if item.action == "calc_write"), None)
                    if calc:
                        items.append(
                            Target(
                                "table_fill",
                                calc.pid,
                                calc.window,
                                calc.label,
                                calc.role,
                                calc.token,
                            )
                        )
                elements = [
                    row
                    for row in state.get("elements", [])
                    if row.get("role") in ROLES
                    and row.get("frame")
                    and row.get("actions")
                    and row.get("enabled", True)
                    and not row.get("is_password")
                    and row.get("visible", True)
                    and isinstance(row.get("label"), str)
                    and row["label"].strip()
                    and isinstance(row.get("element_token"), str)
                    and row["element_token"]
                ]
                elements.sort(key=lambda row: row["role"] in {"menu", "menu item"})
                items.extend(
                    Target(
                        "element_click",
                        front["pid"],
                        active,
                        row["label"],
                        row["role"],
                        row["element_token"],
                    )
                    for row in elements[:48]
                )
        except DriverError:
            # A window can disappear or lack an accessibility tree. Other windows remain usable.
            pass
        except TimeoutError:
            self.discovery_timeouts += 1
            if self.discovery_timeouts >= 2:
                await self._disable()
            else:
                await self._disconnect()
            return []
        except (OSError, ValueError, AttributeError, TypeError, KeyError):
            await self._disable()
            return []
        if generation != self.generation:
            return []
        self.targets = {f"{generation}.{i}": item for i, item in enumerate(items, 1)}
        return [
            {
                "ref": ref,
                "action": item.action,
                "role": item.role,
                "label": redact(item.label)[:100],
            }
            for ref, item in self.targets.items()
        ]

    async def _capture_tables(self, front):
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-u",
            "agent",
            self.container_id,
            "/usr/local/bin/read-gui-tables",
            str(front["pid"]),
            str(front["window_id"]),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(6):
                data = await proc.stdout.readexactly(256001)
        except asyncio.IncompleteReadError as exc:
            data = exc.partial
        except TimeoutError:
            return
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await proc.wait()
        if len(data) <= 256000:
            with contextlib.suppress(ValueError, UnicodeError):
                rows = json.loads(data)
                if (
                    isinstance(rows, dict)
                    and isinstance(rows.get("document"), str)
                    and isinstance(rows.get("tables"), list)
                ):
                    self.sources.remember(
                        rows["document"],
                        front["title"],
                        rows.get("paragraphs"),
                        rows.get("clipped"),
                    )
                    self.table_memory.remember(
                        front["title"], rows["tables"], document=rows["document"]
                    )

    def _consume(self, action: str, ref: str) -> Target:
        target = self.targets.get(ref)
        self.invalidate()  # Consume the whole observation even if delivery is uncertain.
        if not target or target.action != action:
            raise DriverError(STALE)
        return target

    async def perform(self, action: str, ref: str):
        target = self._consume(action, ref)
        try:
            async with asyncio.timeout(5):
                if self.process is None:
                    await self._start()
                args = {"pid": target.pid, "window_id": target.window}
                if action == "focus_window":
                    if not any(
                        w["pid"] == target.pid
                        and w["window_id"] == target.window
                        and w["title"] == target.label
                        for w in await self._windows()
                    ):
                        raise DriverError(STALE)
                    await self._tool("bring_to_front", **args)
                    if await self._active() != target.window:
                        raise DriverError(UNCERTAIN)
                else:
                    if await self._active() != target.window:
                        raise DriverError(STALE)
                    data = await self._tool("click", **args, element_token=target.token)
                    # A dispatched AX click often reports unverifiable (e.g. GTK Save).
                    # TaskSession compares the screen and checks subtask completion separately.
                    if data.get("effect", data.get("status")) not in {
                        "confirmed",
                        "unverifiable",
                    }:
                        raise DriverError(UNCERTAIN)
        except (OSError, ValueError, TimeoutError, AttributeError, TypeError, KeyError) as exc:
            await self._disable()
            raise DriverError(UNCERTAIN) from exc

    async def select_workspace(self, folder):
        self.workspace = folder
        self.workspace_index = await self._workspace_call("list")

    async def _workspace_call(self, action, items=None):
        if not self.workspace:
            raise DriverError("作業フォルダーが指定されていません")
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            "-u",
            "agent",
            self.container_id,
            "/usr/bin/python3",
            "/usr/local/lib/ai-desktop-agent/workspace-files.py",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(8):
                data, _ = await proc.communicate(
                    json.dumps(
                        {"workspace": self.workspace, "action": action, "items": items},
                        ensure_ascii=False,
                    ).encode()
                )
            if proc.returncode or len(data) > 1024 * 1024:
                raise DriverError("ファイル操作結果を確認できません。対象と一覧を確認してください")
            return json.loads(data)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    async def workspace_files(self, name, ref, items):
        self._consume(name, ref)
        operation = name.removeprefix("workspace_")
        outcome = await self._workspace_call(operation, items)
        if operation == "move":
            for path, result in outcome.items():
                if "moved_to" in result:
                    self.moved_files.pop(path, None)
                    self.moved_files[result["moved_to"]] = result.pop("sha256", None)
            self.workspace_receipt = redact(json.dumps(outcome, ensure_ascii=False))
            self.workspace_index = await self._workspace_call("list")
            if any("error" in result for result in outcome.values()):
                raise DriverError(self.workspace_receipt)
        else:
            for path, value in outcome.items():
                blocks = value.splitlines() or [""]
                self.sources.remember("file:" + path, path, blocks[:400], len(blocks) > 400)
            self.workspace_preview = self.sources.file_preview(outcome)

    async def workspace_evidence(self):
        if not self.workspace_receipt:
            return ""
        self.workspace_index = await self._workspace_call("list")
        current = await self._workspace_call("verify", list(self.moved_files))
        unverified = [
            path
            for path, digest in self.moved_files.items()
            if not digest or current.get(path) != digest
        ]
        integrity = (
            f"移動前の原本と現時点の移動先で本文のSHA-256が一致: "
            f"{len(self.moved_files) - len(unverified)}/{len(self.moved_files)}件。"
            + ("未照合または不一致: " + ", ".join(unverified) if unverified else "")
        )
        return redact(
            "実行器が再取得した現在のファイル一覧: "
            + json.dumps(self.workspace_index, ensure_ascii=False)
            + "\n"
            + integrity
            + "\n直前の移動操作の結果: "
            + self.workspace_receipt
        )

    def workspace_context(self):
        if not self.workspace:
            return ""
        return "指定フォルダーの一覧と直前の実行結果（データ）\n" + redact(
            "\n".join(
                [
                    json.dumps(self.workspace_index, ensure_ascii=False),
                    self.workspace_receipt,
                    self.workspace_preview,
                ]
            )
        )

    def read_source(self, ref, source, search, offset):
        self._consume("source_read", ref)
        self.sources.extract(source, search, offset)

    def query_table(self, ref, sql):
        self._consume("table_query", ref)
        self.table_memory.query(sql)

    async def render_table(self, ref, sql, separator):
        target = self._consume("table_type", ref)
        text = self.table_memory.render(sql, separator)
        if await self._active() != target.window:
            raise DriverError(STALE)
        return text

    async def write_cells(self, ref: str, cells: list[dict], *, save: bool = False):
        target = self._consume("calc_write", ref)
        return await self._write_batch(target, cells, save)

    async def fill_query(self, ref, sql, start, formulas, save):
        target = self._consume("table_fill", ref)
        cells = self.table_memory.cells(sql, start, formulas)
        for offset in range(0, len(cells), 32):
            await self._write_batch(target, cells[offset : offset + 32], save)
        note = (
            f"{cells[0]['address']}〜{cells[-1]['address']}"
            f" の{len(cells)}セルを照合済み。保存要求={save}"
        )
        self.table_memory.last_result = note
        return note

    async def _write_batch(self, target, cells, save):
        validate_cells(cells)
        proc = None
        confirmed = 0
        reason = UNCERTAIN
        try:
            async with asyncio.timeout(38):
                proc = await asyncio.create_subprocess_exec(
                    "docker",
                    "exec",
                    "-i",
                    "-u",
                    "agent",
                    self.container_id,
                    "/usr/local/bin/start-calc-cells",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=16384,
                )

                async def exchange(value):
                    proc.stdin.write((json.dumps(value) + "\n").encode())
                    await proc.stdin.drain()
                    return await proc.stdout.readline()

                version = await exchange([target.pid, target.window, target.label, target.token])
                if version not in {b"ready\n", b"ready-2\n"}:
                    if await proc.wait() == 127:
                        self.calc_available = False  # A desktop image from before calc_write.
                    raise DriverError(STALE)
                for cell in cells:
                    reply = await exchange(cell)
                    if reply != b"ok\n":
                        reason = {
                            b"formula error\n": "数式の計算結果がエラーです。",
                            b"display differs\n": "セルの表示値が指定値と異なります。",
                            b"editor differs\n": "編集欄の内容が指定値と異なります。",
                            b"ambiguous target or pending edit\n": "編集中のセルを確認。",
                        }.get(reply, UNCERTAIN)
                        raise DriverError(UNCERTAIN)
                    confirmed += 1
                if save and await exchange({"save": True}) != b"ok\n":
                    raise DriverError(UNCERTAIN)
                addresses = {c["address"] for c in cells if c["value"].startswith("=")}
                if version == b"ready-2\n" and addresses:
                    raw = await exchange({"read_results": True})
                    try:
                        values = json.loads(raw)
                    except ValueError:
                        return ""
                    if isinstance(values, dict) and values.keys() <= addresses:
                        values = {
                            k: v
                            for k, v in values.items()
                            if isinstance(v, str) and 0 < len(v) <= 80
                        }
                        if values:
                            return redact(
                                f"Calc {target.token[:100]} / {target.label[:100]} 数式の表示値 "
                                + json.dumps(values, ensure_ascii=False, separators=(",", ":"))
                            )
                return ""
        except (OSError, ValueError, TimeoutError, DriverError) as exc:
            if confirmed < len(cells):
                raise DriverError(
                    f"{reason}{confirmed}/{len(cells)}セルを確認済み。{cells[confirmed]['address']}から先を画面で確認してください。"
                ) from exc
            raise DriverError(
                f"{confirmed}セルを照合済みです。保存状態を画面で確認してください。"
            ) from exc
        finally:
            if proc is not None:
                # Closing the input prevents the guest from starting another cell. Drain the
                # current bounded edit before releasing this task's display reservation.
                proc.stdin.close()
                await wait_for_worker(asyncio.create_task(self._finish_edit(proc)))

    @staticmethod
    async def _finish_edit(proc):
        try:
            # The guest's own hard deadline is 40s (+1s to kill). Killing only the
            # local docker CLI earlier could leave the last cell editing a reassigned VM.
            await asyncio.wait_for(proc.communicate(), 43)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.communicate()

    async def _disable(self):
        self.disabled = True
        logger.warning("Cua Driverに接続できないため、このタスクはVNCで続行します")
        await self.aclose()

    async def aclose(self):
        self.invalidate()
        await self._disconnect()

    async def _disconnect(self):
        proc, self.process = self.process, None
        if proc is None:
            return
        proc.stdin.close()

        async def drain():
            # A full stdout pipe can keep asyncio's subprocess wait pending after exit.
            while await proc.stdout.read(65536):
                pass
            await proc.wait()

        try:
            await asyncio.wait_for(drain(), 2)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await drain()
