"""Исключения клиента BCS Trade API и разбор единого формата ошибок БКС.

Сервер отдаёт ошибки в одном виде (см. любой раздел документации):
``{"timestamp", "traceId", "type", "errors": [...], "displayOptions": {...}}``,
где ``type`` ∈ VALIDATION_ERROR, RESOURCE_EXHAUSTED, USER_BLOCKED, BAD_REQUEST,
NOT_FOUND, UNAUTHORIZED, FORBIDDEN, CONFLICT, INTERNAL_SERVER_ERROR,
SESSION_NOT_FOUND_ERROR, SESSION_EXPIRED_ERROR, SESSION_FAILED_ERROR.
"""

from __future__ import annotations

import json
from typing import Any, Optional


class BcsError(Exception):
    """Базовое исключение всех ошибок клиента."""


class ConfigError(BcsError):
    """Отсутствует или некорректна конфигурация (например, refresh-токен)."""


class AuthError(BcsError):
    """Не удалось получить access-токен по refresh-токен.

    Обычно означает, что refresh-токен отозван, истёк (90 суток)
    или выбран не тот ``client_id`` (read/write) относительно выпущенного токена.
    """

    def __init__(self, message: str, *, status: Optional[int] = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


class ApiError(BcsError):
    """Сервер вернул ошибочный HTTP-код на запрос к сервису."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        payload: Any = None,
        trace_id: Optional[str] = None,
        api_type: Optional[str] = None,
        url: Optional[str] = None,
        method: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload
        self.trace_id = trace_id
        self.api_type = api_type
        self.url = url
        self.method = method
        self.retry_after = retry_after

    # ------------------------------------------------------------- factories

    @classmethod
    def from_response(
        cls,
        status: int,
        payload: Any,
        *,
        url: Optional[str] = None,
        method: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> ApiError:
        """Построить ошибку нужного типа из кода статуса и тела ответа."""
        api_type, trace_id, details = _describe_error(payload)
        message = f"HTTP {status}"
        if api_type:
            message += f" {api_type}"
        if method or url:
            message += f": {method or 'GET'} {url or ''}".rstrip()
        if details:
            message += " | " + "; ".join(d for d in details if d)
        kwargs: dict[str, Any] = {
            "status": status,
            "payload": payload,
            "trace_id": trace_id,
            "api_type": api_type or None,
            "url": url,
            "method": method,
        }
        if status == 429:
            return RateLimitError(message, retry_after=retry_after, **kwargs)
        if status in (401, 403):
            return UnauthorizedError(message, **kwargs)
        if status == 400 or api_type == "VALIDATION_ERROR":
            return ValidationError(message, **kwargs)
        return cls(message, **kwargs)

    def __str__(self) -> str:  # pragma: no cover - косметика
        base = super().__str__()
        if self.trace_id:
            base += f" (traceId={self.trace_id})"
        return base


class UnauthorizedError(ApiError):
    """401/403 — токен протух или отозван."""


class RateLimitError(ApiError):
    """429 RESOURCE_EXHAUSTED — превышен лимит запросов (RPS/суточный)."""


class ValidationError(ApiError):
    """400 VALIDATION_ERROR — некорректный запрос."""


def _describe_error(payload: Any) -> tuple[str, Optional[str], list[str]]:
    """Вытащить из тела ошибки БКС тип, traceId и список человекочитаемых проблем."""
    if not isinstance(payload, dict):
        if isinstance(payload, str) and payload.strip():
            return ("", None, [payload.strip()[:500]])
        return ("", None, [])
    trace_id = payload.get("traceId")
    errors = payload.get("errors")
    details: list[str] = []
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                field_name = item.get("field")
                text = item.get("type") or item.get("message") or item.get("code")
                if field_name and text:
                    details.append(f"{field_name}: {text}")
                elif text:
                    details.append(str(text))
                else:
                    details.append(json.dumps(item, ensure_ascii=False))
            else:
                details.append(str(item))
    display = payload.get("displayOptions")
    if isinstance(display, dict) and display.get("text"):
        details.insert(0, str(display["text"]))
    elif payload.get("error_description"):
        details.insert(0, str(payload["error_description"]))
    if not details and isinstance(payload.get("raw"), str) and payload["raw"].strip():
        details.append(payload["raw"].strip()[:300])
    return (str(payload.get("type") or ""), str(trace_id) if trace_id else None, details)
