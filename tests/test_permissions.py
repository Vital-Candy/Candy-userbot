"""Уровни доступа (user < trusted < admin < owner), require(), открытые команды, .perm."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.access import AccessService, require
from core.dispatcher import Dispatcher
from core.policy import AccessPolicy
from core.reply import answer
from modules.permissions.module import PermissionsModule
from storage.permissions import PermissionLevel
from storage.service import Storage

try:
    from core.events import EventBridge
except ImportError:  # Telethon не установлен
    EventBridge = None

OWNER = 1
FRIEND = 100
STRANGER = 200


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "candy.sqlite3")
        self.storage.open()
        self.identity = SimpleNamespace(account_id=AsyncMock(return_value=OWNER))
        self.app = SimpleNamespace(
            dispatcher=Dispatcher(),
            storage=self.storage,
            identity=self.identity,
            access=AccessService(self.storage.permissions, self.identity),
        )
        self.clock = [100.0]
        self.app.policy = AccessPolicy(
            self.app.access, self.storage.plugin_data, self.app.dispatcher,
            clock=lambda: self.clock[0], cooldown=2.0,
        )
        self.app.dispatcher.gate = self.app.policy.gate

    def grant(self, subject, level):
        self.storage.permissions.grant(
            account_id=OWNER, subject_id=subject, permission=level, granted_by=OWNER
        )

    def close(self):
        self.storage.close()
        self.tmp.cleanup()


class LevelTests(unittest.TestCase):
    def test_ordering(self):
        U, T, A = PermissionLevel.USER, PermissionLevel.TRUSTED, PermissionLevel.ADMIN
        self.assertTrue(PermissionLevel.OWNER > A > T > U)
        self.assertTrue(A > T > U)
        self.assertTrue(A >= A and T <= T and U < A and A >= U)
        self.assertFalse(U >= T)
        self.assertEqual([A, U, T].__len__(), 3)
        self.assertEqual(sorted([A, U, T]), [U, T, A])
        self.assertEqual(PermissionLevel("admin"), A)

    def test_comparison_with_foreign_type_is_not_supported(self):
        with self.assertRaises(TypeError):
            PermissionLevel.USER < 3  # noqa: B015


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.store = self.fx.storage.permissions

    def tearDown(self):
        self.fx.close()

    def test_highest_level(self):
        self.assertIsNone(self.store.highest_level(account_id=OWNER, subject_id=FRIEND))  # не выдано: игнорируем
        self.fx.grant(FRIEND, "user")
        self.assertEqual(self.store.highest_level(account_id=OWNER, subject_id=FRIEND), PermissionLevel.USER)
        self.fx.grant(FRIEND, "trusted")
        self.assertEqual(self.store.highest_level(account_id=OWNER, subject_id=FRIEND), PermissionLevel.TRUSTED)
        self.fx.grant(FRIEND, "admin")
        self.assertEqual(self.store.highest_level(account_id=OWNER, subject_id=FRIEND), PermissionLevel.ADMIN)
        self.store.revoke(account_id=OWNER, subject_id=FRIEND, permission="admin")
        self.assertEqual(self.store.highest_level(account_id=OWNER, subject_id=FRIEND), PermissionLevel.TRUSTED)

    def test_unknown_stored_permission_never_raises_rights(self):
        self.fx.storage.database.execute(
            "INSERT INTO permissions(account_id, subject_id, permission, granted_by, created_at) "
            "VALUES(?, ?, 'superuser', 1, '2026-01-01T00:00:00+00:00')",
            (OWNER, FRIEND),
        )
        self.assertIsNone(self.store.highest_level(account_id=OWNER, subject_id=FRIEND))

    def test_owner_level_cannot_be_granted_or_gained_from_the_database(self):
        with self.assertRaises(ValueError):
            self.store.grant(account_id=OWNER, subject_id=FRIEND, permission="owner", granted_by=OWNER)
        self.fx.storage.database.execute(
            "INSERT INTO permissions(account_id, subject_id, permission, granted_by, created_at) "
            "VALUES(?, ?, 'owner', 1, '2026-01-01T00:00:00+00:00')",
            (OWNER, FRIEND),
        )
        self.assertIsNone(self.store.highest_level(account_id=OWNER, subject_id=FRIEND))

    def test_grant_is_upsert(self):
        self.fx.grant(FRIEND, "trusted")
        self.fx.grant(FRIEND, "trusted")
        self.assertEqual(len(self.store.list_all(account_id=OWNER)), 1)

    def test_invalid_level_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.grant(account_id=OWNER, subject_id=FRIEND, permission="root", granted_by=OWNER)

    def test_revoke_all_and_list_all_ordering(self):
        self.fx.grant(FRIEND, "trusted")
        self.fx.grant(FRIEND, "admin")
        self.fx.grant(50, "trusted")
        self.assertEqual([(p.subject_id, p.permission) for p in self.store.list_all(account_id=OWNER)],
                         [(50, "trusted"), (FRIEND, "admin"), (FRIEND, "trusted")])
        self.assertEqual(self.store.revoke_all(account_id=OWNER, subject_id=FRIEND), 2)
        self.assertEqual(self.store.revoke_all(account_id=OWNER, subject_id=FRIEND), 0)
        self.assertEqual(len(self.store.list_all(account_id=OWNER)), 1)

    def test_accounts_are_isolated(self):
        self.fx.grant(FRIEND, "admin")
        self.assertEqual(self.store.list_all(account_id=2), [])
        self.assertIsNone(self.store.highest_level(account_id=2, subject_id=FRIEND))


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = Fixture()
        self.access = self.fx.app.access

    async def asyncTearDown(self):
        self.fx.close()

    async def test_owner_is_owner_without_database_record(self):
        self.assertEqual(self.fx.storage.permissions.list_all(account_id=OWNER), [])
        self.assertEqual(await self.access.level_of(OWNER), PermissionLevel.OWNER)
        self.assertTrue(await self.access.has_level(OWNER, "admin"))
        self.assertTrue(await self.access.has_level(OWNER, "owner"))

    async def test_levels(self):
        self.fx.grant(FRIEND, "trusted")
        self.assertEqual(await self.access.level_of(FRIEND), PermissionLevel.TRUSTED)
        self.assertIsNone(await self.access.level_of(STRANGER))  # уровень не выдан: нет никакого
        self.assertTrue(await self.access.has_level(FRIEND, "trusted"))
        self.assertTrue(await self.access.has_level(FRIEND, PermissionLevel.USER))
        self.assertFalse(await self.access.has_level(FRIEND, "admin"))
        self.assertFalse(await self.access.has_level(FRIEND, "owner"))
        self.assertFalse(await self.access.has_level(STRANGER, "user"))

    async def test_rights_follow_the_active_account(self):
        self.fx.grant(FRIEND, "admin")
        self.fx.identity.account_id.return_value = 2  # другой аккаунт: чужие права не действуют
        self.assertIsNone(await self.access.level_of(FRIEND))


class RequireFilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = Fixture()
        self.fx.grant(FRIEND, "trusted")

    async def asyncTearDown(self):
        self.fx.close()

    def event(self, sender, out=False):
        return SimpleNamespace(sender_id=sender, out=out)

    async def test_owner_outgoing_always_passes(self):
        check = require(self.fx.app, "admin")
        self.assertTrue(await check(self.event(None, out=True)))
        self.assertTrue(await check(self.event(OWNER)))  # собственный id и без out

    async def test_levels_are_enforced(self):
        trusted = require(self.fx.app, "trusted")
        admin = require(self.fx.app, PermissionLevel.ADMIN)
        self.assertTrue(await trusted(self.event(FRIEND)))
        self.assertFalse(await admin(self.event(FRIEND)))
        self.assertFalse(await trusted(self.event(STRANGER)))

    async def test_user_level_needs_to_be_granted(self):
        self.assertFalse(await require(self.fx.app, "user")(self.event(STRANGER)))
        self.assertTrue(await require(self.fx.app, "user")(self.event(FRIEND)))

    async def test_owner_level_filter_lets_only_the_owner(self):
        check = require(self.fx.app, "owner")
        self.assertFalse(await check(self.event(FRIEND)))
        self.assertTrue(await check(self.event(OWNER, out=True)))

    async def test_missing_sender_is_denied(self):
        self.assertFalse(await require(self.fx.app, "user")(self.event(None)))
        self.assertFalse(await require(self.fx.app, "user")(SimpleNamespace()))

    async def test_missing_access_service_denies(self):
        app = SimpleNamespace()
        self.assertFalse(await require(app, "trusted")(self.event(FRIEND)))

    async def test_error_in_check_denies(self):
        self.fx.app.access.has_level = AsyncMock(side_effect=RuntimeError("db gone"))
        with self.assertLogs("candy", level="ERROR"):
            self.assertFalse(await require(self.fx.app, "trusted")(self.event(FRIEND)))

    async def test_invalid_level_name_fails_at_creation(self):
        with self.assertRaises(ValueError):
            require(self.fx.app, "root")

    async def test_filter_is_found_by_name_in_logs(self):
        self.assertEqual(require(self.fx.app, "trusted").__name__, "require_trusted")

    async def test_access_created_after_filter_is_still_used(self):
        app = SimpleNamespace(access=None)
        check = require(app, "trusted")
        self.assertFalse(await check(self.event(FRIEND)))
        app.access = self.fx.app.access
        self.assertTrue(await check(self.event(FRIEND)))


def make_event(text, sender=None, out=False, reply_sender=None):
    sent: list[tuple[str, str]] = []

    async def edit(value):
        sent.append(("edit", value))

    async def reply(value):
        sent.append(("reply", value))

    async def respond(value):
        sent.append(("respond", value))

    reply_message = SimpleNamespace(sender_id=reply_sender) if reply_sender is not None else None
    event = SimpleNamespace(
        raw_text=text,
        sender_id=sender,
        out=out,
        edit=edit,
        reply=reply,
        respond=respond,
        get_reply_message=AsyncMock(return_value=reply_message),
        sent=sent,
    )
    return event


class IncomingCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = Fixture()
        self.fx.grant(FRIEND, "trusted")
        self.dispatcher = self.fx.app.dispatcher
        self.calls: list[str] = []

        async def handler(event):
            self.calls.append(event.raw_text)
            await answer(event, "ok")

        self.handler = handler

    async def asyncTearDown(self):
        self.fx.close()

    async def test_commands_are_not_open_by_default(self):
        self.dispatcher.register_command("secret", self.handler)
        event = make_event(".secret", sender=STRANGER, out=False)
        await self.dispatcher.dispatch_command(event, incoming=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(event.sent, [])

    async def test_open_command_requires_filters(self):
        with self.assertRaises(ValueError):
            self.dispatcher.register_command("open", self.handler, allow_incoming=True)

    async def test_open_command_obeys_require(self):
        self.dispatcher.register_command(
            "ping", self.handler, allow_incoming=True, filters=(require(self.fx.app, "trusted"),)
        )
        stranger = make_event(".ping", sender=STRANGER)
        friend = make_event(".ping", sender=FRIEND)
        await self.dispatcher.dispatch_command(stranger, incoming=True)
        await self.dispatcher.dispatch_command(friend, incoming=True)
        self.assertEqual(self.calls, [".ping"])
        self.assertEqual(stranger.sent, [])
        self.assertEqual(friend.sent, [("reply", "ok")])  # чужое сообщение не правим, а отвечаем на него

    async def test_own_message_is_still_edited(self):
        self.dispatcher.register_command("mine", self.handler)
        event = make_event(".mine", sender=OWNER, out=True)
        await self.dispatcher.dispatch_command(event)
        self.assertEqual(event.sent, [("edit", "ok")])

    async def test_incoming_flag_never_runs_closed_commands_even_for_trusted(self):
        self.dispatcher.register_command("closed", self.handler)
        await self.dispatcher.dispatch_command(make_event(".closed", sender=FRIEND), incoming=True)
        self.assertEqual(self.calls, [])

    async def test_open_command_flag_is_recorded(self):
        command = self.dispatcher.register_command(
            "p", self.handler, allow_incoming=True, filters=(require(self.fx.app, "trusted"),)
        )
        self.assertTrue(command.allow_incoming)
        self.assertFalse(self.dispatcher.register_command("q", self.handler).allow_incoming)

    @unittest.skipIf(EventBridge is None, "Telethon не установлен")
    async def test_bridge_routes_incoming_messages_to_open_commands_only(self):
        client = SimpleNamespace(add_event_handler=lambda *a: None, remove_event_handler=lambda *a: None)
        bridge = EventBridge(client, self.dispatcher)
        self.dispatcher.register_command("closed", self.handler)
        self.dispatcher.register_command(
            "ping", self.handler, allow_incoming=True, filters=(require(self.fx.app, "trusted"),)
        )
        await bridge._new_message(make_event(".closed", sender=FRIEND))
        await bridge._new_message(make_event(".ping", sender=STRANGER))
        await bridge._new_message(make_event(".ping", sender=FRIEND))
        await bridge._new_message(make_event(".closed", sender=OWNER, out=True))
        self.assertEqual(self.calls, [".ping", ".closed"])


class PermCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = Fixture()
        self.module = PermissionsModule(self.fx.app)
        await self.module.start()

    async def asyncTearDown(self):
        self.fx.close()

    async def run_cmd(self, text, *, sender=OWNER, out=True, reply_sender=None):
        event = make_event(text, sender=sender, out=out, reply_sender=reply_sender)
        await self.fx.app.dispatcher.dispatch_command(event)
        return event.sent[-1][1] if event.sent else None

    def level(self, subject):
        return self.fx.storage.permissions.highest_level(account_id=OWNER, subject_id=subject)

    async def test_grant_by_id_and_by_reply(self):
        self.assertIn("trusted", await self.run_cmd(".perm grant 100 trusted"))
        self.assertEqual(self.level(100), PermissionLevel.TRUSTED)
        self.assertIn("admin", await self.run_cmd(".perm grant admin", reply_sender=300))
        self.assertEqual(self.level(300), PermissionLevel.ADMIN)

    async def test_grant_errors(self):
        self.assertIn("укажите уровень", await self.run_cmd(".perm grant 100"))
        self.assertIn("user, trusted и admin", await self.run_cmd(".perm grant 100 owner"))
        self.assertIn("числовой ID", await self.run_cmd(".perm grant @name trusted"))
        self.assertIn("укажите ID", await self.run_cmd(".perm grant trusted"))
        self.assertIn("положительным", await self.run_cmd(".perm grant -5 trusted"))
        self.assertIn("и так владелец", await self.run_cmd(f".perm grant {OWNER} admin"))
        self.assertIn("Использование", await self.run_cmd(".perm grant 1 2 trusted"))
        self.assertEqual(self.fx.storage.permissions.list_all(account_id=OWNER), [])

    async def test_revoke_one_level_or_all(self):
        await self.run_cmd(".perm grant 100 trusted")
        await self.run_cmd(".perm grant 100 admin")
        self.assertIn("admin снят", await self.run_cmd(".perm revoke 100 admin"))
        self.assertEqual(self.level(100), PermissionLevel.TRUSTED)
        self.assertIn("нет уровня", await self.run_cmd(".perm revoke 100 admin"))
        self.assertIn("все уровни сняты", await self.run_cmd(".perm revoke 100"))
        self.assertIn("нет выданных", await self.run_cmd(".perm revoke 100"))
        self.assertIsNone(self.level(100))

    async def test_owner_rights_cannot_be_revoked(self):
        self.assertIn("владельца", await self.run_cmd(f".perm revoke {OWNER}"))

    async def test_list(self):
        self.assertIn("игнорируются", await self.run_cmd(".perm list"))
        await self.run_cmd(".perm grant 100 trusted")
        listing = await self.run_cmd(".perm list")
        self.assertIn("100 — trusted", listing)
        single = await self.run_cmd(".perm list 100")
        self.assertIn("trusted", single)
        self.assertIn("нет (игнорируется)", await self.run_cmd(".perm list 999"))
        self.assertIn("owner", await self.run_cmd(f".perm list {OWNER}"))

    async def test_usage(self):
        for text in (".perm", ".perm wat", ".perm list 1 2", ".perm grant 1 2 3 4"):
            self.assertIn("Использование", await self.run_cmd(text), text)

    async def test_stop_unregisters(self):
        await self.module.stop()
        self.assertNotIn("perm", self.fx.app.dispatcher.commands)

    async def test_module_is_optional(self):
        self.assertFalse(PermissionsModule.required)


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    """Гейт доступа: уровни, игнор без выдачи, ручные настройки, пауза, владелец вне правил."""

    async def asyncSetUp(self):
        self.fx = Fixture()
        self.dispatcher = self.fx.app.dispatcher
        self.policy = self.fx.app.policy
        self.calls: list[str] = []

        async def handler(event):
            self.calls.append(event.raw_text)
            await answer(event, "ok")

        self.handler = handler
        for name, level in (("a_user", "user"), ("a_trusted", "trusted"), ("a_admin", "admin")):
            self.dispatcher.register_command(name, handler, level=level)
        self.dispatcher.register_command("owner_only", handler)
        self.fx.grant(100, "user")
        self.fx.grant(101, "trusted")
        self.fx.grant(102, "admin")

    async def asyncTearDown(self):
        self.fx.close()

    async def say(self, text, sender, *, advance=10.0):
        self.fx.clock[0] += advance
        event = make_event(text, sender=sender, out=False)
        await self.dispatcher.dispatch_command(event, incoming=True)
        return event

    async def allowed(self, command, sender):
        before = len(self.calls)
        await self.say("." + command, sender)
        return len(self.calls) > before

    async def test_levels_by_default(self):
        matrix = {
            100: {"a_user": True, "a_trusted": False, "a_admin": False},
            101: {"a_user": True, "a_trusted": True, "a_admin": False},
            102: {"a_user": True, "a_trusted": True, "a_admin": True},
        }
        for sender, row in matrix.items():
            for command, expected in row.items():
                with self.subTest(sender=sender, command=command):
                    self.assertEqual(await self.allowed(command, sender), expected)

    async def test_people_without_level_are_ignored_silently(self):
        event = await self.say(".a_user", STRANGER)
        self.assertEqual(self.calls, [])
        self.assertEqual(event.sent, [])  # ни ответа, ни ошибки: бот делает вид, что ничего не было

    async def test_commands_without_level_are_never_available_to_others(self):
        for sender in (100, 101, 102):
            self.assertFalse(await self.allowed("owner_only", sender))

    async def test_message_without_sender_is_ignored(self):
        event = make_event(".a_user", sender=None, out=False)
        await self.dispatcher.dispatch_command(event, incoming=True)
        self.assertEqual(self.calls, [])

    async def test_owner_is_never_affected_by_settings_or_pause(self):
        await self.policy.set_level("a_user", PermissionLevel.OWNER)  # закрыли для всех
        for _ in range(3):  # подряд, без пауз
            event = make_event(".a_user", sender=OWNER, out=True)
            await self.dispatcher.dispatch_command(event)
        event = make_event(".owner_only", sender=OWNER, out=True)
        await self.dispatcher.dispatch_command(event)
        self.assertEqual(self.calls, [".a_user"] * 3 + [".owner_only"])
        self.assertEqual(self.dispatcher.caller_level(event), PermissionLevel.OWNER)

    async def test_manual_level_can_open_and_close_commands(self):
        self.assertFalse(await self.allowed("a_admin", 101))
        await self.policy.set_level("a_admin", PermissionLevel.TRUSTED)
        self.assertTrue(await self.allowed("a_admin", 101))
        self.assertFalse(await self.allowed("a_admin", 100))
        await self.policy.set_level("a_user", PermissionLevel.OWNER)
        self.assertFalse(await self.allowed("a_user", 102))  # даже admin
        await self.policy.set_level("a_user", None)  # default
        self.assertTrue(await self.allowed("a_user", 100))

    async def test_closed_by_hand_raising_is_not_possible_for_owner_only_commands(self):
        with self.assertRaises(ValueError):
            await self.policy.set_level("owner_only", PermissionLevel.USER)
        with self.assertRaises(ValueError):
            await self.policy.set_level("nothing", PermissionLevel.USER)
        self.assertFalse(await self.allowed("owner_only", 102))

    async def test_manual_levels_survive_restart(self):
        await self.policy.set_level("a_admin", PermissionLevel.USER)
        await self.policy.set_level("a_user", PermissionLevel.OWNER)
        restarted = AccessPolicy(self.fx.app.access, self.fx.storage.plugin_data, Dispatcher())
        for name, level in (("a_user", "user"), ("a_admin", "admin")):
            restarted.dispatcher.register_command(name, self.handler, level=level)
        await restarted.load()
        self.assertEqual(restarted.dispatcher.level_overrides, {"a_admin": "user", "a_user": "owner"})
        command = restarted.dispatcher.commands["a_admin"]
        self.assertEqual(restarted.dispatcher.required_level(command), PermissionLevel.USER)
        self.assertIsNone(restarted.dispatcher.required_level(restarted.dispatcher.commands["a_user"]))

    async def test_reset_returns_defaults_and_forgets_saved_data(self):
        await self.policy.set_level("a_admin", PermissionLevel.USER)
        self.assertEqual(await self.policy.reset(), 1)
        self.assertEqual(self.dispatcher.level_overrides, {})
        self.assertIsNone(self.fx.storage.plugin_data.get(account_id=OWNER, plugin="core.access", key="command_levels"))
        self.assertFalse(await self.allowed("a_admin", 101))

    async def test_damaged_saved_data_is_ignored_safely(self):
        for broken in ("text", ["list"], {"a_admin": "superuser", "a_user": 5}):
            self.fx.storage.plugin_data.set(account_id=OWNER, plugin="core.access", key="command_levels", value=broken)
            await self.policy.load()
            self.assertEqual(self.dispatcher.level_overrides, {})
        self.assertFalse(await self.allowed("a_admin", 101))

    async def test_settings_of_other_account_do_not_apply(self):
        await self.policy.set_level("a_admin", PermissionLevel.USER)
        self.fx.identity.account_id.return_value = 2
        await self.policy.load()
        self.assertEqual(self.dispatcher.level_overrides, {})

    async def test_pause_between_commands_of_one_person(self):
        self.assertTrue(await self.allowed("a_user", 100))
        await self.say(".a_user", 100, advance=0.5)  # слишком быстро
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(await self.allowed("a_user", 101))  # другой человек не ждёт
        self.assertTrue(await self.allowed("a_user", 100))  # прошло время

    async def test_denied_attempts_do_not_start_the_pause(self):
        self.assertFalse(await self.allowed("a_admin", 100))
        self.assertTrue(await self.allowed("a_user", 100))

    async def test_two_messages_at_once_pass_only_once(self):
        import asyncio

        events = [make_event(".a_user", sender=100, out=False) for _ in range(2)]
        self.fx.clock[0] += 10
        await asyncio.gather(*(self.dispatcher.dispatch_command(event, incoming=True) for event in events))
        self.assertEqual(len(self.calls), 1)

    async def test_set_levels_is_all_or_nothing(self):
        with self.assertRaises(ValueError):
            await self.policy.set_levels(["a_user", "nothing"], PermissionLevel.ADMIN)
        self.assertEqual(self.dispatcher.level_overrides, {})
        self.assertEqual(await self.policy.set_levels(["a_user", ".A_ADMIN", "a_user"], PermissionLevel.TRUSTED), ["a_user", "a_admin"])

    async def test_gate_error_denies(self):
        async def broken(event, command):
            raise RuntimeError("база недоступна")

        self.dispatcher.gate = broken
        with self.assertLogs("candy", level="ERROR"):
            await self.say(".a_user", 100)
        self.assertEqual(self.calls, [])

    async def test_without_gate_nobody_but_owner_runs_commands(self):
        self.dispatcher.gate = None
        self.assertFalse(await self.allowed("a_user", 102))

    async def test_pause_table_does_not_grow_forever(self):
        for number in range(1500):
            self.policy._remember(10_000 + number, self.fx.clock[0])
        self.assertLessEqual(len(self.policy._last), 1000)

    async def test_caller_level_is_visible_inside_the_command(self):
        seen: list = []

        async def spy(event):
            seen.append(self.dispatcher.caller_level(event))

        self.dispatcher.register_command("spy", spy, level="user")
        await self.say(".spy", 101)
        await self.say(".spy", 100)
        self.assertEqual(seen, [PermissionLevel.TRUSTED, PermissionLevel.USER])
        self.assertIsNone(self.dispatcher.caller_level(make_event("x", sender=1)))  # вне команды

    async def test_visible_commands_for_help(self):
        names = lambda level: [item.name for item in self.dispatcher.visible_commands(level)]
        self.assertEqual(names(PermissionLevel.USER), ["a_user"])
        self.assertEqual(names(PermissionLevel.TRUSTED), ["a_trusted", "a_user"])
        self.assertEqual(set(names(PermissionLevel.OWNER)), {"a_user", "a_trusted", "a_admin", "owner_only"})
        self.assertEqual(names(None), [])
        await self.policy.set_level("a_admin", PermissionLevel.USER)
        self.assertIn("a_admin", names(PermissionLevel.USER))
        await self.policy.set_level("a_user", PermissionLevel.OWNER)
        self.assertNotIn("a_user", names(PermissionLevel.ADMIN))

    async def test_registration_validates_level(self):
        for bad in ("owner", "root", "ADMINN"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.dispatcher.register_command("x" + bad.lower(), self.handler, level=bad)
        command = self.dispatcher.register_command("upper", self.handler, level="TRUSTED")
        self.assertEqual(command.level, PermissionLevel.TRUSTED)

    async def test_legacy_allow_incoming_still_works_and_gets_no_caller_level(self):
        self.dispatcher.register_command(
            "legacy", self.handler, allow_incoming=True, filters=(require(self.fx.app, "trusted"),)
        )
        await self.say(".legacy", 101)
        await self.say(".legacy", 100)
        self.assertEqual(self.calls, [".legacy"])

    @unittest.skipIf(EventBridge is None, "Telethon не установлен")
    async def test_bridge_uses_the_gate_for_incoming_messages(self):
        client = SimpleNamespace(add_event_handler=lambda *a: None, remove_event_handler=lambda *a: None)
        bridge = EventBridge(client, self.dispatcher)
        self.fx.clock[0] += 10
        await bridge._new_message(make_event(".a_trusted", sender=100))
        await bridge._new_message(make_event(".a_trusted", sender=101))
        self.assertEqual(self.calls, [".a_trusted"])


class PermAccessTests(unittest.IsolatedAsyncioTestCase):
    """Что в .perm может admin и что только владелец; настройка доступа к командам."""

    async def asyncSetUp(self):
        self.fx = Fixture()
        self.dispatcher = self.fx.app.dispatcher
        self.module = PermissionsModule(self.fx.app)
        await self.module.start()
        self.fx.grant(100, "user")
        self.fx.grant(101, "trusted")
        self.fx.grant(102, "admin")
        self.fx.grant(103, "admin")

        async def noop(event):
            await answer(event, "ok")

        self.dispatcher.register_command("view", noop, level="user", description="просмотр")
        self.dispatcher.register_command("danger", noop, description="только владелец")

    async def asyncTearDown(self):
        self.fx.close()

    async def run_cmd(self, text, *, sender=OWNER, reply_sender=None):
        self.fx.clock[0] += 10
        out = sender == OWNER
        event = make_event(text, sender=sender, out=out, reply_sender=reply_sender)
        await self.dispatcher.dispatch_command(event, incoming=not out)
        return event.sent[-1][1] if event.sent else None

    def level(self, subject):
        return self.fx.storage.permissions.highest_level(account_id=OWNER, subject_id=subject)

    async def test_perm_is_available_to_admin_only(self):
        for sender in (100, 101):
            self.assertIsNone(await self.run_cmd(".perm list", sender=sender))
        self.assertIsNone(await self.run_cmd(".perm list", sender=STRANGER))
        self.assertIn("Выданные", await self.run_cmd(".perm list", sender=102))

    async def test_admin_manages_user_and_trusted_only(self):
        self.assertIn("выдан", await self.run_cmd(".perm grant 300 user", sender=102))
        self.assertIn("выдан", await self.run_cmd(".perm grant 301 trusted", sender=102))
        self.assertEqual((self.level(300), self.level(301)), (PermissionLevel.USER, PermissionLevel.TRUSTED))
        self.assertIn("только владелец", await self.run_cmd(".perm grant 302 admin", sender=102))
        self.assertIsNone(self.level(302))
        self.assertIn("снят", await self.run_cmd(".perm revoke 300 user", sender=102))
        self.assertIsNone(self.level(300))

    async def test_admin_cannot_touch_other_admins_owner_or_self(self):
        self.assertIn("другого admin", await self.run_cmd(".perm revoke 103", sender=102))
        self.assertIn("только владелец", await self.run_cmd(".perm revoke 103 admin", sender=102))
        self.assertIn("другого admin", await self.run_cmd(".perm grant 103 user", sender=102))
        self.assertIn("свои уровни", await self.run_cmd(".perm revoke 102", sender=102))
        self.assertIn("владельца", await self.run_cmd(f".perm revoke {OWNER}", sender=102))
        self.assertIn("владелец", await self.run_cmd(f".perm grant {OWNER} user", sender=102))
        self.assertEqual((self.level(102), self.level(103)), (PermissionLevel.ADMIN, PermissionLevel.ADMIN))

    async def test_grant_by_reply_works_for_admin(self):
        self.assertIn("выдан", await self.run_cmd(".perm grant user", sender=102, reply_sender=400))
        self.assertEqual(self.level(400), PermissionLevel.USER)

    async def test_owner_can_do_everything(self):
        self.assertIn("выдан", await self.run_cmd(".perm grant 500 admin"))
        self.assertIn("снят", await self.run_cmd(".perm revoke 500 admin"))
        self.assertIn("все уровни сняты", await self.run_cmd(".perm revoke 103"))

    async def test_cmd_table_is_visible_to_admin_but_only_owner_changes(self):
        table = await self.run_cmd(".perm cmd", sender=102)
        self.assertIn(".view — user и выше", table)
        self.assertIn(".perm — admin и выше", table)
        self.assertIn(".danger", table.split("Только владелец")[1])
        self.assertNotIn(".danger —", table)
        self.assertIn("только владелец", await self.run_cmd(".perm cmd view admin", sender=102))
        self.assertEqual(self.dispatcher.level_overrides, {})

    async def test_owner_changes_command_levels(self):
        self.assertIn("trusted и выше", await self.run_cmd(".perm cmd view trusted"))
        self.assertEqual(self.dispatcher.level_overrides, {"view": "trusted"})
        self.assertIn("вручную; по умолчанию user", await self.run_cmd(".perm cmd"))
        self.assertIn("только владелец", await self.run_cmd(".perm cmd .view owner"))
        self.assertIn("user и выше", await self.run_cmd(".perm cmd view default"))
        self.assertEqual(self.dispatcher.level_overrides, {})
        await self.run_cmd(".perm cmd view admin")
        self.assertIn("Сброшены", (await self.run_cmd(".perm cmd reset")).replace("сброшены", "Сброшены"))
        self.assertEqual(self.dispatcher.level_overrides, {})

    async def test_cmd_errors(self):
        self.assertIn("открыть её нельзя", await self.run_cmd(".perm cmd danger user"))
        self.assertIn("команды .nothing нет", await self.run_cmd(".perm cmd nothing user"))
        self.assertIn("уровень:", await self.run_cmd(".perm cmd view root"))
        self.assertIn("уровень:", await self.run_cmd(".perm cmd view admins"))
        self.assertIn("укажите команды", await self.run_cmd(".perm cmd view"))
        self.assertEqual(self.dispatcher.level_overrides, {})

    # ----------------------------------------------- удобная настройка и аудит

    async def test_several_commands_numbers_and_all(self):
        async def noop(event):
            await answer(event, "ok")

        self.dispatcher.register_command("zeta", noop, level="trusted")
        table = await self.run_cmd(".perm cmd")
        self.assertIn("1. .perm — admin и выше", table)  # номера идут по алфавиту
        self.assertIn("2. .view — user и выше", table)
        self.assertIn("3. .zeta — trusted и выше", table)
        text = await self.run_cmd(".perm cmd view, zeta admin")
        self.assertIn("Изменено команд: 2", text)
        self.assertEqual(self.dispatcher.level_overrides, {"view": "admin", "zeta": "admin"})
        await self.run_cmd(".perm cmd 2 3 user")  # по номерам
        self.assertEqual(self.dispatcher.level_overrides, {"view": "user", "zeta": "user"})
        await self.run_cmd(".perm cmd all trusted")
        self.assertEqual(self.dispatcher.level_overrides.get("view"), "trusted")
        self.assertNotIn("perm", self.dispatcher.level_overrides)  # .perm в «all» не входит
        await self.run_cmd(".perm cmd view zeta off")
        self.assertEqual(self.dispatcher.level_overrides["view"], "owner")
        await self.run_cmd(".perm cmd all default")
        self.assertEqual(self.dispatcher.level_overrides, {})

    async def test_bad_name_or_number_changes_nothing(self):
        for text in (".perm cmd view nothing user", ".perm cmd view 99 user", ".perm cmd view danger user"):
            with self.subTest(text=text):
                reply = await self.run_cmd(text)
                self.assertIn("❌", reply)
                self.assertEqual(self.dispatcher.level_overrides, {})
        self.assertIn("нет: в списке", await self.run_cmd(".perm cmd 99 user"))

    async def test_perm_itself_cannot_be_lowered_below_admin(self):
        for word in ("user", "trusted"):
            self.assertIn("нельзя опустить", await self.run_cmd(f".perm cmd perm {word}"))
        self.assertEqual(self.dispatcher.level_overrides, {})
        self.assertIn("admin и выше", await self.run_cmd(".perm cmd perm admin"))
        self.assertIn("только владелец", await self.run_cmd(".perm cmd perm owner"))
        self.assertIsNone(await self.run_cmd(".perm list", sender=102))  # закрыта и для admin

    async def test_perm_does_nothing_for_low_levels_even_if_opened_by_mistake(self):
        self.dispatcher.level_overrides = {"perm": "user"}  # как будто база повреждена или правили руками
        self.assertIsNone(await self.run_cmd(".perm grant 300 trusted", sender=100))
        self.assertIsNone(self.level(300))

    async def test_audit_summary_warnings_and_journal(self):
        await self.run_cmd(".perm grant 300 trusted")
        await self.run_cmd(".perm grant 300 user")
        await self.run_cmd(".perm cmd view user")  # по умолчанию user: не шире
        async def noop(event):
            await answer(event, "ok")
        self.dispatcher.register_command("zeta", noop, level="trusted")
        await self.run_cmd(".perm cmd zeta user")  # шире обычного
        self.fx.grant(301, "user")
        self.fx.storage.permissions.grant(account_id=OWNER, subject_id=302, permission="user", granted_by=999)
        text = await self.run_cmd(".perm audit", sender=102)  # admin тоже видит аудит
        self.assertIn("🔎 Аудит прав", text)
        self.assertIn("admin (2): 102, 103", text)
        self.assertIn("trusted (2): 101, 300", text)
        self.assertIn("настроено вручную: 2", text)
        self.assertIn(".zeta открыта шире обычного: user вместо trusted", text)
        self.assertIn("302: user выдал 999, у которого сейчас нет прав", text)
        self.assertIn("выдан trusted → 300", text)
        self.assertIn("уровень команд .zeta: user", text)

    async def test_audit_is_clean_without_problems_and_limits_history(self):
        text = await self.run_cmd(".perm audit")
        self.assertIn("Журнал изменений пока пуст", text)
        for number in range(12):
            await self.run_cmd(f".perm grant {400 + number} user")
        self.assertEqual(len((await self.run_cmd(".perm audit 3")).split("Последние изменения (3):")[1].strip().splitlines()), 3)
        self.assertIn("(10)", await self.run_cmd(".perm audit"))
        self.assertIn("от 1 до 50", await self.run_cmd(".perm audit 0"))
        self.assertIn("от 1 до 50", await self.run_cmd(".perm audit 51"))

    async def test_journal_is_capped_and_survives_damage(self):
        from core.policy import MAX_AUDIT

        for number in range(MAX_AUDIT + 5):
            await self.fx.app.policy.record(OWNER, f"запись {number}")
        history = await self.fx.app.policy.history(1000)
        self.assertEqual(len(history), MAX_AUDIT)
        self.assertEqual(history[-1]["what"], f"запись {MAX_AUDIT + 4}")
        self.fx.storage.plugin_data.set(account_id=OWNER, plugin="core.access", key="audit_log", value=["мусор", {"at": 1}, {"at": "2026-01-01T00:00", "what": "ок"}])
        self.assertEqual([item["what"] for item in await self.fx.app.policy.history()], ["ок"])
        self.fx.storage.plugin_data.set(account_id=OWNER, plugin="core.access", key="audit_log", value="не список")
        self.assertEqual(await self.fx.app.policy.history(), [])

    async def test_audit_records_revoke_and_reset(self):
        await self.run_cmd(".perm grant 300 trusted")
        await self.run_cmd(".perm revoke 300")
        await self.run_cmd(".perm cmd view admin")
        await self.run_cmd(".perm cmd reset")
        text = await self.run_cmd(".perm audit")
        for expected in ("снят все уровни → 300", "сброшены ручные уровни команд (было 1)", "выдан trusted → 300"):
            self.assertIn(expected, text)
        await self.run_cmd(".perm revoke 300")  # ничего нет: ошибка, в журнал не попадает
        self.assertEqual(sum("снят" in item["what"] for item in await self.fx.app.policy.history(100)), 1)

    async def test_changed_level_takes_effect_immediately(self):
        sender = 100  # user
        self.assertIn("ok", await self.run_cmd(".view", sender=sender))
        await self.run_cmd(".perm cmd view trusted")
        self.assertIsNone(await self.run_cmd(".view", sender=sender))
        self.assertIn("ok", await self.run_cmd(".view", sender=101))


if __name__ == "__main__":
    unittest.main()
