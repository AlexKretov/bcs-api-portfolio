"""Хранилище токенов BCS Trade API.

Схема авторизации (https://trade-api.bcs.ru/):

* refresh-токен выдаётся в веб-версии «БКС Мир инвестиций», живёт 90 суток,
  привязан к одному брокерскому счёту, показывается один раз;
* access-токен получается из refresh-токена, живёт 24 часа;
* Keycloak при обмене возвращает **новый** refresh-токен (rotation) —
  его нужно сохранять, иначе следующая попытка обмена может упасть с
  ``invalid_grant``.

Файл кэша пишется с правами 0600 и атомарно (во временный файл + ``os.replace``),
чтобы при обрыве не осталось полупустого хранилища.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class TokenSet:
    """Пара токенов, возвращаемая сервисом авторизации."""

    access_token: str
    refresh_token: Optional[str]
    expires_at: float
    refresh_expires_at: Optional[float] = None
    token_type: str = "bearer"
    scope: str = ""
    session_state: str = ""
    #: откуда приехал refresh-токен: файл кэша / конфиг / окружение — для диагностики
    refresh_source: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def access_ttl(self) -> float:
        """Секунд до истечения access-токена (может быть отрицательным)."""
        return self.expires_at - time.time()

    def is_access_valid(self, min_ttl: float = 300.0) -> bool:
        """True, если access-токен годен ещё как минимум ``min_ttl`` секунд."""
        return bool(self.access_token) and self.access_ttl > min_ttl

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "refresh_expires_at": self.refresh_expires_at,
            "token_type": self.token_type,
            "scope": self.scope,
            "session_state": self.session_state,
        }

    @classmethod
    def from_response(cls, data: dict[str, Any], *, fallback_refresh: Optional[str] = None) -> TokenSet:
        """Собрать набор из ответа ``/openid-connect/token``."""
        access = data.get("access_token")
        if not access:
            raise ValueError("в ответе авторизации нет поля access_token")
        now = time.time()
        expires_in = data.get("expires_in")
        refresh_expires_in = data.get("refresh_expires_in")
        return cls(
            access_token=str(access),
            refresh_token=data.get("refresh_token") or fallback_refresh,
            expires_at=now + float(expires_in) if isinstance(expires_in, (int, float)) else now + 24 * 3600,
            refresh_expires_at=now + float(refresh_expires_in)
            if isinstance(refresh_expires_in, (int, float))
            else None,
            token_type=str(data.get("token_type") or "bearer"),
            scope=str(data.get("scope") or ""),
            session_state=str(data.get("session_state") or ""),
            raw=dict(data),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenSet:
        return cls(
            access_token=str(data.get("access_token") or ""),
            refresh_token=data.get("refresh_token"),
            expires_at=float(data.get("expires_at") or 0.0),
            refresh_expires_at=data.get("refresh_expires_at"),
            token_type=str(data.get("token_type") or "bearer"),
            scope=str(data.get("scope") or ""),
            session_state=str(data.get("session_state") or ""),
        )


class TokenStore:
    """Кэш токенов в JSON-файле.

    Parameters
    ----------
    path:
        Путь к файлу кэша. ``None`` — работать только в памяти (полезно,
        когда токен приходит из secret-хранилища и писать на диск нельзя).
    refresh_token_provider:
        Вызывается, когда в кэше нет живого refresh-токена (например,
        кэш повреждён или access-токен просрочили, удалив refresh-токен в ЛК).
    """

    def __init__(
        self,
        path: Optional[os.PathLike[str] | str] = None,
        *,
        refresh_token_provider: Optional[Callable[[], Optional[str]]] = None,
        refresh_source_label: str = "конфиг/окружение",
    ) -> None:
        self.path = Path(path) if path else None
        self._refresh_provider = refresh_token_provider
        self._refresh_source_label = refresh_source_label
        self._tokens: Optional[TokenSet] = None

    # ------------------------------------------------------------------ public

    def get(self) -> Optional[TokenSet]:
        """Текущий набор токенов (память → диск → refresh-токен из окружения)."""
        if self._tokens is None:
            self._tokens = self._read_file()
        if self._tokens is None:
            refresh = self._current_refresh_token()
            if refresh:
                # Это НЕ кэш: файла нет, значение пришло из конфига/окружения.
                self._tokens = TokenSet(
                    access_token="",
                    refresh_token=refresh,
                    expires_at=0.0,
                    refresh_source=self._refresh_source_label,
                )
        return self._tokens

    def refresh_token(self) -> Optional[str]:
        """Актуальный refresh-токен (после rotation — новый из кэша)."""
        tokens = self.get()
        candidate = (tokens.refresh_token if tokens else None) or self._current_refresh_token()
        return candidate or None

    def save(self, tokens: TokenSet) -> None:
        """Сохранить набор токенов (в т.ч. обновлённый refresh-токен)."""
        self._tokens = tokens
        if self.path is None:
            return
        self._write_file(tokens)

    def clear(self) -> None:
        """Забыть кэш: в памяти и на диске."""
        self._tokens = None
        if self.path and self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass

    # ----------------------------------------------------------------- private

    def _current_refresh_token(self) -> Optional[str]:
        return self._refresh_provider() if self._refresh_provider else None

    def _read_file(self) -> Optional[TokenSet]:
        if self.path is None or not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Повреждённый кэш не должен ронять программу — пересоздадим.
            return None
        if not isinstance(data, dict):
            return None
        try:
            tokens = TokenSet.from_dict(data)
        except (TypeError, ValueError):
            return None
        if not tokens.refresh_token:
            tokens.refresh_token = self._current_refresh_token()
            tokens.refresh_source = self._refresh_source_label
        if not tokens.refresh_source:
            tokens.refresh_source = f"файл кэша {self.path}"
        return tokens

    def _write_file(self, tokens: TokenSet) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(tokens.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".bcs-tokens-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except OSError:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            raise
