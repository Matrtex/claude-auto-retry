"""命令行配置解析后的稳定数据结构。"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(ValueError):
    """配置值无效。"""


@dataclass(frozen=True, slots=True)
class NativeRetrySettings:
    """Claude Code 官方内置 retry 的环境变量。"""

    max_retries: int = 10
    api_timeout_ms: int = 600_000
    watchdog: bool = False

    def validate(self) -> None:
        if not 0 <= self.max_retries <= 15:
            raise ConfigError("Claude Code 内置重试次数必须在 0 到 15 之间")
        if self.api_timeout_ms < 1_000:
            raise ConfigError("API timeout 不能小于 1000 ms")

    def apply(self, source: dict[str, str] | None = None) -> dict[str, str]:
        self.validate()
        environment = dict(os.environ if source is None else source)
        environment["CLAUDE_CODE_MAX_RETRIES"] = str(self.max_retries)
        environment["API_TIMEOUT_MS"] = str(self.api_timeout_ms)
        if self.watchdog:
            environment["CLAUDE_CODE_RETRY_WATCHDOG"] = "1"
        else:
            # supervisor 必须保持有界，不能继承无限 watchdog。
            environment.pop("CLAUDE_CODE_RETRY_WATCHDOG", None)
        return environment


@dataclass(frozen=True, slots=True)
class SupervisorSettings:
    """外层 same-session resume 的预算和退避参数。"""

    max_resumes: int = 2
    base_delay: float = 15.0
    max_delay: float = 120.0
    backoff_factor: float = 2.0
    jitter: float = 3.0
    max_elapsed: float = 1_800.0

    def validate(self) -> None:
        if not 0 <= self.max_resumes <= 5:
            raise ConfigError("自动 resume 次数必须在 0 到 5 之间")
        if self.base_delay < 0:
            raise ConfigError("基础等待时间不能为负数")
        if self.max_delay < self.base_delay:
            raise ConfigError("最大等待时间不能小于基础等待时间")
        if not 1.0 <= self.backoff_factor <= 10.0:
            raise ConfigError("退避倍率必须在 1 到 10 之间")
        if self.jitter < 0:
            raise ConfigError("jitter 不能为负数")
        if self.max_elapsed <= 0:
            raise ConfigError("总时间窗口必须大于 0")


def environment_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return fallback
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数") from exc


def environment_float(name: str, fallback: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return fallback
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是数字") from exc
