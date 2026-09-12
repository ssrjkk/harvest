"""Защита от одновременного запуска нескольких фармеров на одной БД.

Неблокирующая file-lock (Windows: msvcrt, POSIX: fcntl) без сторонних
зависимостей. Блокировка держится на первом байте файла и снимается ОС
автоматически при выходе/падении процесса — поэтому «зависший» .lock
не мешает следующему запуску.

Если платформа не поддерживает ни msvcrt, ни fcntl — блокировка не
накладывается (возвращаем False), запуск продолжается без защиты.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import msvcrt
except ImportError:  # pragma: no cover — POSIX-ветка
    msvcrt = None  # type: ignore[assignment]

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows-ветка
    fcntl = None  # type: ignore[assignment]


def default_lock_path(db_path: str) -> str:
    """Путь файла-блокировки рядом с БД (общий для процессов на одной базе)."""
    return str(Path(db_path).with_suffix(Path(db_path).suffix + ".lock"))


class SingleInstance:
    """Неблокирующая блокировка процесса через file lock.

    >>> guard = SingleInstance("farming_state.db.lock")
    >>> if not guard.acquire():
    ...     print(f"Уже запущен другой экземпляр (pid={guard.holder_pid()})")
    ...
    >>> # ... работа ...
    >>> guard.release()
    """

    def __init__(self, lock_path: str | Path) -> None:
        self._path = Path(lock_path)
        self._fd: int | None = None
        self._acquired = False
        self._pid: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> bool:
        """Пытается занять блокировку. True — занята текущим процессом."""
        if self._fd is not None:
            return self._acquired
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            logger.warning(f"Не удалось открыть файл блокировки {self._path}: {e}")
            return False
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\x00")
                os.lseek(fd, 0, os.SEEK_SET)
            if msvcrt is not None:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            elif fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                os.close(fd)
                return False
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        self._acquired = True
        self._pid = str(os.getpid())
        try:
            os.write(fd, f"{self._pid}\n".encode())
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError:
            pass
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        self._acquired = False
        self._pid = None
        try:
            if msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                os.close(fd)
            elif fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass

    def holder_pid(self) -> str | None:
        """PID текущего держателя (для сообщения пользователю).

        Своего держателя читаем из открытого fd — на Windows os.open не даёт
        READ-шаринг, и новый handle для чтения упадёт с Permission denied.
        Чужие процессы (при отказе acquire) — best-effort: ставка на то, что
        конрент файла читается; при недоступности возвращаем None.
        """
        if self._fd is not None and self._pid is not None:
            return self._pid
        try:
            text = self._path.read_text(encoding="utf-8", errors="replace").strip()
            return text or None
        except OSError:
            return None

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise ProcessLockedError(self._path, self.holder_pid())
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class ProcessLockedError(RuntimeError):
    """Ещё один экземпляр фармера уже работает на этой БД."""

    def __init__(self, lock_path: Path, holder_pid: str | None) -> None:
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        holder = f" (pid={holder_pid})" if holder_pid else ""
        super().__init__(f"Уже запущен другой экземпляр{holder} — файл блокировки {lock_path}")
