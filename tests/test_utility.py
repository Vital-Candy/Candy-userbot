"""Команды .user, .ping, .tag: форматирование, разбор аргументов, защита, отправка."""
from __future__ import annotations

import asyncio
import re
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.dispatcher import Dispatcher
from core.errors import UserError
from modules.utility.ping import PingModule, format_ping
from modules.utility.tag import (
    MAX_ALL,
    PER_MESSAGE,
    TagModule,
    TagRequest,
    chunks,
    is_excluded,
    mention,
    parse_tag,
)
from modules.utility.text import clean, code
from modules.utility.user import PRIVATE_FLAG, UserModule, format_user, status_text

try:
    from telethon.errors import FloodWaitError
except ImportError:  # Telethon не установлен
    FloodWaitError = None

UserStatusOffline = type("UserStatusOffline", (), {})
UserStatusOnline = type("UserStatusOnline", (), {})
User = type("User", (), {})


def make_user(**attrs):
    user = User()
    for key, value in attrs.items():
        setattr(user, key, value)
    return user


def make_event(text, **attrs):
    edits: list[str] = []

    async def edit(value):
        edits.append(value)

    values = dict(raw_text=text, edit=edit, edits=edits, out=True, chat_id=100, delete=AsyncMock())
    values.update(attrs)
    return SimpleNamespace(**values)


class TextTests(unittest.TestCase):
    def test_code_wraps_and_neutralizes_markdown(self):
        self.assertEqual(code("abc"), "`abc`")
        self.assertNotIn("`", clean("a`b"))
        for bad in ("**x**", "__x__", "~~x~~", "||x||", "[a](b)"):
            result = clean(bad)
            self.assertNotIn("**", result)
            self.assertNotIn("__", result)
            self.assertNotIn("~~", result)
            self.assertNotIn("||", result)
            self.assertNotIn("](", result)
        self.assertEqual(clean("user_name"), "user_name")  # одиночное подчёркивание не трогаем


class UserFormatTests(unittest.TestCase):
    def setUp(self):
        self.user = make_user(
            id=42, first_name="Ann", last_name="Lee", username="ann_l", phone="998901234567",
            status=UserStatusOnline(), lang_code="ru", premium=True, verified=False, contact=True,
            usernames=[SimpleNamespace(username="ann_l", active=True), SimpleNamespace(username="annie", active=True),
                       SimpleNamespace(username="old", active=False)],
            photo=SimpleNamespace(dc_id=2),
        )
        self.full = SimpleNamespace(
            about="Привет **мир**", common_chats_count=3, birthday=SimpleNamespace(day=5, month=7, year=1999),
            private_forward_name="Anna", blocked=False,
        )

    def test_everything_is_monospace_and_present(self):
        text = format_user(self.user, self.full)
        for expected in ("`42`", "`Ann Lee`", "`@ann_l`", "`@annie`", "`в сети`", "`ru`", "`3`", "`05.07.1999`", "`Anna`", "`2`"):
            self.assertIn(expected, text)
        self.assertNotIn("@old", text)
        self.assertIn("Premium", text)
        self.assertIn("в ваших контактах", text)

    def test_phone_only_with_flag(self):
        self.assertNotIn("998901234567", format_user(self.user, self.full))
        self.assertNotIn("Телефон", format_user(self.user, self.full))
        self.assertIn("`+998901234567`", format_user(self.user, self.full, private=True))

    def test_hidden_phone_with_flag(self):
        self.user.phone = None
        self.assertIn("скрыт или недоступен", format_user(self.user, self.full, private=True))

    def test_bio_cannot_break_markdown(self):
        text = format_user(self.user, self.full)
        self.assertNotIn("**", text)

    def test_missing_fields_are_skipped(self):
        text = format_user(make_user(id=1), None)
        self.assertIn("`1`", text)
        self.assertNotIn("Био", text)
        self.assertNotIn("Имя", text)

    def test_status_text(self):
        moment = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        status = UserStatusOffline()
        status.was_online = moment
        self.assertIn("2026-01-02 03:04 UTC", status_text(status))
        self.assertIsNone(status_text(None))


class UserCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = SimpleNamespace(get_entity=AsyncMock())
        self.app = SimpleNamespace(dispatcher=Dispatcher(), client=self.client)
        self.module = UserModule(self.app)
        self.module._full = AsyncMock(return_value=SimpleNamespace(about="bio"))
        await self.module.start()

    async def run_command(self, text, **attrs):
        event = make_event(text, **attrs)
        await self.app.dispatcher.dispatch_command(event)
        return event.edits[-1]

    async def test_by_username_and_id(self):
        self.client.get_entity.return_value = make_user(id=7, first_name="Bob", username="bob")
        text = await self.run_command(".user @bob")
        self.client.get_entity.assert_awaited_with("@bob")
        self.assertIn("`7`", text)
        await self.run_command(".user 7")
        self.client.get_entity.assert_awaited_with(7)

    async def test_phone_flag(self):
        self.client.get_entity.return_value = make_user(id=7, phone="123")
        self.assertNotIn("Телефон", await self.run_command(".user 7"))
        self.assertIn("`+123`", await self.run_command(f".user 7 {PRIVATE_FLAG}"))
        self.assertIn("`+123`", await self.run_command(f".user {PRIVATE_FLAG} 7"))

    async def test_reply_without_argument(self):
        sender = make_user(id=9, first_name="Zed")
        reply = SimpleNamespace(get_sender=AsyncMock(return_value=sender))
        text = await self.run_command(".user", get_reply_message=AsyncMock(return_value=reply))
        self.assertIn("`9`", text)

    async def test_no_argument_and_no_reply_shows_usage(self):
        text = await self.run_command(".user", get_reply_message=AsyncMock(return_value=None))
        self.assertIn("Использование", text)

    async def test_not_found_and_not_a_user(self):
        self.client.get_entity.side_effect = ValueError("no")
        self.assertIn("Не нашёл", await self.run_command(".user @ghost"))
        self.client.get_entity.side_effect = None
        self.client.get_entity.return_value = type("Channel", (), {})()
        self.assertIn("не пользователь", await self.run_command(".user @chan"))

    async def test_unknown_flag_shows_usage(self):
        self.assertIn("Использование", await self.run_command(".user @a --bad"))
        self.assertIn("Использование", await self.run_command(".user @a @b"))


class PingTests(unittest.IsolatedAsyncioTestCase):
    def test_format(self):
        text = format_ping([40.0, 50.0, 90.0])
        self.assertIn("Pong", text)
        self.assertIn("`50 мс`", text)
        self.assertIn("`40`", text)
        self.assertIn("`90`", text)

    async def test_command_answers_with_median(self):
        app = SimpleNamespace(dispatcher=Dispatcher())
        module = PingModule(app)
        module.measure = AsyncMock(return_value=[10.0, 20.0, 30.0])
        await module.start()
        event = make_event(".ping")
        await app.dispatcher.dispatch_command(event)
        self.assertIn("`20 мс`", event.edits[-1])

    async def test_failure_is_reported_not_raised(self):
        app = SimpleNamespace(dispatcher=Dispatcher())
        module = PingModule(app)
        module.measure = AsyncMock(side_effect=RuntimeError("down"))
        await module.start()
        event = make_event(".ping")
        with self.assertLogs("candy", level="ERROR"):
            await app.dispatcher.dispatch_command(event)
        self.assertIn("❌", event.edits[-1])


class TagParseTests(unittest.TestCase):
    def test_all_and_rand(self):
        self.assertEqual(parse_tag(["all"]), TagRequest("all"))
        self.assertEqual(parse_tag(["rand", "3"]), TagRequest("rand", 3))
        self.assertEqual(parse_tag(["ALL", "-e", "@A", "b", "123"]), TagRequest("all", 0, ("a", "b", "123")))
        self.assertEqual(parse_tag(["rand", "2", "-е", "@x"]).exclude, ("x",))  # русская «е»

    def test_errors(self):
        for args in ([], ["x"], ["rand"], ["rand", "x"], ["rand", "0"], ["rand", "101"], ["all", "-e"],
                     ["all", "@a"], ["all", "-e", "-e"], ["all", "extra"]):
            with self.subTest(args=args), self.assertRaises(UserError):
                parse_tag(args)

    def test_excluded_by_username_or_id(self):
        user = make_user(id=5, username="Bob")
        self.assertTrue(is_excluded(user, ("bob",)))
        self.assertTrue(is_excluded(user, ("5",)))
        self.assertFalse(is_excluded(user, ("alice",)))
        self.assertFalse(is_excluded(make_user(id=6, username=None), ("",)))

    def test_mention_username_or_link(self):
        self.assertEqual(mention(make_user(id=1, username="bob")), "@bob")
        self.assertEqual(mention(make_user(id=2, username=None, first_name="Ann", last_name="Lee")),
                         "[Ann Lee](tg://user?id=2)")
        evil = mention(make_user(id=3, username=None, first_name="a](x) [b*_"))
        self.assertEqual(re.fullmatch(r"\[[^\[\]()*_~|]+\]\(tg://user\?id=3\)", evil) is not None, True)
        self.assertEqual(mention(make_user(id=4, username=None, first_name="")), "[пользователь](tg://user?id=4)")

    def test_chunks(self):
        self.assertEqual(chunks(list("abcde"), 2), [["a", "b"], ["c", "d"], ["e"]])


class TagCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent: list[tuple] = []

        async def send_message(chat_id, text, **kwargs):
            self.sent.append((chat_id, text, kwargs))

        members = [
            make_user(id=1, username="me"),
            make_user(id=2, username="ann", bot=False),
            make_user(id=3, username=None, first_name="Bob"),
            make_user(id=4, username="robot", bot=True),
            make_user(id=5, username="gone", deleted=True),
            make_user(id=6, username="skip"),
        ]
        self.members = members
        self.client = SimpleNamespace(
            send_message=send_message,
            get_me=AsyncMock(return_value=SimpleNamespace(id=1)),
            get_participants=AsyncMock(return_value=members),
        )
        self.app = SimpleNamespace(dispatcher=Dispatcher(), client=self.client, notify_owner=AsyncMock())
        self.module = TagModule(self.app)
        self.pauses: list[float] = []

        async def fake_sleep(seconds):
            self.pauses.append(seconds)
            await asyncio.sleep(0)

        self.module._sleep = fake_sleep
        await self.module.start()

    async def asyncTearDown(self):
        await self.module.stop()

    async def run_command(self, text, **attrs):
        attrs.setdefault("is_group", True)
        event = make_event(text, **attrs)
        await self.app.dispatcher.dispatch_command(event)
        return event

    async def finish(self):
        for job in list(self.module._jobs.values()):
            await job.task

    async def test_all_skips_me_bots_deleted_and_excluded(self):
        event = await self.run_command(".tag all -e @skip")
        await self.finish()
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0][1]
        self.assertEqual(text, "@ann [Bob](tg://user?id=3)")
        self.assertEqual(self.sent[0][2]["parse_mode"], "md")
        event.delete.assert_awaited_once()

    async def test_messages_are_chunked_with_pauses(self):
        self.client.get_participants.return_value = [make_user(id=i + 10, username=f"u{i}") for i in range(12)]
        await self.run_command(".tag all")
        await self.finish()
        self.assertEqual(len(self.sent), 3)  # 5 + 5 + 2
        self.assertEqual(len(self.sent[0][1].split()), PER_MESSAGE)
        self.assertEqual(len(self.pauses), 2)

    async def test_all_is_capped(self):
        self.client.get_participants.return_value = [make_user(id=i + 10, username=f"u{i}") for i in range(MAX_ALL + 50)]
        await self.run_command(".tag all")
        await self.finish()
        self.assertEqual(sum(len(item[1].split()) for item in self.sent), MAX_ALL)

    async def test_rand_picks_requested_number(self):
        await self.run_command(".tag rand 2")
        await self.finish()
        self.assertEqual(len(re.findall(r"@\w+|\[[^\]]+\]\([^)]*\)", " ".join(item[1] for item in self.sent))), 2)

    async def test_rand_more_than_available_takes_everyone_available(self):
        await self.run_command(".tag rand 50")
        await self.finish()
        found = re.findall(r"@\w+|\[[^\]]+\]\([^)]*\)", " ".join(item[1] for item in self.sent))
        self.assertEqual(len(found), 3)  # ann, Bob, skip: без меня, бота и удалённого

    async def test_only_in_groups(self):
        event = await self.run_command(".tag all", is_group=False)
        self.assertIn("только в группах", event.edits[-1])
        self.assertEqual(self.sent, [])
        event.delete.assert_not_awaited()

    async def test_nobody_to_tag(self):
        self.client.get_participants.return_value = [make_user(id=1)]
        event = await self.run_command(".tag all")
        self.assertIn("Некого", event.edits[-1])

    async def test_no_rights_to_list_members(self):
        self.client.get_participants.side_effect = RuntimeError("admin required")
        event = await self.run_command(".tag all")
        self.assertIn("список участников", event.edits[-1])

    async def test_second_job_in_same_chat_is_refused_and_stop_works(self):
        gate = asyncio.Event()

        async def slow(seconds):
            await gate.wait()

        self.module._sleep = slow
        self.client.get_participants.return_value = [make_user(id=i + 10, username=f"u{i}") for i in range(20)]
        await self.run_command(".tag all")
        await asyncio.sleep(0)
        again = await self.run_command(".tag all")
        self.assertIn("уже идёт", again.edits[-1])
        stop = await self.run_command(".tag stop")
        self.assertIn("Остановлено отметок: 1", stop.edits[-1])
        self.assertEqual(self.module._jobs, {})
        nothing = await self.run_command(".tag stop")
        self.assertIn("ничего не отмечается", nothing.edits[-1])

    async def test_send_error_notifies_owner(self):
        async def broken(chat_id, text, **kwargs):
            raise RuntimeError("нет прав")

        self.client.send_message = broken
        with self.assertLogs("candy", level="ERROR"):
            await self.run_command(".tag all")
            await self.finish()
        self.assertIn("нет прав", self.app.notify_owner.await_args.args[0])

    async def test_bad_arguments_show_usage(self):
        event = await self.run_command(".tag")
        self.assertIn("Использование", event.edits[-1])


if __name__ == "__main__":
    unittest.main()
