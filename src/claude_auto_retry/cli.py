"""claude-auto-retry 命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .claude_process import (
    WATCHDOG_MINIMUM_VERSION,
    ClaudeUnavailableError,
    find_claude,
    inspect_claude,
    read_version,
    run_interactive,
)
from .config import (
    ConfigError,
    NativeRetrySettings,
    SupervisorSettings,
    environment_float,
    environment_int,
)
from .exit_codes import ExitCode
from .supervisor import SuperviseRequest, supervise

_SUPERVISOR_DENIED_FLAGS = {
    "-p",
    "--print",
    "-r",
    "--resume",
    "-c",
    "--continue",
    "--fork-session",
    "--session-id",
    "--no-session-persistence",
    "--input-format",
    "--output-format",
    "--include-partial-messages",
    "--include-hook-events",
    "--verbose",
    "--bg",
    "--max-turns",
    "--max-budget-usd",
    "--json-schema",
    "--replay-user-messages",
    "--remote-control",
    "--rc",
    "--teleport",
    "--worktree",
    "-w",
    "--permission-prompt-tool",
    "--help",
    "-h",
    "--version",
    "-v",
    "--exec",
    "--init",
    "--init-only",
    "--maintenance",
    "--from-pr",
}

_SUPERVISOR_DENIED_SHORT_PREFIXES = ("-p", "-r", "-c", "-w", "-v", "-h")


class SafeArgumentParser(argparse.ArgumentParser):
    """让参数错误稳定返回项目定义的 usage code。"""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        raise ConfigError(message)


def _configure_windows_stdio() -> None:
    """避免西文代码页上的 Windows 终端因中文帮助文本触发 UnicodeEncodeError。"""

    if os.name != "nt" or "PYTHONIOENCODING" in os.environ:
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                # 被宿主替换或已经关闭的 stream 不阻塞 CLI 启动。
                continue


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(
        prog="claude-auto-retry",
        description="Claude Code 官方 retry 配置器和安全 same-session resume supervisor。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="检查 Claude CLI、版本和登录状态")
    doctor.add_argument("--claude", metavar="PATH", help="Claude CLI 路径")
    doctor.add_argument("--json", action="store_true", help="输出机器可读结果")
    doctor.set_defaults(handler=_handle_doctor)

    launch = subparsers.add_parser(
        "launch",
        aliases=["interactive"],
        help="携带官方 retry 配置启动交互式 Claude Code",
    )
    launch.add_argument("--claude", metavar="PATH", help="Claude CLI 路径")
    launch.add_argument("--max-retries", type=int, help="Claude 官方内置 retry 次数（0-15）")
    launch.add_argument("--api-timeout-ms", type=int, help="单次 API 请求 timeout（ms）")
    launch.add_argument(
        "--watchdog",
        action="store_true",
        help="启用官方无人值守 watchdog（可能对 429/529 无限等待）",
    )
    launch.add_argument("--dry-run", action="store_true", help="只检查配置，不启动 Claude")
    launch.add_argument("claude_args", nargs=argparse.REMAINDER, help="`--` 后透传给 Claude")
    launch.set_defaults(handler=_handle_launch)

    run = subparsers.add_parser(
        "supervise",
        aliases=["run"],
        help="在内置 retry 耗尽后，安全恢复同一个 headless session",
    )
    run.add_argument("prompt", nargs="?", help="初始 prompt；也可使用 --prompt-file 或 stdin")
    run.add_argument("--prompt-file", type=Path, metavar="FILE", help="从 UTF-8 文件读取 prompt")
    run.add_argument("--cwd", type=Path, default=Path.cwd(), help="Claude 的工作目录")
    run.add_argument("--claude", metavar="PATH", help="Claude CLI 路径")
    run.add_argument("--max-resumes", type=int, help="外层自动 resume 上限（0-5）")
    run.add_argument("--base-delay", type=float, help="基础退避秒数")
    run.add_argument("--max-delay", type=float, help="单次最大退避秒数")
    run.add_argument("--backoff-factor", type=float, help="指数退避倍率")
    run.add_argument("--jitter", type=float, help="随机抖动秒数")
    run.add_argument("--max-elapsed", type=float, help="允许启动下一次 resume 的总时间窗口")
    run.add_argument(
        "--claude-max-retries", type=int, help="每轮 Claude 官方内置 retry 次数（0-15）"
    )
    run.add_argument("--api-timeout-ms", type=int, help="单次 API 请求 timeout（ms）")
    run.add_argument(
        "--raw-stream",
        action="store_true",
        help="把 Claude stream-json 原样写到当前 stdout（可能包含敏感内容）",
    )
    run.add_argument("claude_args", nargs=argparse.REMAINDER, help="`--` 后透传给 Claude")
    run.set_defaults(handler=_handle_supervise)
    return parser


def _normalise_remainder(arguments: Sequence[str]) -> list[str]:
    values = list(arguments)
    if values and values[0] == "--":
        values.pop(0)
    return values


def _environment_int_with_native(wrapper_name: str, native_name: str, fallback: int) -> int:
    if wrapper_name in os.environ:
        return environment_int(wrapper_name, fallback)
    return environment_int(native_name, fallback)


def _native_settings(args: argparse.Namespace, *, watchdog: bool) -> NativeRetrySettings:
    max_retries = (
        args.max_retries
        if hasattr(args, "max_retries") and args.max_retries is not None
        else args.claude_max_retries
        if hasattr(args, "claude_max_retries") and args.claude_max_retries is not None
        else _environment_int_with_native(
            "CLAUDE_AUTO_RETRY_CLAUDE_MAX_RETRIES", "CLAUDE_CODE_MAX_RETRIES", 10
        )
    )
    api_timeout = (
        args.api_timeout_ms
        if args.api_timeout_ms is not None
        else _environment_int_with_native(
            "CLAUDE_AUTO_RETRY_API_TIMEOUT_MS", "API_TIMEOUT_MS", 600_000
        )
    )
    settings = NativeRetrySettings(
        max_retries=max_retries,
        api_timeout_ms=api_timeout,
        watchdog=watchdog,
    )
    settings.validate()
    return settings


def _supervisor_settings(args: argparse.Namespace) -> SupervisorSettings:
    settings = SupervisorSettings(
        max_resumes=args.max_resumes
        if args.max_resumes is not None
        else environment_int("CLAUDE_AUTO_RETRY_MAX_RESUMES", 2),
        base_delay=args.base_delay
        if args.base_delay is not None
        else environment_float("CLAUDE_AUTO_RETRY_BASE_DELAY", 15.0),
        max_delay=args.max_delay
        if args.max_delay is not None
        else environment_float("CLAUDE_AUTO_RETRY_MAX_DELAY", 120.0),
        backoff_factor=args.backoff_factor
        if args.backoff_factor is not None
        else environment_float("CLAUDE_AUTO_RETRY_BACKOFF_FACTOR", 2.0),
        jitter=args.jitter
        if args.jitter is not None
        else environment_float("CLAUDE_AUTO_RETRY_JITTER", 3.0),
        max_elapsed=args.max_elapsed
        if args.max_elapsed is not None
        else environment_float("CLAUDE_AUTO_RETRY_MAX_ELAPSED", 1_800.0),
    )
    settings.validate()
    return settings


def _handle_doctor(args: argparse.Namespace) -> int:
    info = inspect_claude(args.claude)
    payload = {
        "executable": info.executable,
        "version": ".".join(str(part) for part in info.version),
        "version_text": info.version_text,
        "supported": info.supported,
        "auth_logged_in": info.auth_logged_in,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Claude CLI: {info.executable}")
        print(f"版本: {payload['version']} ({'支持' if info.supported else '过旧'})")
        if info.auth_logged_in is True:
            auth = "已登录"
        elif info.auth_logged_in is False:
            auth = "未登录"
        else:
            auth = "未知"
        print(f"认证: {auth}")
    return int(ExitCode.SUCCESS if info.supported else ExitCode.CLAUDE_UNAVAILABLE)


def _handle_launch(args: argparse.Namespace) -> int:
    claude_args = _normalise_remainder(args.claude_args)
    if any(item.split("=", 1)[0] in {"-p", "--print"} for item in claude_args):
        raise ConfigError("launch 只用于交互模式；headless 调用请使用 supervise")
    executable = find_claude(args.claude)
    if args.watchdog:
        _version_text, version = read_version(executable)
        if version < WATCHDOG_MINIMUM_VERSION:
            required = ".".join(str(part) for part in WATCHDOG_MINIMUM_VERSION)
            raise ClaudeUnavailableError(f"--watchdog 要求 Claude Code >= {required}")
    native = _native_settings(args, watchdog=args.watchdog)
    environment = native.apply()
    if args.dry_run:
        mode = "watchdog" if args.watchdog else "bounded"
        print(f"配置有效：mode={mode}, max_retries={native.max_retries}, ", end="")
        print(f"api_timeout_ms={native.api_timeout_ms}, claude_args={len(claude_args)}")
        return int(ExitCode.SUCCESS)
    return run_interactive(executable, claude_args, environment)


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None and args.prompt_file is not None:
        raise ConfigError("PROMPT 与 --prompt-file 只能选择一个")
    if args.prompt_file is not None:
        try:
            prompt = args.prompt_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError("无法读取 prompt 文件") from exc
    elif args.prompt is not None:
        prompt = args.prompt
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        raise ConfigError("必须通过 PROMPT、--prompt-file 或 stdin 提供初始 prompt")
    if not prompt.strip():
        raise ConfigError("初始 prompt 不能为空")
    return prompt


def _validate_supervisor_passthrough(arguments: Sequence[str]) -> None:
    for argument in arguments:
        flag = argument.split("=", 1)[0]
        denied_short_form = any(
            flag.startswith(prefix) and len(flag) > len(prefix)
            for prefix in _SUPERVISOR_DENIED_SHORT_PREFIXES
        )
        if flag in _SUPERVISOR_DENIED_FLAGS or denied_short_form:
            raise ConfigError(f"supervise 不允许透传 {flag}，该参数由 supervisor 管理")


def _handle_supervise(args: argparse.Namespace) -> int:
    prompt = _read_prompt(args)
    claude_args = _normalise_remainder(args.claude_args)
    _validate_supervisor_passthrough(claude_args)
    executable = find_claude(args.claude)
    native = _native_settings(args, watchdog=False)
    supervisor_settings = _supervisor_settings(args)
    request = SuperviseRequest(
        executable=executable,
        prompt=prompt,
        cwd=args.cwd.expanduser().resolve(),
        claude_arguments=tuple(claude_args),
        native=native,
        supervisor=supervisor_settings,
        raw_stream=args.raw_stream,
    )
    return int(supervise(request))


def main(argv: Sequence[str] | None = None) -> int:
    _configure_windows_stdio()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.handler(args))
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return int(ExitCode.USAGE)
    except ClaudeUnavailableError as exc:
        print(f"Claude CLI 错误：{exc}", file=sys.stderr)
        return int(ExitCode.CLAUDE_UNAVAILABLE)
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return int(ExitCode.INTERRUPTED)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass
        return int(ExitCode.SUCCESS)
    except Exception:
        # 默认不输出异常对象或 traceback，避免泄露 prompt、session、路径或认证信息。
        print("内部错误：已安全停止，未执行自动恢复。请先运行 doctor。", file=sys.stderr)
        return int(ExitCode.PROTOCOL_FAILURE)


if __name__ == "__main__":
    raise SystemExit(main())
