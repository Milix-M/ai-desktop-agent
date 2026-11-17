import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="image audit requires jq")


def audit(tmp_path, failure=""):
    scanner = tmp_path / "trivy"
    scanner.write_text("""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
image=args[-1]
name=image.removeprefix('ai-desktop-agent-').removesuffix('-smoke')
gate='--ignore-unfixed' in args
failure=os.environ['AUDIT_FAILURE'] if name=='backend' else ''
if failure=='scan-error' and not gate: sys.exit(2)
family={'backend':'debian','frontend':'alpine','desktop':'ubuntu','qmp':'ubuntu','android':'ubuntu'}[name]
report={'ArtifactType':'container_image', 'Metadata':{'OS':{'Family':family}},
        'Results':[{'Class':'os-pkgs','Type':family,'Packages':[{'Name':'libc'}]}]}
if not gate:
    report['Results'][0]['Vulnerabilities']=[{'VulnerabilityID':'UNFIXED','FixedVersion':''}]
    if failure=='empty': report['Results'][0]['Packages']=[]
    if failure=='missing': del report['Metadata']['OS']
    if failure=='wrong-os': report['Metadata']['OS']['Family']='unknown'
    if failure=='eol': report['Metadata']['OS']['EOSL']=True
if gate and failure=='fixable':
    report['Results'][0]['Vulnerabilities']=[{'VulnerabilityID':'FIXABLE','FixedVersion':'2'}]
Path(args[args.index('--output')+1]).write_text(json.dumps(report))
sys.exit(1 if gate and failure=='fixable' else 0)
""")
    scanner.chmod(0o755)
    output = tmp_path / "reports"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/audit-images.sh"), str(output)],
        env={
            **os.environ,
            "TRIVY_BIN": str(scanner),
            "AUDIT_FAILURE": failure,
            "BACKEND_SMOKE_IMAGE": "ai-desktop-agent-backend-smoke",
            "FRONTEND_SMOKE_IMAGE": "ai-desktop-agent-frontend-smoke",
            "DESKTOP_SMOKE_IMAGE": "ai-desktop-agent-desktop-smoke",
            "QMP_SMOKE_IMAGE": "ai-desktop-agent-qmp-smoke",
            "ANDROID_SMOKE_IMAGE": "ai-desktop-agent-android-smoke",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result, output


def test_audit_keeps_unfixed_evidence_for_all_production_images(tmp_path):
    result, output = audit(tmp_path)
    assert result.returncode == 0, result.stderr
    for name in ("backend", "frontend", "desktop", "qmp", "android"):
        raw = json.loads((output / f"{name}-all.json").read_text())
        assert raw["Results"][0]["Vulnerabilities"][0]["VulnerabilityID"] == "UNFIXED"
        assert (output / f"{name}-gate.json").is_file()


@pytest.mark.parametrize(
    "failure", ["scan-error", "fixable", "empty", "missing", "wrong-os", "eol"]
)
def test_audit_fails_closed_and_still_checks_the_remaining_images(tmp_path, failure):
    result, output = audit(tmp_path, failure)
    assert result.returncode != 0
    assert "backend:" in result.stderr
    for name in ("frontend", "desktop", "qmp", "android"):
        assert (output / f"{name}-all.json").is_file()
        assert (output / f"{name}-gate.json").is_file()
    if failure == "fixable":
        gate = json.loads((output / "backend-gate.json").read_text())
        assert gate["Results"][0]["Vulnerabilities"][0]["VulnerabilityID"] == "FIXABLE"
    else:
        assert not (output / "backend-gate.json").exists()
