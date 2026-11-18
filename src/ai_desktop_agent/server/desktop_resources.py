"""Docker runtime and resource settings for container desktops."""

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def desktop_runtime_from_env() -> str | None:
    runtime = os.environ.get("DESKTOP_RUNTIME") or None
    if runtime not in (None, "runc", "runsc"):
        raise ValueError("DESKTOP_RUNTIME は未設定、runc、runsc のいずれかにしてください")
    return runtime


def _number(name: str, default: str, minimum: Decimal, scale: int = 1) -> int:
    try:
        raw = os.environ.get(name) or default
        if len(raw) > 32 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
            raise ValueError
        value = Decimal(raw)
        scaled = value * scale
        if (
            not value.is_finite()
            or value < minimum
            or scaled > 2**63 - 1
            or scaled != scaled.to_integral_value()
        ):
            raise ValueError
        return int(scaled)
    except (InvalidOperation, ValueError, OverflowError) as error:
        raise ValueError(f"{name} の設定値が不正です。有限の正数を指定してください") from error


@dataclass(frozen=True)
class DesktopResources:
    memory_bytes: int = 2 * 1024**3
    nano_cpus: int = 2_000_000_000
    pids_limit: int = 1024

    @classmethod
    def from_env(cls) -> "DesktopResources":
        memory_mb = _number("DESKTOP_MEMORY_MB", "2048", Decimal(6))
        if memory_mb > (2**63 - 1) // 1024**2:
            raise ValueError("DESKTOP_MEMORY_MB の設定値が大きすぎます")
        return cls(
            memory_bytes=memory_mb * 1024**2,
            nano_cpus=_number("DESKTOP_CPUS", "2", Decimal("0.01"), 1_000_000_000),
            pids_limit=_number("DESKTOP_PIDS_LIMIT", "1024", Decimal(1)),
        )

    def docker_options(self) -> dict[str, int]:
        return {
            "mem_limit": self.memory_bytes,
            # Setting both to the same value prevents unbounded swap pressure.
            "memswap_limit": self.memory_bytes,
            "nano_cpus": self.nano_cpus,
            "pids_limit": self.pids_limit,
        }
