"""Модуль команды .status (одна команда состояния вместо прежних .status и .health).

Вызывает владелец аккаунта; данные берутся из app.health() и core/summary.py.
"""
from __future__ import annotations

from core.health import memory_usage_mb
from core.module import Module
from core.reply import answer
from storage.permissions import PermissionLevel
from core.summary import module_lines, task_lines

USAGE = "Использование: .status [all]"

DETAILS = """\
Показывает, всё ли в порядке: общее состояние (ok, degraded, down), связь с Telegram, базу, число модулей, плагинов и фоновых задач, занятую память, а при проблемах их список.

.status — короткая сводка
.status all — то же и подробно: каждый модуль (обязательный или нет, причина сбоя) и каждая фоновая задача (запуски, сбои, последняя ошибка)

Права: только владелец аккаунта."""


class StatusModule(Module):
    """Команда ``.status``: краткая сводка здоровья, а с ``all`` ещё модули и фоновые задачи."""

    name = "system.status"
    required = True  # системный модуль: сбой запуска останавливает userbot
    description = "Системная команда .status"

    async def start(self) -> None:
        """Регистрирует команду ``.status``."""
        self.app.dispatcher.register_command(
            "status",
            self.status_command,
            description="состояние userbot (all: подробно)",
            usage=".status [all]",
            priority=100,
            details=DETAILS,
            level=PermissionLevel.TRUSTED,
            pause=3,
        )

    async def stop(self) -> None:
        """Снимает регистрацию команды ``.status``."""
        self.app.dispatcher.unregister_command("status")

    async def status_command(self, event) -> None:
        """``.status``: краткая сводка; ``.status all``: ещё список модулей и задач."""
        command = self.app.dispatcher.get_command(event)
        args = [item.lower() for item in (command.args if command else [])]
        if self.app.dispatcher.caller_level(event) != PermissionLevel.OWNER:
            # Чужим показываем только короткую сводку: без памяти, списка проблем, модулей и задач.
            await answer(event, self.public_text())
            return
        if args not in ([], ["all"]):
            await answer(event, USAGE)
            return
        lines = self.app.health().lines()
        memory = memory_usage_mb()
        if memory is not None:
            # Строка памяти идёт после счётчиков, перед списком проблем.
            lines.insert(3, f"Память: {memory:.0f} МБ")
        text = "🍬 Candy Userbot\n" + "\n".join(lines)
        if args == ["all"]:
            text += "\n\n" + self.details_text()
        await answer(event, text)

    def public_text(self) -> str:
        """Короткая сводка для людей с выданным уровнем: только то, что не раскрывает внутренности."""
        lines = self.app.health().lines()
        return "🍬 Candy Userbot\n" + "\n".join(lines[:2])

    def details_text(self) -> str:
        """Подробная часть для ``.status all``: модули и фоновые задачи."""
        states = self.app.modules.states()
        started = sum(state.started for state in states)
        failed = sum(state.failed for state in states)
        tasks = task_lines(self.app)
        text = (
            f"Модули: {started}/{len(states)} запущено, ошибок запуска: {failed}\n"
            + "\n".join(module_lines(self.app))
            + f"\n\nФоновые задачи: {len(tasks)}"
        )
        if tasks:
            text += "\n" + "\n".join(tasks)
        return text
