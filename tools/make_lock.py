#!/usr/bin/env python3
"""Генерация requirements-lock.txt из установленных пакетов (pip freeze lite).

Берёт ИМЕНА из requirements.txt + requirements-portal.txt, версии — из
текущего окружения (importlib.metadata). Назначение: воспроизводимая
установка и честный аудит точных версий. CI не ставит по лок-файлу
напрямую (там Linux/py3.12+3.14), но проверяет его свежесть через --check:
каждый пин обязан удовлетворять объявленным диапазонам.

Использование:
    python tools/make_lock.py                    # пересобрать лок
    python tools/make_lock.py --check            # только проверить свежесть
    pip install -r requirements-lock.txt         # воспроизводимая установка
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = (ROOT / "requirements.txt", ROOT / "requirements-portal.txt")
LOCK = ROOT / "requirements-lock.txt"

_REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+(?:\[[A-Za-z0-9_,.\- ]+\])?)\s*(.*)$")


def _parse_sources() -> list[tuple[str, str]]:
    """[(имя, спецификатор)] в порядке первого упоминания, без дублей."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for src in SOURCES:
        for raw in src.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith(("-", "!")):
                continue
            m = _REQ_RE.match(line)
            if not m:
                continue
            name = re.sub(r"\[.*\]$", "", m.group(1)).strip()
            spec = m.group(2).strip().rstrip("\\").strip()
            key = name.lower().replace("_", "-")
            if key not in seen:
                seen.add(key)
                out.append((name, spec))
    return out


def _installed_version(name: str) -> str | None:
    try:
        from importlib import metadata as _md
    except ImportError:  # pragma: no cover — python без importlib.metadata
        return None
    norm = name.lower().replace("_", "-").replace(".", "-")
    try:
        return _md.version(norm)
    except Exception:  # noqa: BLE001 — пакет не установлен
        return None


def build() -> dict[str, str]:
    """{имя: версия} для всех зависимостей; отсутствующие — пропуск с варнингом."""
    reqs = _parse_sources()
    locked: dict[str, str] = {}
    missing: list[str] = []
    for name, _spec in reqs:
        ver = _installed_version(name)
        if ver is None:
            missing.append(name)
            continue
        locked[name] = ver
    for name in missing:
        print(f"warn: {name} не установлен — пропущен (поставь зависимости и повтори)")
    return locked


def _check() -> int:
    """0 — лок свежий (все пины покрывают диапазоны), иначе 1 + причины."""
    try:
        from packaging.specifiers import SpecifierSet
    except ImportError:
        print("err: нужен пакет packaging (идёт с pip/pytest)")
        return 1
    reqs = _parse_sources()
    pins: dict[str, str] = {}
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, ver = (p.strip() for p in line.split("==", 1))
        pins[name.lower().replace("_", "-")] = ver
    errors = 0
    for name, spec in reqs:
        key = name.lower().replace("_", "-")
        if key not in pins:
            print(f"stale: {name} нет в {LOCK.name} — пересобери: python tools/make_lock.py")
            errors += 1
            continue
        if spec and pins[key] not in SpecifierSet(spec):
            print(f"stale: {name}=={pins[key]} не покрывает '{spec}' — пересобери лок")
            errors += 1
    if errors:
        return 1
    print(f"ok: {LOCK.name} свежий ({len(reqs)} пакетов)")
    return 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        if not LOCK.exists():
            print(f"err: нет {LOCK.name} — сгенерируй: python tools/make_lock.py")
            return 1
        return _check()
    locked = build()
    if not locked:
        print("err: нечего писать — поставь зависимости")
        return 1
    reqs = dict(_parse_sources())
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# requirements-lock.txt — точные версии для воспроизводимой установки.",
        f"# Сгенерирован: {stamp} (python {sys.version.split()[0]}).",
        f"# Источники: {', '.join(p.name for p in SOURCES)}.",
        "# Обновление: python tools/make_lock.py. Проверка в CI: --check.",
        "# pip-audit по этому файлу: python -m pip_audit --strict -r requirements-lock.txt",
        "",
    ]
    for name, ver in locked.items():
        spec = reqs.get(name, "")
        tail = f"  # {spec}" if spec else ""
        lines.append(f"{name}=={ver}{tail}")
    LOCK.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"ok: {LOCK} ({len(locked)} пакетов)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
