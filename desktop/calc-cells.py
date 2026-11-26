#!/usr/bin/python3
"""Edit one approved cell at a time through the active Calc GUI's AT-SPI tree."""

import json
import re
import subprocess
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402


def wait_for(get, reason="control unavailable", seconds=1):
    until = time.monotonic() + seconds
    while True:
        value = get()
        if value:
            return value
        if time.monotonic() >= until:
            raise RuntimeError(reason)
        time.sleep(0.025)


def state(node, flag):
    node.clear_cache()
    return node.get_state_set().contains(flag)


def children(node):
    for index in range(min(128, node.get_child_count())):
        child = node.get_child_at_index(index)
        if child is not None:
            yield child


def find(root, role):
    # A sheet exposes over a billion virtual cells. Traverse only layout containers.
    for child in children(root):
        kind = child.get_role_name()
        if kind == role and state(child, Atspi.StateType.SHOWING):
            return child
        if kind in {"panel", "root pane", "scroll pane", "split pane", "viewport"}:
            found = find(child, role)
            if found is not None:
                return found
    return None


class Calc:
    def __init__(self, pid, xid, sheet, observed_title):
        self.xid = str(xid)
        self.sheet = sheet
        self.formulas = {}
        app = next(
            a
            for a in children(Atspi.get_desktop(0))
            if a.get_name() == "soffice" and a.get_process_id() == pid
        )
        title = subprocess.check_output(["xdotool", "getwindowname", self.xid], text=True).rstrip(
            "\n"
        )
        if title != observed_title:
            raise RuntimeError("window changed")
        self.frame = next(
            f for f in children(app) if f.get_name() == title and state(f, Atspi.StateType.ACTIVE)
        )
        self.document = find(self.frame, "document spreadsheet")
        self.table = find(self.document, "table")
        self.guard()

    def guard(self):
        active = subprocess.check_output(["xdotool", "getactivewindow"], text=True).strip()
        if (
            active != self.xid
            or self.table.get_name() != self.sheet
            or not state(self.table, Atspi.StateType.SHOWING)
            or not state(self.frame, Atspi.StateType.ACTIVE)
        ):
            raise RuntimeError("sheet changed")

    def key(self, *keys):
        self.guard()
        subprocess.run(["xdotool", "key", "--clearmodifiers", *keys], check=True)

    def editor(self, cell):
        if not cell.grab_focus():
            raise RuntimeError("cell unavailable")
        wait_for(lambda: state(cell, Atspi.StateType.FOCUSED))
        self.key("F2")
        node = wait_for(lambda: find(self.document, "paragraph"))
        wait_for(lambda: state(node, Atspi.StateType.FOCUSED), "editor not focused")
        return node

    def write(self, address, value):
        self.guard()
        letters, number = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", address).groups()
        column = 0
        for letter in letters:
            column = column * 26 + ord(letter) - ord("A") + 1
        cell = self.table.get_accessible_at(int(number) - 1, column - 1)
        if cell.get_name() != address or find(self.document, "paragraph") is not None:
            raise RuntimeError("ambiguous target or pending edit")
        editor = self.editor(cell)
        if editor.get_parent().get_child_count() != 1:
            raise RuntimeError("multiline editor")
        if not Atspi.EditableText.set_text_contents(editor, value):
            raise RuntimeError("cell is not editable")
        # Calc commits an AT-SPI edit after a native change event in its editor.
        self.key("End", "space", "BackSpace")
        wait_for(lambda: Atspi.Text.get_text(editor, 0, -1) == value, "editor differs")
        self.key("Return")
        wait_for(lambda: find(self.document, "paragraph") is None)
        if value.startswith("="):
            # AT-SPI's Formula attribute can truncate ranges. Read the native editor back.
            wait_for(lambda: cell.get_attributes().get("Formula"), "formula missing")
            editor = self.editor(cell)
            saved = Atspi.Text.get_text(editor, 0, -1)
            self.key("Escape")
            wait_for(lambda: find(self.document, "paragraph") is None)
            if saved != value:
                raise RuntimeError("stored formula differs")
            # Calc's GTK bridge sometimes exposes a calculation error as an empty
            # accessible string. A blank result cannot certify a successful calculation.
            display = wait_for(
                lambda: Atspi.Text.get_text(cell, 0, -1), "formula result unavailable"
            )
            if re.match(
                r"^(Err:|#(REF!|VALUE!|DIV/0!|NAME\?|N/A|NUM!|NULL!|SPILL!|CALC!))", display.strip()
            ):
                raise RuntimeError("formula error")
            self.formulas[address] = cell
        else:
            wait_for(lambda: Atspi.Text.get_text(cell, 0, -1) == value, "display differs")
        self.guard()

    def displayed_results(self):
        self.guard()
        values = {}
        for address, cell in self.formulas.items():
            value = Atspi.Text.get_text(cell, 0, -1)
            if cell.get_name() == address and value and len(value) <= 80:
                values[address] = value
        self.guard()
        return values

    def save(self):
        self.guard()
        # Use the accessible document name to restrict quick-save to existing files.
        # A new document is saved through the normal Save As dialog by the agent.
        if not self.document.get_name().startswith("file://"):
            raise RuntimeError("save needs a filename")
        self.key("ctrl+s")
        # On GTK the Save button can stay enabled after a successful save. Report
        # delivery and the foreground document; a file audit is a separate check.
        until = time.monotonic() + 0.65
        while time.monotonic() < until:
            self.guard()
            time.sleep(0.075)
        self.guard()


def main():
    calc = Calc(*json.loads(sys.stdin.readline()))
    print("ready-2", flush=True)
    # EOF or a failed readback stops the batch. The host sends only the next cell.
    for line in sys.stdin:
        cell = json.loads(line)
        try:
            if cell == {"read_results": True}:
                print(json.dumps(calc.displayed_results(), ensure_ascii=False), flush=True)
                continue
            if cell == {"save": True}:
                calc.save()
            else:
                calc.write(cell["address"], cell["value"])
        except RuntimeError as exc:
            print(str(exc)[:80], flush=True)
            return
        print("ok", flush=True)


if __name__ == "__main__":
    main()
