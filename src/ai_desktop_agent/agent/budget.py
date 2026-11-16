"""Per-task limits shared by every model request, including retries and decisions."""

import asyncio
import contextvars
import dataclasses
import math
import os
import time
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation


class TaskLimitError(RuntimeError):
    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


@dataclasses.dataclass(frozen=True)
class ExecutionLimits:
    max_seconds: float = 1800
    request_timeout_seconds: float = 120
    max_requests: int = 500
    max_tokens: int = 100_000
    max_cost_usd: Decimal | None = None
    input_usd_per_million: Decimal | None = None
    output_usd_per_million: Decimal | None = None

    def __post_init__(self):
        for name in ("max_seconds", "request_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name}は有限の正数にしてください")
        for name in ("max_requests", "max_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name}は正の整数にしてください")
        for name in ("max_cost_usd", "input_usd_per_million", "output_usd_per_million"):
            value = getattr(self, name)
            if value is not None and (not value.is_finite() or value < 0):
                raise ValueError(f"{name}は有限の0以上の数にしてください")
        rates = (self.input_usd_per_million, self.output_usd_per_million)
        if any(value is not None for value in rates) and any(value is None for value in rates):
            raise ValueError("入力と出力の単価は両方指定してください")
        if self.max_cost_usd is not None:
            if self.max_cost_usd <= 0:
                raise ValueError("TASK_MAX_COST_USDは正数にしてください")
            if self.input_usd_per_million is None or self.output_usd_per_million is None:
                raise ValueError(
                    "費用の上限にはTASK_INPUT_USD_PER_MILLIONと"
                    "TASK_OUTPUT_USD_PER_MILLIONが必要です"
                )

    @classmethod
    def from_env(cls):
        values = {}
        fields = {
            "max_seconds": ("TASK_MAX_SECONDS", float),
            "request_timeout_seconds": ("TASK_REQUEST_TIMEOUT_SECONDS", float),
            "max_requests": ("TASK_MAX_REQUESTS", int),
            "max_tokens": ("TASK_MAX_TOKENS", int),
            "max_cost_usd": ("TASK_MAX_COST_USD", Decimal),
            "input_usd_per_million": ("TASK_INPUT_USD_PER_MILLION", Decimal),
            "output_usd_per_million": ("TASK_OUTPUT_USD_PER_MILLION", Decimal),
        }
        for field, (key, parse) in fields.items():
            if key in os.environ:
                try:
                    values[field] = parse(os.environ[key])
                except (ValueError, InvalidOperation) as error:
                    raise ValueError(f"{key}の値が不正です") from error
        return cls(**values)

    def to_dict(self):
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in dataclasses.asdict(self).items()
        }


_current: contextvars.ContextVar["TaskBudget | None"] = contextvars.ContextVar(
    "task_budget", default=None
)


class TaskBudget:
    def __init__(self, limits: ExecutionLimits):
        self.limits = limits
        self.requests = self.input_tokens = self.output_tokens = 0
        self.usage_complete = True
        self._usage_reports = 0
        self.stop_reason: str | None = None
        self.error_message = ""
        self._started: float | None = None
        self._finished: float | None = None

    def fail(self, reason: str, message: str):
        self.stop_reason = reason
        self.error_message = message
        raise TaskLimitError(reason, message)

    @asynccontextmanager
    async def activate(self, request_stop):
        self._started = time.monotonic()
        owner = asyncio.current_task()
        cancelling = owner.cancelling()
        expired = False

        def expire():
            nonlocal expired
            expired = True
            # Signal the display thread before cancellation starts draining it.
            try:
                request_stop()
            finally:
                owner.cancel()

        token = _current.set(self)
        timer = asyncio.get_running_loop().call_later(self.limits.max_seconds, expire)
        try:
            yield self
        except asyncio.CancelledError:
            if expired and owner.uncancel() <= cancelling:
                self.fail("duration_limit", "タスクの実行時間の上限に到達しました")
            raise
        finally:
            self._finished = time.monotonic()
            timer.cancel()
            _current.reset(token)

    @property
    def estimated_cost(self) -> Decimal | None:
        limits = self.limits
        if limits.input_usd_per_million is None or limits.output_usd_per_million is None:
            return None
        return (
            self.input_tokens * limits.input_usd_per_million
            + self.output_tokens * limits.output_usd_per_million
        ) / Decimal(1_000_000)

    def check(self):
        if self.stop_reason:
            raise TaskLimitError(self.stop_reason, self.error_message)
        if (
            self._started is not None
            and time.monotonic() - self._started >= self.limits.max_seconds
        ):
            self.fail("duration_limit", "タスクの実行時間の上限に到達しました")

    def begin_request(self):
        self.check()
        if self.requests >= self.limits.max_requests:
            self.fail("request_limit", "モデルの送信回数の上限に到達しました")
        self.requests += 1

    def record_usage(self, usage):
        try:
            if usage is None:
                raise ValueError("missing usage")
            if not isinstance(usage, dict):
                usage = usage.model_dump()
            prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
            completion = usage.get("completion_tokens", usage.get("output_tokens"))
            if any(
                isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (prompt, completion)
            ):
                raise ValueError("invalid usage")
        except (AttributeError, TypeError, ValueError):
            self.usage_complete = False
            self.fail("usage_unavailable", "モデルの使用量を取得できないため停止しました")
        self._usage_reports += 1
        self.input_tokens += prompt
        self.output_tokens += completion
        self.check()
        if self.input_tokens + self.output_tokens >= self.limits.max_tokens:
            self.fail("token_limit", "トークン使用量の上限に到達しました")
        if self.limits.max_cost_usd is not None and self.estimated_cost >= self.limits.max_cost_usd:
            self.fail("cost_limit", "設定した単価による推定費用の上限に到達しました")

    def snapshot(self):
        elapsed = (
            0 if self._started is None else (self._finished or time.monotonic()) - self._started
        )
        return {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": str(self.estimated_cost)
            if self.estimated_cost is not None
            else None,
            "usage_complete": self.usage_complete,
            "elapsed_seconds": elapsed,
        }


@asynccontextmanager
async def model_request():
    """Count actual sends and bound each request; standalone providers remain usable."""
    budget = _current.get()
    if budget is None:
        yield None
        return
    budget.begin_request()
    reports = budget._usage_reports
    timeout = asyncio.timeout(budget.limits.request_timeout_seconds)
    try:
        async with timeout:
            yield budget
    except TimeoutError:
        if timeout.expired():
            budget.fail("request_timeout", "モデルの応答待ち時間の上限に到達しました")
        raise
    finally:
        if reports == budget._usage_reports:
            budget.usage_complete = False
