"""VMコンテナ関連ファイルのバリデーションテスト。

Dockerfile / entrypoint.sh / prepare-image.sh の構文と
必須項目が正しいことを確認する。Dockerビルドは行わない。
"""

import re
import subprocess
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VM_DIR = PROJECT_ROOT / "vm"


# ── Dockerfile ────────────────────────────────────────


class TestDockerfile:
    """vm/Dockerfile の構文と内容を検証する。"""

    def test_exists(self):
        assert (VM_DIR / "Dockerfile").is_file(), "vm/Dockerfile が存在しません"

    def test_has_from(self):
        content = (VM_DIR / "Dockerfile").read_text()
        assert re.search(r"^FROM\s+", content, re.MULTILINE), "Dockerfile に FROM 命令が必要です"

    def test_has_entrypoint(self):
        content = (VM_DIR / "Dockerfile").read_text()
        assert re.search(r"^ENTRYPOINT\s", content, re.MULTILINE), (
            "Dockerfile に ENTRYPOINT 命令が必要です"
        )

    def test_has_expose(self):
        content = (VM_DIR / "Dockerfile").read_text()
        assert re.search(r"^EXPOSE\s+5900", content, re.MULTILINE), (
            "Dockerfile に EXPOSE 5900 が必要です"
        )

    def test_installs_qemu(self):
        content = (VM_DIR / "Dockerfile").read_text()
        assert "qemu-system-x86" in content, (
            "Dockerfile で qemu-system-x86 をインストールする必要があります"
        )

    def test_has_builder_stage(self):
        """プリビルド方式: マルチステージビルドで builder ステージがあること。"""
        content = (VM_DIR / "Dockerfile").read_text()
        assert "AS builder" in content, (
            "Dockerfile にマルチステージビルドの builder ステージが必要です"
        )

    def test_healthcheck_present(self):
        content = (VM_DIR / "Dockerfile").read_text()
        assert "HEALTHCHECK" in content, "Dockerfile に HEALTHCHECK が必要です"


# ── entrypoint.sh ─────────────────────────────────────


class TestEntrypoint:
    """vm/entrypoint.sh の構文と内容を検証する。"""

    def test_exists(self):
        assert (VM_DIR / "entrypoint.sh").is_file(), "vm/entrypoint.sh が存在しません"

    def test_is_executable(self):
        path = VM_DIR / "entrypoint.sh"
        assert path.stat().st_mode & 0o111, "entrypoint.sh に実行権限が必要です"

    def test_bash_syntax_valid(self):
        """bash -n で構文エラーがないことを確認。"""
        path = VM_DIR / "entrypoint.sh"
        result = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"entrypoint.sh に構文エラーがあります:\n{result.stderr}"

    def test_uses_qemu_system_x86_64(self):
        content = (VM_DIR / "entrypoint.sh").read_text()
        assert "qemu-system-x86_64" in content, (
            "entrypoint.sh で qemu-system-x86_64 を呼び出す必要があります"
        )

    def test_passes_enable_kvm(self):
        content = (VM_DIR / "entrypoint.sh").read_text()
        assert "-enable-kvm" in content, "entrypoint.sh で -enable-kvm を指定する必要があります"

    def test_vnc_flag_present(self):
        content = (VM_DIR / "entrypoint.sh").read_text()
        assert "-vnc" in content, "entrypoint.sh で -vnc を指定する必要があります"

    def test_uses_kernel_direct_boot(self):
        """プリビルド方式: -kernel と -initrd でダイレクトブートすること。"""
        content = (VM_DIR / "entrypoint.sh").read_text()
        assert "-kernel" in content, "entrypoint.sh で -kernel を指定する必要があります"
        assert "-initrd" in content, "entrypoint.sh で -initrd を指定する必要があります"


# ── prepare-image.sh ──────────────────────────────────


class TestPrepareImage:
    """vm/prepare-image.sh の構文と内容を検証する。"""

    def test_exists(self):
        assert (VM_DIR / "prepare-image.sh").is_file(), "vm/prepare-image.sh が存在しません"

    def test_is_executable(self):
        path = VM_DIR / "prepare-image.sh"
        assert path.stat().st_mode & 0o111, "prepare-image.sh に実行権限が必要です"

    def test_bash_syntax_valid(self):
        path = VM_DIR / "prepare-image.sh"
        result = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"prepare-image.sh に構文エラーがあります:\n{result.stderr}"

    def test_uses_debootstrap(self):
        content = (VM_DIR / "prepare-image.sh").read_text()
        assert "debootstrap" in content, (
            "prepare-image.sh で debootstrap を使う必要があります"
        )

    def test_installs_kde(self):
        content = (VM_DIR / "prepare-image.sh").read_text()
        assert "kde-plasma-desktop" in content, (
            "prepare-image.sh で KDE Plasma をインストールする必要があります"
        )

    def test_configures_getty_autologin(self):
        content = (VM_DIR / "prepare-image.sh").read_text()
        assert "agetty --autologin agent" in content, (
            "prepare-image.sh で agent の getty 自動ログインを設定する必要があります"
        )

    def test_creates_qcow2(self):
        content = (VM_DIR / "prepare-image.sh").read_text()
        assert "qemu-img convert" in content, (
            "prepare-image.sh で qemu-img convert を実行する必要があります"
        )


# ── docker-compose.yml ────────────────────────────────


class TestDockerCompose:
    """docker-compose.yml の検証。"""

    def test_exists(self):
        assert (PROJECT_ROOT / "docker-compose.yml").is_file(), "docker-compose.yml が存在しません"

    def test_is_valid_yaml(self):
        path = PROJECT_ROOT / "docker-compose.yml"
        data = yaml.safe_load(path.read_text())
        assert isinstance(data, dict), "docker-compose.yml はYAMLマッピングである必要があります"

    def test_has_vm_service(self):
        path = PROJECT_ROOT / "docker-compose.yml"
        data = yaml.safe_load(path.read_text())
        services = data.get("services", {})
        assert "vm" in services, "docker-compose.yml に vm サービスが必要です"

    def test_has_backend_service(self):
        path = PROJECT_ROOT / "docker-compose.yml"
        data = yaml.safe_load(path.read_text())
        services = data.get("services", {})
        assert "backend" in services, "docker-compose.yml に backend サービスが必要です"

    def test_has_websockify_service(self):
        path = PROJECT_ROOT / "docker-compose.yml"
        data = yaml.safe_load(path.read_text())
        services = data.get("services", {})
        assert "websockify" in services, "docker-compose.yml に websockify サービスが必要です"

    def test_vm_uses_tcg_by_default(self):
        """KVM がない環境向けに、デフォルトは TCG（ソフトウェアエミュレーション）。"""
        content = (PROJECT_ROOT / "docker-compose.yml").read_text()
        assert "USE_KVM" in content, "vm サービスに USE_KVM 環境変数が必要です"
        has_tcg_default = "USE_KVM=${USE_KVM:-false}" in content
        assert has_tcg_default, "USE_KVM のデフォルトは TCG (false) であるべき"

    def test_vm_exposes_vnc_port(self):
        content = (PROJECT_ROOT / "docker-compose.yml").read_text()
        assert '"5900:5900"' in content or "'5900:5900'" in content, (
            "vm サービスは 5900 ポートを公開する必要があります"
        )

    def test_backend_depends_on_vm(self):
        content = (PROJECT_ROOT / "docker-compose.yml").read_text()
        assert "vm:" in content, "backend サービスが vm に依存する設定が必要です"


# ── バックエンド Dockerfile ───────────────────────────


class TestBackendDockerfile:
    """ルート Dockerfile（バックエンド用）の検証。"""

    def test_exists(self):
        assert (PROJECT_ROOT / "Dockerfile").is_file(), "Dockerfile（バックエンド用）が存在しません"

    def test_has_from(self):
        content = (PROJECT_ROOT / "Dockerfile").read_text()
        assert re.search(r"^FROM\s+", content, re.MULTILINE), "Dockerfile に FROM 命令が必要です"

    def test_installs_dependencies(self):
        content = (PROJECT_ROOT / "Dockerfile").read_text()
        assert "uv sync" in content, "Dockerfile で uv sync による依存インストールが必要です"

    def test_exposes_backend_port(self):
        content = (PROJECT_ROOT / "Dockerfile").read_text()
        assert "EXPOSE 8081" in content, "Dockerfile に EXPOSE 8081 が必要です"
