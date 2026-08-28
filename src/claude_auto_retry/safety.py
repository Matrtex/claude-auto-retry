"""重试前的工具副作用屏障。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

READ_ONLY_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "WebSearch",
        "WebFetch",
        "ToolSearch",
    }
)


def is_read_only_tool(name: object) -> bool:
    """仅精确匹配审核过的内置只读工具。"""

    return isinstance(name, str) and name in READ_ONLY_TOOLS


@dataclass(frozen=True)
class ToolUse:
    """从流中观察到的一次工具调用。"""

    name: str | None
    tool_use_id: str | None
    read_only: bool
    source: str


class SideEffectTracker:
    """锁存工具副作用。

    一旦出现非 allowlist 工具、未知工具、缺失名称或矛盾的工具记录，
    ``side_effect_detected`` 永久保持为真。创建新 tracker 才能开始新的运行。
    """

    def __init__(self) -> None:
        self.tool_uses: list[ToolUse] = []
        self.side_effect_detected = False
        self.first_side_effect: ToolUse | None = None
        self.barrier_reason: str | None = None
        self._tools_by_id: dict[str, ToolUse] = {}

    @property
    def safe_to_retry(self) -> bool:
        return not self.side_effect_detected

    @property
    def has_side_effects(self) -> bool:
        return self.side_effect_detected

    def _latch(self, tool_use: ToolUse, reason: str) -> None:
        self.side_effect_detected = True
        if self.first_side_effect is None:
            self.first_side_effect = tool_use
            self.barrier_reason = reason

    def observe_tool_use(
        self,
        name: object,
        *,
        tool_use_id: object = None,
        source: str = "event",
    ) -> ToolUse | None:
        """记录工具调用；相同 ``tool_use_id`` 的重复流事件会被去重。"""

        normalized_name = name if isinstance(name, str) and name else None
        normalized_id = tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None
        read_only = is_read_only_tool(normalized_name)
        observed = ToolUse(normalized_name, normalized_id, read_only, source)

        if normalized_id is not None and normalized_id in self._tools_by_id:
            previous = self._tools_by_id[normalized_id]
            if previous.name != normalized_name:
                # 同一 ID 对应不同名称意味着流无法可靠解释，必须锁存屏障。
                self._latch(observed, "conflicting_tool_name")
            return None

        if normalized_id is not None:
            self._tools_by_id[normalized_id] = observed
        self.tool_uses.append(observed)

        if normalized_name is None:
            self._latch(observed, "unknown_tool")
        elif not read_only:
            self._latch(observed, "non_read_only_tool")

        return observed

    def _observe_content_blocks(
        self,
        content: object,
        *,
        source: str,
    ) -> list[ToolUse]:
        observed: list[ToolUse] = []
        if not isinstance(content, list):
            malformed = self.observe_tool_use(None, source=f"{source}:malformed_content")
            if malformed is not None:
                observed.append(malformed)
            return observed

        for block in content:
            if not isinstance(block, Mapping):
                malformed = self.observe_tool_use(None, source=f"{source}:malformed_block")
                if malformed is not None:
                    observed.append(malformed)
                continue
            block_type = block.get("type")
            if block_type not in {"tool_use", "server_tool_use"}:
                if isinstance(block_type, str) and block_type.endswith("tool_use"):
                    malformed = self.observe_tool_use(
                        None,
                        source=f"{source}:unknown_tool_block",
                    )
                    if malformed is not None:
                        observed.append(malformed)
                continue
            tool_use = self.observe_tool_use(
                block.get("name"),
                tool_use_id=block.get("id"),
                source=source,
            )
            if tool_use is not None:
                observed.append(tool_use)
        return observed

    def _observe_content_block_start(
        self,
        event: Mapping[str, Any],
        *,
        source: str,
    ) -> list[ToolUse]:
        block = event.get("content_block")
        if not isinstance(block, Mapping):
            malformed = self.observe_tool_use(None, source=f"{source}:malformed_block")
            return [malformed] if malformed is not None else []
        block_type = block.get("type")
        if block_type not in {"tool_use", "server_tool_use"}:
            if isinstance(block_type, str) and block_type.endswith("tool_use"):
                malformed = self.observe_tool_use(
                    None,
                    source=f"{source}:unknown_tool_block",
                )
                return [malformed] if malformed is not None else []
            return []
        tool_use = self.observe_tool_use(
            block.get("name"),
            tool_use_id=block.get("id"),
            source=source,
        )
        return [tool_use] if tool_use is not None else []

    def observe_event(self, event: object) -> tuple[ToolUse, ...]:
        """检查一条 Claude Code ``stream-json`` 事件中的工具调用。"""

        if not isinstance(event, Mapping):
            malformed = self.observe_tool_use(None, source="malformed_event")
            return (malformed,) if malformed is not None else ()

        event_type = event.get("type")
        observed: list[ToolUse] = []

        if event_type == "assistant":
            message = event.get("message")
            if not isinstance(message, Mapping):
                malformed = self.observe_tool_use(None, source="assistant:malformed_message")
                return (malformed,) if malformed is not None else ()
            observed.extend(
                self._observe_content_blocks(message.get("content"), source="assistant")
            )
        elif event_type == "message":
            # 某些版本会转发原始 API message；只检查 assistant 角色，避免解析
            # user/tool_result 内可能包含的普通 JSON 文本。
            message = event.get("message", event)
            if isinstance(message, Mapping) and message.get("role") == "assistant":
                observed.extend(
                    self._observe_content_blocks(message.get("content"), source="message")
                )
        elif event_type == "stream_event":
            inner_event = event.get("event")
            if not isinstance(inner_event, Mapping):
                malformed = self.observe_tool_use(None, source="stream_event:malformed_event")
                return (malformed,) if malformed is not None else ()
            if inner_event.get("type") == "content_block_start":
                observed.extend(
                    self._observe_content_block_start(inner_event, source="stream_event")
                )
        elif event_type == "content_block_start":
            observed.extend(self._observe_content_block_start(event, source="content_block_start"))
        elif event_type == "tool_progress":
            tool_use = self.observe_tool_use(
                event.get("tool_name"),
                tool_use_id=event.get("tool_use_id"),
                source="tool_progress",
            )
            if tool_use is not None:
                observed.append(tool_use)
        elif event_type == "system":
            subtype = event.get("subtype")
            if isinstance(subtype, str) and subtype.startswith("hook_"):
                tool_use = self.observe_tool_use(
                    "Hook",
                    tool_use_id=event.get("hook_id"),
                    source=subtype,
                )
                if tool_use is not None:
                    observed.append(tool_use)
            elif subtype == "files_persisted":
                tool_use = self.observe_tool_use(
                    "FilesPersisted",
                    source="system:files_persisted",
                )
                if tool_use is not None:
                    observed.append(tool_use)
        elif event_type == "result":
            origin = event.get("origin")
            if isinstance(origin, Mapping) and origin.get("kind") == "task-notification":
                tool_use = self.observe_tool_use(
                    "BackgroundTask",
                    source="result:task_notification",
                )
                if tool_use is not None:
                    observed.append(tool_use)
            elif subtype in {"task_started", "task_notification"}:
                tool_use = self.observe_tool_use(
                    "BackgroundTask",
                    tool_use_id=event.get("task_id"),
                    source=f"system:{subtype}",
                )
                if tool_use is not None:
                    observed.append(tool_use)

        return tuple(observed)


# 更短的公共名称适合 supervisor 持有。
SafetyTracker = SideEffectTracker


__all__ = [
    "READ_ONLY_TOOLS",
    "SafetyTracker",
    "SideEffectTracker",
    "ToolUse",
    "is_read_only_tool",
]
