"""Сборка деплой-архива бота BotForge: botforge/harvest-botforge.zip.

На BotForge заливается архив, а не репозиторий. Раньше он собирался руками,
и после правки исходников (например, bump зависимостей) архив молча устаревал:
`tools/verify_site.py` ловил это проверкой «botforge zip fresh», но починить
можно было только вручную. Теперь сборка воспроизводима одной командой:

    python tools/build_botforge_zip.py

Состав архива (как его ждёт BotForge):
    main.py         — копия bot.py (BotForge запускает main.py)
    bot.py          — исходник
    requirements.txt
    README.md

Временные метки записей фиксированы: иначе каждая пересборка меняет zip
байт в байт и в истории Git остаётся шум из-за mtime.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BFDIR = ROOT / "botforge"
ZIP_PATH = BFDIR / "harvest-botforge.zip"

# Источники → имена внутри архива. main.py — копия bot.py: BotForge запускает
# main.py, а исходник в репозитории лежит под своим именем.
SOURCES: tuple[tuple[str, str], ...] = (
    ("bot.py", "main.py"),
    ("bot.py", "bot.py"),
    ("requirements.txt", "requirements.txt"),
    ("README.md", "README.md"),
)

# Фиксированная метка (2024-01-01 00:00:00) — сборка должна быть детерминированной.
FIXED_TIME = (2024, 1, 1, 0, 0, 0)


def build(zip_path: Path = ZIP_PATH) -> list[str]:
    """Пересобирает архив. Возвращает список записей."""
    entries: list[tuple[str, bytes]] = []
    for src_name, arc_name in SOURCES:
        src = BFDIR / src_name
        if not src.exists():
            raise FileNotFoundError(f"нет исходника: {src}")
        entries.append((arc_name, src.read_bytes()))

    tmp = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc_name, data in entries:
            info = zipfile.ZipInfo(arc_name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    tmp.replace(zip_path)
    return [name for name, _ in entries]


def main() -> int:
    try:
        names = build()
    except FileNotFoundError as e:
        print(f"сборка не удалась: {e}")
        return 1
    print(f"архив собран: {ZIP_PATH.relative_to(ROOT)}")
    for n in names:
        print(f"  + {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
