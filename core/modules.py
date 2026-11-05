"""Реестр и жизненный цикл модулей userbot: порядок запуска по ``requires`` и откат при сбоях.

Используется app.py при старте и остановке; состояния читают health.py и summary.py.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from core.errors import UserError
from core.module import Module
from utils.logger import logger

ERROR_TEXT_LIMIT = 200


class ModuleStartError(UserError):
    """Обязательный модуль не запустился: userbot должен остановиться."""


@dataclass(slots=True)
class ModuleState:
    """Состояние одного модуля: запущен, упал и причина падения."""
    module: Module
    started: bool = False
    failed: bool = False
    error: str | None = None

    @property
    def required(self) -> bool:
        """Обязательный ли модуль (его сбой останавливает весь userbot)."""
        return bool(self.module.required)

    @property
    def status(self) -> str:
        """``running``, ``failed`` или ``stopped``."""
        if self.started:
            return "running"
        return "failed" if self.failed else "stopped"


def _describe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return text[:ERROR_TEXT_LIMIT]


class ModuleManager:
    """Lifecycle и реестр модулей userbot."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._modules: dict[str, ModuleState] = {}
        self._start_order: list[str] = []  # в порядке реального запуска
        # Сбои необязательных модулей при старте: о них нужно сообщить владельцу.
        self.startup_failures: list[tuple[str, str]] = []

    def register(self, module: Module) -> None:
        """Добавляет модуль в реестр, не запуская.

        Raises:
            ValueError: пустое имя, дубликат, ``requires`` строкой или зависимость от самого себя.
        """
        name = module.name.strip()
        if not name:
            raise ValueError("Модуль должен иметь имя")
        if name in self._modules:
            raise ValueError(f"Модуль '{name}' уже зарегистрирован")
        requires = module.requires
        if isinstance(requires, str):
            raise ValueError(f"Модуль '{name}': requires должен быть кортежем имён, а не строкой")
        if name in requires:
            raise ValueError(f"Модуль '{name}' не может зависеть от самого себя")
        self._modules[name] = ModuleState(module=module)

    # ------------------------------------------------------------------ order

    def start_order(self) -> tuple[list[str], dict[str, str]]:
        """Порядок запуска с учётом ``requires``.

        Возвращает ``(порядок, проблемы)``. Модули в порядке регистрации, но
        зависимость всегда раньше зависимого. В ``проблемы`` попадают модули
        с неизвестной зависимостью или участники цикла: их запускать нельзя.
        """
        problems: dict[str, str] = {}
        for name, state in self._modules.items():
            for dep in state.module.requires:
                if dep not in self._modules:
                    problems[name] = f"неизвестная зависимость '{dep}'"
                    break

        order: list[str] = []
        visiting: list[str] = []
        done: set[str] = set()

        def visit(name: str) -> None:
            """Обход в глубину: добавляет модуль в ``order`` после его зависимостей."""
            # Модуль уже обработан (или запись о цикле уже сделана): повторно не обходим.
            if name in done:
                return
            if name in visiting:
                cycle = visiting[visiting.index(name):]
                for member in cycle:
                    problems.setdefault(member, "циклическая зависимость: " + " → ".join(cycle + [name]))
                return
            # Постфиксный обход: зависимости попадают в order раньше зависимого.
            visiting.append(name)
            for dep in self._modules[name].module.requires:
                if dep in self._modules:
                    visit(dep)
            visiting.pop()
            done.add(name)
            order.append(name)

        for name in self._modules:
            visit(name)
        return order, problems

    # ------------------------------------------------------------------ start

    async def start_all(self) -> None:
        """Запускает все модули в порядке зависимостей.

        Сбой необязательного модуля только логируется и запоминается в
        ``startup_failures``. Сбой обязательного (или его зависимости)
        поднимает ``ModuleStartError``: остальные модули не запускаются.
        """
        # Список относится к одному запуску: перезапуск начинает с чистого листа.
        self.startup_failures.clear()
        order, problems = self.start_order()
        for name in order:
            state = self._modules[name]
            if name in problems:
                self._mark_failed(state, problems[name])
                logger.error("Модуль '%s' не запущен: %s.", name, problems[name])
                self._reject(state, problems[name])
                continue
            blocker = self._blocker(state)
            if blocker is not None:
                reason = f"зависимость '{blocker}' не запущена"
                self._mark_failed(state, reason)
                logger.error("Модуль '%s' пропущен: %s.", name, reason)
                self._reject(state, reason)
                continue
            # start() сам помечает сбой и логирует; здесь только решаем, фатален ли он.
            if not await self.start(name):
                self._reject(state, state.error or "ошибка запуска")

    def _reject(self, state: ModuleState, reason: str) -> None:
        name = state.module.name
        # Обязательный модуль: прерываем весь запуск, чтобы app откатил уже запущенное.
        if state.required:
            raise ModuleStartError(f"Обязательный модуль '{name}' не запустился: {reason}")
        self.startup_failures.append((name, reason))

    def _blocker(self, state: ModuleState) -> str | None:
        # Первая незапущенная зависимость блокирует запуск.
        for dep in state.module.requires:
            dep_state = self._modules.get(dep)
            if dep_state is None or not dep_state.started:
                return dep
        return None

    @staticmethod
    def _mark_failed(state: ModuleState, reason: str) -> None:
        state.started = False
        state.failed = True
        state.error = reason[:ERROR_TEXT_LIMIT]

    async def start(self, name: str) -> bool:
        """Запускает один модуль.

        Возвращает False при сбое (причина в ``state.error``), исключение не бросает.
        """
        state = self._modules[name]
        if state.started:
            return True
        blocker = self._blocker(state)
        if blocker is not None:
            self._mark_failed(state, f"зависимость '{blocker}' не запущена")
            logger.error("Модуль '%s' не запущен: %s.", name, state.error)
            return False
        try:
            result = state.module.start()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            self._mark_failed(state, _describe(exc))
            logger.exception("Не удалось запустить модуль '%s'.", name)
            return False
        state.started = True
        state.failed = False
        state.error = None
        # Перезапуск переносит модуль в конец, чтобы порядок остановки оставался обратным.
        if name in self._start_order:
            self._start_order.remove(name)
        self._start_order.append(name)
        logger.info("Модуль '%s' запущен.", name)
        return True

    # ------------------------------------------------------------------- stop

    async def stop_all(self) -> None:
        """Останавливает все запущенные модули в порядке, обратном запуску."""
        # Обратный порядок реального запуска: зависимости останавливаются последними.
        for name in reversed(tuple(self._start_order)):
            await self.stop(name, _cascade=False)

    async def stop(self, name: str, *, _cascade: bool = True) -> bool:
        """Останавливает модуль (сначала зависимые от него). False, если ``stop()`` модуля упал."""
        state = self._modules[name]
        if not state.started:
            return True
        if _cascade:
            # Сначала те, кто от него зависит.
            for other in reversed(tuple(self._start_order)):
                if other != name and name in self._modules[other].module.requires:
                    await self.stop(other)
        try:
            result = state.module.stop()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("Ошибка остановки модуля '%s'.", name)
            return False
        state.started = False
        if name in self._start_order:
            self._start_order.remove(name)
        logger.info("Модуль '%s' остановлен.", name)
        return True

    # ------------------------------------------------------------------- info

    def get(self, name: str) -> Module | None:
        """Модуль по имени или None."""
        state = self._modules.get(name)
        return state.module if state else None

    def states(self) -> list[ModuleState]:
        """Состояния всех модулей в порядке регистрации."""
        return list(self._modules.values())

    def failures(self) -> list[ModuleState]:
        """Модули, которые не удалось запустить."""
        return [state for state in self._modules.values() if state.failed]
