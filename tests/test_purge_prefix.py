"""Команды .purge и .setprefix: точность удаления, права, тишина, префикс и справка."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.commands import help_command_text, help_list, register_commands, with_prefix
from core.dispatcher import Dispatcher
from core.errors import UserError
from modules.system.prefix import PrefixModule, validate_prefix
from modules.utility.purge import MAX_PURGE, PurgeModule, can_delete_others, parse_purge
from storage.service import Storage

try:
    from telethon.errors import FloodWaitError
except ImportError:  # Telethon не установлен
    FloodWaitError = None


def make_event(text, **attrs):
    edits: list[str] = []

    async def edit(value):
        edits.append(value)

    values = dict(
        raw_text=text, edit=edit, edits=edits, out=True, chat_id=100, id=500,
        is_private=False, is_group=True, is_channel=False, delete=AsyncMock(),
    )
    values.update(attrs)
    return SimpleNamespace(**values)


class ParseTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_purge(["10"]).count, 10)
        self.assertFalse(parse_purge(["10"]).everyone)
        request = parse_purge(["10", "ALL"])
        self.assertEqual((request.count, request.everyone), (10, True))
        self.assertEqual(parse_purge(["all", "3"]).count, 3)

    def test_invalid(self):
        for args in ([], ["all"], ["abc"], ["0"], [str(MAX_PURGE + 1)], ["5", "6"], ["5", "all", "all"], ["-3"], ["5", "x"]):
            with self.subTest(args=args), self.assertRaises(UserError):
                parse_purge(args)

    def test_rights(self):
        admin = SimpleNamespace(is_creator=False, is_admin=True, delete_messages=True)
        self.assertTrue(can_delete_others(admin))
        self.assertTrue(can_delete_others(SimpleNamespace(is_creator=True, is_admin=True, delete_messages=False)))
        self.assertFalse(can_delete_others(SimpleNamespace(is_creator=False, is_admin=True, delete_messages=False)))
        self.assertFalse(can_delete_others(SimpleNamespace(is_creator=False, is_admin=False, delete_messages=False)))
        self.assertFalse(can_delete_others(None))


def messages(ids, out=True):
    return [SimpleNamespace(id=number, out=out) for number in ids]


class PurgeCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.deleted: list[list[int]] = []
        self.calls: list[dict] = []
        self.history = messages(range(499, 0, -1))  # от новых к старым, ниже команды (500)

        async def delete_messages(chat_id, ids):
            self.deleted.append(list(ids))

        def iter_messages(chat_id, **kwargs):
            self.calls.append(kwargs)

            async def generator():
                limit = kwargs.get("limit")
                max_id = kwargs.get("max_id")
                sent = 0
                for message in self.history:
                    if max_id and message.id >= max_id:
                        continue
                    if limit is not None and sent >= limit:
                        return
                    sent += 1
                    yield message
            return generator()

        self.client = SimpleNamespace(
            delete_messages=delete_messages,
            iter_messages=iter_messages,
            get_permissions=AsyncMock(return_value=SimpleNamespace(is_creator=False, is_admin=True, delete_messages=True)),
        )
        self.app = SimpleNamespace(dispatcher=Dispatcher(), client=self.client, notify_owner=AsyncMock())
        self.module = PurgeModule(self.app)
        self.pauses: list[float] = []

        async def fake_sleep(seconds):
            self.pauses.append(seconds)

        self.module._sleep = fake_sleep
        await self.module.start()

    async def asyncTearDown(self):
        await self.module.stop()

    async def run_command(self, text, **attrs):
        event = make_event(text, **attrs)
        await self.app.dispatcher.dispatch_command(event)
        return event

    def all_deleted(self):
        return [number for chunk in self.deleted for number in chunk]

    async def test_is_optional_and_registered(self):
        self.assertFalse(self.module.required)
        self.assertIn("purge", self.app.dispatcher.commands)

    async def test_deletes_exactly_n_own_messages_and_not_more(self):
        event = await self.run_command(".purge 10")
        self.assertEqual(self.all_deleted(), list(range(499, 489, -1)))
        self.assertEqual(len(self.all_deleted()), 10)
        self.assertNotIn(500, self.all_deleted())  # команда удаляется отдельно
        event.delete.assert_awaited_once()
        self.assertEqual(event.edits, [])  # тишина: никаких ответов
        self.app.notify_owner.assert_not_awaited()
        self.assertEqual(self.calls[0]["from_user"], "me")
        self.assertEqual(self.calls[0]["limit"], 10)
        self.assertEqual(self.calls[0]["max_id"], 500)

    async def test_command_is_deleted_before_anything_else(self):
        order: list[str] = []
        event = make_event(".purge 3")

        async def delete():
            order.append("command")

        event.delete = delete
        original = self.client.delete_messages

        async def tracking(chat_id, ids):
            order.append("messages")
            await original(chat_id, ids)

        self.client.delete_messages = tracking
        await self.app.dispatcher.dispatch_command(event)
        self.assertEqual(order, ["command", "messages"])

    async def test_never_more_than_n_even_if_source_returns_more(self):
        # Источник игнорирует limit и отдаёт всё подряд: всё равно удаляется ровно 7.
        def greedy(chat_id, **kwargs):
            async def generator():
                for message in self.history:
                    yield message
            return generator()

        self.client.iter_messages = greedy
        await self.run_command(".purge 7")
        self.assertEqual(len(self.all_deleted()), 7)

    async def test_newer_than_command_and_foreign_ids_are_skipped(self):
        self.history = messages([700, 500, 499, 498, 498, 497])  # 700 и 500 не старше команды, 498 повторяется
        await self.run_command(".purge 10")
        self.assertEqual(self.all_deleted(), [499, 498, 497])

    async def test_fewer_messages_than_requested(self):
        self.history = messages([5, 4, 3])
        await self.run_command(".purge 50")
        self.assertEqual(self.all_deleted(), [5, 4, 3])

    async def test_nothing_to_delete_is_silent(self):
        self.history = []
        event = await self.run_command(".purge 5")
        self.assertEqual(self.deleted, [])
        self.assertEqual(event.edits, [])
        self.app.notify_owner.assert_not_awaited()

    async def test_large_count_is_split_in_chunks_of_100(self):
        self.history = messages(range(499, 0, -1))
        await self.run_command(".purge 250")
        self.assertEqual([len(chunk) for chunk in self.deleted], [100, 100, 50])
        self.assertEqual(len(set(self.all_deleted())), 250)

    async def test_all_requires_rights_and_deletes_everyones(self):
        self.history = messages(range(499, 0, -1), out=False)
        await self.run_command(".purge 5 all")
        self.client.get_permissions.assert_awaited_once()
        self.assertEqual(self.all_deleted(), list(range(499, 494, -1)))
        self.assertNotIn("from_user", self.calls[0])

    async def test_all_without_rights_deletes_nothing_and_explains(self):
        self.client.get_permissions = AsyncMock(
            return_value=SimpleNamespace(is_creator=False, is_admin=True, delete_messages=False)
        )
        event = await self.run_command(".purge 5 all")
        self.assertEqual(self.deleted, [])
        self.assertEqual(self.calls, [])
        event.delete.assert_awaited_once()  # команда всё равно убрана
        self.assertIn("нет прав", self.app.notify_owner.await_args.args[0])

    async def test_all_in_private_chat_needs_no_permission_check(self):
        await self.run_command(".purge 4 all", is_private=True, is_group=False)
        self.client.get_permissions.assert_not_awaited()
        self.assertEqual(len(self.all_deleted()), 4)

    async def test_own_messages_in_broadcast_channel_are_filtered_by_out(self):
        history = []
        for number in range(499, 0, -1):
            history.append(SimpleNamespace(id=number, out=number % 2 == 0))
        self.history = history
        await self.run_command(".purge 3", is_channel=True, is_group=False)
        self.assertEqual(self.all_deleted(), [498, 496, 494])
        self.assertNotIn("from_user", self.calls[0])

    async def test_bad_arguments_answer_visibly_and_delete_nothing(self):
        event = await self.run_command(".purge abc")
        self.assertIn("Использование", event.edits[-1])
        event.delete.assert_not_awaited()
        self.assertEqual(self.deleted, [])

    async def test_failure_goes_to_saved_messages_not_chat(self):
        async def broken(chat_id, ids):
            raise RuntimeError("нет доступа")

        self.client.delete_messages = broken
        with self.assertLogs("candy", level="ERROR"):
            event = await self.run_command(".purge 2")
        self.assertEqual(event.edits, [])
        self.assertIn("нет доступа", self.app.notify_owner.await_args.args[0])

    @unittest.skipIf(FloodWaitError is None, "Telethon не установлен")
    async def test_short_flood_wait_is_waited_and_retried(self):
        calls = {"n": 0}
        original = self.client.delete_messages

        async def flood_once(chat_id, ids):
            calls["n"] += 1
            if calls["n"] == 1:
                error = FloodWaitError.__new__(FloodWaitError)
                error.seconds = 3
                raise error
            await original(chat_id, ids)

        self.client.delete_messages = flood_once
        with self.assertLogs("candy", level="WARNING"):
            await self.run_command(".purge 2")
        self.assertEqual(len(self.all_deleted()), 2)
        self.assertEqual(self.pauses, [4])
        self.app.notify_owner.assert_not_awaited()


class PrefixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Storage(Path(self.tmp.name) / "t.sqlite3")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.identity = SimpleNamespace(account_id=AsyncMock(return_value=7))
        self.app = self.make_app()
        self.module = PrefixModule(self.app)
        await self.module.start()

    def make_app(self):
        return SimpleNamespace(dispatcher=Dispatcher(), storage=self.storage, identity=self.identity)

    async def say(self, text, app=None):
        app = app or self.app
        event = make_event(text)
        await app.dispatcher.dispatch_command(event)
        return event.edits[-1] if event.edits else None

    def test_validation(self):
        for good in ("!", "/", ",", "!!", "$$$", "→"):
            self.assertEqual(validate_prefix(good), good)
        for bad in ("", "abcd", "a", "1", "@", " ", "! ", "!!!!", "\n", "x!"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_prefix(bad)

    async def test_show_current(self):
        self.assertIn("Префикс команд: .", await self.say(".setprefix"))

    async def test_change_then_commands_use_new_prefix(self):
        reply = await self.say(".setprefix !")
        self.assertIn("Префикс команд: !", reply)
        self.assertEqual(self.app.dispatcher.parser.prefix, "!")
        self.assertIn("Префикс команд: !", await self.say("!setprefix"))
        self.assertIsNone(await self.say(".setprefix"))  # старая точка больше не команда

    async def test_reset_returns_dot_and_forgets_saved_value(self):
        await self.say(".setprefix !")
        await self.say("!setprefix reset")
        self.assertEqual(self.app.dispatcher.parser.prefix, ".")
        again = self.make_app()
        await PrefixModule(again).start()
        self.assertEqual(again.dispatcher.parser.prefix, ".")

    async def test_prefix_survives_restart_and_is_per_account(self):
        await self.say(".setprefix !")
        restarted = self.make_app()
        await PrefixModule(restarted).start()
        self.assertEqual(restarted.dispatcher.parser.prefix, "!")
        other = self.make_app()
        other.identity = SimpleNamespace(account_id=AsyncMock(return_value=8))
        await PrefixModule(other).start()
        self.assertEqual(other.dispatcher.parser.prefix, ".")

    async def test_invalid_input_changes_nothing(self):
        for text in (".setprefix abc", ".setprefix @", ".setprefix !!!!", ".setprefix a b"):
            with self.subTest(text=text):
                reply = await self.say(text)
                self.assertTrue(reply.startswith("❌") or "Использование" in reply)
        self.assertEqual(self.app.dispatcher.parser.prefix, ".")

    async def test_bad_saved_value_falls_back_to_dot(self):
        self.storage.plugin_data.set(account_id=7, plugin="core.prefix", key="prefix", value="abc")
        app = self.make_app()
        with self.assertLogs("candy", level="WARNING"):
            await PrefixModule(app).start()
        self.assertEqual(app.dispatcher.parser.prefix, ".")


class HelpPrefixTests(unittest.IsolatedAsyncioTestCase):
    async def test_with_prefix_replaces_only_known_commands(self):
        text = "Команды: .help и .label, файл .env и candy.help, версия 1.2"
        result = with_prefix(text, "!", ["help", "label"])
        self.assertEqual(result, "Команды: !help и !label, файл .env и candy.help, версия 1.2")
        self.assertEqual(with_prefix(text, ".", ["help"]), text)

    async def test_help_uses_new_prefix_and_accepts_it_in_requests(self):
        dispatcher = Dispatcher(command_prefix="!")
        register_commands(dispatcher, AsyncMock())
        commands = dispatcher.command_help()
        listing = help_list(commands, "!")
        self.assertIn("!help — ", listing)
        self.assertIn("Подробнее: !help <команда>", listing)
        self.assertNotIn(".help", listing)
        text = help_command_text(commands[0], "!", [item.name for item in commands])
        self.assertIn("📖 !help", text)

        sent: list[str] = []

        async def edit(value):
            sent.append(value)

        for query in ("!help !stop", "!help stop", "!help .stop"):
            await dispatcher.dispatch_command(SimpleNamespace(raw_text=query, edit=edit, out=True))
            self.assertIn("📖 !stop", sent[-1], query)


if __name__ == "__main__":
    unittest.main()
