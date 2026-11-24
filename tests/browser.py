"""A small CDP driver for browser checks without adding a browser SDK dependency."""

import asyncio
import json
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

BROWSER = shutil.which("google-chrome") or shutil.which("chromium")


class Browser:
    def __init__(self, socket):
        self.socket = socket
        self.sequence = 0
        self.events = []
        self.requests = {}
        self.network_failures = []

    async def command(self, method, **params):
        self.sequence += 1
        sequence = self.sequence
        await self.socket.send(json.dumps({"id": sequence, "method": method, "params": params}))
        async with asyncio.timeout(15):
            while True:
                result = json.loads(await self.socket.recv())
                method = result.get("method")
                params = result.get("params", {})
                if method == "Network.requestWillBeSent":
                    self.requests[params["requestId"]] = {
                        "url": params["request"]["url"].split("?", 1)[0],
                        "type": params.get("type"),
                    }
                elif method == "Network.responseReceived":
                    request = self.requests.get(params["requestId"])
                    if request is not None:
                        request["status"] = params["response"]["status"]
                elif method in {"Network.loadingFinished", "Network.loadingFailed"}:
                    request = self.requests.pop(params["requestId"], None)
                    if method == "Network.loadingFailed":
                        self.network_failures.append(
                            {"request": request, "error": params["errorText"]}
                        )
                if method in {
                    "Runtime.exceptionThrown",
                    "Runtime.consoleAPICalled",
                    "Log.entryAdded",
                }:
                    self.events.append(result)
                if result.get("id") == sequence:
                    assert "error" not in result, result
                    return result.get("result", {})

    async def evaluate(self, expression):
        result = await self.command(
            "Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True
        )
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    async def wait(self, expression, timeout=20):
        try:
            async with asyncio.timeout(timeout):
                while not await self.evaluate(expression):
                    await asyncio.sleep(0.1)
        except TimeoutError as error:
            text = await self.evaluate("document.body.innerText")
            loading = await self.evaluate(
                "({readyState:document.readyState, scripts:[...document.scripts]"
                ".filter(script => script.src).map(script => script.src)})"
            )
            raise AssertionError(
                f"Browser wait failed: {expression}\n{text}\n{self.events[-4:]}\n"
                f"Loading: {loading}\nPending: {self.requests}\n"
                f"Network failures: {self.network_failures[-10:]}"
            ) from error

    async def type_keys(self, text):
        for char in text:
            key = "Enter" if char == "\n" else char
            code = (
                "Enter"
                if char == "\n"
                else f"Key{char.upper()}"
                if char.isalpha()
                else "Unidentified"
            )
            await self.command("Input.dispatchKeyEvent", type="keyDown", key=key, code=code)
            await self.command("Input.dispatchKeyEvent", type="keyUp", key=key, code=code)


async def wait_for_debugger(profile: Path, process) -> str:
    """Wait for a usable page target on a newly started browser's debugging port."""
    port_file = profile / "DevToolsActivePort"
    last_status = "waiting for DevToolsActivePort"
    try:
        async with asyncio.timeout(60), httpx.AsyncClient(trust_env=False, timeout=3) as client:
            while process.poll() is None:
                try:
                    port = port_file.read_text().partition("\n")[0]
                except FileNotFoundError:
                    port = ""
                if port.isdigit():
                    try:
                        response = await client.get(f"http://127.0.0.1:{port}/json")
                        response.raise_for_status()
                        for target in response.json():
                            url = target.get("webSocketDebuggerUrl")
                            if target.get("type") == "page" and url:
                                return url
                        last_status = "no page target yet"
                    except (httpx.HTTPError, json.JSONDecodeError) as error:
                        last_status = type(error).__name__
                await asyncio.sleep(0.1)
        reason = f"exited with status {process.returncode}"
    except TimeoutError:
        reason = f"startup timed out ({last_status})"
    log = (profile.parent / "browser.log").read_text(errors="replace")[-2000:]
    raise RuntimeError(f"{BROWSER} {reason}\n{log}")


@asynccontextmanager
async def browser(directory: Path):
    profile = directory / "browser-profile"
    with (directory / "browser.log").open("wb") as log:
        process = subprocess.Popen(
            [
                BROWSER,
                "--headless",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            debugger_url = await wait_for_debugger(profile, process)
            async with connect(debugger_url, proxy=None) as socket:
                instance = Browser(socket)
                await instance.command("Page.enable")
                await instance.command("Network.enable")
                await instance.command("Log.enable")
                await instance.command("Runtime.enable")
                try:
                    yield instance
                finally:
                    (directory / "network-failures.json").write_text(
                        json.dumps(instance.network_failures, ensure_ascii=False, indent=2)
                    )
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
