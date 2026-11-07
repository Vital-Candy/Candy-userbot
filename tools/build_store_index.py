#!/usr/bin/env python3
"""Собирает store/index.json по файлам store/plugins/*.py (версии, описания, SHA-256, размеры).

Запуск: ``python3 tools/build_store_index.py`` пересобирает индекс; ``--check`` ничего не пишет и
завершается с кодом 1, если индекс не соответствует файлам. Скрипт публикации (tools/publish.sh) запускает
сборку сам, так что индекс всегда совпадает с тем, что уходит в репозиторий.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.store import StoreError, build_index, dump_index  # noqa: E402


def main(argv: list[str]) -> int:
    """Точка входа: 0 успех, 1 индекс устарел (``--check``) или плагин не прошёл проверку."""
    check = "--check" in argv
    unknown = [item for item in argv if item != "--check"]
    if unknown:
        print("Использование: build_store_index.py [--check]", file=sys.stderr)
        return 2
    store = ROOT / "store"
    if not (store / "plugins").is_dir():
        print("Каталога store/plugins нет: индекс не нужен.")
        return 0
    try:
        text = dump_index(build_index(store))
    except StoreError as exc:
        print(f"Ошибка плагина: {exc}", file=sys.stderr)
        return 1
    target = store / "index.json"
    current = target.read_text(encoding="utf-8") if target.exists() else None
    if check:
        if current != text:
            print("store/index.json устарел: запустите python3 tools/build_store_index.py", file=sys.stderr)
            return 1
        return 0
    if current != text:
        target.write_text(text, encoding="utf-8")
        print("store/index.json обновлён.")
    else:
        print("store/index.json актуален.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
