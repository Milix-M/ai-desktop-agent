"""Per-task limits shared by every model request, including retries and decisions."""

import asyncio
import contextvars
import dataclasses
import math
import os
import time
from collections.abc import Callable
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
    subtask_timeout_seconds: float = 60

    def __post_init__(self):
        for name in ("max_seconds", "request_timeout_seconds", "subtask_timeout_seconds"):
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
            "subtask_timeout_seconds": ("TASK_SUBTASK_TIMEOUT_SECONDS", float),
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


def default_subtask_timeout() -> float:
    budget = _current.get()
    return (
        budget.limits.subtask_timeout_seconds if budget else ExecutionLimits.subtask_timeout_seconds
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
        self._subtask_id: str | None = None
        self._subtask_deadline: float | None = None
        self._subtask_message = ""
        self._reschedule: Callable[[], None] | None = None
        self._cancel_requested = False

    def request_cancel(self):
        # This latch is independent of a provider's handling of CancelledError.
        self._cancel_requested = True

    def set_subtask(self, identifier: str | None, seconds: float = 0, description: str = ""):
        if self._reschedule is None:
            return
        # Check the old deadline before accepting a completion or replacement.
        self.check()
        if identifier == self._subtask_id:
            return
        self._subtask_id = identifier
        self._subtask_deadline = time.monotonic() + seconds if identifier is not None else None
        self._subtask_message = (
            f"サブタスクの実行時間の上限に到達しました: {description}（{seconds:g}秒）"
        )
        self._reschedule()

    def _next_deadline(self) -> tuple[float, str, str]:
        deadline = (
            self._started + self.limits.max_seconds if self._started is not None else math.inf
        )
        if self._subtask_deadline is not None and self._subtask_deadline < deadline:
            return self._subtask_deadline, "subtask_timeout", self._subtask_message
        return deadline, "duration_limit", "タスクの実行時間の上限に到達しました"

    def fail(self, reason: str, message: str):
        if self._cancel_requested:
            raise asyncio.CancelledError
        self.stop_reason = reason
        self.error_message = message
        raise TaskLimitError(reason, message)

    @asynccontextmanager
    async def activate(self, request_stop):
        self._started = time.monotonic()
        self._finished = None
        self._subtask_id = self._subtask_deadline = None
        owner = asyncio.current_task()
        cancelling = owner.cancelling()
        expired: tuple[str, str] | None = None

        def expire():
            nonlocal expired
            expired = self._next_deadline()[1:]
            # Signal the display thread before cancellation starts draining it.
            try:
                request_stop()
            finally:
                owner.cancel()

        token = _current.set(self)
        loop = asyncio.get_running_loop()
        timer = loop.call_later(self.limits.max_seconds, expire)

        def reschedule():
            nonlocal timer
            timer.cancel()
            remaining = max(0, self._next_deadline()[0] - time.monotonic())
            timer = loop.call_later(remaining, expire)

        self._reschedule = reschedule
        try:
            yield self
            self.check()
        except asyncio.CancelledError:
            if expired and owner.cancelling() <= cancelling + 1:
                self.fail(*expired)
            raise
        except Exception:
            if self._cancel_requested:
                raise asyncio.CancelledError from None
            raise
        finally:
            # Providers may suppress cancellation and then hit check(). Remove
            # this timer's request on every exit without consuming operator stop.
            if expired:
                owner.uncancel()
            self._finished = time.monotonic()
            timer.cancel()
            self._reschedule = None
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
        if self._cancel_requested:
            raise asyncio.CancelledError
        if self.stop_reason:
            raise TaskLimitError(self.stop_reason, self.error_message)
        deadline, reason, message = self._next_deadline()
        if time.monotonic() >= deadline:
            self.fail(reason, message)

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
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget.limits.request_timeout_seconds
    timeout = asyncio.timeout_at(deadline)
    owner = asyncio.current_task()
    cancelling = owner.cancelling()

    def check_response():
        # A provider may catch CancelledError. Keep external stop requests while
        # letting asyncio.timeout remove its own cancellation on context exit.
        if owner.cancelling() > cancelling + int(timeout.expired()):
            raise asyncio.CancelledError
        # CPU-bound response processing can finish before a due timer runs.
        if loop.time() >= deadline:
            budget.fail("request_timeout", "モデルの応答待ち時間の上限に到達しました")

    try:
        async with timeout:
            try:
                yield budget
            except TaskLimitError:
                raise
            except Exception:
                check_response()
                raise
            else:
                check_response()
    except TimeoutError:
        if timeout.expired():
            budget.fail("request_timeout", "モデルの応答待ち時間の上限に到達しました")
        raise
    finally:
        if reports == budget._usage_reports:
            budget.usage_complete = False
