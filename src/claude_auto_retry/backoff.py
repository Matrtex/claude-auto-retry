"""有界指数退避与 ``Retry-After`` 解析。"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from numbers import Real

RandomSource = Callable[[], float]


def _non_negative_finite(value: float, field_name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} 必须是非负有限数")
    return number


def parse_retry_after(
    value: str | Real | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """把 HTTP ``Retry-After`` 转换为等待秒数。

    支持 delta-seconds 和 HTTP-date。格式错误返回 ``None``，过去的日期返回
    ``0.0``。布尔值虽然是 ``int`` 的子类，但不属于合法 header 值。
    """

    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, Real):
        number = float(value)
        if not math.isfinite(number):
            return None
        return max(0.0, number)

    if not isinstance(value, str):
        return None

    raw_value = value.strip()
    if not raw_value:
        return None

    try:
        number = float(raw_value)
    except ValueError:
        number = None
    if number is not None:
        if not math.isfinite(number):
            return None
        return max(0.0, number)

    try:
        retry_at = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError, OverflowError):
        return None

    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    return max(0.0, (retry_at - current).total_seconds())


@dataclass(frozen=True)
class BackoffPolicy:
    """指数退避策略。

    ``attempt`` 从 1 开始，表示 supervisor 即将进行的第几次重试。指数部分和
    jitter 均受 ``max_delay_seconds`` 限制；可信的 ``Retry-After`` 是最低等待，
    因而可以高于该上限。
    """

    initial_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 60.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        initial = _non_negative_finite(self.initial_delay_seconds, "initial_delay_seconds")
        maximum = _non_negative_finite(self.max_delay_seconds, "max_delay_seconds")
        multiplier = float(self.multiplier)
        jitter = float(self.jitter_ratio)

        if not math.isfinite(multiplier) or multiplier < 1:
            raise ValueError("multiplier 必须是大于等于 1 的有限数")
        if not math.isfinite(jitter) or not 0 <= jitter <= 1:
            raise ValueError("jitter_ratio 必须位于 0 到 1 之间")
        if initial > maximum:
            raise ValueError("initial_delay_seconds 不能大于 max_delay_seconds")

    def delay_for_attempt(
        self,
        attempt: int,
        *,
        retry_after_seconds: float | None = None,
        random_source: RandomSource | None = None,
    ) -> float:
        """计算一次重试前的等待秒数。

        注入 ``random_source`` 可让调用方在既有测试或审计场景中获得确定结果。
        它必须返回闭区间 ``[0, 1]`` 内的值。
        """

        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt 必须是从 1 开始的整数")

        # 先在指数运算前判断是否已经到达上限，避免极大 attempt 产生溢出。
        if self.initial_delay_seconds == 0:
            exponential_delay = 0.0
        else:
            try:
                exponential_delay = self.initial_delay_seconds * (self.multiplier ** (attempt - 1))
            except OverflowError:
                exponential_delay = self.max_delay_seconds
        exponential_delay = min(exponential_delay, self.max_delay_seconds)

        if self.jitter_ratio == 0 or exponential_delay == 0:
            jittered_delay = exponential_delay
        else:
            source = random_source or random.random
            random_value = float(source())
            if not math.isfinite(random_value) or not 0 <= random_value <= 1:
                raise ValueError("random_source 必须返回 0 到 1 之间的有限数")
            low_factor = 1.0 - self.jitter_ratio
            factor = low_factor + (2.0 * self.jitter_ratio * random_value)
            jittered_delay = min(
                self.max_delay_seconds,
                max(0.0, exponential_delay * factor),
            )

        if retry_after_seconds is None:
            return jittered_delay

        retry_after = _non_negative_finite(retry_after_seconds, "retry_after_seconds")
        return max(jittered_delay, retry_after)


def delay_for_attempt(
    attempt: int,
    *,
    initial_delay_seconds: float = 1.0,
    multiplier: float = 2.0,
    max_delay_seconds: float = 60.0,
    jitter_ratio: float = 0.2,
    retry_after_seconds: float | None = None,
    random_source: RandomSource | None = None,
) -> float:
    """使用临时策略计算退避时间的便利函数。"""

    policy = BackoffPolicy(
        initial_delay_seconds=initial_delay_seconds,
        multiplier=multiplier,
        max_delay_seconds=max_delay_seconds,
        jitter_ratio=jitter_ratio,
    )
    return policy.delay_for_attempt(
        attempt,
        retry_after_seconds=retry_after_seconds,
        random_source=random_source,
    )


# 为调用方保留语义清晰的别名。
compute_backoff = delay_for_attempt


__all__ = [
    "BackoffPolicy",
    "RandomSource",
    "compute_backoff",
    "delay_for_attempt",
    "parse_retry_after",
]
