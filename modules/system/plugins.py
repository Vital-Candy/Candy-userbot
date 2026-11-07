"""Модуль команды .plugin.

Синтаксис: ``.plugin [list]`` и ``.plugin load|unload|reload|info <имя>``. Вызывать могут
только пользователи с уровнем ADMIN (владелец всегда ADMIN). Менеджер плагинов: core/plugins.py.
"""
from __future__ import annotations

import re

from core.access import require
from core.module import Module
from core.reply import answer
from storage.permissions import PermissionLevel
from core.summary import plugin_info_lines, plugin_lines
from utils.text import escape_markdown

USAGE = (
    "Использование:\n"
    ".plugin [list]\n"
    ".plugin load|unload|reload|info <имя>"
)
DETAILS = """\
Управление плагинами: файлами plugins/<имя>.py. Папка plugins/ не обновляется и не публикуется, там ваши расширения. Готовые плагины ставятся из магазина: .store.

.plugin — список плагинов и их состояние
.plugin list — то же самое
.plugin load <имя> — загрузить плагин
.plugin unload <имя> — выгрузить (его команды и задачи снимаются)
.plugin reload <имя> — перезагрузить после правки файла
.plugin info <имя> — версия, описание, зависимости, число команд, обработчиков и задач, последняя ошибка

Имя: латинские буквы, цифры, _ и -.
Плагины не изолированы: они выполняют код с теми же правами, что и userbot, ставьте только свои или проверенные.

Права: администратор (владелец аккаунта им является всегда)."""
NAME = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
ACTIONS = ("load", "unload", "reload", "info")


class PluginsModule(Module):
    """Команда ``.plugin``: список плагинов, load/unload/reload/info."""

    name = "system.plugins"
    required = True  # системный модуль: сбой запуска останавливает userbot
    description = "Команда .plugin: управление плагинами из Telegram"

    async def start(self) -> None:
        """Регистрирует команду ``.plugin`` (только для ADMIN)."""
        self.app.dispatcher.register_command(
            "plugin",
            self.plugin_command,
            description="плагины: список, загрузка, выгрузка",
            usage=".plugin [list | load | unload | reload | info <имя>]",
            priority=90,
            details=DETAILS,
            filters=(require(self.app, PermissionLevel.ADMIN),),
        )

    async def stop(self) -> None:
        """Снимает регистрацию команды ``.plugin``."""
        self.app.dispatcher.unregister_command("plugin")

    async def plugin_command(self, event) -> None:
        """Разбирает ``.plugin``; без аргументов показывает список."""
        command = self.app.dispatcher.get_command(event)
        args = list(command.args) if command else []
        manager = self.app.plugins

        if not args or (args[0].lower() == "list" and len(args) == 1):
            lines = plugin_lines(manager, esc=escape_markdown)
            text = "🔌 Плагины:\n" + "\n".join(lines) if lines else "🔌 Плагинов нет (папка plugins/ пуста)."
            await answer(event, text)
            return

        action = args[0].lower()
        if action not in ACTIONS or len(args) != 2:
            await answer(event, USAGE)
            return
        name = args[1]
        # Имя попадает в путь plugins/<имя>.py, поэтому допускаем только безопасные символы.
        if not NAME.match(name):
            await answer(event, "❌ Имя плагина: латинские буквы, цифры, _ и -.")
            return

        try:
            text = await self._run(action, name)
        except KeyError:
            text = f"❌ Плагин «{name}» не найден (нужен файл plugins/{name}.py)."
        await answer(event, text)

    async def _run(self, action: str, name: str) -> str:
        """Выполняет действие над плагином и возвращает текст ответа; ``KeyError``, если плагина нет."""
        esc = escape_markdown
        manager = self.app.plugins
        if action == "info":
            return "\n".join(plugin_info_lines(manager.info(name), esc=escape_markdown))
        if action == "unload":
            state = next((s for s in manager.states() if s.name == name), None)
            if state is None:
                raise KeyError(name)
            if not state.loaded:
                return f"ℹ️ Плагин {name} и так не загружен."
            ok = await manager.unload(name)
            if ok:
                return f"✅ Плагин {name} выгружен."
            return f"⚠️ Плагин {name} выгружен, но teardown() упал: {esc(self._error(name))}. Регистрации сняты."
        ok = await getattr(manager, action)(name)
        if ok:
            info = manager.info(name)
            return f"✅ Плагин {name} {'загружен' if action == 'load' else 'перезагружен'} (v{info['version']})."
        return f"❌ Плагин {name} не загрузился: {esc(self._error(name))}"

    def _error(self, name: str) -> str:
        """Последняя ошибка плагина или заглушка «см. лог»."""
        return self.app.plugins.info(name)["error"] or "см. лог"
