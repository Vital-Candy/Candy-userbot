"""Этап 3: фильтры, контекст команды, ответ пользователю, таймауты, ленивый Raw."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from config.settings import load_settings
from core import filters as F
from core.dispatcher import Dispatcher
from core.reply import answer

try:
    from telethon.errors import FloodWaitError, MessageNotModifiedError

    from core.events import EventBridge
except ImportError:  # Telethon не установлен
    EventBridge = None


def make_event(text="", **attrs):
    edits: list[str] = []

    async def edit(value):
        edits.append(value)

    event = SimpleNamespace(raw_text=text, edit=edit, edits=edits, **attrs)
    return event


class FilterTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, flt, event):
        result = flt(event)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def test_basic_filters(self):
        private = SimpleNamespace(out=True, is_private=True, is_group=False, is_channel=False, is_reply=True)
        group = SimpleNamespace(out=False, is_private=False, is_group=True, is_channel=False)
        self.assertTrue(F.private(private) and not F.private(group))
        self.assertTrue(F.group(group) and not F.group(private))
        self.assertTrue(F.outgoing(private) and F.incoming(group))
        self.assertFalse(F.channel(private))
        self.assertTrue(F.is_reply(private) and not F.is_reply(group))

    async def test_missing_attributes_are_false(self):
        bare = SimpleNamespace()
        self.assertFalse(F.private(bare) or F.group(bare) or F.channel(bare) or F.outgoing(bare) or F.is_reply(bare))
        self.assertTrue(F.incoming(bare))

    async def test_all_and_any_with_sync_filters_stay_sync(self):
        event = SimpleNamespace(out=True, is_private=True)
        self.assertIs(F.all_filters(F.outgoing, F.private)(event), True)
        self.assertIs(F.all_filters(F.outgoing, F.group)(event), False)
        self.assertIs(F.any_filter(F.group, F.private)(event), True)
        self.assertIs(F.any_filter(F.group, F.channel)(event), False)
        self.assertIs(F.all_filters()(event), True)
        self.assertIs(F.any_filter()(event), False)
        self.assertIs(F.not_filter(F.group)(event), True)

    async def test_async_filters_are_supported_in_combinators(self):
        async def yes(_e):
            return True

        async def no(_e):
            return False

        event = SimpleNamespace()
        self.assertTrue(await self.check(F.all_filters(F.incoming, yes, F.incoming), event))
        self.assertFalse(await self.check(F.all_filters(yes, no, F.incoming), event))
        self.assertTrue(await self.check(F.any_filter(no, yes), event))
        self.assertTrue(await self.check(F.any_filter(no, F.incoming), event))
        self.assertFalse(await self.check(F.any_filter(no, no), event))
        self.assertTrue(await self.check(F.not_filter(no), event))
        self.assertFalse(await self.check(F.not_filter(yes), event))

    async def test_filter_combinations_short_circuit(self):
        calls: list[str] = []

        def first(_e):
            calls.append("first")
            return False

        def second(_e):
            calls.append("second")
            return True

        F.all_filters(first, second)(SimpleNamespace())
        self.assertEqual(calls, ["first"])


class HandlerFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_filters_decide(self):
        dispatcher = Dispatcher()
        seen: list[str] = []
        dispatcher.register("new_message", lambda e: seen.append("private"), filters=(F.private,))
        dispatcher.register("new_message", lambda e: seen.append("group"), filters=(F.group,))
        await dispatcher.dispatch("new_message", SimpleNamespace(is_private=True))
        self.assertEqual(seen, ["private"])


class CommandFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_only_command_is_silent_in_group(self):
        dispatcher = Dispatcher()
        called: list[str] = []

        async def cmd(event):
            called.append("x")

        dispatcher.register_command("pm", cmd, filters=(F.private,))
        group = make_event(".pm", is_private=False, is_group=True)
        private = make_event(".pm", is_private=True)

        await dispatcher.dispatch_command(group)
        self.assertEqual(called, [])
        self.assertEqual(group.edits, [])  # молча: сообщение не трогаем

        await dispatcher.dispatch_command(private)
        self.assertEqual(called, ["x"])

    async def test_combined_and_async_command_filters(self):
        dispatcher = Dispatcher()
        called: list[str] = []

        async def admin(_e):
            return True

        dispatcher.register_command(
            "adm", lambda e: called.append("ok"), filters=(F.private, F.all_filters(admin, F.outgoing))
        )
        await dispatcher.dispatch_command(make_event(".adm", is_private=True, out=False))
        self.assertEqual(called, [])
        await dispatcher.dispatch_command(make_event(".adm", is_private=True, out=True))
        self.assertEqual(called, ["ok"])

    async def test_filters_are_kept_in_command_record(self):
        dispatcher = Dispatcher()
        command = dispatcher.register_command("c", lambda e: None, filters=[F.private])
        self.assertEqual(command.filters, (F.private,))
        self.assertEqual(dispatcher.register_command("d", lambda e: None).filters, ())

    async def test_failing_filter_is_reported_as_command_error(self):
        dispatcher = Dispatcher()

        def broken(_e):
            raise RuntimeError("filter boom")

        dispatcher.register_command("bad", lambda e: None, filters=(broken,))
        event = make_event(".bad")
        with self.assertLogs("candy", level="ERROR"):
            await dispatcher.dispatch_command(event)
        self.assertIn("Ошибка команды .bad", event.edits[-1])


class PriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_equal_priority_keeps_registration_order(self):
        dispatcher = Dispatcher()
        order: list[int] = []
        for number in range(5):
            dispatcher.register("new_message", lambda e, n=number: order.append(n), priority=3)
        await dispatcher.dispatch("new_message", SimpleNamespace())
        self.assertEqual(order, [0, 1, 2, 3, 4])

    async def test_unregister_during_dispatch_skips_removed_handler(self):
        dispatcher = Dispatcher()
        order: list[str] = []
        second = None

        def first(_e):
            order.append("first")
            dispatcher.unregister(second)

        dispatcher.register("new_message", first, priority=2)
        second = dispatcher.register("new_message", lambda e: order.append("second"), priority=1)
        dispatcher.register("new_message", lambda e: order.append("third"), priority=0)
        await dispatcher.dispatch("new_message", SimpleNamespace())
        self.assertEqual(order, ["first", "third"])
        self.assertFalse(second.enabled)

    async def test_register_during_dispatch_does_not_affect_current_round(self):
        dispatcher = Dispatcher()
        order: list[str] = []

        def first(_e):
            order.append("first")
            dispatcher.register("new_message", lambda e: order.append("late"))

        dispatcher.register("new_message", first)
        await dispatcher.dispatch("new_message", SimpleNamespace())
        self.assertEqual(order, ["first"])
        await dispatcher.dispatch("new_message", SimpleNamespace())
        self.assertEqual(order, ["first", "first", "late"])


class CommandContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_is_available_inside_and_cleared_after(self):
        dispatcher = Dispatcher()
        seen = []

        async def cmd(event):
            seen.append(dispatcher.get_command(event).args)

        dispatcher.register_command("c", cmd)
        event = make_event(".c a b")
        await dispatcher.dispatch_command(event)
        self.assertEqual(seen, [("a", "b")])
        self.assertIsNone(dispatcher.get_command(event))

    async def test_parallel_commands_do_not_mix_up(self):
        dispatcher = Dispatcher()
        gate = asyncio.Event()
        seen: dict[str, tuple] = {}

        async def cmd(event):
            before = dispatcher.get_command(event).args
            await gate.wait()
            seen[event.raw_text] = (before, dispatcher.get_command(event).args)

        dispatcher.register_command("c", cmd)
        one, two = make_event(".c one"), make_event(".c two")
        tasks = [asyncio.create_task(dispatcher.dispatch_command(e)) for e in (one, two)]
        await asyncio.sleep(0.02)
        gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(seen[".c one"], (("one",), ("one",)))
        self.assertEqual(seen[".c two"], (("two",), ("two",)))

    async def test_context_is_for_the_event_it_belongs_to(self):
        dispatcher = Dispatcher()
        other = make_event(".x")
        seen = []

        async def cmd(event):
            seen.append(dispatcher.get_command(other))

        dispatcher.register_command("c", cmd)
        await dispatcher.dispatch_command(make_event(".c"))
        self.assertEqual(seen, [None])

    async def test_context_survives_a_failing_command(self):
        dispatcher = Dispatcher()

        async def cmd(event):
            raise RuntimeError("x")

        dispatcher.register_command("c", cmd)
        event = make_event(".c")
        with self.assertLogs("candy", level="ERROR"):
            await dispatcher.dispatch_command(event)
        self.assertIsNone(dispatcher.get_command(event))

    async def test_event_with_slots_works(self):
        class Slotted:
            __slots__ = ("raw_text",)

        dispatcher = Dispatcher()
        seen = []
        dispatcher.register_command("c", lambda e: seen.append(dispatcher.get_command(e).name))
        event = Slotted()
        event.raw_text = ".c"
        await dispatcher.dispatch_command(event)
        self.assertEqual(seen, ["c"])


class AnswerTests(unittest.IsolatedAsyncioTestCase):
    def event(self, edit, respond=None):
        return SimpleNamespace(edit=edit, respond=respond)

    async def test_plain_edit(self):
        edit = AsyncMock()
        self.assertTrue(await answer(self.event(edit), "hi"))
        edit.assert_awaited_once_with("hi")

    @unittest.skipIf(EventBridge is None, "Telethon не установлен")
    async def test_message_not_modified_is_success(self):
        respond = AsyncMock()
        edit = AsyncMock(side_effect=MessageNotModifiedError(request=None))
        self.assertTrue(await answer(self.event(edit, respond), "same"))
        respond.assert_not_awaited()

    @unittest.skipIf(EventBridge is None, "Telethon не установлен")
    async def test_short_flood_wait_is_retried_once(self):
        edit = AsyncMock(side_effect=[FloodWaitError(request=None, capture=0), None])
        respond = AsyncMock()
        with self.assertLogs("candy", level="WARNING"):
            self.assertTrue(await answer(self.event(edit, respond), "hi"))
        self.assertEqual(edit.await_count, 2)
        respond.assert_not_awaited()

    @unittest.skipIf(EventBridge is None, "Telethon не установлен")
    async def test_long_flood_wait_falls_back_to_respond(self):
        edit = AsyncMock(side_effect=FloodWaitError(request=None, capture=9999))
        respond = AsyncMock()
        with self.assertLogs("candy", level="WARNING"):
            self.assertTrue(await answer(self.event(edit, respond), "hi"))
        edit.assert_awaited_once()
        respond.assert_awaited_once_with("hi")

    @unittest.skipIf(EventBridge is None, "Telethon не установлен")
    async def test_second_flood_wait_falls_back_to_respond(self):
        edit = AsyncMock(side_effect=FloodWaitError(request=None, capture=0))
        respond = AsyncMock()
        with self.assertLogs("candy", level="WARNING"):
            self.assertTrue(await answer(self.event(edit, respond), "hi"))
        respond.assert_awaited_once_with("hi")

    async def test_other_edit_error_falls_back_to_respond(self):
        edit = AsyncMock(side_effect=RuntimeError("can't edit"))
        respond = AsyncMock()
        with self.assertLogs("candy", level="WARNING"):
            self.assertTrue(await answer(self.event(edit, respond), "hi"))
        respond.assert_awaited_once_with("hi")

    async def test_everything_failing_returns_false_and_never_raises(self):
        edit = AsyncMock(side_effect=RuntimeError("a"))
        respond = AsyncMock(side_effect=RuntimeError("b"))
        with self.assertLogs("candy", level="WARNING"):
            self.assertFalse(await answer(self.event(edit, respond), "hi"))

    async def test_event_without_respond(self):
        edit = AsyncMock(side_effect=RuntimeError("a"))
        with self.assertLogs("candy", level="WARNING"):
            self.assertFalse(await answer(SimpleNamespace(edit=edit), "hi"))

    async def test_cancellation_is_not_swallowed(self):
        started = asyncio.Event()

        async def slow(_text):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(answer(self.event(slow, AsyncMock()), "hi"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class TimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_handler_is_cut_and_next_one_runs(self):
        dispatcher = Dispatcher(handler_timeout=0.05)
        order: list[str] = []
        cancelled = asyncio.Event()

        async def hung(_e):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        dispatcher.register("new_message", hung, priority=5, name="hung")
        dispatcher.register("new_message", lambda e: order.append("next"))
        with self.assertLogs("candy", level="ERROR") as logs:
            await asyncio.wait_for(dispatcher.dispatch("new_message", SimpleNamespace()), 2)
        self.assertEqual(order, ["next"])
        self.assertTrue(cancelled.is_set())
        self.assertIn("hung", logs.output[0])
        self.assertIn("прерван", logs.output[0])

    async def test_fast_handlers_are_not_affected(self):
        dispatcher = Dispatcher(handler_timeout=1)
        order: list[str] = []

        async def quick(_e):
            await asyncio.sleep(0.01)
            order.append("done")

        dispatcher.register("new_message", quick)
        await dispatcher.dispatch("new_message", SimpleNamespace())
        self.assertEqual(order, ["done"])

    async def test_timeout_error_from_inside_is_an_error_not_a_timeout(self):
        dispatcher = Dispatcher(handler_timeout=5)
        order: list[str] = []

        async def inner(_e):
            raise TimeoutError("socket")

        dispatcher.register("new_message", inner, name="inner")
        dispatcher.register("new_message", lambda e: order.append("next"))
        with self.assertLogs("candy", level="ERROR") as logs:
            await dispatcher.dispatch("new_message", SimpleNamespace())
        self.assertEqual(order, ["next"])
        self.assertIn("Ошибка обработчика 'inner'", logs.output[0])

    async def test_zero_or_none_disables_limit(self):
        for value in (0, None):
            dispatcher = Dispatcher(handler_timeout=value, command_timeout=value)
            self.assertIsNone(dispatcher.handler_timeout)
            self.assertIsNone(dispatcher.command_timeout)

    async def test_outer_cancellation_still_propagates(self):
        dispatcher = Dispatcher(handler_timeout=5)
        started = asyncio.Event()

        async def waiting(_e):
            started.set()
            await asyncio.Event().wait()

        dispatcher.register("new_message", waiting)
        task = asyncio.create_task(dispatcher.dispatch("new_message", SimpleNamespace()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_hung_command_is_cut_and_user_is_told(self):
        dispatcher = Dispatcher(command_timeout=0.05)

        async def hung(_e):
            await asyncio.Event().wait()

        dispatcher.register_command("slow", hung)
        event = make_event(".slow")
        with self.assertLogs("candy", level="ERROR"):
            await asyncio.wait_for(dispatcher.dispatch_command(event), 2)
        self.assertIn("не уложилась", event.edits[-1])
        self.assertIsNone(dispatcher.get_command(event))

    async def test_failed_command_tells_the_user(self):
        dispatcher = Dispatcher()

        async def broken(_e):
            raise ValueError("boom")

        dispatcher.register_command("bad", broken)
        event = make_event(".bad")
        with self.assertLogs("candy", level="ERROR"):
            await dispatcher.dispatch_command(event)
        self.assertEqual(event.edits, ["❌ Ошибка команды .bad. Подробности в логе."])

    async def test_command_that_answered_before_failing_gets_error_appended_safely(self):
        # Если edit тоже падает, ошибка ответа не должна выйти наружу.
        dispatcher = Dispatcher()

        async def broken(_e):
            raise ValueError("boom")

        dispatcher.register_command("bad", broken)

        async def bad_edit(_t):
            raise RuntimeError("no edit")

        event = SimpleNamespace(raw_text=".bad", edit=bad_edit)
        with self.assertLogs("candy", level="WARNING"):
            await dispatcher.dispatch_command(event)  # не бросает


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_listeners_and_has_handlers(self):
        dispatcher = Dispatcher()
        seen: list[str] = []
        dispatcher.add_listener(seen.append)
        self.assertFalse(dispatcher.has_handlers("raw"))
        handler = dispatcher.register("raw", lambda e: None)
        self.assertTrue(dispatcher.has_handlers("raw"))
        dispatcher.unregister(handler)
        self.assertFalse(dispatcher.has_handlers("raw"))
        self.assertEqual(seen, ["raw", "raw"])
        dispatcher.remove_listener(seen.append)

    async def test_broken_listener_does_not_break_registration(self):
        dispatcher = Dispatcher()

        def broken(_t):
            raise RuntimeError("listener")

        dispatcher.add_listener(broken)
        with self.assertLogs("candy", level="ERROR"):
            handler = dispatcher.register("new_message", lambda e: None)
        self.assertTrue(handler.enabled)


class FakeClient:
    def __init__(self):
        self.added: list = []
        self.removed: list = []

    def add_event_handler(self, callback, builder):
        self.added.append((callback, builder))

    def remove_event_handler(self, callback, builder):
        self.removed.append((callback, builder))

    def active(self):
        live = list(self.added)
        for item in self.removed:
            live.remove(item)
        return live


@unittest.skipIf(EventBridge is None, "Telethon не установлен")
class LazyRawTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FakeClient()
        self.dispatcher = Dispatcher()
        self.bridge = EventBridge(self.client, self.dispatcher)

    def raw_active(self):
        return [item for item in self.client.active() if item[0] == self.bridge._raw]

    def test_raw_is_not_registered_without_raw_handlers(self):
        self.bridge.register()
        self.assertEqual(len(self.client.active()), 1)  # только новые сообщения
        self.assertEqual(self.raw_active(), [])

    def test_raw_follows_raw_handlers(self):
        self.bridge.register()
        first = self.dispatcher.register("raw", lambda e: None)
        second = self.dispatcher.register("raw", lambda e: None)
        self.assertEqual(len(self.raw_active()), 1)  # один раз, не по разу на обработчик
        self.dispatcher.unregister(first)
        self.assertEqual(len(self.raw_active()), 1)
        self.dispatcher.unregister(second)
        self.assertEqual(self.raw_active(), [])

    def test_other_event_types_do_not_touch_raw(self):
        self.bridge.register()
        self.dispatcher.register("new_message", lambda e: None)
        self.assertEqual(self.raw_active(), [])

    def test_existing_raw_handlers_are_picked_up_on_register(self):
        self.dispatcher.register("raw", lambda e: None)
        self.bridge.register()
        self.assertEqual(len(self.raw_active()), 1)

    def test_unregister_removes_everything_and_stops_listening(self):
        self.bridge.register()
        self.dispatcher.register("raw", lambda e: None)
        self.bridge.unregister()
        self.assertEqual(self.client.active(), [])
        self.dispatcher.register("raw", lambda e: None)
        self.assertEqual(self.client.active(), [])
        self.bridge.unregister()  # повторный вызов безопасен

    def test_register_is_idempotent(self):
        self.bridge.register()
        self.bridge.register()
        self.assertEqual(len(self.client.active()), 1)

    def test_edited_deleted_callback_follow_their_handlers(self):
        self.bridge.register()
        for event_type in ("edited", "deleted", "callback"):
            with self.subTest(event_type=event_type):
                before = len(self.client.active())
                first = self.dispatcher.register(event_type, lambda e: None)
                second = self.dispatcher.register(event_type, lambda e: None)
                self.assertEqual(len(self.client.active()), before + 1)  # одна подписка на тип
                self.dispatcher.unregister(first)
                self.assertEqual(len(self.client.active()), before + 1)
                self.dispatcher.unregister(second)
                self.assertEqual(len(self.client.active()), before)

    def test_existing_handlers_of_every_type_are_picked_up_on_register(self):
        for event_type in ("edited", "deleted", "callback", "raw"):
            self.dispatcher.register(event_type, lambda e: None)
        self.bridge.register()
        self.assertEqual(len(self.client.active()), 5)
        self.bridge.unregister()
        self.assertEqual(self.client.active(), [])

    async def test_edited_deleted_callback_events_reach_handlers(self):
        got = []
        for event_type in ("edited", "deleted", "callback"):
            self.dispatcher.register(event_type, lambda e, t=event_type: got.append((t, e)))
        await self.bridge._edited("a")
        await self.bridge._deleted("b")
        await self.bridge._callback("c")
        self.assertEqual(got, [("edited", "a"), ("deleted", "b"), ("callback", "c")])

    async def test_raw_events_reach_handlers(self):
        got = []
        self.dispatcher.register("raw", got.append)
        self.bridge.register()
        await self.bridge._raw("update")
        self.assertEqual(got, ["update"])

    async def test_outgoing_message_runs_command_incoming_does_not(self):
        calls = []
        self.dispatcher.register_command("c", lambda e: calls.append(e.out))
        await self.bridge._new_message(make_event(".c", out=False))
        await self.bridge._new_message(make_event(".c", out=True))
        self.assertEqual(calls, [True])


class TimeoutSettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = load_settings({})
        self.assertEqual((settings.handler_timeout, settings.command_timeout), (30.0, 120.0))

    def test_overrides_and_zero(self):
        settings = load_settings({"CANDY_HANDLER_TIMEOUT": "5.5", "CANDY_COMMAND_TIMEOUT": "0"})
        self.assertEqual((settings.handler_timeout, settings.command_timeout), (5.5, 0.0))
        self.assertEqual(settings.warnings, ())

    def test_invalid_values_fall_back_with_warning(self):
        for bad in ("abc", "-3", "nan", "inf"):
            with self.subTest(bad=bad):
                settings = load_settings({"CANDY_HANDLER_TIMEOUT": bad})
                self.assertEqual(settings.handler_timeout, 30.0)
                self.assertEqual(len(settings.warnings), 1)
                self.assertIn("CANDY_HANDLER_TIMEOUT", settings.warnings[0])


if __name__ == "__main__":
    unittest.main()
