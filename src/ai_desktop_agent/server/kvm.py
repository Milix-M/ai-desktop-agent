"""KVM利用可否の判定 — VM起動制限の正本。

判定順序:
1. `USE_KVM` の明示指定があればそれに従う (`true` → 可、`false` → 不可)。
   未設定時の既定は `auto`。
2. `auto` の場合は backend 自身から見える `/dev/kvm` を確認する
   （ホスト直実行の開発時用。コンテナ内からは通常見えない）。
3. それでも不明な場合は Docker 上の `vm` サービス存在有無を見る。
   `vm` は `kvm` プロファイルでのみ作成されるため、存在すれば
   KVM利用可とみなす。Docker不通時は不可扱い。

TCGフォールバックの明示許可 (`ALLOW_TCG_VM=true`) はここでは見ない。
呼び出し側 (`vm_control` / `vm_pool`) が先に評価する。
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

KVM_DEVICE = "/dev/kvm"


def resolve_use_kvm() -> str:
    """`USE_KVM` の解決値 (`true` / `false` / `auto`) を返す。"""
    return os.environ.get("USE_KVM", "auto").strip().lower()


def is_tcg_allowed() -> bool:
    """TCG実行の明示許可 (`ALLOW_TCG_VM=true`) があるかどうか。"""
    return os.environ.get("ALLOW_TCG_VM", "false").strip().lower() == "true"


def is_kvm_available(client: Any | None = None) -> bool:
    """KVMが利用可能かどうかを返す。"""
    mode = resolve_use_kvm()
    if mode == "true":
        return True
    if mode == "false":
        return False
    # auto: backend 自身のデバイスを見る（ホスト直実行時）
    if os.path.exists(KVM_DEVICE):
        return True
    # コンテナ内からは見えないため、Docker上の vm サービス存在で判断する
    try:
        from ai_desktop_agent.server.vm_pool import (
            BASE_VM_SERVICE,
            COMPOSE_SERVICE_LABEL,
        )

        if client is None:
            try:
                import docker
            except ImportError:
                return False
            try:
                client = docker.from_env()
                client.ping()
            except Exception:
                return False
        candidates = client.containers.list(
            all=True, filters={"label": f"{COMPOSE_SERVICE_LABEL}={BASE_VM_SERVICE}"}
        )
        return len(candidates) > 0
    except Exception:
        logger.debug("KVM利用可否の判定に失敗", exc_info=True)
        return False


class KvmUnavailableError(ValueError):
    """KVMが利用できない環境でVM起動が要求された場合のエラー。

    `ValueError` 継承のため、API層の既存ハンドラで400/409として返せる。
    """

    def __init__(self) -> None:
        super().__init__(
            "KVMが利用できない環境ではVM起動は制限されています。"
            "コンテナ環境（desktop）を使用してください。"
            "デバッグ目的でTCG実行を許可する場合は ALLOW_TCG_VM=true を設定してください。"
        )
