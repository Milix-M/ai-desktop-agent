import subprocess

import pytest

from scripts.ci_scope import changed_paths, check_results, needs_full_ci


@pytest.mark.parametrize(
    "paths",
    [
        ["README.md"],
        ["docs/testing.md", "docs/research/clef.md", "android/README.md"],
    ],
)
def test_only_known_documentation_can_skip_expensive_checks(paths):
    assert not needs_full_ci(paths)


@pytest.mark.parametrize(
    "path",
    [
        "src/ai_desktop_agent/server/app.py",
        "frontend/src/app/page.tsx",
        "tests/test_server.py",
        "pyproject.toml",
        "uv.lock",
        "Dockerfile",
        "docker-compose.yml",
        ".github/workflows/ci.yml",
        "scripts/ci_scope.py",
        "docs/example.py",
        "docs/fixtures.json",
        "new-directory/README.md",
    ],
)
def test_code_and_unknown_paths_run_every_check(path):
    assert needs_full_ci(["docs/testing.md", path])


def test_empty_or_initial_diff_runs_every_check():
    assert needs_full_ci([])
    assert needs_full_ci(changed_paths("0" * 40))


def test_failed_diff_is_not_treated_as_documentation():
    with pytest.raises(subprocess.CalledProcessError):
        changed_paths("missing-ci-base-commit")


def test_diff_includes_both_sides_of_rename_and_handles_newlines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    source = tmp_path / "runtime.py"
    source.write_text("print('runtime')\n")
    subprocess.run(["git", "add", "."], check=True)
    # Write a tree without creating any commits or changing the repository under test.
    base = subprocess.check_output(["git", "write-tree"], text=True).strip()
    (tmp_path / "docs").mkdir()
    source.rename(tmp_path / "docs" / "example.md")
    (tmp_path / "docs" / "with\nnewline.md").write_text("documentation\n")
    subprocess.run(["git", "add", "-A"], check=True)
    head = subprocess.check_output(["git", "write-tree"], text=True).strip()
    paths = changed_paths(base, head)
    assert set(paths) == {"runtime.py", "docs/example.md", "docs/with\nnewline.md"}
    assert needs_full_ci(paths)


def successful_needs(full):
    needs = {
        job: {"result": "success"}
        for job in ("changes", "lint", "format", "dependencies", "frontend", "test", "desktop")
    }
    needs["changes"]["outputs"] = {"full": str(full).lower()}
    if not full:
        needs["test"]["result"] = needs["desktop"]["result"] = "skipped"
    return needs


@pytest.mark.parametrize("full", [True, False])
def test_accepts_successful_full_and_documentation_runs(full):
    assert check_results(successful_needs(full)) == []


@pytest.mark.parametrize("full", [True, False])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None])
@pytest.mark.parametrize("job", ["changes", "lint", "format", "dependencies", "frontend"])
def test_required_checks_cannot_be_skipped_or_fail(full, result, job):
    needs = successful_needs(full)
    needs[job]["result"] = result
    assert check_results(needs)


@pytest.mark.parametrize("job", ["test", "desktop"])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None])
def test_full_runs_require_expensive_checks_to_succeed(job, result):
    needs = successful_needs(True)
    needs[job]["result"] = result
    assert check_results(needs)


@pytest.mark.parametrize("full", [None, "", "unknown"])
def test_selection_must_be_explicit(full):
    needs = successful_needs(False)
    needs["changes"]["outputs"]["full"] = full
    assert check_results(needs)


@pytest.mark.parametrize("job", ["test", "desktop"])
@pytest.mark.parametrize("result", ["failure", "cancelled", None])
def test_documentation_runs_cannot_hide_failed_expensive_checks(job, result):
    needs = successful_needs(False)
    needs[job]["result"] = result
    assert check_results(needs)
