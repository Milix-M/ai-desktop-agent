"""Operate only on regular files beneath a caller-selected Documents subdirectory."""

import contextlib
import ctypes
import hashlib
import json
import os
import stat
import sys

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
MAX_BYTES = 24000


def parts(path):
    pieces = path.split("/")
    if len(pieces) > 12 or any(
        not p or p in {".", ".."} or len(p) > 255 or any(ord(c) < 32 for c in p) for p in pieces
    ):
        raise ValueError("Invalid relative path")
    return pieces


@contextlib.contextmanager
def directory(root, components):
    fd = os.dup(root)
    try:
        for name in components:
            child = os.open(name, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def inventory(root):
    found = []

    def visit(fd, prefix, depth):
        with os.scandir(fd) as entries:
            for entry in entries:
                if len(found) >= 256:
                    raise ValueError("Workspace exceeds 256 entries")
                path = prefix + entry.name
                if entry.is_dir(follow_symlinks=False):
                    found.append(path + "/")
                    if depth < 4:
                        with directory(fd, [entry.name]) as child:
                            visit(child, path + "/", depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    found.append(path)

    visit(root, "", 0)
    return sorted(found)


def read(root, path, remaining):
    names = parts(path)
    with directory(root, names[:-1]) as parent:
        fd = os.open(names[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Expected regular file")
            data = file.read(remaining + 1)
            if len(data) > remaining:
                raise ValueError("Text selection exceeds 24000 bytes")
            return data.decode("utf-8"), len(data)


def checksum(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            return None
        data = file.read(1024 * 1024 + 1)
        return hashlib.sha256(data).hexdigest() if len(data) <= 1024 * 1024 else None


def move(root, source, target):
    src, dst = parts(source), parts(target)
    with directory(root, src[:-1]) as a, directory(root, dst[:-1]) as b:
        if not stat.S_ISREG(os.stat(src[-1], dir_fd=a, follow_symlinks=False).st_mode):
            raise ValueError("Expected regular file")
        # Linux RENAME_NOREPLACE also prevents overwrites if another actor wins a race.
        digest = checksum(a, src[-1])
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.renameat2(a, os.fsencode(src[-1]), b, os.fsencode(dst[-1]), 1):
            raise OSError(os.strerror(ctypes.get_errno()))
        return digest


def execute(root, action, items):
    if action == "list":
        return inventory(root)
    if action == "verify" and isinstance(items, list) and len(items) <= 256:
        result = {}
        for path in items:
            names = parts(path)
            try:
                with directory(root, names[:-1]) as parent:
                    result[path] = checksum(parent, names[-1])
            except OSError:
                result[path] = None
        return result
    if action not in {"read", "move"} or not isinstance(items, list) or not 1 <= len(items) <= 24:
        raise ValueError("Expected 1-24 items")
    results = {}
    remaining = MAX_BYTES
    for item in items:
        source = item["source"]
        if action == "read":
            results[source], size = read(root, source, remaining)
            remaining -= size
        else:
            try:
                digest = move(root, source, item["target"])
                results[source] = {"moved_to": item["target"], "sha256": digest}
            except (ValueError, OSError) as error:
                results[source] = {"error": str(error)}
                break
    return results


if __name__ == "__main__":
    request = json.loads(sys.stdin.buffer.read(1024 * 1024 + 1))
    fd = os.open("/home/agent/Documents", DIRECTORY)
    try:
        with directory(fd, parts(request["workspace"])) as root:
            print(
                json.dumps(
                    execute(root, request["action"], request.get("items")), ensure_ascii=False
                )
            )
    finally:
        os.close(fd)
