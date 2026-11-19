"""Opt-in measurement of Python running in the real backend Docker image.

The image and its command are unchanged. Only backend-labelled test containers
receive read-only instrumentation and a separate output mount. SIGKILL remains
SIGKILL: a process that cannot save its data contributes no coverage.
"""

import hashlib
import json
import platform
import shutil
import sys
from pathlib import Path

from coverage import Coverage, CoverageData

INSTRUMENTATION = "/opt/agent-test-coverage"
OUTPUT = "/agent-test-coverage"
PACKAGE = "/app/src/ai_desktop_agent/"
ROOT = Path(__file__).resolve().parents[1]

IDENTITY = """import hashlib,json,platform,sys
from pathlib import Path
root=Path('/app/src/ai_desktop_agent')
files={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
       for p in sorted(root.rglob('*.py'))}
print(json.dumps(dict(files=files,cache_tag=sys.implementation.cache_tag,
                     machine=platform.machine())))
"""


def source_hashes():
    root = ROOT / "src/ai_desktop_agent"
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.py"))
    }


class ContainerCoverage:
    def __init__(self, client, image, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.raw = self.directory / "raw"
        self.raw.mkdir(mode=0o777)
        self.raw.chmod(0o777)  # Some real permission probes run as uid 65534.
        self.instrumentation = self.directory / "instrumentation"
        self.instrumentation.mkdir()
        import coverage

        shutil.copytree(Path(coverage.__file__).parent, self.instrumentation / "coverage")
        (self.instrumentation / "sitecustomize.py").write_text(
            "import coverage\ncoverage.process_startup()\n"
        )
        (self.instrumentation / "coverage.ini").write_text(
            "[run]\nbranch = true\nparallel = true\n"
            f"source = {PACKAGE.rstrip('/')}\ndata_file = {OUTPUT}/.coverage\n"
        )
        details = client.images.get(image)
        self.images = {image, details.id, details.id.removeprefix("sha256:")}
        image_environment = dict(
            item.split("=", 1) for item in details.attrs["Config"].get("Env", [])
        )
        self.pythonpath = image_environment.get("PYTHONPATH", "")
        probe = client.containers.run(
            details.id,
            entrypoint=["/app/.venv/bin/python", "-c"],
            command=[IDENTITY],
            network_mode="none",
            detach=True,
            cap_drop=["ALL"],
        )
        try:
            assert probe.wait(timeout=30)["StatusCode"] == 0, probe.logs().decode()
            self.identity = json.loads(probe.logs())
        finally:
            probe.remove(force=True)
        assert self.identity["files"] == source_hashes(), "backend image source differs"
        assert self.identity["cache_tag"] == sys.implementation.cache_tag, "Python ABI differs"
        assert self.identity["machine"] == platform.machine(), "container architecture differs"
        self.identity.update(image=details.id, coverage_version=coverage.__version__)
        (self.directory / "identity.json").write_text(json.dumps(self.identity, indent=2))

    def options(self, image, options):
        labels = options.get("labels") or {}
        if image not in self.images or labels.get("com.docker.compose.service") != "backend":
            return options
        options = dict(options)
        environment = dict(options.get("environment") or {})
        pythonpath = environment.get("PYTHONPATH", self.pythonpath)
        environment.update(
            PYTHONPATH=INSTRUMENTATION + (":" + pythonpath if pythonpath else ""),
            COVERAGE_PROCESS_START=INSTRUMENTATION + "/coverage.ini",
        )
        volumes = dict(options.get("volumes") or {})
        assert not any(value["bind"] in {INSTRUMENTATION, OUTPUT} for value in volumes.values()), (
            "test coverage mount conflicts with an existing mount"
        )
        volumes.update(
            {
                str(self.instrumentation): {"bind": INSTRUMENTATION, "mode": "ro"},
                str(self.raw): {"bind": OUTPUT, "mode": "rw"},
            }
        )
        options.update(environment=environment, volumes=volumes)
        return options

    def finish(self):
        assert self.identity["files"] == source_hashes(), "source changed while tests ran"
        combined = CoverageData(basename=str(self.directory / "container.coverage"))
        combined.erase()
        raw_files = sorted(self.raw.glob(".coverage.*"))
        assert raw_files, "no container process saved coverage data"
        measured = set()
        for path in raw_files:
            data = CoverageData(basename=str(path))
            data.read()
            arcs = {}
            for filename in data.measured_files():
                assert filename.startswith(PACKAGE), filename
                relative = filename.removeprefix(PACKAGE)
                assert relative in self.identity["files"], filename
                destination = str(ROOT / "src/ai_desktop_agent" / relative)
                arcs[destination] = data.arcs(filename) or []
                measured.add(relative)
            combined.add_arcs(arcs)
        # Loading data for reporting does not discover unimported modules.
        # Empty entries retain them in the denominator without inventing arcs.
        combined.touch_files(
            [str(ROOT / "src/ai_desktop_agent" / relative) for relative in self.identity["files"]]
        )
        combined.write()
        report = Coverage(
            config_file=str(ROOT / "pyproject.toml"),
            data_file=str(self.directory / "container.coverage"),
        )
        report.load()
        report_path = self.directory / "container-coverage.json"
        report.json_report(outfile=str(report_path))
        reported = json.loads(report_path.read_text())["files"]
        assert set(reported) == {
            "src/ai_desktop_agent/" + relative for relative in self.identity["files"]
        }, "unimported source files were omitted from the coverage report"
        (self.directory / "collection.json").write_text(
            json.dumps(
                {"saved_processes": len(raw_files), "measured_files": sorted(measured)}, indent=2
            )
        )
