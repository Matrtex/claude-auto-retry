"""Claude Code headless same-session resume 状态机。"""

from __future__ import annotations

import random
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backoff import BackoffPolicy
from .classifier import Classification, FailureKind, classify_failure
from .claude_process import ensure_supported, run_stream_invocation
from .config import NativeRetrySettings, SupervisorSettings
from .exit_codes import ExitCode

CONTINUATION_PROMPT = """Continue the existing task from the last durable point. First inspect the
conversation, tool results, workspace, and any relevant external state. Do not repeat a completed
or outcome-unknown side effect. If you cannot verify whether a write, commit, push, deployment,
message, payment, deletion, or other external action succeeded, stop and report what requires
manual verification."""

_TERMINAL_RESULT_SUBTYPES = frozenset(
    {
        "error_max_turns",
        "error_max_budget_usd",
        "error_max_structured_output_retries",
    }
)

_TERMINAL_REASONS = frozenset(
    {
        "max_turns",
        "tool_deferred",
        "aborted_streaming",
        "aborted_tools",
        "hook_stopped",
        "stop_hook_prevented",
        "blocking_limit",
        "rapid_refill_breaker",
        "prompt_too_long",
        "image_error",
    }
)

_OFFICIAL_API_ERROR_TYPES = frozenset(
    {
        "authentication_failed",
        "oauth_org_not_allowed",
        "billing_error",
        "rate_limit",
        "rate_limited",
        "overloaded",
        "overloaded_error",
        "invalid_request",
        "model_not_found",
        "server_error",
        "max_output_tokens",
        "unknown",
    }
)


@dataclass(frozen=True, slots=True)
class SuperviseRequest:
    """一次 supervisor 运行的完整、不可变输入。"""

    executable: str
    prompt: str
    cwd: Path
    claude_arguments: tuple[str, ...]
    native: NativeRetrySettings
    supervisor: SupervisorSettings
    raw_stream: bool = False


def _build_command(request: SuperviseRequest, session_id: str, *, resume: bool) -> list[str]:
    command = [
        request.executable,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--include-hook-events",
    ]
    if resume:
        command.extend(("--resume", session_id))
    else:
        command.extend(("--session-id", session_id))
    command.extend(request.claude_arguments)
    return command


def _print_api_retry(event: dict[str, Any], *, raw_stream: bool) -> None:
    if raw_stream or event.get("type") != "system" or event.get("subtype") != "api_retry":
        return
    attempt = event.get("attempt")
    maximum = event.get("max_retries")
    delay_ms = event.get("retry_delay_ms")
    category = event.get("error")
    status = event.get("error_status")
    safe_values = (
        isinstance(attempt, int),
        isinstance(maximum, int),
        isinstance(delay_ms, int),
        isinstance(category, str),
        status is None or isinstance(status, int),
    )
    if not all(safe_values):
        return
    status_text = f", status={status}" if status is not None else ""
    print(
        "[claude-auto-retry] Claude 内置 retry "
        f"{attempt}/{maximum}, category={category}, delay_ms={delay_ms}{status_text}",
        file=sys.stderr,
    )


def _classify(outcome: Any) -> Classification:
    stream = outcome.stream
    result = stream.result or {}
    extra_messages: list[str] = []
    for field in ("terminal_reason", "stop_reason"):
        value = result.get(field)
        if isinstance(value, str):
            extra_messages.append(value)
    classification = classify_failure(
        stream.error_text,
        status_code=stream.error_status,
        error_type=stream.error_type,
        stderr=outcome.stderr_tail,
        extra_messages=extra_messages,
    )
    if classification.retryable and not _has_api_failure_origin(outcome):
        return Classification(
            FailureKind.UNKNOWN,
            "missing_api_failure_origin",
            classification.status_code,
            classification.error_type,
        )
    return classification


def _has_api_failure_origin(outcome: Any) -> bool:
    """防止把本地工具、MCP 或项目服务错误误判为 Provider 故障。"""

    stream = outcome.stream
    if stream.last_api_retry is not None or stream.error_status is not None:
        return True
    if (
        isinstance(stream.error_type, str)
        and stream.error_type.lower() in _OFFICIAL_API_ERROR_TYPES
    ):
        return True
    result = stream.result or {}
    if result.get("terminal_reason") == "model_error":
        return True
    evidence = "\n".join(
        value
        for value in (stream.error_text, outcome.stderr_tail)
        if isinstance(value, str)
    ).lower()
    return any(
        marker in evidence
        for marker in (
            "api error",
            "unable to connect to api",
            "anthropic api",
            "api.anthropic.com",
            "bedrock runtime",
            "vertex ai",
            "microsoft foundry",
        )
    )


def _calculate_delay(
    settings: SupervisorSettings,
    attempt: int,
    *,
    retry_after_seconds: float | None,
) -> float:
    """使用绝对秒 jitter；可信 retry-after 始终作为最低等待。"""

    policy = BackoffPolicy(
        initial_delay_seconds=settings.base_delay,
        multiplier=settings.backoff_factor,
        max_delay_seconds=settings.max_delay,
        jitter_ratio=0.0,
    )
    base_delay = policy.delay_for_attempt(attempt)
    jittered = min(
        settings.max_delay,
        max(0.0, base_delay + random.uniform(-settings.jitter, settings.jitter)),
    )
    if retry_after_seconds is None:
        return jittered
    return max(jittered, retry_after_seconds)


def _manual_resume_message(session_id: str, reason: str) -> None:
    print(
        f"[claude-auto-retry] 已停止自动恢复：{reason}。",
        file=sys.stderr,
    )
    print(
        "请先人工核对 workspace 和外部系统状态，再按需执行：\n"
        f"  claude --resume {session_id}",
        file=sys.stderr,
    )


def supervise(request: SuperviseRequest) -> ExitCode:
    """运行初始任务，并在满足全部安全条件时 resume 同一个 session。"""

    request.native.validate()
    request.supervisor.validate()
    if not request.cwd.is_dir():
        print("[claude-auto-retry] 工作目录不存在或不是目录。", file=sys.stderr)
        return ExitCode.USAGE
    ensure_supported(request.executable)

    session_id = str(uuid.uuid4())
    environment = request.native.apply()
    for variable in ("CLAUDE_CODE_SKIP_PROMPT_HISTORY", "CLAUDE_CODE_CHILD_SESSION"):
        value = environment.get(variable, "").strip().lower()
        if value not in {"", "0", "false", "no", "off"}:
            print(
                f"[claude-auto-retry] {variable} 会禁用可恢复 session，拒绝启动。",
                file=sys.stderr,
            )
            return ExitCode.USAGE
    started_at = time.monotonic()
    resume_count = 0
    prompt = request.prompt
    resume = False

    while True:
        command = _build_command(request, session_id, resume=resume)
        outcome = run_stream_invocation(
            command,
            cwd=request.cwd,
            environment=environment,
            input_text=prompt,
            raw_stream=request.raw_stream,
            on_event=lambda event: _print_api_retry(event, raw_stream=request.raw_stream),
        )

        if outcome.succeeded:
            return ExitCode.SUCCESS

        stream = outcome.stream
        if stream.stream_corrupted or not stream.saw_init or stream.session_id != session_id:
            reasons = ",".join(stream.corruption_reasons) or "missing_or_mismatched_init"
            print(
                f"[claude-auto-retry] 协议状态不可信，拒绝自动恢复：{reasons}",
                file=sys.stderr,
            )
            return ExitCode.PROTOCOL_FAILURE

        result_subtype = stream.result.get("subtype") if stream.result else None
        if result_subtype in _TERMINAL_RESULT_SUBTYPES:
            print(
                f"[claude-auto-retry] Claude 到达终止边界：{result_subtype}",
                file=sys.stderr,
            )
            return ExitCode.TERMINAL_FAILURE

        terminal_reason = stream.result.get("terminal_reason") if stream.result else None
        if terminal_reason in _TERMINAL_REASONS:
            print(
                f"[claude-auto-retry] Claude 到达终止边界：{terminal_reason}",
                file=sys.stderr,
            )
            return ExitCode.TERMINAL_FAILURE

        classification = _classify(outcome)
        if classification.kind is not FailureKind.TRANSIENT:
            print(
                "[claude-auto-retry] 不满足自动恢复条件："
                f"category={classification.kind.value}, reason={classification.reason}, "
                f"exit_code={outcome.return_code}",
                file=sys.stderr,
            )
            return ExitCode.TERMINAL_FAILURE

        if not outcome.safety.safe_to_retry:
            _manual_resume_message(
                session_id,
                f"命中副作用屏障 ({outcome.safety.barrier_reason or 'unknown_tool'})",
            )
            return ExitCode.SIDE_EFFECT_BARRIER

        if resume_count >= request.supervisor.max_resumes:
            _manual_resume_message(session_id, "same-session resume 预算已耗尽")
            return ExitCode.RETRY_EXHAUSTED

        next_resume = resume_count + 1
        delay = _calculate_delay(
            request.supervisor,
            next_resume,
            retry_after_seconds=stream.retry_after_seconds,
        )
        elapsed = time.monotonic() - started_at
        if elapsed + delay > request.supervisor.max_elapsed:
            _manual_resume_message(session_id, "总时间窗口不足以等待下一次 resume")
            return ExitCode.RETRY_EXHAUSTED

        print(
            "[claude-auto-retry] 将恢复同一个 session："
            f"resume={next_resume}/{request.supervisor.max_resumes}, "
            f"category={classification.reason}, delay={delay:.1f}s",
            file=sys.stderr,
        )
        time.sleep(delay)
        resume_count = next_resume
        prompt = CONTINUATION_PROMPT
        resume = True


__all__ = ["CONTINUATION_PROMPT", "SuperviseRequest", "supervise"]
