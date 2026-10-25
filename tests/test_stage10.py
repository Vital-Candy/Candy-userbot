"""Тесты справки: ``.help`` (список), ``.help <команда>`` (подробно) и одна команда ``.status``."""
from __future__ import annotations

import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.commands import find_command, help_command_text, help_list, register_commands
from core.dispatcher import Dispatcher
from modules.permissions.module import PermissionsModule
from modules.storage.labels import LabelsModule
from modules.spam.module import SpamModule
from modules.system.accounts import AccountsModule
from modules.utility.mute import MuteModule
from modules.utility.ping import PingModule
from modules.utility.purge import PurgeModule
from modules.utility.tag import TagModule
from modules.utility.user import UserModule
from modules.system.backup import BackupModule
from modules.system.plugins import PluginsModule
from modules.system.prefix import PrefixModule
from modules.system.restart import RestartModule
from modules.system.status import StatusModule
from modules.system.update import UpdateModule

EXPECTED = {
    "help", "stop", "status", "account", "backup", "plugin", "update", "perm",
    "label", "labels", "goto", "unlabel", "restart", "spam",
    "user", "ping", "tag", "purge", "setprefix", "mute",
}
# В ответах Telegram включён Markdown: эти символы сломали бы или исказили справку.
MARKDOWN = re.compile(r"[*`]|~~|__|\]\(")


async def build() -> tuple[Dispatcher, list[str]]:
    """Диспетчер со всеми встроенными командами; второй результат: тексты ответов."""
    dispatcher = Dispatcher()
    plugin_data = SimpleNamespace(get=lambda **kw: None, delete=lambda **kw: False, set=lambda **kw: None)
    app = SimpleNamespace(
        dispatcher=dispatcher, access=None, storage=SimpleNamespace(plugin_data=plugin_data),
        identity=SimpleNamespace(account_id=AsyncMock(return_value=1)),
    )
    register_commands(dispatcher, AsyncMock())
    for module in (
        StatusModule, AccountsModule, BackupModule, PluginsModule, UpdateModule,
        RestartModule, SpamModule, UserModule, PingModule, TagModule, PurgeModule, MuteModule, PrefixModule, PermissionsModule, LabelsModule,
    ):
        await module(app).start()
    return dispatcher, []


async def ask(dispatcher: Dispatcher, text: str) -> str:
    """Выполняет команду как владелец и возвращает ответ (отредактированный текст)."""
    sent: list[str] = []

    async def edit(reply: str) -> None:
        sent.append(reply)

    await dispatcher.dispatch_command(SimpleNamespace(raw_text=text, edit=edit, out=True))
    return sent[-1]


class HelpTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_builtin_command_is_registered_with_details(self):
        dispatcher, _ = await build()
        self.assertEqual(set(dispatcher.commands), EXPECTED)
        for command in dispatcher.commands.values():
            with self.subTest(command=command.name):
                self.assertTrue(command.description, "нет описания")
                self.assertTrue(command.usage.startswith("."), "usage должен начинаться с точки")
                self.assertGreater(len(command.details), 40, "нет подробной справки")
                self.assertIsNone(MARKDOWN.search(command.details + command.usage + command.description))

    async def test_help_without_arguments_is_a_short_list(self):
        dispatcher, _ = await build()
        text = await ask(dispatcher, ".help")
        for name in EXPECTED:
            self.assertIn(f".{name} — ", text)
        self.assertIn(".help <команда>", text)
        self.assertNotIn("Использование:", text)  # подробности только в .help <команда>
        self.assertLess(len(text), 1200)

    async def test_help_for_command_shows_full_description(self):
        dispatcher, _ = await build()
        text = await ask(dispatcher, ".help label")
        self.assertIn("📖 .label", text)
        self.assertIn("Использование: .label <имя>", text)
        self.assertIn("ответом на сообщение", text)
        self.assertIn(".goto", text)  # связанные команды
        # Точка и регистр не важны.
        self.assertEqual(await ask(dispatcher, ".help .LABEL"), text)

    async def test_help_for_subcommands_lists_all_of_them(self):
        dispatcher, _ = await build()
        text = await ask(dispatcher, ".help account")
        for part in (".account list", ".account use <id|@имя>"):
            self.assertIn(part, text)
        text = await ask(dispatcher, ".help status")
        self.assertIn(".status all", text)
        text = await ask(dispatcher, ".help update")
        self.assertIn(".update check", text)
        text = await ask(dispatcher, ".help backup")
        for part in ("create", "list", "restore"):
            self.assertIn(part, text)

    async def test_unknown_command_suggests_similar(self):
        dispatcher, _ = await build()
        text = await ask(dispatcher, ".help lable")
        self.assertIn("Команды .lable нет", text)
        self.assertIn(".label", text)
        self.assertIn("Список: .help", await ask(dispatcher, ".help qqqq"))

    async def test_help_with_extra_arguments_shows_usage(self):
        dispatcher, _ = await build()
        self.assertIn("Использование: .help [команда]", await ask(dispatcher, ".help a b"))

    async def test_command_without_details_still_has_help(self):
        dispatcher = Dispatcher()
        register_commands(dispatcher, AsyncMock())
        dispatcher.register_command("demo", AsyncMock(), description="пример", usage=".demo")
        text = await ask(dispatcher, ".help demo")
        self.assertIn(".demo — пример", text)
        self.assertIn("Подробного описания у этой команды нет", text)

    def test_formatters_are_pure(self):
        dispatcher = Dispatcher()
        command = dispatcher.register_command("x", AsyncMock(), description="d", details="  подробно  ")
        self.assertEqual(command.details, "подробно")
        self.assertIs(find_command([command], ".X"), command)
        self.assertIsNone(find_command([command], "y"))
        self.assertIn(".x — d", help_list([command]))
        self.assertIn("подробно", help_command_text(command))


class StatusCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_command_is_gone_and_status_has_two_modes(self):
        dispatcher, _ = await build()
        self.assertNotIn("health", dispatcher.commands)
        self.assertIn("Использование: .status [all]", await ask(dispatcher, ".status foo"))


class RestartAndMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_answers_and_requests_restart(self):
        app = SimpleNamespace(dispatcher=Dispatcher(), access=None, request_restart=AsyncMock())
        module = RestartModule(app)
        await module.start()
        self.assertFalse(module.required)
        self.assertIn("Перезапускаюсь", await ask(app.dispatcher, ".restart"))
        app.request_restart.assert_awaited_once()

    async def test_restart_with_arguments_only_shows_usage(self):
        app = SimpleNamespace(dispatcher=Dispatcher(), access=None, request_restart=AsyncMock())
        await RestartModule(app).start()
        self.assertIn("Использование", await ask(app.dispatcher, ".restart now"))
        app.request_restart.assert_not_awaited()

    async def test_memory_usage_is_a_positive_number_or_none(self):
        from core.health import memory_usage_mb

        value = memory_usage_mb()
        self.assertTrue(value is None or value > 1)

    async def test_status_shows_memory_line_when_available(self):
        from unittest.mock import patch

        report = SimpleNamespace(lines=lambda: ["a", "b", "c", "• проблема"])
        app = SimpleNamespace(dispatcher=Dispatcher(), access=None, health=lambda: report)
        await StatusModule(app).start()
        with patch("modules.system.status.memory_usage_mb", return_value=23.6):
            text = await ask(app.dispatcher, ".status")
        self.assertEqual(text.splitlines()[1:], ["a", "b", "c", "Память: 24 МБ", "• проблема"])
        with patch("modules.system.status.memory_usage_mb", return_value=None):
            self.assertNotIn("Память", await ask(app.dispatcher, ".status"))



if __name__ == "__main__":
    unittest.main()
