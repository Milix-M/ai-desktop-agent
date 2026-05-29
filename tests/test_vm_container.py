"""VMコンテナ関連ファイルのバリデーションテスト。

Dockerfile / entrypoint.sh / build-vm-image.sh の構文と
必須項目が正しいことを確認する。Dockerビルドは行わない。
"""

import re
import subprocess
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VM_DIR = PROJECT_ROOT / "vm"


def _read_cmdline(path: Path) -> str:
    """コメントを除いた実質的なスクリプト行のみを返す。"""
    lines = path.read_text().splitlines()
    code_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        code_lines.append(stripped)
    return "\n".join(code_lines)


# ── Dockerfile ────────────────────────────────────────


class TestDockerfile:
    """vm/Dockerfile の構文と内容を検証する。"""

    def test_exists(self):
        assert (VM_DIR / "Dockerfile").is_file(), "vm/Dockerfile が存在しません"

    def test_has_from(self):
        content = (VM_DIR / "Dockerfile").read_text()
        assert re.search(r"^FROM\s+", content, re.MULTILINE), "Dockerfile に FROM 命令が必要です"

    def test_single_stage(self):
        """ホスト事前ビルド方式: マルチステージ不要。FROM は1回のみ。"""
        content = (VM_DIR / "Dockerfile").read_text()
        from_count = len(re.findall(r"^FROM\s+", content, re.MULTILINE))
        assert from_count == 1, (
            f"Dockerfile はシングルステージであるべきです（FROM が {from_count} 回あります）"
        )

    def test_has_entrypoint(self):
        content = (VM_DIR / "Dockerfile").read_text()
        assert re.search(r'^ENTRYPOINT\s+\[".*entrypoint\.sh"\]', content, re.MULTILINE), (
            'Dockerfile に ENTRYPOINT [".../entrypoint.sh"] が必要です'
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

    def test_installs_netcat_for_healthcheck(self):
        content = (VM_DIR / "Dockerfile").read_text()
        assert "netcat-openbsd" in content, (
            "Dockerfile で healthcheck 用に netcat-openbsd をインストールする必要があります"
        )

    def test_env_vars_set(self):
        content = (VM_DIR / "Dockerfile").read_text()
        assert "VM_IMAGE=" in content, "Dockerfile に VM_IMAGE 環境変数が必要です"
        assert "CMDLINE_FILE=" in content, "Dockerfile に CMDLINE_FILE 環境変数が必要です"
        assert "VM_VNC_PORT=" in content, "Dockerfile に VM_VNC_PORT 環境変数が必要です"

    def test_copies_entrypoint(self):
        content = (VM_DIR / "Dockerfile").read_text()
        assert re.search(r"^COPY\s+entrypoint\.sh", content, re.MULTILINE), (
            "Dockerfile で entrypoint.sh を COPY する必要があります"
        )

    def test_no_qcow2_copy(self):
        """qcow2 は Docker イメージ内に含めず、docker-compose で volume マウントする。"""
        content = (VM_DIR / "Dockerfile").read_text()
        assert not re.search(r"^COPY\s+.*desktop\.qcow2", content, re.MULTILINE), (
            "Dockerfile で qcow2 を COPY してはいけません（volume マウントを使用）"
        )
        assert not re.search(r"^COPY\s+.*vmlinuz", content, re.MULTILINE), (
            "Dockerfile で vmlinuz を COPY してはいけません（volume マウントを使用）"
        )
        # ENV でのパス指定は OK（ファイルパスのデフォルト値として必要）

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
        content = (VM_DIR / "entrypoint.sh").read_text()
        assert "-kernel" in content, "entrypoint.sh で -kernel を指定する必要があります"
        assert "-initrd" in content, "entrypoint.sh で -initrd を指定する必要があります"

    def test_uses_sparse_bundle_loading(self):
        """-kernel + -initrd で qcow2 のみをマウントする方式を確認。"""
        content = _read_cmdline(VM_DIR / "entrypoint.sh")
        # qcow2 は drive として virtio で接続、format 指定は必須
        assert "format=qcow2" in content, "qcow2 ドライブに format=qcow2 を指定する必要があります"

    def test_serial_stdio_for_debugging(self):
        content = (VM_DIR / "entrypoint.sh").read_text()
        assert "-serial stdio" in content, (
            "デバッグ用に -serial stdio が必要です（docker logs でカーネル出力を確認）"
        )

    def test_uses_display_none(self):
        content = (VM_DIR / "entrypoint.sh").read_text()
        assert "-display none" in content, (
            "QEMU のローカル表示を無効にする -display none が必要です"
        )

    def test_tcg_fallback_for_no_kvm(self):
        content = (VM_DIR / "entrypoint.sh").read_text()
        assert "-enable-kvm" in content, "KVM 有効時のコードパスが必要です"
        assert "qemu64" in content or "TCG" in content, "KVM 無効時の TCG フォールバックが必要です"


# ── build-vm-image.sh ──────────────────────────────────


class TestBuildVmImage:
    """vm/build-vm-image.sh の構文と内容を検証する。"""

    def test_exists(self):
        assert (VM_DIR / "build-vm-image.sh").is_file(), "vm/build-vm-image.sh が存在しません"

    def test_is_executable(self):
        path = VM_DIR / "build-vm-image.sh"
        assert path.stat().st_mode & 0o111, "build-vm-image.sh に実行権限が必要です"

    def test_bash_syntax_valid(self):
        path = VM_DIR / "build-vm-image.sh"
        result = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"build-vm-image.sh に構文エラーがあります:\n{result.stderr}"

    def test_uses_debootstrap(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "debootstrap" in content, "build-vm-image.sh で debootstrap を使う必要があります"

    def test_installs_kde_packages(self):
        content = _read_cmdline(VM_DIR / "build-vm-image.sh")
        assert "plasma-desktop" in content, "KDE plasma-desktop をインストールする必要があります"
        assert "plasma-workspace" in content, (
            "KDE plasma-workspace をインストールする必要があります"
        )
        assert "kwin-x11" in content, "KDE kwin-x11 をインストールする必要があります"
        assert "sddm" in content, "SDDM ディスプレイマネージャをインストールする必要があります"

    def test_installs_firefox(self):
        content = _read_cmdline(VM_DIR / "build-vm-image.sh")
        assert "firefox" in content, (
            "ブラウザ操作エージェント用に firefox をインストールする必要があります"
        )

    def test_installs_automation_tools(self):
        content = _read_cmdline(VM_DIR / "build-vm-image.sh")
        assert "xdotool" in content, "GUI 操作用に xdotool をインストールする必要があります"
        assert "wmctrl" in content, "ウィンドウ制御用に wmctrl をインストールする必要があります"

    def test_configures_sddm_autologin(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "sddm.conf.d" in content, "SDDM 設定ディレクトリを作成する必要があります"
        assert "[Autologin]" in content, "SDDM Autologin セクションが必要です"
        assert "User=agent" in content, "agent ユーザーの自動ログインを設定する必要があります"
        assert "Session=plasma-x11" in content, "Plasma X11 セッションを指定する必要があります"

    def test_creates_agent_user(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "useradd" in content, "agent ユーザーを作成する必要があります"
        assert "NOPASSWD:ALL" in content, "agent にパスワードなし sudo 権限が必要です"

    def test_disables_screen_lock(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "kscreenlockerrc" in content, "kscreenlockerrc を設定する必要があります"
        assert "Autolock=false" in content, "Autolock=false が必要です"
        assert "powermanagementprofilesrc" in content, "powermanagementprofilesrc を設定する必要があります"
        assert "SuspendWhenIdle=false" in content, "SuspendWhenIdle=false が必要です"

    def test_creates_qcow2(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "truncate" in content, "スパースファイル作成に truncate を使う必要があります"
        assert "mkfs.ext4 -F -d" in content, "ext4 を作成する必要があります"
        assert "qemu-img convert" in content, "qcow2 に変換する必要があります"

    def test_outputs_all_required_files(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "desktop.qcow2" in content, "desktop.qcow2 を出力する必要があります"
        assert "vmlinuz" in content, "vmlinuz を出力する必要があります"
        assert "initrd.img" in content, "initrd.img を出力する必要があります"
        assert "cmdline.txt" in content, "cmdline.txt を出力する必要があります"

    def test_sets_fstab(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "/dev/vda" in content, "root デバイス /dev/vda の fstab エントリが必要です"
        assert "ext4" in content, "ext4 ファイルシステムの fstab エントリが必要です"

    def test_cross_arch_support(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "aarch64" in content or "arm64" in content, (
            "ARM64 ホストからのクロスアーキテクチャビルドに対応する必要があります"
        )
        assert "qemu-user-static" in content, (
            "ARM64→AMD64 用に qemu-user-static を参照する必要があります"
        )

    def test_cleanup_on_exit(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "trap" in content, "ビルド失敗時に後片付けする trap が必要です"


# ── docker-compose.yml ────────────────────────────────


class TestDockerCompose:
    """docker-compose.yml の検証。"""

    @staticmethod
    def _compose_data():
        path = PROJECT_ROOT / "docker-compose.yml"
        return yaml.safe_load(path.read_text())

    @staticmethod
    def _vm_service():
        data = TestDockerCompose._compose_data()
        return data["services"]["vm"]

    def test_exists(self):
        assert (PROJECT_ROOT / "docker-compose.yml").is_file(), "docker-compose.yml が存在しません"

    def test_is_valid_yaml(self):
        data = self._compose_data()
        assert isinstance(data, dict), "docker-compose.yml はYAMLマッピングである必要があります"

    def test_has_all_services(self):
        data = self._compose_data()
        services = data.get("services", {})
        for name in ("vm", "backend", "frontend", "websockify"):
            assert name in services, f"docker-compose.yml に {name} サービスが必要です"

    def test_vm_uses_tcg_by_default(self):
        content = (PROJECT_ROOT / "docker-compose.yml").read_text()
        assert "USE_KVM=${USE_KVM:-false}" in content, (
            "USE_KVM のデフォルトは false（TCG エミュレーション）であるべき"
        )

    def test_vm_exposes_vnc_port(self):
        vm = self._vm_service()
        ports = vm.get("ports", [])
        assert "5900:5900" in ports, "vm サービスは 5900 ポートを公開する必要があります"

    def test_backend_depends_on_vm_healthy(self):
        data = self._compose_data()
        backend = data["services"]["backend"]
        depends_on = backend.get("depends_on", {})
        assert "vm" in depends_on, "backend が vm に依存する必要があります"
        assert depends_on["vm"].get("condition") == "service_healthy", (
            "backend は vm の healthcheck 通過を待つ必要があります"
        )

    def test_vm_volume_mounts(self):
        vm = self._vm_service()
        volumes = vm.get("volumes", [])
        assert len(volumes) == 4, f"vm には4つの volume マウントが必要です（実際: {len(volumes)}）"

        # 文字列のリストか、長形式かを正規化
        vol_specs = {}
        for v in volumes:
            if isinstance(v, str):
                parts = v.split(":")
                src = parts[0]
                ro = len(parts) >= 3 and parts[-1] == "ro"
            else:
                src = v.get("source", "")
                ro = v.get("read_only", False)
            vol_specs[Path(src).name] = ro

        # qcow2 は書き込み可能（rw）
        assert "desktop.qcow2" in vol_specs, "desktop.qcow2 の volume マウントが必要です"
        assert vol_specs["desktop.qcow2"] is False, (
            "desktop.qcow2 は rw でマウントする必要があります"
        )

        # kernel / initrd / cmdline は読み取り専用
        for fname in ("vmlinuz", "initrd.img", "cmdline.txt"):
            assert fname in vol_specs, f"{fname} の volume マウントが必要です"
            assert vol_specs[fname] is True, f"{fname} は ro でマウントする必要があります"

    def test_vm_environment_variables(self):
        vm = self._vm_service()
        env = vm.get("environment", [])
        env_dict = {}
        for e in env:
            if "=" in e:
                k, v = e.split("=", 1)
                env_dict[k] = v

        assert "VM_IMAGE" in env_dict, "VM_IMAGE 環境変数が必要です"
        assert env_dict["VM_IMAGE"] == "/vm/desktop.qcow2"
        assert "CMDLINE_FILE" in env_dict, "CMDLINE_FILE 環境変数が必要です"
        assert env_dict["CMDLINE_FILE"] == "/vm/cmdline.txt"

    def test_vm_healthcheck_configured(self):
        vm = self._vm_service()
        hc = vm.get("healthcheck", {})
        assert hc, "vm サービスに healthcheck 設定が必要です"
        assert "test" in hc, "healthcheck に test が必要です"
        assert hc.get("retries", 0) >= 5, "healthcheck の retries は最低5回必要です"

    def test_websockify_command(self):
        data = self._compose_data()
        ws = data["services"]["websockify"]
        cmd = ws.get("command", "")
        assert "6080" in cmd, "websockify はポート 6080 を使用する必要があります"
        assert "vm:5900" in cmd, "websockify は vm:5900 に接続する必要があります"


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
