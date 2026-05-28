"""VMコンテナ関連ファイルのバリデーションテスト。

Dockerfile / entrypoint.sh / cloud-init ファイルの構文と
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

    def test_installs_cloud_image_utils(self):
        content = (VM_DIR / "Dockerfile").read_text()
        assert "cloud-image-utils" in content, "cloud-init seed 作成に cloud-image-utils が必要です"

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

    def test_first_boot_handling(self):
        """初回起動時のイメージダウンロードロジックがあること。"""
        content = (VM_DIR / "entrypoint.sh").read_text()
        assert (
            'if [ ! -f "$VM_IMAGE" ]' in content
            or 'if [ ! -f "${VM_IMAGE}" ]' in content
            or "if [ ! -f" in content
        ), "初回起動時の VM イメージ確認ロジックが必要です"


# ── cloud-init ────────────────────────────────────────


class TestCloudInit:
    """cloud-init ファイルの検証。"""

    def test_user_data_exists(self):
        assert (VM_DIR / "cloud-init" / "user-data").is_file(), (
            "vm/cloud-init/user-data が存在しません"
        )

    def test_meta_data_exists(self):
        assert (VM_DIR / "cloud-init" / "meta-data").is_file(), (
            "vm/cloud-init/meta-data が存在しません"
        )

    def test_user_data_is_valid_yaml(self):
        path = VM_DIR / "cloud-init" / "user-data"
        content = path.read_text()
        data = yaml.safe_load(content)
        assert isinstance(data, dict), "user-data はYAMLマッピングである必要があります"

    def test_user_data_has_cloud_config_header(self):
        content = (VM_DIR / "cloud-init" / "user-data").read_text()
        assert content.startswith("#cloud-config"), (
            "user-data は #cloud-config で始まる必要があります"
        )

    def test_user_data_has_packages(self):
        path = VM_DIR / "cloud-init" / "user-data"
        data = yaml.safe_load(path.read_text())
        assert "packages" in data, "user-data に packages キーが必要です"

    def test_user_data_has_users(self):
        path = VM_DIR / "cloud-init" / "user-data"
        data = yaml.safe_load(path.read_text())
        assert "users" in data, "user-data に users キーが必要です"

    def test_user_data_installs_kde_plasma_desktop(self):
        path = VM_DIR / "cloud-init" / "user-data"
        data = yaml.safe_load(path.read_text())
        packages = data.get("packages", [])
        assert "kde-plasma-desktop" in packages, "KDE Plasma デスクトップが必要です"

    def test_user_data_installs_sddm(self):
        path = VM_DIR / "cloud-init" / "user-data"
        data = yaml.safe_load(path.read_text())
        packages = data.get("packages", [])
        assert "sddm" in packages, "KDE用ディスプレイマネージャ sddm が必要です"

    def test_user_data_installs_automation_tools(self):
        path = VM_DIR / "cloud-init" / "user-data"
        data = yaml.safe_load(path.read_text())
        packages = data.get("packages", [])
        assert "xdotool" in packages, "ウィンドウ操作用に xdotool が必要です"
        assert "wmctrl" in packages, "ウィンドウ情報取得に wmctrl が必要です"

    def test_user_data_creates_agent_user(self):
        path = VM_DIR / "cloud-init" / "user-data"
        data = yaml.safe_load(path.read_text())
        users = data.get("users", [])
        user_names = [u.get("name") for u in users if isinstance(u, dict)]
        assert "agent" in user_names, "エージェント用ユーザー 'agent' が必要です"

    def test_user_data_configures_sddm_autologin(self):
        content = (VM_DIR / "cloud-init" / "user-data").read_text()
        assert "User=agent" in content, "SDDM の自動ログイン設定 (User=agent) が必要です"
        assert "Session=plasma" in content, "Plasma セッション設定が必要です"

    def test_meta_data_has_instance_id(self):
        path = VM_DIR / "cloud-init" / "meta-data"
        data = yaml.safe_load(path.read_text())
        assert "instance-id" in data, "meta-data に instance-id が必要です"

    def test_meta_data_has_hostname(self):
        path = VM_DIR / "cloud-init" / "meta-data"
        data = yaml.safe_load(path.read_text())
        assert "local-hostname" in data, "meta-data に local-hostname が必要です"


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

    def test_vm_has_kvm_device(self):
        content = (PROJECT_ROOT / "docker-compose.yml").read_text()
        assert "/dev/kvm:/dev/kvm" in content, "vm サービスに /dev/kvm デバイスマウントが必要です"

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
        assert "EXPOSE 8080" in content, "Dockerfile に EXPOSE 8080 が必要です"
