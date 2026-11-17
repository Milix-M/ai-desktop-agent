"""Select documentation-only CI runs and reject unexpected skipped checks."""

import argparse
import json
import os
import subprocess
from pathlib import Path


def needs_full_ci(paths: list[str]) -> bool:
    # Unknown paths (including new scripts/assets under docs/) run every check.
    return not paths or any(
        path not in {"README.md", "android/README.md"}
        and not (path.startswith("docs/") and path.endswith(".md"))
        for path in paths
    )


def changed_paths(base: str, head: str = "HEAD") -> list[str]:
    if not base or set(base) == {"0"}:
        return []
    # Include both sides of renames, so moving code into docs cannot skip tests.
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", base, head, "--"],
        check=True,
        capture_output=True,
    )
    return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]


def check_results(needs: dict) -> list[str]:
    errors = []
    changes = needs.get("changes", {})
    full = changes.get("outputs", {}).get("full")
    if full not in {"true", "false"}:
        errors.append("Missing or invalid CI selection")
    for job in ("changes", "lint", "format", "dependencies", "frontend", "test", "desktop"):
        result = needs.get(job, {}).get("result")
        expected = "skipped" if job in {"test", "desktop"} and full == "false" else "success"
        if result != expected:
            errors.append(f"{job}: expected {expected}, got {result}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select")
    select.add_argument("base")
    commands.add_parser("verify")
    args = parser.parse_args()
    if args.command == "select":
        full = needs_full_ci(changed_paths(args.base))
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"full={str(full).lower()}\n")
        print("Run all checks" if full else "Documentation only; retain lint and dependency audits")
    else:
        errors = check_results(json.loads(os.environ["CI_NEEDS"]))
        if errors:
            raise SystemExit("\n".join(errors))
        print("All required checks passed")


if __name__ == "__main__":
    main()
