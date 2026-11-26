"""Run with CUA_SMOKE_IMAGE set to the image built with --build-arg CUA_DRIVER_ENABLED=true."""

import asyncio
import os
from contextlib import closing

import docker
import pytest

from ai_desktop_agent.evaluation.cases import CASES, read_artifact, spreadsheet_matches
from ai_desktop_agent.evaluation.desktop import ENV, exec_guest, prepare_case, wait_desktop
from ai_desktop_agent.vm.cua import CuaDesktop, DriverError

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(not os.getenv("CUA_SMOKE_IMAGE"), reason="requires a built Xfce image"),
]


async def target(driver, action, label):
    async with asyncio.timeout(15):
        while True:
            rows = await driver.observe()
            for row in rows:
                matches = (
                    label in row["label"] if action == "focus_window" else label == row["label"]
                )
                if row["action"] == action and matches:
                    return row["ref"]
            await asyncio.sleep(0.1)


async def test_switch_then_save_in_a_moved_dialog(monkeypatch):
    with closing(docker.from_env()) as client:
        guest = client.containers.run(
            os.environ["CUA_SMOKE_IMAGE"], detach=True, shm_size="1g", cap_drop=["NET_RAW"]
        )
        driver = CuaDesktop(guest.id)
        try:
            await asyncio.to_thread(wait_desktop, guest)
            # Missing optional software must leave VNC available.
            missing = CuaDesktop(guest.id)
            try:
                assert await missing.observe() == [] and missing.disabled
            finally:
                await missing.aclose()
            await asyncio.to_thread(exec_guest, guest, ["install-cua-driver"])
            wm = exec_guest(guest, ["pgrep", "-o", "-u", "agent", "-x", "xfwm4"]).output.strip()
            env = exec_guest(guest, ["cat", f"/proc/{int(wm)}/environ"]).output.split(b"\x00")
            address = next(
                item.split(b"=", 1)[1]
                for item in env
                if item.startswith(b"DBUS_SESSION_BUS_ADDRESS=")
            )
            monkeypatch.setitem(ENV, "DBUS_SESSION_BUS_ADDRESS", address.decode())
            exec_guest(guest, ["mousepad", "--disable-server"], detach=True)
            exec_guest(guest, ["thunar", "/tmp"], detach=True)
            # Make the initial foreground deterministic regardless of application startup order.
            async with asyncio.timeout(15):
                while True:
                    windows = exec_guest(guest, ["wmctrl", "-l"]).output.decode().splitlines()
                    thunar = next((w.split()[0] for w in windows if "Thunar" in w), None)
                    if thunar and any("Mousepad" in w for w in windows):
                        break
                    await asyncio.sleep(0.1)
            exec_guest(guest, ["xdotool", "windowactivate", "--sync", thunar])
            ref = await target(driver, "focus_window", "Mousepad")
            await driver.perform("focus_window", ref)
            exec_guest(guest, ["xdotool", "type", "Cua integration sample"])
            exec_guest(guest, ["xdotool", "key", "ctrl+shift+s"])
            async with asyncio.timeout(8):
                while (
                    b"Save As"
                    not in exec_guest(guest, ["xdotool", "getactivewindow", "getwindowname"]).output
                ):
                    await asyncio.sleep(0.1)
            exec_guest(guest, ["xdotool", "key", "ctrl+a"])
            exec_guest(guest, ["xdotool", "type", "/tmp/cua-work.txt"])
            ref = await target(driver, "element_click", "Save")
            dialog = int(exec_guest(guest, ["xdotool", "getactivewindow"]).output)
            geometry = ["xdotool", "getwindowgeometry", "--shell", str(dialog)]
            before = exec_guest(guest, geometry).output
            exec_guest(guest, ["wmctrl", "-ir", hex(dialog), "-e", "0,30,48,-1,-1"])
            async with asyncio.timeout(3):
                while before == exec_guest(guest, geometry).output:
                    await asyncio.sleep(0.05)
            await driver.perform("element_click", ref)
            with pytest.raises(DriverError):
                await driver.perform("element_click", ref)
            async with asyncio.timeout(4):
                while True:
                    saved = guest.exec_run(["cat", "/tmp/cua-work.txt"], user="agent")
                    if saved.exit_code == 0:
                        assert saved.output.rstrip(b"\n") == b"Cua integration sample"
                        break
                    await asyncio.sleep(0.1)
            case = next(case for case in CASES if case.id == "spreadsheet-total")
            folder = "/home/agent/Documents/cua-calc"
            await asyncio.to_thread(prepare_case, guest, case, folder)
            sheet = "Sheet 売上"
            cells = [{"address": "A4", "value": "合計"}, {"address": "B4", "value": "=SUM(B2:B3)"}]
            ref = await target(driver, "calc_write", sheet)
            note = await driver.write_cells(ref, cells, save=True)
            assert '"B4":"2000"' in note
            async with asyncio.timeout(5):
                while not spreadsheet_matches(read_artifact(guest, f"{folder}/{case.filename}")):
                    await asyncio.sleep(0.05)
            with pytest.raises(DriverError):
                await driver.write_cells(ref, cells)
            ref = await target(driver, "calc_write", sheet)
            # A calculation error is visible in the GUI. Stop before touching the sentinel.
            with pytest.raises(DriverError, match="1/3.*D4"):
                await driver.write_cells(
                    ref,
                    [
                        {"address": "C4", "value": "first"},
                        {"address": "D4", "value": "=1/0"},
                        {"address": "A4", "value": "unexpected"},
                    ],
                )
            ref = await target(driver, "element_click", "Save")
            await driver.perform("element_click", ref)
            async with asyncio.timeout(5):
                while True:
                    data = read_artifact(guest, f"{folder}/{case.filename}")
                    if data and spreadsheet_matches(data):
                        break
                    await asyncio.sleep(0.1)
        finally:
            try:
                await driver.aclose()
            finally:
                guest.remove(force=True, v=True)
