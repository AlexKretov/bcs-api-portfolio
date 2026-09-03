"""Диагностика конфигурации и refresh-токена.

Модуль отвечает на вопрос «почему БКС вернул ``invalid_grant``», не раскрывая сам секрет:
значение токена нигде не печатается целиком, только длина, маска и claims из JWT.

Три частые причины, которые здесь ловятся:

1. **Значение взялось не из того места.** Приоритет источников — CLI → переменная
   окружения → конфиг-файл → кэш токенов. Если когда-то сделали ``export BCS_REFRESH_TOKEN=...``,
   оно молча переопределит то, что вписано в ``bcs-config.json``.
2. **Файл не читается.** Программа ищет ``bcs-config.json``; файл с опечаткой в имени
   (``bsc-config.json``) не будет прочитан никогда — мы это замечаем и подсказываем.
3. **В строку токена попало лишнее.** Кавычки, пробел/перенос строки, неразрывный пробел,
   многоточие из документации, префикс ``Bearer`` — всё это сервер видит как «плохой токен».
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: Что мы считаем «мусором» вокруг токена. Кавычки и пробелы — частый след копирования в JSON.
_JUNK_CHARS = {
    " ": "пробел",
    "\t": "табуляция",
    "\n": "перенос строки",
    "\r": "возврат каретки",
    "\xa0": "неразрывный пробел",
    "\u200b": "нулевой ширины (zero-width)",
    "\u200c": "zero-width non-joiner",
    "\ufeff": "BOM",
    "\u2026": "многоточие «…» (токен скопирован из документации, где он скрыт)",
    '"': "двойная кавычка",
    "'": "одинарная кавычка",
    "`": "backtick",
    "«": "кавычка-ёлочка",
    "»": "кавычка-ёлочка",
}

_PLACEHOLDER_MARKERS = (
    "подставьте",
    "ваш_",
    "ваш ",
    "токен",
    "example",
    "xxx",
    "yyy",
    "todo",
    "changeme",
    "valid-refresh-token",
)

_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_TOKEN_OK = re.compile(r"^[A-Za-z0-9._\-]+$")


@dataclass
class TokenReport:
    """Результат осмотра refresh-токена. Секрет не раскрывается намеренно."""

    present: bool = False
    source: str = ""
    length: int = 0
    masked: str = ""
    cleaned: bool = False
    removed: tuple[str, ...] = ()
    looks_like_jwt: bool = False
    claims: dict[str, Any] = field(default_factory=dict)
    expired_at: Optional[dt.datetime] = None
    issued_at: Optional[dt.datetime] = None
    azp: Optional[str] = None
    token_type: Optional[str] = None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def render(self) -> str:
        lines = ["Диагностика refresh-токена", "-" * 32]
        lines.append(f"источник значения : {self.source or '—'}")
        if not self.present:
            lines.append("токен            : НЕ задан ни в одном источнике")
            for problem in self.problems:
                lines.append(f"  ! {problem}")
            return "\n".join(lines)
        lines.append(f"длина / маска     : {self.length} симв. · {self.masked}")
        shape = "JWT (3 части)" if self.looks_like_jwt else "непрозрачная строка Keycloak (не JWT)"
        lines.append(f"форма             : {shape}")
        for key, value in (
            ("выдан (iat)", _fmt_time(self.issued_at)),
            ("действителен до (exp)", _fmt_time(self.expired_at)),
            ("azp (client_id в токене)", self.azp or "—"),
            ("typ", self.token_type or "—"),
        ):
            lines.append(f"{key:18s}: {value}")
        for note in self.notes:
            lines.append(f"  · {note}")
        for problem in self.problems:
            lines.append(f"  ! {problem}")
        return "\n".join(lines)


def normalize_refresh_token(raw: Optional[str]) -> tuple[Optional[str], list[str]]:
    """Вернуть (чистое значение, список замечаний).

    Убираем то, что неизбежно появляется при переносе токена через буфер обмена и JSON,
    и сообщаем об этом — чтобы «неверный токен» не выглядел загадкой.
    """
    notes: list[str] = []
    if raw is None:
        return None, notes
    if not isinstance(raw, str):
        return None, [f"значение refresh_token имеет тип {type(raw).__name__}, а не строка"]

    value = raw
    stripped = value.strip()
    if stripped != value:
        notes.append("по краям значения были пробельные символы — убраны")
        value = stripped

    # Bearer-префикс при копировании из примеров заголовков
    for prefix in ("Bearer ", "bearer ", "Basic "):
        if value.startswith(prefix):
            notes.append(f"найден префикс {prefix.strip()!r} — отрезан (в теле запроса его быть не должно)")
            value = value[len(prefix) :].strip()

    if value.count('"') >= 2 or value.startswith(('"', "'")) or value.endswith(('"', "'")):
        cleaned = value.strip("\"'")
        if cleaned != value:
            notes.append("значение было обёрнуто в кавычки — сняты")
            value = cleaned

    # неразрывные пробелы и zero-width внутри строки
    removed_inside = sorted({_JUNK_CHARS[ch] for ch in value if ch in _JUNK_CHARS})
    if removed_inside:
        value = "".join(ch for ch in value if ch not in _JUNK_CHARS)
        notes.append("внутри значения найдены и удалены: " + ", ".join(removed_inside))

    if not value:
        return None, [*notes, "после очистки от токена осталась пустая строка"]
    return value, notes


def inspect_token(
    raw: Optional[str],
    *,
    source: str = "",
    requested_client_id: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> TokenReport:
    """Осмотреть refresh-токен: форма, срок действия, заявленный ``client_id``."""
    from .client import mask_secret

    now = now or dt.datetime.now(dt.timezone.utc)
    cleaned, notes = normalize_refresh_token(raw)
    report = TokenReport(source=source, cleaned=bool(notes), notes=list(notes))

    if not cleaned:
        report.problems.append(
            "refresh-токен не задан (пусто) — проверьте, что файл конфига реально читается: "
            "нужно bcs-config.json в текущей папке, либо BCS_CONFIG=<путь>"
        )
        return report

    report.present = True
    report.length = len(cleaned)
    report.masked = mask_secret(cleaned)
    report.cleaned = bool(notes)

    if len(cleaned) < 20:
        report.problems.append(
            f"длина всего {len(cleaned)} символов — похоже, скопирована не вся строка "
            "(токен из ЛК выдаётся целиком, без обрезки и без «…»)"
        )
    if not _TOKEN_OK.match(cleaned):
        bad = sorted({ch for ch in cleaned if not _TOKEN_OK.match(ch)})
        report.problems.append(
            f"в значении {len(bad)} недопустимых для токена симв. (кавычки/пробелы/кириллица/«…»)"
            " — скопируйте строку целиком из ЛК заново"
        )
    # Важно: не печатаем ни подстроку токена, ни имя совпавшей заглушки — иначе проверка
    # «секрет нигде не светится» перестала бы быть гарантией.
    if any(marker in cleaned.lower() for marker in _PLACEHOLDER_MARKERS):
        report.problems.append(
            "значение совпадает с известной служебной заглушкой (пример из документации или тестовый токен) "
            "— впишите реальный токен из «Токены API»"
        )

    parts = cleaned.split(".")
    if len(parts) == 3 and all(_BASE64URL.match(p or "") for p in parts):
        report.looks_like_jwt = True
        claims = _decode_segment(parts[1])
        if claims:
            report.claims = claims
            report.azp = claims.get("azp")
            report.token_type = claims.get("typ")
            report.expired_at = _epoch_to_dt(claims.get("exp"))
            report.issued_at = _epoch_to_dt(claims.get("iat"))

    if report.expired_at and report.expired_at <= now:
        age = now - report.expired_at
        report.problems.append(
            f"срок действия токена истёк {age.days} дн. назад (exp={report.expired_at:%Y-%m-%d %H:%M} UTC): "
            "refresh-токен живёт 90 суток — выпустите новый в «Токены API»"
        )
    if report.issued_at and report.issued_at > now + dt.timedelta(minutes=5):
        report.problems.append(
            f"токен выдан «из будущего» ({report.issued_at:%Y-%m-%d %H:%M} UTC) — проверьте системные часы: "
            "расхождение времени ломает проверку подписи на стороне брокера"
        )
    if report.azp and requested_client_id and report.azp != requested_client_id:
        report.problems.append(
            f"в токене зашит client_id={report.azp!r}, а запрос идёт с client_id={requested_client_id!r}: "
            f'поставьте в конфиге "client_id": "{report.azp}" (или выпустите токен с нужными правами)'
        )
    return report


def _decode_segment(segment: str) -> Optional[dict[str, Any]]:
    """Раскодировать часть JWT без проверки подписи (нужно только для диагностики)."""
    try:
        padding = "=" * (-len(segment) % 4)
        payload = base64.urlsafe_b64decode(segment + padding)
        data = json.loads(payload.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _epoch_to_dt(value: Any) -> Optional[dt.datetime]:
    try:
        if value in (None, ""):
            return None
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _fmt_time(value: Optional[dt.datetime]) -> str:
    return f"{value:%Y-%m-%d %H:%M} UTC" if value else "—"


# --------------------------------------------------------- поиск конфигурации


def scan_config_files(start: Path, *, limit: int = 6, max_depth: int = 2) -> list[dict[str, Any]]:
    """Найти рядом с запуском JSON-файлы, похожие на конфиг БКС (в т.ч. с опечаткой в имени).

    Возвращает списки вида ``{"path", "readable", "has_refresh_token", "refresh_masked",
    "client_id", "is_expected_name"}`` — секреты не возвращаются.
    """
    start = Path(start).resolve()
    found: list[dict[str, Any]] = []
    seen: set[Path] = set()

    def consider(candidate: Path) -> None:
        if candidate in seen or not candidate.is_file() or candidate.name == "bcs-config.example.json":
            return
        seen.add(candidate)
        record: dict[str, Any] = {
            "path": str(candidate),
            "name": candidate.name,
            "is_expected_name": candidate.name in ("bcs-config.json", ".bcs-config.json"),
            "readable": False,
            "has_refresh_token": False,
            "refresh_masked": None,
            "client_id": None,
            "error": None,
        }
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            record["readable"] = True
            if isinstance(data, dict):
                token = data.get("refresh_token")
                record["has_refresh_token"] = bool(token)
                record["client_id"] = data.get("client_id")
                if token:
                    from .client import mask_secret

                    record["refresh_masked"] = mask_secret(str(token))
        except (OSError, ValueError) as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        found.append(record)

    def matches(name: str) -> bool:
        lowered = name.lower()
        return lowered.endswith(".json") and ("bcs" in lowered or "bsc" in lowered or "config" in lowered)

    for depth in range(max_depth + 1):
        base = start
        for _ in range(depth):  # сначала текущая папка, затем родительские
            base = base.parent
        try:
            entries: Iterable[Path] = sorted(base.glob("*.json"))
        except OSError:
            continue
        for entry in entries:
            if matches(entry.name):
                consider(entry)
        if found:
            break
    return found[:limit]


def format_config_scan(records: Sequence[dict[str, Any]], *, loaded_path: Optional[str]) -> str:
    """Человекочитаемый отчёт по найденным конфигам (для ``token --check``)."""
    if not records:
        return "Рядом с запуском не найдено ни одного JSON-конфига (bcs-config.json / .bcs-config.json / BCS_CONFIG)."
    lines = ["Найденные файлы-кандидаты:", ""]
    for rec in records:
        mark = (
            "читается программой"
            if loaded_path and Path(rec["path"]) == Path(loaded_path)
            else (
                "НЕ читается: имя не то"
                if not rec["is_expected_name"] and rec["has_refresh_token"]
                else "не используется"
            )
        )
        lines.append(f"  {rec['name']}  ({rec['path']}) — {mark}")
        if rec["error"]:
            lines.append(f"      ! битый JSON: {rec['error']}")
        if rec["has_refresh_token"]:
            client_id = rec["client_id"] or "trade-api-read (по умолчанию)"
            lines.append(f"      refresh_token: {rec['refresh_masked']}, client_id: {client_id}")
        elif rec["readable"]:
            lines.append("      ! refresh_token: в файле не задан")
    return "\n".join(lines)
