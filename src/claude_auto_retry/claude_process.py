"""Claude Code CLI 的定位、版本检查和子进程调用。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import StreamJsonState
from .safety import SideEffectTracker

MINIMUM_VERSION = (2, 1, 223)
WATCHDOG_MINIMUM_VERSION = (2, 1, 239)
_VERSION_PATTERN = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)")


class ClaudeUnavailableError(RuntimeError):
    """Claude CLI 缺失、不可执行或版本不受支持。"""


@dataclass(frozen=True, slots=True)
class ClaudeInfo:
    executable: str
    version_text: str
    version: tuple[int, int, int]
    supported: bool
    auth_logged_in: bool | None


@dataclass(slots=True)
class InvocationOutcome:
    return_code: int
    stream: StreamJsonState
    safety: SideEffectTracker
    stderr_tail: str
    emitted_text: bool

    @property
    def succeeded(self) -> bool:
        result = self.stream.result
        return bool(
            self.return_code == 0
            and isinstance(result, dict)
            and result.get("subtype") == "success"
            and result.get("is_error") is False
        )


class _TailBuffer:
    """只在内存保留少量 stderr 尾部，避免日志和凭据落盘。"""

    def __init__(self, limit: int = 32_768) -> None:
        self._limit = limit
        self._text = ""
        self._lock = threading.Lock()

    def append(self, value: str) -> None:
        with self._lock:
            self._text = (self._text + value)[-self._limit :]

    def value(self) -> str:
        with self._lock:
            return self._text


def find_claude(explicit: str | None = None) -> str:
    """按显式参数、环境变量、PATH 和官方默认目录定位 Claude CLI。"""

    def resolve(candidate: str) -> str | None:
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path.resolve())
        discovered_path = shutil.which(candidate)
        return str(Path(discovered_path).resolve()) if discovered_path else None

    if explicit:
        resolved = resolve(explicit)
        if resolved:
            return resolved
        raise ClaudeUnavailableError("--claude 指定的 Claude Code CLI 不存在或不可执行")

    configured = os.environ.get("CLAUDE_BIN")
    if configured:
        resolved = resolve(configured)
        if resolved:
            return resolved
        raise ClaudeUnavailableError("CLAUDE_BIN 指定的 Claude Code CLI 不存在或不可执行")

    candidates: list[str] = []
    discovered = shutil.which("claude") or shutil.which("claude.exe")
    if discovered:
        candidates.append(discovered)
    if os.name == "nt":
        candidates.append(str(Path.home() / ".local" / "bin" / "claude.exe"))

    for candidate in candidates:
        resolved = resolve(candidate)
        if resolved:
            return resolved
    raise ClaudeUnavailableError(
        "未找到 Claude Code CLI；请先安装官方 CLI，或通过 --claude/CLAUDE_BIN 指定路径"
    )


def read_version(executable: str) -> tuple[str, tuple[int, int, int]]:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeUnavailableError("无法执行 Claude Code CLI") from exc
    text = (completed.stdout or completed.stderr).strip()
    match = _VERSION_PATTERN.search(text)
    if completed.returncode != 0 or not match:
        raise ClaudeUnavailableError("无法识别 Claude Code CLI 版本")
    version = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
    return text, version  # type: ignore[return-value]


def read_auth_status(executable: str) -> bool | None:
    """只返回登录布尔值，不打印或保存认证内容。"""

    try:
        completed = subprocess.run(
            [executable, "auth", "status"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        return completed.returncode == 0 if completed.returncode in (0, 1) else None
    logged_in = payload.get("loggedIn")
    return logged_in if isinstance(logged_in, bool) else None


def inspect_claude(explicit: str | None = None) -> ClaudeInfo:
    executable = find_claude(explicit)
    version_text, version = read_version(executable)
    return ClaudeInfo(
        executable=executable,
        version_text=version_text,
        version=version,
        supported=version >= MINIMUM_VERSION,
        auth_logged_in=read_auth_status(executable),
    )


def ensure_supported(executable: str) -> None:
    _text, version = read_version(executable)
    if version < MINIMUM_VERSION:
        required = ".".join(str(part) for part in MINIMUM_VERSION)
        actual = ".".join(str(part) for part in version)
        raise ClaudeUnavailableError(f"Claude Code {actual} 过旧；至少需要 {required}")


def run_interactive(executable: str, arguments: Iterable[str], environment: dict[str, str]) -> int:
    try:
        return subprocess.call([executable, *arguments], env=environment)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        raise ClaudeUnavailableError("无法启动 Claude Code CLI") from exc


def run_stream_invocation(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    input_text: str,
    raw_stream: bool = False,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> InvocationOutcome:
    """消费一轮 NDJSON，实时输出文本，并只在内存保留必要判定状态。"""

    stream = StreamJsonState()
    safety = SideEffectTracker()
    stderr_tail = _TailBuffer()
    emitted_text = False

    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        raise ClaudeUnavailableError("无法启动 Claude Code CLI") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    assert process.stdin is not None

    def drain_stderr() -> None:
        for chunk in iter(process.stderr.readline, ""):
            stderr_tail.append(chunk)
            sys.stderr.write(chunk)
            sys.stderr.flush()

    stderr_thread = threading.Thread(target=drain_stderr, name="claude-stderr", daemon=True)
    stderr_thread.start()

    # prompt 通过 stdin 传入，避免出现在进程命令行和进程列表中。
    try:
        process.stdin.write(input_text)
        process.stdin.close()
    except OSError:
        pass

    try:
        for line in iter(process.stdout.readline, ""):
            if raw_stream:
                sys.stdout.write(line)
                sys.stdout.flush()
            event = stream.feed_line(line)
            if not isinstance(event, dict):
                continue
            safety.observe_event(event)
            if on_event is not None:
                on_event(event)
            if raw_stream:
                continue
            if event.get("type") == "stream_event":
                delta = event.get("event", {}).get("delta", {})
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str):
                        sys.stdout.write(text)
                        sys.stdout.flush()
                        emitted_text = True
        return_code = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        stderr_thread.join(timeout=5)

    stream.finish()
    if not raw_stream:
        if emitted_text:
            sys.stdout.write("\n")
            sys.stdout.flush()
        elif isinstance(stream.result, dict):
            result_text = stream.result.get("result")
            if isinstance(result_text, str) and result_text:
                print(result_text)

    return InvocationOutcome(
        return_code=return_code,
        stream=stream,
        safety=safety,
        stderr_tail=stderr_tail.value(),
        emitted_text=emitted_text,
    )
