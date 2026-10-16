"""Санитизация входящих данных и защита от троллинга.

Строгая проверка ДО тяжёлой бизнес-логики:
- размер, глубина вложенности, число элементов, длина строк;
- null-байты и невидимые управляющие символы (кроме \\t\\n\\r);
- эмодзи-спам (эвристика по долям символов);
- флуд одинаковыми пейлоадами (дедупликация последних K хэшей субъекта).

Без regex с обратным отслеживанием (защита от ReDoS): только прямые
сканы и unicodedata. Два режима: `check()` — строгий (ошибка),
`clean_text()` — мягкий для свободного ввода (чистит и режет).
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections import deque
from dataclasses import dataclass, field

from .config import SanitizeConfig

_ALLOWED_WS = {"\t", "\n", "\r"}
_BAD_CATS = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
_EMOJI_CATS = {"So", "Sk"}


class SanitizationError(ValueError):
    """Входящие данные не прошли guard."""


@dataclass
class _SubjectFlood:
    hashes: deque = field(default_factory=lambda: deque(maxlen=32))


def _emoji_ratio(text: str) -> float:
    if not text:
        return 0.0
    total = 0
    bad = 0
    for ch in text:
        o = ord(ch)
        if o < 128 and (ch.isalnum() or ch.isspace() or ch in ".,;:!?-_+/*=()[]{}<>@#"):
            continue
        total += 1
        cat = unicodedata.category(ch)
        if cat in _EMOJI_CATS or 0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF:
            bad += 1
    return (bad / total) if total else 0.0


def clean_text(text: str, max_len: int = 4096) -> str:
    """Мягкая чистка свободного ввода: управляющие символы, обрезка."""
    out = []
    for ch in text:
        if ch in _ALLOWED_WS:
            out.append(ch)
        elif unicodedata.category(ch) in _BAD_CATS:
            continue
        else:
            out.append(ch)
    cleaned = "".join(out)
    return cleaned[:max(1, max_len)]


class PayloadGuard:
    """Строгая проверка структур + дедупликация флуда по субъектам."""

    def __init__(self, config: SanitizeConfig) -> None:
        config.validate()
        self.cfg = config
        self._flood: dict[str, _SubjectFlood] = {}
        self.rejections = 0

    def _reject(self, reason: str) -> SanitizationError:
        self.rejections += 1
        return SanitizationError(reason)

    def check_bytes(self, raw: bytes | None) -> None:
        if raw is None:
            return
        if len(raw) > self.cfg.max_bytes:
            raise self._reject(f"payload_too_big {len(raw)} > {self.cfg.max_bytes}")

    def check(self, obj: object) -> None:
        """Рекурсивная проверка структуры. Raises SanitizationError."""
        self._walk(obj, depth=0)

    def _walk(self, obj: object, depth: int) -> None:
        cfg = self.cfg
        if depth > cfg.max_depth:
            raise self._reject(f"too_deep depth={depth} > {cfg.max_depth}")
        if isinstance(obj, dict):
            if len(obj) > cfg.max_items:
                raise self._reject(f"too_many_keys {len(obj)} > {cfg.max_items}")
            for k, v in obj.items():
                if isinstance(k, str):
                    self._check_str(k, what="key")
                self._walk(v, depth + 1)
        elif isinstance(obj, (list, tuple)):
            if len(obj) > cfg.max_items:
                raise self._reject(f"too_many_items {len(obj)} > {cfg.max_items}")
            for v in obj:
                self._walk(v, depth + 1)
        elif isinstance(obj, str):
            self._check_str(obj, what="value")

    def _check_str(self, s: str, what: str) -> None:
        cfg = self.cfg
        if len(s) > cfg.max_str:
            raise self._reject(f"string_too_long {what} {len(s)} > {cfg.max_str}")
        for ch in s:
            if ch in _ALLOWED_WS:
                continue
            if ch == "\x00" or unicodedata.category(ch) in _BAD_CATS:
                raise self._reject(f"bad_char {what} U+{ord(ch):04X}")
        if _emoji_ratio(s) > cfg.emoji_ratio:
            raise self._reject("emoji_spam")

    def flood_duplicate(self, subject: str, payload: bytes | str) -> bool:
        """True, если такой пейлоад уже видели недавно (флуд)."""
        raw = payload.encode("utf-8", "ignore") if isinstance(payload, str) else bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()
        box = self._flood.get(subject)
        if box is None:
            box = _SubjectFlood(deque(maxlen=self.cfg.flood_k))
            self._flood[subject] = box
        if digest in box.hashes:
            return True
        box.hashes.append(digest)
        if len(self._flood) > self.cfg.flood_max_subjects:
            oldest = next(iter(self._flood))
            del self._flood[oldest]
        return False

    def evict(self) -> int:
        """Сбросить историю флуда (вызывается фоновым сборщиком)."""
        n = len(self._flood)
        self._flood.clear()
        return n
