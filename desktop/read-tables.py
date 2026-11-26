"""Read bounded table and prose snapshots of the active Firefox document via AT-SPI."""

import hashlib
import json
import subprocess
import sys

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402


def active_window():
    return int(subprocess.check_output(["xdotool", "getactivewindow"], text=True))


def walk(node, remaining, depth=0):
    if not node or depth > 32 or remaining[0] <= 0:
        return
    remaining[0] -= 1
    role = node.get_role_name()
    yield node, role
    if role in ("table", "password text") or (
        role == "document web" and not node.get_state_set().contains(Atspi.StateType.SHOWING)
    ):
        return
    for idx in range(min(node.get_child_count(), 128)):
        yield from walk(node.get_child_at_index(idx), remaining, depth + 1)


def tables(pid, xid):
    if active_window() != xid:
        return []
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    bus.call_sync(
        "org.a11y.Bus",
        "/org/a11y/bus",
        "org.freedesktop.DBus.Properties",
        "Set",
        GLib.Variant("(ssv)", ("org.a11y.Status", "ScreenReaderEnabled", GLib.Variant("b", True))),
        None,
        0,
        1000,
        None,
    )
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app.get_process_id() != pid:
            continue
        for j in range(app.get_child_count()):
            frame = app.get_child_at_index(j)
            if not frame.get_state_set().contains(Atspi.StateType.ACTIVE):
                continue
            documents = [
                n
                for n, role in walk(frame, [1200])
                if role == "document web" and n.get_state_set().contains(Atspi.StateType.SHOWING)
            ]
            if len(documents) != 1:
                continue
            result = []
            paragraphs = []
            chars = 0
            clipped = False
            remaining = [1200]
            for node, role in walk(documents[0], remaining):
                if role in (
                    "heading",
                    "paragraph",
                    "list item",
                    "static",
                    "text",
                ) and not node.get_state_set().contains(Atspi.StateType.EDITABLE):
                    if (
                        role == "text"
                        and node.get_interfaces()
                        and "EditableText" in node.get_interfaces()
                    ):
                        continue
                    value = node.get_name() or ""
                    if "Text" in node.get_interfaces():
                        length = Atspi.Text.get_character_count(node)
                        value = Atspi.Text.get_text(node, 0, min(6000, length)) or value
                        clipped |= length > 6000
                    value = value.replace("\ufffc", " ").strip()
                    if value and (not paragraphs or value != paragraphs[-1]):
                        if (
                            chars + len(value) <= 48000
                            and len(paragraphs) < 400
                            and len(value) <= 6000
                        ):
                            paragraphs.append(value)
                            chars += len(value)
                        else:
                            clipped = True
                if role != "table":
                    continue
                rows, cols = node.get_n_rows(), node.get_n_columns()
                if not (1 < rows <= 256 and 0 < cols <= 32 and rows * cols <= 2048):
                    continue
                data = []
                has_headers = False
                for r in range(rows):
                    row = []
                    for c in range(cols):
                        cell = node.get_accessible_at(r, c)
                        if cell is None:
                            return []
                        if r == 0 and cell.get_role_name() in (
                            "column header",
                            "table column header",
                        ):
                            has_headers = True
                        value = cell.get_name() or ""
                        if len(value) > 256:
                            return []
                        row.append(value)
                    data.append(row)
                if not has_headers:
                    data.insert(0, [f"c{i + 1}" for i in range(cols)])
                result.append(data)
                if len(result) >= 8:
                    break
            document = documents[0]
            if active_window() != xid or not document.get_state_set().contains(
                Atspi.StateType.SHOWING
            ):
                return {}
            uri = document.get_document_attribute_value("DocURL") or ""
            identity = hashlib.sha256(uri.encode()).hexdigest() if uri else str(xid)
            return {
                "document": identity,
                "tables": result,
                "paragraphs": paragraphs,
                "clipped": clipped or remaining[0] == 0,
            }
    return []


if __name__ == "__main__":
    print(json.dumps(tables(*map(int, sys.argv[1:])), ensure_ascii=False))
