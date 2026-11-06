"""Версия ядра: плагины могут требовать минимальную (``min_core``)."""
from __future__ import annotations

# Версию читает и обновление (core/updater.py): держите строку в виде CORE_VERSION = "X.Y.Z".
CORE_VERSION = "2.2.0"


def parse_version(text: str) -> tuple[int, ...]:
    """``"1.2"`` → ``(1, 2)``. Неверная строка: ValueError."""
    parts = text.strip().lstrip("vV").split(".")
    try:
        numbers = tuple(int(part) for part in parts)
    except ValueError:
        numbers = ()
    if not numbers or any(number < 0 for number in numbers):
        raise ValueError(f"неверный номер версии: {text!r}")
    return numbers


def at_least(have: str, need: str) -> bool:
    """True, если версия ``have`` не ниже ``need`` (недостающие части считаются нулями)."""
    left, right = parse_version(have), parse_version(need)
    size = max(len(left), len(right))
    # Выравниваем длину нулями: 1.2 и 1.2.0 равны.
    pad = lambda value: value + (0,) * (size - len(value))  # noqa: E731
    return pad(left) >= pad(right)
