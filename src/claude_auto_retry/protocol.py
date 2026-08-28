"""Claude Code ``stream-json`` NDJSON 协议解析与状态追踪。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

JsonObject = dict[str, Any]


class ProtocolError(ValueError):
    """一行数据不符合 ``stream-json`` 协议。"""


@dataclass(frozen=True)
class ApiRetryEvent:
    """规范化后的 ``system/api_retry`` 事件。"""

    attempt: int
    max_retries: int
    retry_delay_ms: int
    error_status: int | None
    error: str
    session_id: str
    uuid: str

    @property
    def retry_delay_seconds(self) -> float:
        return self.retry_delay_ms / 1000.0


def parse_ndjson_line(line: str | bytes) -> JsonObject | None:
    """严格解析一行 NDJSON。

    空白行被忽略并返回 ``None``；非 UTF-8、无效 JSON 或非 object JSON 会抛出
    ``ProtocolError``。
    """

    if isinstance(line, bytes):
        try:
            text = line.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"stdout 不是有效 UTF-8: {exc}") from exc
    elif isinstance(line, str):
        text = line
    else:
        raise ProtocolError("NDJSON 行必须是 str 或 bytes")

    if "\ufffd" in text:
        # subprocess 使用 errors="replace" 时也不能让编码损坏静默通过。
        raise ProtocolError("stdout 包含 UTF-8 替换字符")

    text = text.strip()
    if not text:
        return None

    try:
        event = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"无效 JSON（第 {exc.lineno} 行第 {exc.colno} 列）") from exc

    if not isinstance(event, dict):
        raise ProtocolError("NDJSON 事件必须是 JSON object")
    return event


class StreamJsonProtocol:
    """增量解析并追踪单次 Claude Code headless 运行。

    解析错误不会中断 stdout 排空，而是锁存 ``stream_corrupted``。调用方应在进程
    结束后调用 ``finish``；缺少终态 ``result`` 同样视为协议损坏。
    """

    _KNOWN_RESULT_SUBTYPES = frozenset(
        {
            "success",
            "error_during_execution",
            "error_max_turns",
            "error_max_budget_usd",
            "error_max_structured_output_retries",
        }
    )

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.result: JsonObject | None = None
        self.api_retries: list[ApiRetryEvent] = []
        self.error_status: int | None = None
        self.error_type: str | None = None
        self.error_messages: list[str] = []
        self.stream_corrupted = False
        self.corruption_reasons: list[str] = []
        self.event_count = 0
        self.saw_init = False
        self.saw_result = False
        self.finished = False

    @property
    def is_corrupted(self) -> bool:
        return self.stream_corrupted

    @property
    def stream_damaged(self) -> bool:
        return self.stream_corrupted

    @property
    def is_complete(self) -> bool:
        return self.finished and self.saw_result and not self.stream_corrupted

    @property
    def result_is_error(self) -> bool | None:
        if self.result is None:
            return None
        value = self.result.get("is_error")
        return value if isinstance(value, bool) else None

    @property
    def result_text(self) -> str | None:
        if self.result is None:
            return None
        value = self.result.get("result")
        return value if isinstance(value, str) else None

    @property
    def error_text(self) -> str | None:
        return "\n".join(self.error_messages) if self.error_messages else None

    @property
    def last_api_retry(self) -> ApiRetryEvent | None:
        return self.api_retries[-1] if self.api_retries else None

    @property
    def retry_after_seconds(self) -> float | None:
        event = self.last_api_retry
        return event.retry_delay_seconds if event is not None else None

    def _corrupt(self, reason: str) -> None:
        self.stream_corrupted = True
        if reason not in self.corruption_reasons:
            self.corruption_reasons.append(reason)

    def _append_error_message(self, message: object) -> None:
        if isinstance(message, str) and message.strip():
            normalized = message.strip()
            if normalized not in self.error_messages:
                self.error_messages.append(normalized)

    @staticmethod
    def _valid_int(value: object, *, minimum: int = 0) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= minimum

    @classmethod
    def _valid_status(cls, value: object) -> bool:
        return cls._valid_int(value, minimum=100) and int(value) <= 599

    def _track_session(self, event: Mapping[str, Any], *, required: bool) -> None:
        value = event.get("session_id")
        if value is None:
            if required:
                self._corrupt("missing_session_id")
            return
        if not isinstance(value, str) or not value:
            self._corrupt("invalid_session_id")
            return
        if self.session_id is None:
            self.session_id = value
        elif value != self.session_id:
            self._corrupt("session_id_mismatch")

    def _observe_api_retry(self, event: Mapping[str, Any]) -> None:
        self._track_session(event, required=True)
        attempt = event.get("attempt")
        max_retries = event.get("max_retries")
        retry_delay_ms = event.get("retry_delay_ms")
        error_status = event.get("error_status")
        error = event.get("error")
        session_id = event.get("session_id")
        uuid = event.get("uuid")

        valid = True
        if not self._valid_int(attempt, minimum=1):
            self._corrupt("invalid_api_retry_attempt")
            valid = False
        if not self._valid_int(max_retries, minimum=0):
            self._corrupt("invalid_api_retry_max_retries")
            valid = False
        if not self._valid_int(retry_delay_ms, minimum=0):
            self._corrupt("invalid_api_retry_delay")
            valid = False
        if error_status is not None and not self._valid_status(error_status):
            self._corrupt("invalid_api_retry_status")
            valid = False
        if not isinstance(error, str) or not error:
            self._corrupt("invalid_api_retry_error")
            valid = False
        if not isinstance(session_id, str) or not session_id:
            valid = False
        if not isinstance(uuid, str) or not uuid:
            self._corrupt("invalid_api_retry_uuid")
            valid = False

        if not valid:
            return

        normalized = ApiRetryEvent(
            attempt=int(attempt),
            max_retries=int(max_retries),
            retry_delay_ms=int(retry_delay_ms),
            error_status=int(error_status) if error_status is not None else None,
            error=error,
            session_id=session_id,
            uuid=uuid,
        )
        self.api_retries.append(normalized)
        self.error_status = normalized.error_status
        self.error_type = normalized.error
        self._append_error_message(normalized.error)

    def _observe_assistant(self, event: Mapping[str, Any]) -> None:
        self._track_session(event, required=True)
        raw_error = event.get("error")
        has_api_error_evidence = False
        if raw_error is not None:
            if isinstance(raw_error, str) and raw_error:
                self.error_type = raw_error
                self._append_error_message(raw_error)
                has_api_error_evidence = True
            else:
                self._corrupt("invalid_assistant_error")

        # Assistant API 错误使用 camelCase；result 事件则使用 snake_case。
        raw_status = event.get("apiErrorStatus")
        if raw_status is None:
            raw_status = event.get("api_error_status")
        if raw_status is not None:
            if self._valid_status(raw_status):
                self.error_status = int(raw_status)
                has_api_error_evidence = True
            else:
                self._corrupt("invalid_assistant_api_status")

        for flag_name in ("isApiErrorMessage", "is_api_error_message", "is_error"):
            if flag_name not in event:
                continue
            flag = event.get(flag_name)
            if isinstance(flag, bool):
                has_api_error_evidence = has_api_error_evidence or flag
            else:
                self._corrupt("invalid_assistant_error_flag")

        message = event.get("message")
        if has_api_error_evidence and isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, Mapping) and block.get("type") == "text":
                        self._append_error_message(block.get("text"))

    def _observe_rate_limit(self, event: Mapping[str, Any]) -> None:
        self._track_session(event, required=True)
        info = event.get("rate_limit_info")
        if not isinstance(info, Mapping):
            self._corrupt("invalid_rate_limit_info")
            return

        status = info.get("status")
        if status not in {"allowed", "allowed_warning", "rejected"}:
            self._corrupt("invalid_rate_limit_status")
            return
        if status != "rejected":
            return

        self.error_status = 429
        self.error_type = "rate_limit"
        # 当前官方 schema 允许 rejected 时不携带 bucket；拒绝本身就代表 usage
        # window 不允许继续，必须按永久限制处理，不能当作容量型 429。
        self._append_error_message("usage limit rejected")
        rate_limit_type = info.get("rateLimitType", info.get("rate_limit_type"))
        if isinstance(rate_limit_type, str) and rate_limit_type:
            # rejected 的这些类型均表示账户 usage window，而非瞬时服务端节流。
            self._append_error_message(f"usage limit {rate_limit_type}")
        elif rate_limit_type is not None:
            self._corrupt("invalid_rate_limit_type")

        disabled_reason = info.get("overageDisabledReason", info.get("overage_disabled_reason"))
        if isinstance(disabled_reason, str) and disabled_reason:
            self._append_error_message(disabled_reason)
        elif disabled_reason is not None:
            self._corrupt("invalid_overage_disabled_reason")

    def _observe_result(self, event: Mapping[str, Any]) -> None:
        if self.saw_result:
            self._corrupt("duplicate_result")
            return

        self.saw_result = True
        self._track_session(event, required=True)
        subtype = event.get("subtype")
        is_error = event.get("is_error")

        if not isinstance(subtype, str) or subtype not in self._KNOWN_RESULT_SUBTYPES:
            self._corrupt("invalid_result_subtype")
        if not isinstance(is_error, bool):
            self._corrupt("invalid_result_is_error")
        if isinstance(subtype, str) and subtype.startswith("error_") and is_error is not True:
            self._corrupt("inconsistent_result_error_state")
        if is_error is True and isinstance(subtype, str) and subtype.startswith("error_"):
            # loop 级终态必须覆盖此前 api_retry 的临时分类，避免陈旧信号触发 resume。
            self.error_type = subtype

        terminal_reason = event.get("terminal_reason")
        if terminal_reason is not None and not isinstance(terminal_reason, str):
            self._corrupt("invalid_terminal_reason")
        elif is_error is True and terminal_reason in {"aborted_streaming", "aborted_tools"}:
            self.error_type = terminal_reason

        raw_status = event.get("api_error_status")
        if raw_status is not None:
            if self._valid_status(raw_status):
                self.error_status = int(raw_status)
            else:
                self._corrupt("invalid_result_api_status")
        if is_error is False and raw_status is not None:
            self._corrupt("success_result_has_api_error_status")

        raw_result = event.get("result")
        if raw_result is not None and not isinstance(raw_result, str):
            self._corrupt("invalid_result_text")
        if is_error is True:
            self._append_error_message(raw_result)

        raw_errors = event.get("errors")
        if raw_errors is not None:
            if not isinstance(raw_errors, list) or not all(
                isinstance(item, str) for item in raw_errors
            ):
                self._corrupt("invalid_result_errors")
            else:
                for item in raw_errors:
                    self._append_error_message(item)

        raw_error_type = event.get("error")
        if raw_error_type is not None:
            if isinstance(raw_error_type, str) and raw_error_type:
                self.error_type = raw_error_type
                self._append_error_message(raw_error_type)
            else:
                self._corrupt("invalid_result_error_type")

        self.result = dict(event)

    def observe_event(self, event: Mapping[str, Any]) -> JsonObject:
        """处理一个已解析事件，并返回普通 ``dict`` 副本。"""

        if self.finished:
            self._corrupt("event_after_finish")
        normalized = dict(event)
        event_type = normalized.get("type")
        if not isinstance(event_type, str) or not event_type:
            self._corrupt("missing_event_type")
            return normalized

        if self.saw_result:
            self._corrupt("event_after_result")

        self.event_count += 1
        if event_type == "result":
            self._observe_result(normalized)
        elif event_type == "system":
            subtype = normalized.get("subtype")
            if not isinstance(subtype, str) or not subtype:
                self._corrupt("invalid_system_subtype")
            elif subtype == "init":
                self.saw_init = True
                self._track_session(normalized, required=True)
            elif subtype == "api_retry":
                self._observe_api_retry(normalized)
            else:
                self._track_session(normalized, required=False)
        elif event_type == "assistant":
            self._observe_assistant(normalized)
        elif event_type == "rate_limit_event":
            self._observe_rate_limit(normalized)
        else:
            # 未知事件类型允许前向兼容，但若带 session_id，仍检查一致性。
            self._track_session(normalized, required=False)

        return normalized

    def feed_line(self, line: str | bytes) -> JsonObject | None:
        """解析并处理一行；损坏时锁存状态并继续接收后续输出。"""

        try:
            event = parse_ndjson_line(line)
        except ProtocolError as exc:
            self._corrupt(str(exc))
            return None
        if event is None:
            return None
        return self.observe_event(event)

    def feed_lines(self, lines: Iterable[str | bytes]) -> list[JsonObject]:
        """处理多行并返回成功解析的事件。"""

        events: list[JsonObject] = []
        for line in lines:
            event = self.feed_line(line)
            if event is not None:
                events.append(event)
        return events

    def finish(self) -> StreamJsonProtocol:
        """标记 stdout 结束，并完成终态一致性检查。"""

        if self.finished:
            return self
        self.finished = True
        if not self.saw_result:
            self._corrupt("missing_result")
        if self.saw_result and self.session_id is None:
            self._corrupt("missing_session_id")
        return self


def parse_stream(lines: Iterable[str | bytes]) -> StreamJsonProtocol:
    """一次性解析完整 NDJSON 流。"""

    protocol = StreamJsonProtocol()
    protocol.feed_lines(lines)
    return protocol.finish()


# 调用方可按 parser/state 语义选用同一个实现。
StreamJsonParser = StreamJsonProtocol
StreamJsonState = StreamJsonProtocol


__all__ = [
    "ApiRetryEvent",
    "JsonObject",
    "ProtocolError",
    "StreamJsonParser",
    "StreamJsonProtocol",
    "StreamJsonState",
    "parse_ndjson_line",
    "parse_stream",
]
