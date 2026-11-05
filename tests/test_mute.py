"""Команда .mute: разбор, режимы, подтверждения, остановка ответом stop, срок и возобновление."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.access import AccessService
from core.dispatcher import Dispatcher
from core.errors import UserError
from modules.utility.mute import (
    MAX_SECONDS,
    MIN_SECONDS,
    MuteModule,
    format_left,
    parse_duration,
    parse_mute,
)
from storage.service import Storage

try:
    from telethon.errors import FloodWaitError
except ImportError:  # Telethon не установлен
    FloodWaitError = None

CHAT = 100
ANCHOR = 500


class ParseTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(parse_duration(["30s"]), 30)
        self.assertEqual(parse_duration(["10m"]), 600)
        self.assertEqual(parse_duration(["2h"]), 7200)
        self.assertEqual(parse_duration(["1d"]), 86400)
        self.assertEqual(parse_duration(["1h30m"]), 5400)
        self.assertEqual(parse_duration(["1h", "30m"]), 5400)
        self.assertEqual(parse_duration(["10"]), 600)  # без буквы: минуты
        self.assertEqual(parse_duration(["5мин"]), 300)
        self.assertEqual(parse_duration(["2ч"]), 7200)
        self.assertEqual(parse_duration(["90С"]), 90)

    def test_bad_durations(self):
        for tokens in (["abc"], ["h"], ["10x"], ["1.5h"], [f"{MIN_SECONDS - 1}s"], ["0"], [f"{MAX_SECONDS + 1}s"], ["-5m"]):
            with self.subTest(tokens=tokens), self.assertRaises(UserError):
                parse_duration(tokens)

    def test_parse_mute(self):
        self.assertEqual(parse_mute([]).action, "start")
        self.assertIsNone(parse_mute([]).seconds)
        request = parse_mute(["10m", "--all"])
        self.assertEqual((request.action, request.seconds, request.everyone), ("start", 600, True))
        self.assertTrue(parse_mute(["--ALL"]).everyone)
        self.assertEqual(parse_mute(["off"]).action, "off")
        self.assertEqual(parse_mute(["LIST"]).action, "list")
        for bad in (["--all", "--all"], ["--x"], ["off", "5m"], ["5m", "off"], ["list", "list"]):
            with self.subTest(bad=bad), self.assertRaises(UserError):
                parse_mute(bad)

    def test_format_left(self):
        self.assertEqual(format_left(None), "пока не остановят")
        self.assertEqual(format_left(65), "1 мин 5 с")
        self.assertEqual(format_left(3700), "1 ч 1 мин")
        self.assertEqual(format_left(90000), "1 д 1 ч")
        self.assertEqual(format_left(-3), "0 с")


def make_message(message_id, *, out=False, text="", sender=2, reply_to=None, chat_id=CHAT, **attrs):
    values = dict(
        id=message_id, out=out, raw_text=text, sender_id=1 if out else sender, reply_to_msg_id=reply_to,
        chat_id=chat_id, is_private=False, is_group=True, is_channel=False,
    )
    values.update(attrs)
    return SimpleNamespace(**values)


class MuteTests(unittest.IsolatedAsyncioTestCase):
    def build_app(self):
        identity = SimpleNamespace(account_id=AsyncMock(return_value=7))
        return SimpleNamespace(
            dispatcher=Dispatcher(),
            client=self.client,
            storage=self.storage,
            identity=identity,
            access=AccessService(self.storage.permissions, identity),
            notify_owner=AsyncMock(return_value=True),
        )

    def grant(self, subject, level):
        self.storage.permissions.grant(account_id=7, subject_id=subject, permission=level, granted_by=7)

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Storage(Path(self.tmp.name) / "t.sqlite3")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.deleted: list[tuple[int, list[int]]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.delete_error: Exception | None = None
        self.rights = SimpleNamespace(is_creator=False, is_admin=True, delete_messages=True)
        self.timers: list[tuple[float, asyncio.Future]] = []

        async def delete_messages(chat_id, ids):
            if self.delete_error is not None:
                raise self.delete_error
            self.deleted.append((chat_id, list(ids)))

        async def edit_message(chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

        self.client = SimpleNamespace(
            delete_messages=delete_messages,
            edit_message=edit_message,
            get_permissions=AsyncMock(side_effect=lambda *a, **k: self.rights),
            get_me=AsyncMock(return_value=SimpleNamespace(id=1)),
        )
        self.app = self.build_app()
        self.module = self.make_module(self.app)
        await self.module.start()
        self.addAsyncCleanup(self.module.stop)

    def make_module(self, app):
        module = MuteModule(app)

        async def fake_sleep(seconds):
            if seconds > 1:  # длинные ожидания (срок, подтверждение) стоят, пока тест не «дёрнет» их
                future = asyncio.get_running_loop().create_future()
                self.timers.append((seconds, future))
                await future
            else:
                await asyncio.sleep(0)

        module._sleep = fake_sleep
        module._now = lambda: 1000.0 + self.clock_shift
        return module

    clock_shift = 0.0

    async def settle(self):
        for _ in range(30):
            await asyncio.sleep(0)

    async def deliver(self, event, app=None):
        """Доставляет сообщение так, как это делает EventBridge: сначала обработчики, потом команды."""
        app = app or self.app
        await app.dispatcher.dispatch("new_message", event)
        if event.out:
            await app.dispatcher.dispatch_command(event)
        await self.settle()

    async def command(self, text, *, message_id=ANCHOR, reply=None, **attrs):
        edits: list[str] = []

        async def edit(value):
            edits.append(value)

        async def get_reply_message():
            return reply

        event = make_message(
            message_id, out=True, text=text, edit=edit, get_reply_message=get_reply_message, **attrs
        )
        event.edits = edits
        event.reply_to_msg_id = getattr(reply, "id", None)
        await self.deliver(event)
        return event

    def deleted_ids(self):
        return [number for _, ids in self.deleted for number in ids]

    def saved(self):
        return self.storage.plugin_data.get(account_id=7, plugin="utility.mute", key="active")

    # ---------------------------------------------------------- личный чат

    async def test_private_chat_deletes_everything_after_command(self):
        await self.command(".mute", is_private=True, is_group=False)
        await self.deliver(make_message(501, sender=2, is_private=True, is_group=False))
        await self.deliver(make_message(502, out=True, text="привет", is_private=True, is_group=False))
        await self.deliver(make_message(400, sender=2, is_private=True, is_group=False))  # старее команды
        self.assertEqual(sorted(self.deleted_ids()), [501, 502])
        self.assertIn("🔇 mute", self.edits[-1][2])
        self.assertIn("stop", self.edits[-1][2])
        self.assertEqual(self.saved()[0]["mode"], "chat")

    async def test_stop_reply_deletes_command_and_stop_and_ends(self):
        await self.command(".mute", is_private=True, is_group=False)
        await self.deliver(make_message(501, is_private=True, is_group=False))
        await self.deliver(make_message(502, out=True, text="Stop", reply_to=ANCHOR, is_private=True, is_group=False))
        self.assertIn(([ANCHOR, 502]), [ids for _, ids in self.deleted])
        self.assertIn(501, self.deleted_ids())
        self.assertNotIn(CHAT, self.module._active)
        self.assertIsNone(self.saved())
        self.assertEqual(self.app.dispatcher.handlers["new_message"], [])  # наблюдатель отключён
        await self.deliver(make_message(503, is_private=True, is_group=False))
        self.assertNotIn(503, self.deleted_ids())
        self.app.notify_owner.assert_not_awaited()  # ручная остановка без шума

    async def test_stop_in_russian_and_only_as_reply_to_the_command(self):
        await self.command(".mute", is_private=True, is_group=False)
        await self.deliver(make_message(502, out=True, text="stop", reply_to=999, is_private=True, is_group=False))
        self.assertIn(CHAT, self.module._active)  # ответ не на команду: не остановка
        await self.deliver(make_message(503, out=True, text="стоп", reply_to=ANCHOR, is_private=True, is_group=False))
        self.assertNotIn(CHAT, self.module._active)

    async def test_off_command_works_even_while_everything_is_deleted(self):
        await self.command(".mute", is_private=True, is_group=False)
        await self.command(".mute off", message_id=510, is_private=True, is_group=False)
        self.assertNotIn(CHAT, self.module._active)
        self.assertNotIn(510, [n for _, ids in self.deleted[:-1] for n in ids])  # не удалено наблюдателем
        self.assertIn(510, self.deleted[-1][1])  # убрано самой командой
        self.assertIn(ANCHOR, self.deleted_ids())
        self.assertIn("остановлен", self.app.notify_owner.await_args.args[0])

    async def test_all_flag_is_refused_in_private_chat(self):
        event = await self.command(".mute --all", is_private=True, is_group=False)
        self.assertIn("только в группах", event.edits[-1])
        self.assertEqual(self.module._active, {})

    # ---------------------------------------------------------------- группы

    async def test_group_default_deletes_only_my_messages(self):
        await self.command(".mute")
        await self.deliver(make_message(501, sender=2))
        await self.deliver(make_message(502, out=True, text="мой"))
        self.assertEqual(self.deleted_ids(), [502])
        self.client.get_permissions.assert_not_awaited()  # права для своих не нужны

    async def test_all_flag_needs_rights_first(self):
        self.rights = SimpleNamespace(is_creator=False, is_admin=True, delete_messages=False)
        event = await self.command(".mute --all")
        self.assertIn("Нет прав", event.edits[-1])
        self.assertEqual((self.module._active, self.module._pending), ({}, {}))

    async def test_all_flag_needs_two_confirmations(self):
        event = await self.command(".mute 10m --all")
        self.assertIn("1 из 2", event.edits[-1])
        self.assertEqual(self.module._active, {})
        # Обычное сообщение и неверное слово ничего не меняют.
        await self.deliver(make_message(501, sender=2))
        await self.deliver(make_message(502, out=True, text="подтверждаю", reply_to=ANCHOR))
        self.assertEqual(self.module._pending[CHAT].stage, 1)
        self.assertEqual(self.deleted_ids(), [])
        # Первое подтверждение.
        await self.deliver(make_message(503, out=True, text="Да", reply_to=ANCHOR))
        self.assertEqual(self.module._pending[CHAT].stage, 2)
        self.assertIn("2 из 2", self.edits[-1][2])
        self.assertEqual(self.deleted_ids(), [503])  # ответ-подтверждение убран
        self.assertEqual(self.module._active, {})
        # Второе подтверждение включает режим.
        await self.deliver(make_message(504, out=True, text="подтверждаю", reply_to=ANCHOR))
        self.assertIn(CHAT, self.module._active)
        self.assertEqual(self.module._active[CHAT].mode, "all")
        self.assertIn(504, self.deleted_ids())
        self.assertIn("🔇 mute", self.edits[-1][2])
        # Теперь удаляются сообщения всех.
        await self.deliver(make_message(505, sender=2))
        await self.deliver(make_message(506, sender=3))
        self.assertTrue({505, 506} <= set(self.deleted_ids()))
        self.assertEqual(self.saved()[0]["until"], 1600.0)

    async def test_second_word_does_not_skip_first_confirmation(self):
        await self.command(".mute --all")
        await self.deliver(make_message(502, out=True, text="подтверждаю", reply_to=ANCHOR))
        await self.deliver(make_message(503, out=True, text="yes", reply_to=ANCHOR))
        await self.deliver(make_message(504, out=True, text="да", reply_to=ANCHOR))  # «да» на втором шаге не принимается
        self.assertEqual(self.module._pending[CHAT].stage, 2)
        self.assertEqual(self.module._active, {})

    async def test_confirmation_can_be_cancelled(self):
        await self.command(".mute --all")
        await self.deliver(make_message(502, out=True, text="стоп", reply_to=ANCHOR))
        self.assertEqual(self.module._pending, {})
        self.assertIn([502, ANCHOR], [ids for _, ids in self.deleted])
        self.assertEqual(self.app.dispatcher.handlers["new_message"], [])

    async def test_confirmation_times_out(self):
        await self.command(".mute --all")
        self.assertEqual([seconds for seconds, _ in self.timers], [120])
        self.timers[0][1].set_result(None)
        await self.settle()
        self.assertEqual(self.module._pending, {})
        self.assertIn("не включён", self.edits[-1][2])
        self.assertEqual(self.app.dispatcher.handlers["new_message"], [])

    # ------------------------------------------------ флаг уровня (-user/-trusted/-admin)

    async def start_all(self, flag=""):
        await self.command(f".mute --all {flag}".strip())
        await self.deliver(make_message(502, out=True, text="да", reply_to=ANCHOR))
        await self.deliver(make_message(503, out=True, text="подтверждаю", reply_to=ANCHOR))
        self.deleted.clear()

    async def test_all_without_flag_lets_only_owner_write(self):
        self.grant(2, "admin")
        await self.start_all()
        await self.deliver(make_message(510, out=True, text="я"))
        await self.deliver(make_message(511, sender=2))  # даже admin
        await self.deliver(make_message(512, sender=3))
        self.assertEqual(sorted(self.deleted_ids()), [511, 512])

    async def test_level_flag_lets_that_level_and_above_write(self):
        for subject, level in ((2, "user"), (3, "trusted"), (4, "admin")):
            self.grant(subject, level)
        await self.start_all("-trusted")
        for number, sender in ((520, 2), (521, 3), (522, 4), (523, 5)):
            await self.deliver(make_message(number, sender=sender))
        self.assertEqual(sorted(self.deleted_ids()), [520, 523])  # user и без уровня удаляются
        self.assertEqual(self.module._active[CHAT].allow, "trusted")
        self.assertEqual(self.saved()[0]["allow"], "trusted")

    async def test_each_level_flag(self):
        for subject, level in ((2, "user"), (3, "trusted"), (4, "admin")):
            self.grant(subject, level)
        expected = {"-user": [530 + 3], "-trusted": [530, 533], "--admin": [530, 531, 533]}
        for flag, gone in expected.items():
            with self.subTest(flag=flag):
                await self.module._finish(self.module._active[CHAT], reason="тест", quiet=True) if CHAT in self.module._active else None
                self.deleted.clear()
                await self.start_all(flag)
                for number, sender in ((530, 2), (531, 3), (532, 4), (533, 5)):
                    await self.deliver(make_message(number, sender=sender))
                self.assertEqual(sorted(self.deleted_ids()), gone)

    async def test_level_flag_in_private_chat(self):
        self.grant(2, "trusted")
        await self.command(".mute -trusted", is_private=True, is_group=False)
        await self.deliver(make_message(540, sender=2, is_private=True, is_group=False))
        await self.deliver(make_message(541, sender=3, is_private=True, is_group=False))
        self.assertEqual(self.deleted_ids(), [541])

    async def test_level_flag_needs_all_or_private_and_no_reply(self):
        event = await self.command(".mute -user")
        self.assertIn("с --all", event.edits[-1])
        self.assertEqual(self.module._active, {})
        target = make_message(300, sender=2)
        event = await self.command(".mute --all -user", reply=target)
        self.assertIn("нельзя совмещать", event.edits[-1])

    async def test_level_flag_parse_and_restore(self):
        self.assertEqual(parse_mute(["--all", "-admin"]).allow, "admin")
        self.assertEqual(parse_mute(["10m", "--user", "--all"]).allow, "user")
        for bad in (["-user", "-admin"], ["--all", "-owner"], ["off", "-user"]):
            with self.subTest(bad=bad), self.assertRaises(UserError):
                parse_mute(bad)
        self.grant(2, "user")
        await self.start_all("-user")
        await self.module.stop()
        again = self.make_module(self.app)
        await again.start()
        self.addAsyncCleanup(again.stop)
        self.assertEqual(again._active[CHAT].allow, "user")
        await self.deliver(make_message(550, sender=2))
        await self.deliver(make_message(551, sender=9))
        self.assertEqual(self.deleted_ids(), [551])

    async def test_failed_level_check_deletes(self):
        self.grant(2, "admin")
        await self.start_all("-user")
        self.app.access.has_level = AsyncMock(side_effect=RuntimeError("база"))
        await self.deliver(make_message(560, sender=2))
        self.assertEqual(self.deleted_ids(), [560])

    async def test_all_and_reply_cannot_be_combined(self):
        target = make_message(300, sender=2)
        event = await self.command(".mute --all", reply=target)
        self.assertIn("нельзя совмещать", event.edits[-1])

    async def test_reply_targets_one_user_and_needs_rights(self):
        target = make_message(300, sender=2)

        async def get_sender():
            return SimpleNamespace(username="test1")

        target.get_sender = get_sender
        await self.command(".mute", reply=target)
        mute = self.module._active[CHAT]
        self.assertEqual((mute.mode, mute.target_id, mute.label), ("user", 2, "@test1"))
        self.client.get_permissions.assert_awaited_once()
        await self.deliver(make_message(501, sender=2))
        await self.deliver(make_message(502, sender=3))
        await self.deliver(make_message(503, out=True, text="мой"))
        self.assertEqual(self.deleted_ids(), [501])
        self.assertIn("@test1", self.edits[-1][2])

    async def test_reply_without_rights_is_refused(self):
        self.rights = None
        event = await self.command(".mute", reply=make_message(300, sender=2))
        self.assertIn("Нет прав", event.edits[-1])
        self.assertEqual(self.module._active, {})

    async def test_reply_to_my_own_message_means_own_mode(self):
        await self.command(".mute", reply=make_message(300, out=True))
        self.assertEqual(self.module._active[CHAT].mode, "own")
        self.client.get_permissions.assert_not_awaited()

    async def test_reply_to_message_without_author_is_refused(self):
        event = await self.command(".mute", reply=make_message(300, sender=None, sender_id=None))
        self.assertIn("нет автора", event.edits[-1])

    async def test_channels_are_not_supported(self):
        event = await self.command(".mute", is_channel=True, is_group=False)
        self.assertIn("каналах", event.edits[-1])

    async def test_second_mute_in_same_chat_is_refused(self):
        await self.command(".mute")
        event = await self.command(".mute 5m", message_id=510)
        self.assertIn("уже включён", event.edits[-1])
        self.assertEqual(self.module._active[CHAT].anchor_id, ANCHOR)

    async def test_other_chats_are_untouched(self):
        await self.command(".mute --all")
        for number, word in ((502, "да"), (503, "подтверждаю")):
            await self.deliver(make_message(number, out=True, text=word, reply_to=ANCHOR))
        await self.deliver(make_message(900, sender=2, chat_id=999))
        self.assertNotIn(900, self.deleted_ids())

    # ----------------------------------------------------------------- срок

    async def test_timed_mute_ends_deletes_command_and_notifies(self):
        await self.command(".mute 10m", is_private=True, is_group=False)
        self.assertEqual([seconds for seconds, _ in self.timers], [600])
        await self.deliver(make_message(501, is_private=True, is_group=False))
        self.timers[0][1].set_result(None)
        await self.settle()
        self.assertNotIn(CHAT, self.module._active)
        self.assertIn([ANCHOR], [ids for _, ids in self.deleted])
        self.assertIn("срок вышел", self.app.notify_owner.await_args.args[0])
        self.assertIn("Удалено сообщений: 1", self.app.notify_owner.await_args.args[0])
        self.assertIsNone(self.saved())

    async def test_forever_mute_has_no_timer(self):
        await self.command(".mute", is_private=True, is_group=False)
        self.assertEqual(self.timers, [])
        self.assertIsNone(self.saved()[0]["until"])

    async def test_list(self):
        self.assertIn("нет", (await self.command(".mute list")).edits[-1])
        await self.command(".mute 10m", message_id=510, is_private=True, is_group=False, chat_id=CHAT)
        text = (await self.command(".mute list", message_id=511, chat_id=CHAT + 1)).edits[-1]
        self.assertIn(f"чат {CHAT}", text)
        self.assertIn("10 мин", text)

    # ------------------------------------------------------ пачки и ошибки

    async def test_bursts_are_deleted_in_chunks_of_100(self):
        await self.command(".mute", is_private=True, is_group=False)
        for number in range(501, 651):
            await self.module._on_message(make_message(number, is_private=True, is_group=False))
        await self.settle()
        self.assertEqual([len(ids) for _, ids in self.deleted], [100, 50])
        self.assertEqual(self.module._active[CHAT].deleted, 150)

    async def test_repeated_delete_failures_stop_the_mute_and_notify(self):
        await self.command(".mute --all")
        for number, word in ((502, "да"), (503, "подтверждаю")):
            await self.deliver(make_message(number, out=True, text=word, reply_to=ANCHOR))
        self.delete_error = RuntimeError("права пропали")
        for number in range(510, 514):
            await self.deliver(make_message(number, sender=2))
        self.assertNotIn(CHAT, self.module._active)
        self.assertIn("проверьте права", self.app.notify_owner.await_args.args[0])
        self.assertIsNone(self.saved())

    @unittest.skipIf(FloodWaitError is None, "Telethon не установлен")
    async def test_short_flood_wait_is_retried(self):
        await self.command(".mute", is_private=True, is_group=False)
        calls = {"n": 0}
        original = self.client.delete_messages

        async def flood_once(chat_id, ids):
            calls["n"] += 1
            if calls["n"] == 1:
                error = FloodWaitError.__new__(FloodWaitError)
                error.seconds = 0  # пауза 0+1 с: в тесте проходит без ожидания
                raise error
            await original(chat_id, ids)

        self.client.delete_messages = flood_once
        with self.assertLogs("candy", level="WARNING"):
            await self.deliver(make_message(501, is_private=True, is_group=False))
        self.assertEqual(self.deleted_ids(), [501])
        self.assertIn(CHAT, self.module._active)

    # ------------------------------------------------------- перезапуск

    async def test_active_mutes_survive_restart(self):
        await self.command(".mute 10m", is_private=True, is_group=False)
        await self.module.stop()
        self.assertIsNotNone(self.saved())  # остановка модуля не стирает сохранённое

        app = self.build_app()
        module = self.make_module(app)
        await module.start()
        self.addAsyncCleanup(module.stop)
        self.assertIn(CHAT, module._active)
        self.assertEqual(module._active[CHAT].anchor_id, ANCHOR)
        self.assertIn("возобновлено", app.notify_owner.await_args.args[0])
        await self.deliver(make_message(501, is_private=True, is_group=False), app=app)
        self.assertEqual(self.deleted_ids(), [501])

    async def test_expired_mutes_are_dropped_on_restart(self):
        await self.command(".mute 10m", is_private=True, is_group=False)
        await self.module.stop()
        type(self).clock_shift = 5000.0
        self.addCleanup(setattr, type(self), "clock_shift", 0.0)
        app = self.build_app()
        module = self.make_module(app)
        await module.start()
        self.addAsyncCleanup(module.stop)
        self.assertEqual(module._active, {})
        self.assertIsNone(self.saved())
        app.notify_owner.assert_not_awaited()

    async def test_stop_of_module_releases_the_watcher(self):
        await self.command(".mute", is_private=True, is_group=False)
        await self.module.stop()
        self.assertEqual(self.app.dispatcher.handlers["new_message"], [])
        self.assertNotIn("mute", self.app.dispatcher.commands)


if __name__ == "__main__":
    unittest.main()
