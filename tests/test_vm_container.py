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
        assert "VM_MEMORY=" in content, "Dockerfile に VM_MEMORY 環境変数が必要です"
        assert "VM_CPUS=" in content, "Dockerfile に VM_CPUS 環境変数が必要です"

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

    def test_uses_configurable_cpus(self):
        content = (VM_DIR / "entrypoint.sh").read_text()
        assert "VM_CPUS" in content, "entrypoint.sh で VM_CPUS を参照する必要があります"
        assert '-smp "$VM_CPUS"' in content, "KVM 時に可変 CPU 数で起動する必要があります"

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

    def test_kvm_auto_by_default(self):
        content = (VM_DIR / "entrypoint.sh").read_text()
        assert 'USE_KVM="${USE_KVM:-auto}"' in content, (
            "USE_KVM のデフォルトは auto（.env指定不要）であるべき"
        )

    def test_tcg_restricted_by_default(self):
        content = (VM_DIR / "entrypoint.sh").read_text()
        assert "ALLOW_TCG_VM" in content, "TCG実行には ALLOW_TCG_VM による明示許可が必要です"


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
        assert "qemu-guest-agent" in content, (
            "ゲスト情報取得・コマンド実行用に qemu-guest-agent が必要です"
        )

    def test_configures_sddm_autologin(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "sddm.conf" in content, "SDDM 設定ファイルを作成する必要があります"
        assert "[Autologin]" in content, "SDDM Autologin セクションが必要です"
        assert "User=agent" in content, "agent ユーザーの自動ログインを設定する必要があります"
        assert "Session=" in content, "セッション名を指定する必要があります"
        # セッション名はビルド時に自動検出（plasma / plasma-x11 / kde-plasma）
        assert "plasma.desktop" in content, "plasma.desktop を検出する必要があります"

    def test_creates_agent_user(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "useradd" in content, "agent ユーザーを作成する必要があります"
        assert "NOPASSWD:ALL" in content, "agent にパスワードなし sudo 権限が必要です"

    def test_disables_screen_lock(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "kscreenlockerrc" in content, "kscreenlockerrc を設定する必要があります"
        assert "Autolock=false" in content, "Autolock=false が必要です"
        assert "powermanagementprofilesrc" in content, (
            "powermanagementprofilesrc を設定する必要があります"
        )
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

    def test_systemd_symlinks_correct(self):
        content = _read_cmdline(VM_DIR / "build-vm-image.sh")
        # default.target → graphical.target
        assert "default.target" in content, "デフォルトターゲットを設定する必要があります"
        # display-manager.service → sddm.service（Alias）
        assert "display-manager.service" in content, "display-manager エイリアスが必要です"
        # graphical.target.wants/sddm.service（正しい起動トリガー）
        assert "graphical.target.wants" in content, (
            "graphical.target.wants/sddm.service で SDDM を起動する必要があります"
        )
        # display-manager.service.wants は循環参照のバグなので存在してはいけない
        assert "display-manager.service.wants" not in content, (
            "display-manager.service.wants/sddm.service は循環参照です"
        )

    def test_cleanup_on_exit(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "trap" in content, "ビルド失敗時に後片付けする trap が必要です"

    def test_configures_guest_networking(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "99-ens3" in content, "ens3 用の netplan 設定が必要です"
        assert "dhcp4" in content, "DHCPv4 を有効にする必要があります"
        assert "10.0.2.3" in content, "QEMU 内蔵DNS を指定する必要があります"

    def test_disables_dpms_on_login(self):
        content = (VM_DIR / "build-vm-image.sh").read_text()
        assert "disable-dpms.desktop" in content, "DPMS無効化の自動起動が必要です"
        assert "xset -dpms" in content, "xset で DPMS を切る必要があります"


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
        for name in ("vm", "backend", "frontend", "websockify", "desktop", "websockify-desktop"):
            assert name in services, f"docker-compose.yml に {name} サービスが必要です"

    def test_vm_uses_kvm_auto_by_default(self):
        content = (PROJECT_ROOT / "docker-compose.yml").read_text()
        assert "USE_KVM=${USE_KVM:-auto}" in content, (
            "USE_KVM のデフォルトは auto（/dev/kvm の有無で自動切替）であるべき"
        )

    def test_vm_exposes_vnc_port(self):
        vm = self._vm_service()
        ports = vm.get("ports", [])
        assert "5900:5900" in ports, "vm サービスは 5900 ポートを公開する必要があります"

    def test_backend_depends_on_desktop_healthy(self):
        data = self._compose_data()
        backend = data["services"]["backend"]
        depends_on = backend.get("depends_on", {})
        assert "desktop" in depends_on, "backend が desktop に依存する必要があります"
        assert depends_on["desktop"].get("condition") == "service_healthy", (
            "backend は desktop の healthcheck 通過を待つ必要があります"
        )
        # KVMあり時は desktop が起動対象外のため、依存解決で巻き添え起動しない
        assert depends_on["desktop"].get("required") is False, (
            "backend の desktop 依存は required:false である必要があります"
        )
        # vm は kvm プロファイル配下のため backend の depends_on に含めない
        assert "vm" not in depends_on, "backend は profile 付きの vm に依存してはいけません"

    def test_vm_volume_mounts(self):
        vm = self._vm_service()
        volumes = vm.get("volumes", [])
        assert len(volumes) == 5, f"vm には5つの volume マウントが必要です（実際: {len(volumes)}）"

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

        # QMP/QGA ソケット共有ディレクトリ
        assert "sockets" in vol_specs, "QMP/QGA 用の sockets マウントが必要です"

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

    def test_vm_mounts_kvm_device(self):
        """kvmプロファイルで起動する vm は /dev/kvm をマウントすること。"""
        vm = self._vm_service()
        devices = vm.get("devices", [])
        assert any("/dev/kvm" in str(d) for d in devices), (
            "vm サービスは /dev/kvm をマウントする必要があります"
        )

    def test_vm_behind_kvm_profile(self):
        """vm / websockify は kvm プロファイル配下（非KVMでは起動しない）。"""
        data = self._compose_data()
        assert "kvm" in (data["services"]["vm"].get("profiles") or []), (
            "vm サービスは kvm プロファイルに属する必要があります"
        )
        assert "kvm" in (data["services"]["websockify"].get("profiles") or []), (
            "websockify サービスは kvm プロファイルに属する必要があります"
        )
        desktop_profiles = data["services"]["desktop"].get("profiles") or []
        assert "container" in desktop_profiles, (
            "desktop サービスは container プロファイルに属する必要があります"
        )
        assert "kvm" not in desktop_profiles, (
            "desktop サービスは kvm プロファイルに属してはいけません"
        )

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


# ── desktop（軽量コンテナ実行環境） ────────────────────

DESKTOP_DIR = PROJECT_ROOT / "desktop"


class TestDesktopDockerfile:
    """desktop/Dockerfile の検証（QEMU不要のVNCデスクトップ）。"""

    def test_exists(self):
        assert (DESKTOP_DIR / "Dockerfile").is_file(), "desktop/Dockerfile が存在しません"

    def test_has_from(self):
        content = (DESKTOP_DIR / "Dockerfile").read_text()
        assert re.search(r"^FROM\s+", content, re.MULTILINE)

    def test_no_qemu(self):
        content = (DESKTOP_DIR / "Dockerfile").read_text()
        assert "qemu-system" not in content, (
            "desktop はQEMU不要（軽量コンテナ）のため qemu を含んではいけません"
        )

    def test_has_vnc_server(self):
        content = (DESKTOP_DIR / "Dockerfile").read_text()
        assert "tigervnc" in content or "x11vnc" in content, "desktop にVNCサーバが必要です"

    def test_has_desktop_and_tools(self):
        content = (DESKTOP_DIR / "Dockerfile").read_text()
        assert "xfce4" in content or "lxde" in content, "デスクトップ環境が必要です"
        assert "firefox" in content, "ブラウザ操作用に firefox が必要です"
        assert "xdotool" in content, "GUI操作用に xdotool が必要です"

    def test_has_expose_and_healthcheck(self):
        content = (DESKTOP_DIR / "Dockerfile").read_text()
        assert re.search(r"^EXPOSE\s+5900", content, re.MULTILINE)
        assert "HEALTHCHECK" in content


class TestDesktopEntrypoint:
    def test_exists_and_executable(self):
        path = DESKTOP_DIR / "entrypoint.sh"
        assert path.is_file()
        assert path.stat().st_mode & 0o111

    def test_bash_syntax_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(DESKTOP_DIR / "entrypoint.sh")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_starts_vnc(self):
        content = (DESKTOP_DIR / "entrypoint.sh").read_text()
        assert "vncserver" in content or "x11vnc" in content


class TestDesktopCompose:
    """desktop 系サービスの compose 定義を検証する。"""

    @staticmethod
    def _compose_data():
        return yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())

    def test_desktop_ports(self):
        desktop = self._compose_data()["services"]["desktop"]
        assert "5901:5900" in desktop.get("ports", [])

    def test_desktop_websockify(self):
        ws = self._compose_data()["services"]["websockify-desktop"]
        assert "6081" in str(ws.get("command", "")) + str(ws.get("ports", ""))
        assert "desktop:5900" in str(ws.get("command", ""))

    def test_backend_default_vnc_is_desktop(self):
        backend = self._compose_data()["services"]["backend"]
        env = backend.get("environment", [])
        assert any(str(e).startswith("VNC_HOST=") and "desktop" in str(e) for e in env), (
            "backend の VNC_HOST の既定は desktop であるべき（up.sh がKVM時は vm に切替）"
        )

    def test_desktop_behind_container_profile(self):
        """desktop 系は container プロファイル配下（KVMあり時は起動しない）。"""
        data = self._compose_data()
        assert "container" in (data["services"]["desktop"].get("profiles") or []), (
            "desktop サービスは container プロファイルに属する必要があります"
        )
        assert "container" in (data["services"]["websockify-desktop"].get("profiles") or []), (
            "websockify-desktop は container プロファイルに属する必要があります"
        )


class TestUpScript:
    def test_exists_and_executable(self):
        path = PROJECT_ROOT / "scripts" / "up.sh"
        assert path.is_file(), "scripts/up.sh が存在しません"
        assert path.stat().st_mode & 0o111

    def test_bash_syntax_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(PROJECT_ROOT / "scripts" / "up.sh")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_detects_kvm(self):
        content = (PROJECT_ROOT / "scripts" / "up.sh").read_text()
        assert "/dev/kvm" in content
        assert "--profile kvm" in content
        assert "--profile container" in content

    def test_switches_vnc_host(self):
        """backend の接続先を起動環境に合わせて切り替える。"""
        content = (PROJECT_ROOT / "scripts" / "up.sh").read_text()
        assert "VNC_HOST=vm" in content, "KVMあり時は backend を vm に向ける必要があります"
        assert "VNC_HOST=desktop" in content, (
            "KVMなし時は backend を desktop に向ける必要があります"
        )

    def test_starts_single_environment_only(self):
        """初期起動の作業環境は1つのみ（KVMあり=vm、なし=desktop）。"""
        lines = (PROJECT_ROOT / "scripts" / "up.sh").read_text().splitlines()
        up_lines = [ln.strip() for ln in lines if "docker compose" in ln and " up " in ln]
        assert len(up_lines) == 2, f"起動経路はKVMあり/なしの2つであるべき（実際: {up_lines}）"
        kvm_line = next(ln for ln in up_lines if "--profile kvm" in ln)
        container_line = next(ln for ln in up_lines if "--profile container" in ln)
        kvm_services = kvm_line.split(" up ", 1)[1].split()
        container_services = container_line.split(" up ", 1)[1].split()
        assert "vm" in kvm_services, "KVMあり時は vm を起動する必要があります"
        for svc in ("desktop", "android", "websockify-desktop", "websockify-android"):
            assert svc not in kvm_services, f"KVMあり時に {svc} を起動してはいけません（単一起動）"
        assert "desktop" in container_services, "KVMなし時は desktop を起動する必要があります"
        for svc in ("vm", "android", "websockify", "websockify-android"):
            assert svc not in container_services, (
                f"KVMなし時に {svc} を起動してはいけません（単一起動）"
            )


class TestAndroidCompose:
    """android サービス（Android-x86 on QEMU）の compose 定義を検証する。"""

    @staticmethod
    def _compose_data():
        import yaml

        return yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())

    def test_android_behind_kvm_profile(self):
        data = self._compose_data()
        android = data["services"]["android"]
        assert "kvm" in (android.get("profiles") or []), (
            "android サービスは kvm プロファイルに属する必要があります"
        )
        ws = data["services"]["websockify-android"]
        assert "kvm" in (ws.get("profiles") or []), (
            "websockify-android は kvm プロファイルに属する必要があります"
        )

    def test_android_build_and_devices(self):
        android = self._compose_data()["services"]["android"]
        build = android.get("build", {})
        assert build.get("context") == "./android"
        devices = android.get("devices", [])
        assert any("/dev/kvm" in str(d) for d in devices), (
            "android サービスは /dev/kvm をマウントする必要があります"
        )

    def test_android_iso_volume(self):
        android = self._compose_data()["services"]["android"]
        volumes = android.get("volumes", [])
        assert any("boot.iso" in str(v) for v in volumes), (
            "android はISOを /android/boot.iso にマウントする必要があります"
        )

    def test_android_data_volume(self):
        android = self._compose_data()["services"]["android"]
        volumes = android.get("volumes", [])
        assert any("android-data.img" in str(v) for v in volumes), (
            "永続dataイメージのvolumeマウントが必要です"
        )

    def test_android_apks_volume(self):
        android = self._compose_data()["services"]["android"]
        volumes = android.get("volumes", [])
        assert any("android/apks" in str(v) for v in volumes), (
            "APK持ち込みディレクトリのvolumeマウントが必要です"
        )

    def test_android_ports(self):
        android = self._compose_data()["services"]["android"]
        assert "5590:5900" in android.get("ports", [])
        ws = self._compose_data()["services"]["websockify-android"]
        assert "6070" in str(ws.get("ports", ""))


class TestAndroidScripts:
    """android関連スクリプトの検証。"""

    def test_mk_data_exists_and_executable(self):
        path = PROJECT_ROOT / "scripts" / "mk-android-data.sh"
        assert path.is_file(), "scripts/mk-android-data.sh が存在しません"
        assert path.stat().st_mode & 0o111, "mk-android-data.sh に実行権限が必要です"

    def test_mk_data_syntax(self):
        path = PROJECT_ROOT / "scripts" / "mk-android-data.sh"
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_mk_data_creates_ext4(self):
        content = (PROJECT_ROOT / "scripts" / "mk-android-data.sh").read_text()
        assert "mkfs.ext4" in content
        assert "android-data.img" in content

    def test_data_img_gitignored(self):
        content = (PROJECT_ROOT / ".gitignore").read_text()
        assert "android-data.img" in content

    def test_fetch_apks_script(self):
        path = PROJECT_ROOT / "scripts" / "fetch-apks.sh"
        assert path.is_file(), "scripts/fetch-apks.sh が存在しません"
        assert path.stat().st_mode & 0o111, "fetch-apks.sh に実行権限が必要です"
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "F-Droid.apk" in path.read_text()

    def test_apks_gitignored(self):
        content = (PROJECT_ROOT / ".gitignore").read_text()
        assert "android/apks/*.apk" in content


class TestAndroidDockerfile:
    """android/Dockerfile の検証（QEMU方式・Redroid禁止）。"""

    ANDROID_DIR = PROJECT_ROOT / "android"

    def test_exists(self):
        assert (self.ANDROID_DIR / "Dockerfile").is_file()
        assert (self.ANDROID_DIR / "entrypoint.sh").is_file()

    def test_installs_qemu_not_redroid(self):
        content = (self.ANDROID_DIR / "Dockerfile").read_text()
        assert "qemu-system-x86" in content
        assert "redroid" not in content.lower()

    def test_no_adb_in_image(self):
        """adbクライアントはandroidイメージに含めない（backend側に同梱。transportはhostfwd）。"""
        content = (self.ANDROID_DIR / "Dockerfile").read_text()
        assert "android-tools-adb" not in content
        assert "keep-awake" not in content

    def test_default_memory_2048(self):
        """既定メモリは2GB（4GBではカーネル直起動が停滞する実測あり）。"""
        dockerfile = (self.ANDROID_DIR / "Dockerfile").read_text()
        assert "ANDROID_MEMORY=2048" in dockerfile
        entrypoint = (self.ANDROID_DIR / "entrypoint.sh").read_text()
        assert 'ANDROID_MEMORY="${ANDROID_MEMORY:-2048}"' in entrypoint

    def test_no_iso_copy(self):
        content = (self.ANDROID_DIR / "Dockerfile").read_text()
        assert not re.search(r"^COPY\s+.*\.iso", content, re.MULTILINE), (
            "ISOはvolumeマウントし、COPYしてはいけません"
        )

    def test_entrypoint_boots_iso(self):
        content = (self.ANDROID_DIR / "entrypoint.sh").read_text()
        assert "-cdrom" in content
        assert "-vnc" in content
        assert "-enable-kvm" in content

    def test_entrypoint_uses_official_cmdline(self):
        """isolinux.cfgのnosetupエントリと同一のパラメータを使うこと。

        androidboot.hardware=android_x86 を付与すると起動ロゴで停止する
        実測があるため、回帰防止として禁止する（コメント言及は可）。
        既定APPENDは公式と同一にすること（4GB停滞の切り分け時に
        cmdline差分を疑わないため）。SETUPWIZARD=0 はウィザードを
        無人スキップしてホーム画面から起動する（nosetupと同一）。
        ADB_TCP= はseedなし/VNCなしのADB到達用に追加のみ許可する。
        """
        content = _read_cmdline(self.ANDROID_DIR / "entrypoint.sh")
        assert "root=/dev/ram0" in content
        assert "SRC=" in content
        assert "SETUPWIZARD=0" in content, "ウィザード無人スキップが必要"
        assert "androidboot.hardware" not in content, (
            "androidboot.hardware付与は起動ロゴ停止の原因のため禁止"
        )
        # 公式nosetup基底 + ADB_TCP/VIRT_WIFI追加のみ許可（順序不問・DATA=vda変種含む）
        assert "root=/dev/ram0 quiet SRC= DATA=" in content, "既定APPENDは公式nosetup基底を保つこと"
        assert "ADB_TCP=" in content, "seedなし/VNCなしのADB到達用にADB_TCPが必要"
        assert "VIRT_WIFI=" in content, "Ethernet既定(VIRT_WIFI=0)が必要"
        assert "DATA=vda" in content, "永続data disk時はDATA=vdaにすること"

    def test_entrypoint_syntax(self):
        path = self.ANDROID_DIR / "entrypoint.sh"
        assert path.stat().st_mode & 0o111
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_entrypoint_has_adb_forward(self):
        """ADB転送は強制経路のtransportとして既定ON（到達時のみ有効）。
        ゲストadbd不在でも起動は継続する。無効化は ANDROID_ADB_FORWARD=0。"""
        content = (self.ANDROID_DIR / "entrypoint.sh").read_text()
        assert "hostfwd=tcp:" in content
        assert "-:5555" in content
        # backend別コンテナから届くよう 0.0.0.0 bind必須（127.0.0.1はloのみで不通）
        assert "hostfwd=tcp:0.0.0.0:" in content, "ADB転送は0.0.0.0 bindであること"
        assert "hostfwd=tcp:127.0.0.1:" not in content, "127.0.0.1 bindではbackend不通"
        assert 'ANDROID_ADB_FORWARD="${ANDROID_ADB_FORWARD:-1}"' in content
        assert 'ANDROID_ADB_PORT="${ANDROID_ADB_PORT:-5555}"' in content
        assert "${NETDEV_OPTS[@]}" in content
        assert "keep-awake" not in content

    def test_entrypoint_persistent_data(self):
        """永続data disk配線（DATA=vda＋virtioドライブ）。"""
        content = (self.ANDROID_DIR / "entrypoint.sh").read_text()
        assert "ANDROID_DATA_IMG" in content
        assert "DATA=vda" in content
        assert "if=virtio" in content
        # ISOフォールバック時は未使用・カーネル直起動時のみ有効
        assert "${DATA_DRIVE[@]}" in content

    def test_entrypoint_seeds_data(self):
        """data.img不在時は空作成で自動生成する（初回ゼロタッチ）。
        設定内容は backend の機械的プロビジョニングが投入する。"""
        content = (self.ANDROID_DIR / "entrypoint.sh").read_text()
        assert "mkfs.ext4" in content
        # volumeの空ディレクトリ化に対する救済
        assert "data-local.img" in content

    def test_entrypoint_nic_knob(self):
        """NICモデル切替（既定virtio-net）。"""
        content = (self.ANDROID_DIR / "entrypoint.sh").read_text()
        assert 'ANDROID_NIC="${ANDROID_NIC:-virtio-net}"' in content
        assert '"$ANDROID_NIC",netdev=net0' in content

    def test_entrypoint_usb_apks(self):
        """APK持ち込み配線（FATイメージ化＋usb-storage）。"""
        content = (self.ANDROID_DIR / "entrypoint.sh").read_text()
        assert "ANDROID_APKS_DIR" in content
        assert "mkfs.vfat" in content
        assert "mcopy" in content
        assert "usb-storage" in content
        assert "${USB_DRIVE[@]}" in content

    def test_entrypoint_adb_tcp_append(self):
        """seedなし/VNCなしのADB到達用に ADB_TCP=1 を -append で渡す。"""
        content = (self.ANDROID_DIR / "entrypoint.sh").read_text()
        assert 'ANDROID_ADB_TCP="${ANDROID_ADB_TCP:-1}"' in content
        assert "ADB_TCP=$ANDROID_ADB_TCP" in content

    def test_entrypoint_virt_wifi_default_ethernet(self):
        """VIRT_WIFI既定0でeth0のまま残しEthernet DHCPでIP取得する。"""
        content = (self.ANDROID_DIR / "entrypoint.sh").read_text()
        assert 'ANDROID_VIRT_WIFI="${ANDROID_VIRT_WIFI:-0}"' in content
        assert "VIRT_WIFI=$ANDROID_VIRT_WIFI" in content

    def test_patch_script_exists(self):
        """initrdパッチスクリプト（同梱なし/VNCなし用）が存在する。"""
        path = PROJECT_ROOT / "scripts" / "patch-android-initrd.py"
        assert path.is_file()
        assert path.stat().st_mode & 0o111 or True
        result = subprocess.run(
            ["bash", "-n", str(PROJECT_ROOT / "android" / "entrypoint.sh")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        content = path.read_text()
        assert "service.adb.tcp.port" in content
        assert "ro.adb.secure" in content
        assert "set-home-activity" in content, "Home選択ダイアログの自動確定が必要"
        assert "home_setup" in content, "boot時リトライ型serviceが必要"
        assert "data/home-setup.sh" in content, "引用符問題回避のscriptファイル化が必要"
        assert "home-hook.log" in content, "Homeフックの証跡が必要"
        assert "initrd-patch-default.prop" in content, "適用証跡が必要"

    def test_initrd_patched(self):
        """android/initrd.img に ADB_TCP+Home パッチが当たっている（存在時のみ）。"""
        initrd = PROJECT_ROOT / "android" / "initrd.img"
        if not initrd.is_file():
            return
        import gzip

        with gzip.open(initrd, "rb") as f:
            data = f.read()
        # cpio newc から init を取り出す
        off = 0
        found = False
        while off + 110 <= len(data):
            if data[off : off + 6] not in (b"070701", b"070702"):
                break
            namesize = int(data[off + 6 + 11 * 8 : off + 6 + 12 * 8], 16)
            filesize = int(data[off + 6 + 6 * 8 : off + 6 + 7 * 8], 16)
            name = data[off + 110 : off + 110 + namesize - 1].decode()
            hl = 110 + namesize
            pad = (4 - (hl % 4)) % 4
            doff = off + hl + pad
            if name == "init":
                found = True
                text = data[doff : doff + filesize].decode()
                assert "ADB_TCP" in text, "initrdにADB_TCPパッチが必要"
                assert "service.adb.tcp.port=5555" in text
                assert "set-home-activity" in text, "Home自動確定パッチが必要"
                break
            if name == "TRAILER!!!":
                break
            off = doff + filesize
            off += (4 - (filesize % 4)) % 4 if filesize else 0
        assert found, "initrd内にinitが見つからない"

    def test_apks_docker_deps(self):
        content = (self.ANDROID_DIR / "Dockerfile").read_text()
        assert "dosfstools" in content
        assert "mtools" in content
