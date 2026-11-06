"""Уровни доступа в командах .user, .status, .tag, .ping, .help и .mute: что видят и могут чужие."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.commands import register_commands
from modules.system.status import StatusModule
from modules.utility.ping import PingModule
from modules.utility.tag import MAX_RAND_OTHERS, TagModule
from modules.utility.user import UserModule, format_user, view_for
from storage.permissions import PermissionLevel
from tests.test_permissions import OWNER, Fixture, make_event
from tests.test_utility import make_user

try:
    from telethon.tl.types import UserStatusOnline
except ImportError:  # Telethon не установлен
    UserStatusOnline = None


class UserViewTests(unittest.TestCase):
    def setUp(self):
        self.user = make_user(
            id=42, first_name="Ann", last_name="Lee", username="ann_l", phone="998901234567",
            lang_code="ru", premium=True, bot=False, contact=True, mutual_contact=True, close_friend=True,
            photo=SimpleNamespace(dc_id=2),
        )
        self.full = SimpleNamespace(
            about="био", common_chats_count=3, private_forward_name="Anna", blocked=True,
            birthday=SimpleNamespace(day=5, month=7, year=1999),
        )

    def text(self, view, **kw):
        return format_user(self.user, self.full, view=view, **kw)

    def test_user_sees_only_reference_data(self):
        text = self.text("user")
        for needed in ("`42`", "@ann_l", "tg://user?id=42", "Premium"):
            self.assertIn(needed, text)
        for hidden in ("Ann", "Lee", "Anna", "998901234567", "био", "в ваших контактах", "близкий", "заблокирован",
                       "Общих чатов", "05.07", "`ru`", "Дата-центр"):
            self.assertNotIn(hidden, text)

    def test_trusted_has_no_phone_contacts_or_common_chats(self):
        self.user.contact = False
        text = self.text("trusted")
        self.assertIn("`Ann Lee`", text)
        self.assertIn("био", text)
        for hidden in ("998901234567", "Общих чатов", "заблокирован", "близкий", "взаимный"):
            self.assertNotIn(hidden, text)

    def test_trusted_does_not_see_name_taken_from_your_contacts(self):
        text = self.text("trusted")  # contact=True
        for hidden in ("Ann", "Lee", "Anna", "в ваших контактах"):
            self.assertNotIn(hidden, text)

    def test_owner_sees_everything_and_phone_only_with_flag(self):
        text = self.text("owner")
        for needed in ("Ann Lee", "Anna", "Общих чатов", "заблокирован вами", "в ваших контактах"):
            self.assertIn(needed, text)
        self.assertNotIn("998901234567", text)
        self.assertIn("+998901234567", self.text("owner", private=True))

    def test_phone_is_never_shown_to_others_even_if_asked(self):
        for view in ("trusted", "user", "garbage"):
            self.assertNotIn("998901234567", self.text(view, private=True))
            self.assertNotIn("Телефон", self.text(view, private=True))

    def test_view_for_level(self):
        self.assertEqual(view_for(PermissionLevel.OWNER), "owner")
        self.assertEqual(view_for(PermissionLevel.ADMIN), "trusted")
        self.assertEqual(view_for(PermissionLevel.TRUSTED), "trusted")
        self.assertEqual(view_for(PermissionLevel.USER), "user")
        self.assertEqual(view_for(None), "user")


class CommandsByLevelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = Fixture()
        self.dispatcher = self.fx.app.dispatcher
        self.sent_tags: list[str] = []
        members = [make_user(id=n, username=f"u{n}") for n in range(2, 30)] + [make_user(id=OWNER, username="me")]

        async def send_message(chat_id, text, **kwargs):
            self.sent_tags.append(text)

        self.fx.app.client = SimpleNamespace(
            get_entity=AsyncMock(return_value=self.entity()),
            get_me=AsyncMock(return_value=SimpleNamespace(id=OWNER)),
            get_participants=AsyncMock(return_value=members),
            send_message=send_message,
        )
        self.fx.app.notify_owner = AsyncMock()
        self.fx.app.health = lambda: SimpleNamespace(
            lines=lambda: ["голова", "Telegram: подключён", "Модули 5/5", "• секрет-проблема"]
        )
        self.fx.app.modules = SimpleNamespace(states=lambda: [])
        self.user = UserModule(self.fx.app)
        self.user._full = AsyncMock(return_value=SimpleNamespace(about="био", common_chats_count=4))
        self.tag = TagModule(self.fx.app)
        self.tag._sleep = AsyncMock()
        self.status = StatusModule(self.fx.app)
        self.ping = PingModule(self.fx.app)
        for module in (self.user, self.tag, self.status, self.ping):
            await module.start()
        self.fx.grant(100, "user")
        self.fx.grant(101, "trusted")
        self.fx.grant(102, "admin")

    def entity(self):
        return make_user(id=42, first_name="Ann", username="ann_l", phone="998901234567", contact=False)

    async def asyncTearDown(self):
        await self.tag.stop()
        self.fx.close()

    async def run_cmd(self, text, sender, *, advance=10, **attrs):
        self.fx.clock[0] += advance
        out = sender == OWNER
        event = make_event(text, sender=sender, out=out)
        event.is_group = True
        event.chat_id = 5
        event.delete = AsyncMock()
        for key, value in attrs.items():
            setattr(event, key, value)
        await self.dispatcher.dispatch_command(event, incoming=not out)
        for _ in range(20):
            await asyncio.sleep(0)
        return event

    def reply_text(self, event):
        return event.sent[-1][1] if event.sent else None

    # ------------------------------------------------------------------ .user

    async def test_user_by_level(self):
        owner = self.reply_text(await self.run_cmd(".user 42", OWNER))
        self.assertIn("Ann", owner)
        low = self.reply_text(await self.run_cmd(".user 42", 100))
        self.assertIn("`42`", low)
        self.assertNotIn("Ann", low)
        self.assertNotIn("998901234567", low)
        mid = self.reply_text(await self.run_cmd(".user 42", 101))
        self.assertIn("Ann", mid)
        self.assertNotIn("Общих чатов", mid)

    async def test_not_private_flag_is_refused_for_others(self):
        for sender in (100, 101, 102):
            text = self.reply_text(await self.run_cmd(".user 42 --not-private", sender))
            self.assertIn("только владельцу", text)
            self.assertNotIn("998901234567", text)
        self.assertIn("+998901234567", self.reply_text(await self.run_cmd(".user 42 --not-private", OWNER)))

    async def test_ungranted_user_gets_nothing(self):
        event = await self.run_cmd(".user 42", 999)
        self.assertEqual(event.sent, [])

    # ---------------------------------------------------------------- .status

    async def test_status_is_short_for_others(self):
        full = self.reply_text(await self.run_cmd(".status", OWNER))
        self.assertIn("секрет-проблема", full)
        self.assertEqual(self.reply_text(await self.run_cmd(".status", 100)), None)  # user: нельзя
        for sender in (101, 102):
            text = self.reply_text(await self.run_cmd(".status all", sender))
            self.assertIn("подключён", text)
            for hidden in ("секрет-проблема", "Модули", "Память", "Фоновые"):
                self.assertNotIn(hidden, text)

    # ------------------------------------------------------------------ .ping

    async def test_ping_is_open_to_user(self):
        self.ping.measure = AsyncMock(return_value=[10.0, 12.0, 11.0])
        event = await self.run_cmd(".ping", 100)
        self.assertTrue(event.sent)

    # ------------------------------------------------------------------- .tag

    async def test_tag_for_others_is_only_small_rand(self):
        for text in (".tag all", ".tag rand 5 -e @u2", f".tag rand {MAX_RAND_OTHERS + 1}", ".tag stop"):
            with self.subTest(text=text):
                event = await self.run_cmd(text, 101, advance=100)
                self.assertIn("❌", self.reply_text(event))
        self.assertEqual(self.sent_tags, [])
        event = await self.run_cmd(".tag rand 5", 101, advance=100)
        for _ in range(50):
            await asyncio.sleep(0)
        self.assertTrue(self.sent_tags)
        self.assertEqual(sum(text.count("@u") for text in self.sent_tags), 5)
        event.delete.assert_not_awaited()  # чужое сообщение не удаляем

    async def test_tag_has_long_pause_for_every_level_including_admin(self):
        for sender, wait in ((101, 60), (102, 20)):  # trusted 3 с и admin 1 с, умноженные на вес 20
            with self.subTest(sender=sender):
                self.sent_tags.clear()
                await self.run_cmd(".tag rand 2", sender, advance=100)
                await self.run_cmd(".tag rand 2", sender, advance=wait - 1)
                await asyncio.sleep(0)
                self.assertEqual(len(self.sent_tags), 1)  # вторая заблокирована паузой
                await self.run_cmd(".tag rand 2", sender, advance=2)
                for _ in range(20):
                    await asyncio.sleep(0)
                self.assertEqual(len(self.sent_tags), 2)

    async def test_owner_has_no_pause(self):
        for _ in range(3):
            event = await self.run_cmd(".tag rand 2", OWNER, advance=0)
            self.assertNotIn("Подождите", self.reply_text(event) or "")

    async def test_tag_for_owner_is_unchanged(self):
        event = await self.run_cmd(".tag all", OWNER)
        event.delete.assert_awaited()
        for _ in range(100):
            await asyncio.sleep(0)
        self.assertGreater(sum(text.count("@u") for text in self.sent_tags), MAX_RAND_OTHERS)

    async def test_user_level_cannot_tag(self):
        event = await self.run_cmd(".tag rand 3", 100)
        self.assertEqual(event.sent, [])
        self.assertEqual(self.sent_tags, [])

    # ------------------------------------------------------------------ .help

    async def test_help_shows_only_available_commands(self):
        register_commands(self.dispatcher, AsyncMock())
        owner = self.reply_text(await self.run_cmd(".help", OWNER))
        low = self.reply_text(await self.run_cmd(".help", 100))
        mid = self.reply_text(await self.run_cmd(".help", 101))
        self.assertIn(".status", owner)
        self.assertIn(".ping", low)
        self.assertIn(".user", low)
        self.assertNotIn(".tag", low)
        self.assertNotIn(".status", low)
        self.assertIn(".tag", mid)
        self.assertNotIn(".restart", mid)
        self.assertNotIn(".update", mid)

    async def test_help_for_a_command_shows_access_line(self):
        register_commands(self.dispatcher, AsyncMock())
        text = self.reply_text(await self.run_cmd(".help tag", OWNER))
        self.assertIn("Доступ:", text)
        self.assertIn("trusted", text)
        await self.fx.app.policy.set_level("tag", PermissionLevel.ADMIN)
        text = self.reply_text(await self.run_cmd(".help tag", OWNER))
        self.assertIn("admin", text)
        self.assertIn("вручную", text)
