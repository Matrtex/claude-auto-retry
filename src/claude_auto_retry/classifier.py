"""Claude Code 失败分类。

本模块采用 allowlist：只有证据明确落入临时故障集合时才允许重试。任何永久
错误证据都优先于临时错误证据，未知情况默认停止。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class FailureKind(str, Enum):
    """失败的稳定分类。"""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


# 对外同时提供更贴近日常用语的名称。
FailureClass = FailureKind


@dataclass(frozen=True)
class Classification:
    """一次失败分类的结果。"""

    kind: FailureKind
    reason: str
    status_code: int | None = None
    error_type: str | None = None

    @property
    def retryable(self) -> bool:
        return self.kind is FailureKind.TRANSIENT

    @property
    def category(self) -> FailureKind:
        """``kind`` 的只读同义属性，便于日志和旧调用方使用。"""

        return self.kind

    def __bool__(self) -> bool:
        """布尔值只表示是否允许重试。"""

        return self.retryable


_PERMANENT_ERROR_TYPES = frozenset(
    {
        "authentication_failed",
        "oauth_org_not_allowed",
        "billing_error",
        "invalid_request",
        "model_not_found",
        "max_output_tokens",
        "permission_error",
        "permission_denied",
        "policy_error",
        "safety_error",
        "content_filter",
        "refusal",
        "request_too_large",
        "error_during_execution",
        "error_max_turns",
        "error_max_budget_usd",
        "error_max_structured_output_retries",
        "aborted_streaming",
        "aborted_tools",
        "interrupted",
        "cancelled",
    }
)

_TRANSIENT_ERROR_TYPES = frozenset(
    {
        "overloaded",
        "overloaded_error",
        "server_error",
    }
)

_RATE_LIMIT_ERROR_TYPES = frozenset({"rate_limit", "rate_limited"})

_STATUS_PATTERN = re.compile(
    r"\b(?:http(?:\s+status)?|status(?:\s+code)?|api\s+error)\s*[:=]?\s*([1-5]\d{2})\b",
    re.IGNORECASE,
)

# 这些信号一旦出现就停止，即使同一段文本中也出现 429、529 或 server_error。
_PERMANENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "explicit_do_not_retry",
        re.compile(r"\bx-should-retry\s*:\s*false\b", re.IGNORECASE),
    ),
    (
        "user_interrupted",
        re.compile(
            r"\b(?:sigint|keyboardinterrupt|interrupted\s+by\s+(?:the\s+)?user|"
            r"user\s+(?:interrupt(?:ed)?|cancel(?:l?ed)?|aborted)|"
            r"cancel(?:l?ed)?\s+by\s+(?:the\s+)?user)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "policy_or_refusal",
        re.compile(
            r"\b(?:policy\s+violation|blocked\s+by\s+(?:a\s+)?(?:safety|policy)|"
            r"safety\s+(?:policy|filter)|content\s+(?:policy|filter)|"
            r"request\s+was\s+refused|model\s+refused|refusal)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "billing_or_credits",
        re.compile(
            r"\b(?:billing(?:\s+error)?|payment\s+required|insufficient\s+"
            r"(?:credits?|funds?)|credit\s+balance|out\s+of\s+(?:credits?|extra\s+usage)|"
            r"extra\s+usage\s+(?:is\s+)?(?:disabled|exhausted))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "quota_or_usage_limit",
        re.compile(
            r"\b(?:quota(?:\s+(?:exceeded|exhausted|limit))?|usage\s+limit|"
            r"spend(?:ing)?\s+limit|plan\s+limit|(?:daily|weekly|monthly|five[-\s]?hour)\s+limit|"
            r"usage\s+(?:cap|quota)|overage(?:\s+status)?\s*[:=]?\s*rejected|"
            r"out_of_credits)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "authentication_or_permission",
        re.compile(
            r"\b(?:authentication\s+failed|unauthori[sz]ed|invalid\s+(?:api\s+)?key|"
            r"api\s+key\s+(?:is\s+)?invalid|oauth(?:\s+organization)?\s+not\s+allowed|"
            r"forbidden|permission\s+denied|access\s+denied)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "invalid_request_or_model",
        re.compile(
            r"\b(?:invalid[_\s-]+request|model[_\s-]+not[_\s-]+found|"
            r"model\s+does\s+not\s+exist|request\s+(?:entity\s+)?too\s+large|"
            r"prompt\s+is\s+too\s+long|context\s+(?:length|window).*(?:exceed|too\s+long)|"
            r"max(?:imum)?[_\s-]+output[_\s-]+tokens)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "tls_certificate",
        re.compile(
            r"\b(?:certificate\s+verify\s+failed|certificate\s+has\s+expired|"
            r"hostname\s+mismatch|self[-\s]?signed\s+certificate|"
            r"unable\s+to\s+get\s+(?:local\s+)?issuer\s+certificate|"
            r"unknown\s+certificate\s+authority)\b",
            re.IGNORECASE,
        ),
    ),
)

_TRANSIENT_NETWORK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "network_timeout",
        re.compile(
            r"\b(?:etimedout|esockettimedout|network\s+timeout|connection\s+timed\s+out|"
            r"request\s+timed\s+out|socket\s+timeout|timeout\s+while\s+connecting)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "connection_reset",
        re.compile(
            r"\b(?:econnreset|econnaborted|epipe|connection\s+(?:was\s+)?reset|"
            r"connection\s+aborted|broken\s+pipe|socket\s+hang\s+up|"
            r"remote\s+(?:host|end)\s+closed\s+(?:the\s+)?connection)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "dns_transient",
        re.compile(
            r"\b(?:eai_again|temporary\s+failure\s+in\s+name\s+resolution|"
            r"dns\s+(?:lookup\s+)?(?:timed\s+out|temporarily\s+failed|error)|"
            r"getaddrinfo\s+(?:eai_again|temporary\s+failure))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "tls_transient",
        re.compile(
            r"\b(?:(?:tls|ssl)\s+handshake\s+(?:timed\s+out|timeout|was\s+reset)|"
            r"ssl_error_syscall|tls\s+connection\s+(?:was\s+)?reset)\b",
            re.IGNORECASE,
        ),
    ),
)

_OVERLOADED_PATTERN = re.compile(
    r"\b(?:overloaded(?:_error)?|server\s+overloaded)\b", re.IGNORECASE
)
_SERVER_ERROR_PATTERN = re.compile(r"\b(?:server_error|internal\s+server\s+error)\b", re.IGNORECASE)
_RATE_LIMIT_PATTERN = re.compile(
    r"\b(?:rate[_\s-]+limit(?:ed|ing)?|too\s+many\s+requests)\b", re.IGNORECASE
)
_NEGATED_LIMIT_PATTERN = re.compile(
    r"\bnot\s+(?:(?:your|an?|the)\s+)?(?:usage\s+limit|quota|billing(?:\s+issue)?|"
    r"out\s+of\s+credits?)\b",
    re.IGNORECASE,
)


def _normalize_error_type(error_type: str | None) -> str | None:
    if not isinstance(error_type, str):
        return None
    normalized = error_type.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def _normalize_status(status_code: int | None, text: str) -> int | None:
    if isinstance(status_code, bool):
        return None
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        return status_code
    match = _STATUS_PATTERN.search(text)
    return int(match.group(1)) if match else None


def _join_messages(
    message: str | None,
    stderr: str | None,
    extra_messages: Iterable[str],
) -> str:
    parts: list[str] = []
    for candidate in (message, stderr, *extra_messages):
        if isinstance(candidate, str) and candidate.strip():
            parts.append(candidate.strip())
    return "\n".join(parts)


def classify_failure(
    message: str | None = None,
    *,
    status_code: int | None = None,
    error_type: str | None = None,
    stderr: str | None = None,
    extra_messages: Iterable[str] = (),
) -> Classification:
    """根据明确证据判断一次失败是否允许重试。

    判断顺序固定为永久错误、永久 HTTP 状态、明确临时错误。没有足够证据时返回
    ``UNKNOWN``，其 ``retryable`` 为 ``False``。
    """

    text = _join_messages(message, stderr, extra_messages)
    normalized_type = _normalize_error_type(error_type)
    normalized_status = _normalize_status(status_code, text)
    # Anthropic 的临时 429 文案明确包含 “not your usage limit”；先屏蔽否定短语，
    # 但保留同一错误中其他真实 billing/quota 证据。
    permanent_evidence_text = _NEGATED_LIMIT_PATTERN.sub("", text)

    if normalized_type in _PERMANENT_ERROR_TYPES:
        return Classification(
            FailureKind.PERMANENT,
            "permanent_error_type",
            normalized_status,
            normalized_type,
        )

    for reason, pattern in _PERMANENT_PATTERNS:
        if pattern.search(permanent_evidence_text):
            return Classification(
                FailureKind.PERMANENT,
                reason,
                normalized_status,
                normalized_type,
            )

    # 429 是唯一有条件允许重试的 4xx；其他 4xx 都按永久失败处理。
    if normalized_status is not None and 400 <= normalized_status <= 499:
        if normalized_status != 429:
            return Classification(
                FailureKind.PERMANENT,
                "permanent_http_status",
                normalized_status,
                normalized_type,
            )
        return Classification(
            FailureKind.TRANSIENT,
            "rate_limit",
            normalized_status,
            normalized_type,
        )

    if normalized_status is not None and 500 <= normalized_status <= 599:
        reason = "overloaded" if normalized_status == 529 else "server_http_status"
        return Classification(
            FailureKind.TRANSIENT,
            reason,
            normalized_status,
            normalized_type,
        )

    if normalized_type in _TRANSIENT_ERROR_TYPES:
        return Classification(
            FailureKind.TRANSIENT,
            normalized_type,
            normalized_status,
            normalized_type,
        )

    if normalized_type in _RATE_LIMIT_ERROR_TYPES:
        return Classification(
            FailureKind.TRANSIENT,
            "rate_limit",
            normalized_status,
            normalized_type,
        )

    if _OVERLOADED_PATTERN.search(text):
        return Classification(
            FailureKind.TRANSIENT,
            "overloaded",
            normalized_status,
            normalized_type,
        )

    if _SERVER_ERROR_PATTERN.search(text):
        return Classification(
            FailureKind.TRANSIENT,
            "server_error",
            normalized_status,
            normalized_type,
        )

    if _RATE_LIMIT_PATTERN.search(text):
        return Classification(
            FailureKind.TRANSIENT,
            "rate_limit",
            normalized_status,
            normalized_type,
        )

    # 网络文本只在确实没有 HTTP 响应时有效，避免用模糊 timeout 覆盖 4xx。
    if normalized_status is None:
        for reason, pattern in _TRANSIENT_NETWORK_PATTERNS:
            if pattern.search(text):
                return Classification(
                    FailureKind.TRANSIENT,
                    reason,
                    None,
                    normalized_type,
                )

    return Classification(
        FailureKind.UNKNOWN,
        "insufficient_evidence",
        normalized_status,
        normalized_type,
    )


def classify_result(
    result: Mapping[str, Any],
    *,
    stderr: str | None = None,
    extra_messages: Iterable[str] = (),
) -> Classification:
    """直接分类 Claude Code ``result`` 事件。"""

    raw_errors = result.get("errors")
    errors = (
        tuple(item for item in raw_errors if isinstance(item, str))
        if isinstance(raw_errors, list)
        else ()
    )
    raw_status = result.get("api_error_status")
    status = (
        raw_status if isinstance(raw_status, int) and not isinstance(raw_status, bool) else None
    )
    raw_error_type = result.get("error")
    error_type = raw_error_type if isinstance(raw_error_type, str) else None
    raw_message = result.get("result")
    message = raw_message if isinstance(raw_message, str) else None
    return classify_failure(
        message,
        status_code=status,
        error_type=error_type,
        stderr=stderr,
        extra_messages=(*errors, *extra_messages),
    )


def is_retryable_failure(*args: Any, **kwargs: Any) -> bool:
    """返回 ``classify_failure`` 的布尔结论。"""

    return classify_failure(*args, **kwargs).retryable


# 常见命名的兼容入口。
classify_error = classify_failure


__all__ = [
    "Classification",
    "FailureClass",
    "FailureKind",
    "classify_error",
    "classify_failure",
    "classify_result",
    "is_retryable_failure",
]
