"""进程对外使用的稳定退出码。"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Supervisor 的稳定退出码。

    这些数值属于命令行公共契约。新增状态时应分配新值，不应改变现有值。
    """

    SUCCESS = 0
    USAGE = 2
    CLAUDE_UNAVAILABLE = 3
    TERMINAL_FAILURE = 4
    RETRY_EXHAUSTED = 5
    SIDE_EFFECT_BARRIER = 6
    PROTOCOL_FAILURE = 7
    INTERRUPTED = 130

    # 兼容直观的同义名称，同时保留上面的名称作为规范输出名称。
    OK = SUCCESS
    CONFIG_ERROR = USAGE
    PERMANENT_FAILURE = TERMINAL_FAILURE
    RETRIES_EXHAUSTED = RETRY_EXHAUSTED
    SIDE_EFFECT_DETECTED = SIDE_EFFECT_BARRIER
    PROTOCOL_ERROR = PROTOCOL_FAILURE


__all__ = ["ExitCode"]
