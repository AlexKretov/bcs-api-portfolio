"""Транспортный слой: авторизация, rate-limit, ретраи, разбор ошибок BCS."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable, Optional
from urllib.parse import urljoin

import requests

from .errors import ApiError, AuthError
from .tokens import TokenSet, TokenStore

log = logging.getLogger("bcs.http")

#: Ограничения по https://trade-api.bcs.ru/restrictions/ — 10 RPS на сервис HTTP.
DEFAULT_RPS = 10.0
#: «Неторговые операции» — 3 RPS.
NONTRADE_RPS = 3.0

AUTH_URL = "https://be.broker.ru/trade-api-keycloak/realms/tradeapi/protocol/openid-connect/token"
CLIENT_IDS = ("trade-api-read", "trade-api-write")


class TokenBucket:
    """Токен-бакет: не более ``rate`` запросов в секунду, потокобезопасно."""

    def __init__(self, rate: float, capacity: Optional[float] = None) -> None:
        self.rate = max(float(rate), 0.1)
        self.capacity = float(capacity if capacity is not None else max(1.0, rate))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def next_delay(self) -> float:
        """Сколько секунд нужно подождать, чтобы взять разрешение (0 — можно сразу)."""
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
            self._updated = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            return (1.0 - self._tokens) / self.rate

    def acquire(self) -> None:
        while True:
            delay = self.next_delay()
            if delay <= 0:
                return
            time.sleep(delay)


class BcsHttp:
    """HTTP-обёртка над BCS Trade API.

    Делает всё, что должно быть в аккуратном клиенте:

    * обмен refresh-токена на access-токен, авто-обновление (TTL 24 ч) и
      сохранение **повёрнутого** Keycloak refresh-токена;
    * соблюдение RPS-лимита (10 RPS; 3 RPS для неторговых операций);
    * ретраи на 429/5xx/сетевые ошибки с экспоненциальной задержкой и
      уважением ``Retry-After``;
    * единый разбор формата ошибок БКС в типизированные исключения.
    """

    def __init__(
        self,
        *,
        store: TokenStore,
        session: Optional[requests.Session] = None,
        auth_url: str = AUTH_URL,
        client_id: str = "trade-api-read",
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        rps: float = DEFAULT_RPS,
        sleep: Callable[[float], None] = time.sleep,
        configured_refresh: Optional[Callable[[], Optional[str]]] = None,
        token_source: str = "",
    ) -> None:
        if client_id not in CLIENT_IDS:
            raise ValueError(f"client_id должен быть одним из {CLIENT_IDS}")
        self.store = store
        self.session = session or requests.Session()
        self.auth_url = auth_url
        self.client_id = client_id
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._bucket = TokenBucket(rps)
        self._slow_bucket = TokenBucket(NONTRADE_RPS)
        self._auth_lock = threading.Lock()
        self._configured_refresh = configured_refresh or (lambda: None)
        self.token_source = token_source
        self._sleep = sleep
        self._forced = False
        self.request_count = 0

    # ------------------------------------------------------------------ auth

    def invalidate_cache(self) -> None:
        """Забыть, что access-токен свежий: следующий запрос перевыпустит его."""
        self._forced = True

    @property
    def token_set(self) -> Optional[TokenSet]:
        return self.store.get()

    def authenticate(self, *, force: bool = False) -> TokenSet:
        """Вернуть живые токены, при необходимости — обновить пару access/refresh."""
        tokens = self.store.get()
        if not force and tokens and tokens.is_access_valid():
            return tokens

        with self._auth_lock:
            tokens = self.store.get()
            # Поток, шедший до нас, мог уже обновить токен.
            if not force and tokens and tokens.is_access_valid():
                return tokens

            candidates = self._refresh_candidates(tokens)
            if not candidates:
                raise AuthError(
                    "нет refresh-токена. Получите его в веб-версии «БКС Мир инвестиций»: "
                    "Профиль → «Счета и тарифы» → ваш счёт → «Токены API» → «Выпустить токен», "
                    "затем задайте переменную окружения BCS_REFRESH_TOKEN "
                    f"(или поле refresh_token в {self._config_hint()})"
                )

            last_error: Optional[AuthError] = None
            for index, (token, origin) in enumerate(candidates):
                try:
                    data = self._token_exchange(token)
                except AuthError as exc:
                    last_error = exc
                    # Кэш может хранить повёрнутый (или, наоборот, устаревший) refresh-токен.
                    # Пробуем следующий кандидат, прежде чем жаловаться пользователю.
                    if index + 1 < len(candidates):
                        log.warning(
                            "обмен по токену из %s не удался (%s) — пробую значение из %s",
                            origin,
                            exc,
                            candidates[index + 1][1],
                        )
                        continue
                    raise self._with_diagnostics(exc, candidates) from exc
                new_tokens = TokenSet.from_response(data, fallback_refresh=token)
                self.store.save(new_tokens)
                self._forced = False
                if index > 0:
                    log.info("пара токенов обновлена значением из %s (кэш оказался негодным)", origin)
                log.info(
                    "access-токен получен (client_id=%s, хватит на %.1f ч)",
                    self.client_id,
                    new_tokens.access_ttl / 3600,
                )
                return new_tokens
            raise AuthError(str(last_error) if last_error else "не удалось получить access-токен")

    def _refresh_candidates(self, tokens: Optional[TokenSet]) -> list[tuple[str, str]]:
        """Живые кандидаты refresh-токена: сначала кэш (в нём повёрнутое значение), затем конфиг.

        Порядок важен: Keycloak при обмене может выдать новый refresh-токен, и тогда
        «первозданное» значение из конфига уже недействительно. Дедупликация исключает
        повторную отправку того же значения.
        """
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        cached_origin = (tokens.refresh_source if tokens and tokens.refresh_source else None) or (
            f"файл кэша {self.store.path}" if self.store.path else "кэш в памяти"
        )
        for token, origin in (
            (tokens.refresh_token if tokens else None, cached_origin),
            (self.store.refresh_token(), cached_origin),
            (self._configured_refresh(), self.token_source or "конфиг/окружение"),
        ):
            if token and token not in seen:
                seen.add(token)
                out.append((token, origin))
        return out

    def _config_hint(self) -> str:
        from .client import CONFIG_FILENAMES

        return " или ".join(CONFIG_FILENAMES)

    def _with_diagnostics(self, exc: AuthError, candidates: list[tuple[str, str]]) -> AuthError:
        """Дописать к серверной ошибке локальные наблюдения за значением токена."""
        from .diagnostics import inspect_token

        lines: list[str] = []
        for token, origin in candidates:
            report = inspect_token(
                token,
                source=origin,
                requested_client_id=self.client_id,
            )
            if report.cleaned or report.problems or report.expired_at:
                lines.append(f"  [{origin}] длина {report.length}, маска {report.masked}")
                for note in report.notes:
                    lines.append(f"      · {note}")
                for problem in report.problems:
                    lines.append(f"      ! {problem}")
        if not lines:
            lines.append(
                "  локальных признаков проблемы не найдено: значение правильной формы, "
                "срок по JWT не истёк, client_id совпадает"
            )
            lines.append("  ⇒ вероятно, токен отозван в ЛК, относится к другому счёту либо выпущен не тем типом")
        message = str(exc) + "\n\nПроверка значения токена:\n" + "\n".join(lines)
        enriched = AuthError(message, status=exc.status, payload=exc.payload)
        return enriched

    def _token_exchange(self, refresh_token: str) -> dict[str, Any]:
        """POST /openid-connect/token — единственное место, где нужен refresh-токен."""
        body = {"grant_type": "refresh_token", "client_id": self.client_id, "refresh_token": refresh_token}
        attempt = 0
        last_error: Optional[BaseException] = None
        while attempt <= self.max_retries:
            attempt += 1
            self._bucket.acquire()
            self.request_count += 1
            try:
                resp = self.session.post(
                    self.auth_url,
                    data=body,  # application/x-www-form-urlencoded, как требует документация
                    headers={"Accept": "application/json", "User-Agent": "bcs-trade-api-python/1.0"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt <= self.max_retries:
                    self._backoff(attempt, retry_after=None)
                    continue
                break

            if resp.status_code == 200:
                try:
                    payload = resp.json()
                except ValueError as exc:
                    raise AuthError(f"сервер авторизации вернул не JSON: {resp.text[:200]!r}") from exc
                if not isinstance(payload, dict):
                    raise AuthError(f"неожиданный формат ответа авторизации: {type(payload).__name__}")
                return payload

            payload = _safe_json(resp)
            error_code = error_description = ""
            if isinstance(payload, dict):
                error_code = str(payload.get("error") or "")
                error_description = str(payload.get("error_description") or "")
            details = " / ".join(x for x in (error_code, error_description) if x) or resp.text[:200]
            if resp.status_code in (400, 401):
                raise AuthError(
                    f"авторизация не удалась (HTTP {resp.status_code}): {details}" + _auth_hint(error_code),
                    status=resp.status_code,
                    payload=payload,
                )
            if resp.status_code in (429, 503) or resp.status_code >= 500:
                last_error = AuthError(f"сервер авторизации: HTTP {resp.status_code}", status=resp.status_code)
                if attempt <= self.max_retries:
                    self._backoff(attempt, retry_after=_retry_after(resp))
                    continue
                break
            raise AuthError(
                f"авторизация: неожиданный HTTP {resp.status_code}: {resp.text[:200]}",
                status=resp.status_code,
                payload=payload,
            )

        raise AuthError(f"сервер авторизации недоступен после {attempt} попыток: {last_error}")

    # ----------------------------------------------------------------- calls

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Any] = None,
        slow: bool = False,
    ) -> Any:
        """Авторизованный запрос к сервису. Возвращает декодированный JSON (или None)."""
        tokens = self.authenticate()
        bucket = self._slow_bucket if slow else self._bucket
        attempt = 0
        while True:
            attempt += 1
            bucket.acquire()
            self.request_count += 1
            headers = {
                "Authorization": f"Bearer {tokens.access_token}",
                "Accept": "application/json",
                "User-Agent": "bcs-trade-api-python/1.0",
            }
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            log.debug("%s %s params=%s body=%s", method, url, params, json_body)
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=_clean_params(params),
                    data=json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body is not None else None,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt > self.max_retries:
                    raise ApiError(f"сетевая ошибка при запросе {method} {url}: {exc}", url=url, method=method) from exc
                self._backoff(attempt, retry_after=None)
                continue

            if resp.status_code in (200, 201, 204):
                if resp.status_code == 204 or not resp.content:
                    return None
                try:
                    return resp.json()
                except ValueError as exc:
                    raise ApiError(
                        f"сервер вернул не JSON (HTTP {resp.status_code}): {resp.text[:200]!r}",
                        status=resp.status_code,
                        url=url,
                        method=method,
                    ) from exc

            error = ApiError.from_response(
                resp.status_code,
                _safe_json(resp),
                url=url,
                method=method,
                retry_after=_retry_after(resp),
            )

            if resp.status_code in (401, 403) and attempt == 1 and not self._forced:
                # Access-токен могли удалить в ЛК раньше его 24 часов — перевыпускаем один раз.
                log.warning("HTTP %s от %s — перевыпускаю access-токен", resp.status_code, url)
                self._forced = True
                tokens = self.authenticate(force=True)
                continue
            if resp.status_code == 429 and attempt <= self.max_retries:
                self._backoff(attempt, retry_after=error.retry_after)
                continue
            if resp.status_code >= 500 and attempt <= self.max_retries:
                self._backoff(attempt, retry_after=error.retry_after)
                continue
            raise error

    def _backoff(self, attempt: int, *, retry_after: Optional[float]) -> None:
        delay = float(retry_after) if retry_after and retry_after > 0 else self.backoff_base * (2 ** (attempt - 1))
        delay = min(delay, 30.0) + random.uniform(0, self.backoff_base)
        log.warning("превышен лимит/сбой — повторная попытка №%d через %.2f с", attempt + 1, delay)
        self._sleep(delay)


# --------------------------------------------------------------------- helpers


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:2000]} if resp.text else None


def _retry_after(resp: requests.Response) -> Optional[float]:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _clean_params(params: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """Убрать None/пустое; списки отдаём повторением ключа (как ожидает FastAPI)."""
    if not params:
        return None
    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            values = [v for v in value if v is not None]
            if values:
                out[key] = values
            continue
        out[key] = value
    return out or None


def _auth_hint(description: str) -> str:
    key = (description or "").split(":")[0].strip()
    hints = {
        "invalid_grant": (
            ". Причина обычно в том, что refresh-токен истёк (90 суток), отозван в личном кабинете "
            "или выпущен с правами, не совпадающими с client_id (trade-api-read ↔ trade-api-write)"
        ),
        "invalid_client": ". Проверьте client_id: 'trade-api-read' или 'trade-api-write'",
        "unauthorized_client": ". Tокен выпущен с другими правами: смените client_id",
    }
    return hints.get(key, "")


def join_url(base: str, path: str) -> str:
    """Склеить base и path, сохранив служебный префикс вида ``/trade-api-bff-portfolio``."""
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))
