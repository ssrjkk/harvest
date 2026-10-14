"""Быстрый старт: ставит зависимости только если изменились манифесты.

Панель запускает STARTUP_CMD при каждом (ре)старте; голый `pip install`
каждый раз пересчитывает дерево (минуты на слабом CPU), а сервер всё это
время висит в stopping. Этот скрипт сверяет sha256 манифестов с меткой
.pip_ok и зовёт pip только при расхождении. Второй и следующие старты —
миллисекунды.
"""

import hashlib
import os
import subprocess
import sys

MANIFESTS = ("requirements.txt", "requirements-portal.txt")
STAMP_FILE = ".pip_ok"


def manifests_digest(root: str) -> str | None:
    h = hashlib.sha256()
    try:
        for name in MANIFESTS:
            with open(os.path.join(root, name), "rb") as f:
                h.update(f.read())
    except OSError:
        return None
    return h.hexdigest()


def main(root: str | None = None) -> int:
    if root is None:
        root = os.path.dirname(os.path.abspath(__file__))
    digest = manifests_digest(root)
    if digest is None:
        print("bootstrap: requirements not found, running pip anyway")
    else:
        stamp = os.path.join(root, STAMP_FILE)
        try:
            with open(stamp, encoding="utf-8") as f:
                if f.read().strip() == digest:
                    print("bootstrap: deps up to date, skip pip")
                    return 0
        except OSError:
            pass
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-r",
        "requirements.txt",
        "-r",
        "requirements-portal.txt",
        "--quiet",
    ]
    print("bootstrap: installing requirements...", flush=True)
    rc = subprocess.run(cmd, cwd=root).returncode
    if rc == 0 and digest is not None:
        try:
            with open(os.path.join(root, STAMP_FILE), "w", encoding="utf-8") as f:
                f.write(digest)
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
