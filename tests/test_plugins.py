"""Этап 6: plugin API, регистрации, зависимости, версии, хранилище плагина."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.background import BackgroundManager
from core.dispatcher import Dispatcher
from core.commands import register_commands
from core.plugin_api import PluginAPI
from core.plugins import PluginManager
from core.version import CORE_VERSION, at_least, parse_version
from storage.plugin_data import MAX_KEYS_PER_PLUGIN, MAX_VALUE_BYTES, PluginDataStore
from storage.service import Storage

TEMPLATE = """\
from core.plugin import Plugin

class P(Plugin):
    name = "{name}"
    version = "{version}"
    description = "{description}"
    requires = {requires}
    min_core = "{min_core}"

    async def setup(self):
        self.app.events_log.append("setup {name}")
        self.api.register_command("{command}", self.cmd, description="тест")
        self.api.register_handler("new_message", self.on_message)
        await self.api.add_background_task("tick", self.tick, interval=0.01)
        {setup_tail}

    async def teardown(self):
        self.app.events_log.append("teardown {name}")
        {teardown}

    async def cmd(self, event):
        self.app.events_log.append("cmd {version}")

    async def on_message(self, event):
        pass

    async def tick(self):
        self.app.ticks.append("{name}")

def create_plugin(app):
    return P(app)
"""


def source(name="demo", *, version="1.0", description="d", requires=(), min_core="",
           command=None, setup_tail="pass", teardown="pass"):
    return TEMPLATE.format(
        name=name, version=version, description=description, requires=repr(tuple(requires)),
        min_core=min_core, command=command or name, setup_tail=setup_tail, teardown=teardown,
    )


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "plugins"
        self.root.mkdir()
        self.storage = Storage(Path(self.tmp.name) / "db" / "candy.sqlite3")
        self.storage.open()
        self.app = SimpleNamespace(
            dispatcher=Dispatcher(),
            background=BackgroundManager(),
            storage=self.storage,
            identity=SimpleNamespace(account_id=AsyncMock(return_value=1)),
            events_log=[],
            ticks=[],
        )
        register_commands(self.app.dispatcher, AsyncMock())  # .help и .stop как у настоящего ядра
        self.manager = PluginManager(self.app, self.root)

    def write(self, name, text):
        (self.root / f"{name}.py").write_text(text, encoding="utf-8")

    def close(self):
        self.storage.close()
        self.tmp.cleanup()

    @property
    def commands(self):
        return set(self.app.dispatcher.commands)

    @property
    def task_names(self):
        return [item.name for item in self.app.background.list()]


class PluginApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = Fixture()

    async def asyncTearDown(self):
        await self.fx.app.background.stop_all()
        self.fx.close()

    async def test_plugin_registers_command_handler_and_task(self):
        self.fx.write("demo", source())
        self.assertTrue(await self.fx.manager.load("demo"))
        self.assertIn("demo", self.fx.commands)
        self.assertEqual(self.fx.task_names, ["plugin.demo.tick"])
        self.assertEqual(self.fx.app.dispatcher.handlers["new_message"][0].name, "demo.on_message")
        info = self.fx.manager.info("demo")
        self.assertEqual((info["commands"], info["handlers"], info["tasks"]), (1, 1, 1))
        await asyncio.sleep(0.05)
        self.assertIn("demo", self.fx.app.ticks)

    async def test_unload_removes_everything(self):
        self.fx.write("demo", source())
        await self.fx.manager.load("demo")
        self.assertTrue(await self.fx.manager.unload("demo"))
        self.assertEqual(self.fx.commands, {"help", "stop"})
        self.assertEqual(self.fx.task_names, [])
        self.assertEqual(self.fx.app.dispatcher.handlers["new_message"], [])
        self.assertEqual(self.fx.manager.info("demo")["commands"], 0)
        ticks = len(self.fx.app.ticks)
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.fx.app.ticks), ticks)  # задача действительно остановлена

    async def test_failing_teardown_still_removes_registrations(self):
        self.fx.write("demo", source(teardown='raise RuntimeError("teardown boom")'))
        await self.fx.manager.load("demo")
        with self.assertLogs("candy", level="ERROR"):
            self.assertFalse(await self.fx.manager.unload("demo"))
        self.assertEqual(self.fx.commands, {"help", "stop"})
        self.assertEqual(self.fx.task_names, [])
        self.assertEqual(self.fx.app.dispatcher.handlers["new_message"], [])
        self.assertIn("teardown boom", self.fx.manager.info("demo")["error"])

    async def test_failing_setup_leaves_nothing_behind(self):
        self.fx.write("demo", source(setup_tail='raise RuntimeError("setup boom")'))
        with self.assertLogs("candy", level="ERROR"):
            self.assertFalse(await self.fx.manager.load("demo"))
        self.assertEqual(self.fx.commands, {"help", "stop"})
        self.assertEqual(self.fx.task_names, [])
        self.assertEqual(self.fx.app.dispatcher.handlers["new_message"], [])
        state = self.fx.manager.states()[0]
        self.assertTrue(state.failed)
        self.assertEqual(state.error, "RuntimeError: setup boom")

    async def test_failing_setup_and_failing_teardown_still_clean(self):
        self.fx.write("demo", source(setup_tail='raise RuntimeError("s")', teardown='raise RuntimeError("t")'))
        with self.assertLogs("candy", level="ERROR"):
            self.assertFalse(await self.fx.manager.load("demo"))
        self.assertEqual(self.fx.commands, {"help", "stop"})
        self.assertEqual(self.fx.task_names, [])

    async def test_command_name_collision_rolls_back_earlier_registrations(self):
        self.fx.write("demo", source(command="help"))  # .help уже занят ядром
        with self.assertLogs("candy", level="ERROR"):
            self.assertFalse(await self.fx.manager.load("demo"))
        self.assertEqual(self.fx.commands, {"help", "stop"})
        self.assertIn("уже зарегистрирована", self.fx.manager.states()[0].error)

    async def test_other_plugins_keep_working_when_one_fails(self):
        self.fx.write("bad", source("bad", setup_tail='raise RuntimeError("x")'))
        self.fx.write("good", source("good"))
        with self.assertLogs("candy", level="ERROR"):
            await self.fx.manager.load_all()
        states = {s.name: s for s in self.fx.manager.states()}
        self.assertFalse(states["bad"].loaded)
        self.assertTrue(states["good"].loaded)
        self.assertIn("good", self.fx.commands)
        self.assertNotIn("bad", self.fx.commands)

    async def test_reload_replaces_command_and_task_without_restart(self):
        self.fx.write("demo", source(version="1.0"))
        await self.fx.manager.load("demo")
        old = self.fx.app.dispatcher.commands["demo"].callback
        self.fx.write("demo", source(version="2.0"))
        self.assertTrue(await self.fx.manager.reload("demo"))
        self.assertIsNot(self.fx.app.dispatcher.commands["demo"].callback, old)
        self.assertEqual(self.fx.task_names, ["plugin.demo.tick"])
        self.assertEqual(len(self.fx.app.dispatcher.handlers["new_message"]), 1)
        self.assertEqual(self.fx.manager.info("demo")["version"], "2.0")
        await self.fx.app.dispatcher.dispatch_command(SimpleNamespace(raw_text=".demo"))
        self.assertEqual(self.fx.app.events_log[-1], "cmd 2.0")

    async def test_reload_with_broken_new_version_leaves_no_old_registrations(self):
        self.fx.write("demo", source())
        await self.fx.manager.load("demo")
        self.fx.write("demo", source(setup_tail='raise RuntimeError("new broken")'))
        with self.assertLogs("candy", level="ERROR"):
            self.assertFalse(await self.fx.manager.reload("demo"))
        self.assertEqual(self.fx.commands, {"help", "stop"})
        self.assertEqual(self.fx.task_names, [])

    async def test_unload_all_removes_all_plugins(self):
        for name in ("a", "b", "c"):
            self.fx.write(name, source(name))
        await self.fx.manager.load_all()
        self.assertEqual(len(self.fx.task_names), 3)
        await self.fx.manager.unload_all()
        self.assertEqual(self.fx.task_names, [])
        self.assertEqual(self.fx.commands, {"help", "stop"})

    async def test_cancel_during_setup_cleans_up(self):
        tail = "await __import__('asyncio').Event().wait()"
        self.fx.write("demo", source(setup_tail=tail))
        task = asyncio.create_task(self.fx.manager.load("demo"))
        await asyncio.sleep(0.05)
        self.assertIn("demo", self.fx.commands)  # setup уже успел зарегистрировать
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.fx.commands, {"help", "stop"})
        self.assertEqual(self.fx.task_names, [])
        self.assertFalse(self.fx.manager.states()[0].loaded)

    async def test_foreign_command_with_same_name_is_not_removed(self):
        self.fx.write("demo", source())
        await self.fx.manager.load("demo")
        dispatcher = self.fx.app.dispatcher
        dispatcher.unregister_command("demo")
        other = dispatcher.register_command("demo", lambda e: None)
        await self.fx.manager.unload("demo")
        self.assertIs(dispatcher.commands["demo"], other)


class PluginApiObjectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = Fixture()
        self.api = PluginAPI(self.fx.app, "x")

    async def asyncTearDown(self):
        await self.api.cleanup()
        await self.fx.app.background.stop_all()
        self.fx.close()

    async def test_closed_api_refuses_new_registrations(self):
        await self.api.cleanup()
        self.assertTrue(self.api.closed)
        with self.assertRaisesRegex(RuntimeError, "выгружен"):
            self.api.register_command("late", lambda e: None)
        with self.assertRaises(RuntimeError):
            self.api.register_handler("new_message", lambda e: None)
        with self.assertRaises(RuntimeError):
            await self.api.add_background_task("t", lambda: None, interval=1)
        with self.assertRaises(RuntimeError):
            self.api.on_cleanup(lambda: None)
        with self.assertRaises(RuntimeError):
            await self.api.storage.set("k", 1)
        self.assertNotIn("late", self.fx.commands)

    async def test_cleanup_runs_in_reverse_order_and_isolates_errors(self):
        order = []

        def first():
            order.append("first")

        async def second():
            order.append("second")
            raise RuntimeError("cleanup failed")

        def third():
            order.append("third")

        for callback in (first, second, third):
            self.api.on_cleanup(callback)
        with self.assertLogs("candy", level="ERROR"):
            await self.api.cleanup()
        self.assertEqual(order, ["third", "second", "first"])
        await self.api.cleanup()  # повторный вызов безопасен
        self.assertEqual(order, ["third", "second", "first"])

    async def test_duplicate_task_name_is_refused(self):
        await self.api.add_background_task("t", lambda: None, interval=1)
        with self.assertRaises(ValueError):
            await self.api.add_background_task("t", lambda: None, interval=1)
        self.assertEqual(self.api.registrations["tasks"], 1)

    async def test_task_not_started_when_start_false(self):
        name = await self.api.add_background_task("t", lambda: None, interval=1, start=False)
        self.assertEqual(self.fx.app.background.get(name).status, "STOPPED")

    async def test_logger_is_namespaced_under_candy(self):
        self.assertEqual(self.api.logger.name, "candy.plugin.x")
        with self.assertLogs("candy", level="WARNING") as logs:
            self.api.logger.warning("привет")
        self.assertIn("привет", logs.output[0])

    async def test_invalid_command_options_are_not_counted(self):
        with self.assertRaises(ValueError):
            self.api.register_command("", lambda e: None)
        self.assertEqual(self.api.registrations["commands"], 0)


class PluginStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = Fixture()
        self.a = PluginAPI(self.fx.app, "a")
        self.b = PluginAPI(self.fx.app, "b")

    async def asyncTearDown(self):
        self.fx.close()

    async def test_roundtrip_and_types(self):
        value = {"list": [1, 2, {"k": "значение"}], "n": None, "f": 1.5}
        await self.a.storage.set("cfg", value)
        self.assertEqual(await self.a.storage.get("cfg"), value)
        self.assertEqual(await self.a.storage.get("missing", "dflt"), "dflt")
        await self.a.storage.set("cfg", 5)  # перезапись
        self.assertEqual(await self.a.storage.get("cfg"), 5)
        self.assertEqual(await self.a.storage.keys(), ["cfg"])
        self.assertTrue(await self.a.storage.delete("cfg"))
        self.assertFalse(await self.a.storage.delete("cfg"))

    async def test_plugins_are_isolated(self):
        await self.a.storage.set("k", "a-value")
        self.assertIsNone(await self.b.storage.get("k"))
        await self.b.storage.set("k", "b-value")
        self.assertEqual(await self.a.storage.get("k"), "a-value")

    async def test_accounts_are_isolated(self):
        await self.a.storage.set("k", 1)
        self.fx.app.identity.account_id.return_value = 2
        self.assertIsNone(await self.a.storage.get("k"))

    async def test_survives_reopening_the_database(self):
        await self.a.storage.set("k", [1])
        self.fx.storage.close()
        self.fx.storage.open()
        self.assertEqual(await self.a.storage.get("k"), [1])

    async def test_limits_and_bad_values(self):
        with self.assertRaises(ValueError):
            await self.a.storage.set("k", object())
        with self.assertRaises(ValueError):
            await self.a.storage.set("", 1)
        with self.assertRaises(ValueError):
            await self.a.storage.set("k" * 200, 1)
        with self.assertRaisesRegex(ValueError, "КиБ"):
            await self.a.storage.set("big", "x" * (MAX_VALUE_BYTES + 1))
        self.assertEqual(await self.a.storage.keys(), [])

    async def test_key_count_limit(self):
        store = PluginDataStore(self.fx.storage.database)
        for number in range(MAX_KEYS_PER_PLUGIN):
            store.set(account_id=1, plugin="p", key=str(number), value=number)
        with self.assertRaisesRegex(ValueError, "ключей"):
            store.set(account_id=1, plugin="p", key="one-more", value=1)
        store.set(account_id=1, plugin="p", key="0", value="overwrite is fine")
        self.assertEqual(store.clear(account_id=1, plugin="p"), MAX_KEYS_PER_PLUGIN)

    async def test_failed_set_is_atomic(self):
        store = PluginDataStore(self.fx.storage.database)
        store.set(account_id=1, plugin="p", key="k", value="old")
        with self.assertRaises(ValueError):
            store.set(account_id=1, plugin="p", key="k", value=object())
        self.assertEqual(store.get(account_id=1, plugin="p", key="k"), "old")

    async def test_plugin_data_survives_plugin_reload(self):
        self.fx.write("demo", source(setup_tail="await self.api.storage.set('seen', ((await self.api.storage.get('seen')) or 0) + 1)"))
        await self.fx.manager.load("demo")
        await self.fx.manager.reload("demo")
        self.assertEqual(await PluginAPI(self.fx.app, "demo").storage.get("seen"), 2)
        await self.fx.manager.unload_all()
        await self.fx.app.background.stop_all()


class DependencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = Fixture()

    async def asyncTearDown(self):
        await self.fx.app.background.stop_all()
        self.fx.close()

    def log(self):
        return [line for line in self.fx.app.events_log if line.startswith(("setup", "teardown"))]

    async def test_dependency_is_loaded_first_even_if_alphabetically_later(self):
        self.fx.write("a_user", source("a_user", requires=("z_base",)))
        self.fx.write("z_base", source("z_base"))
        await self.fx.manager.load_all()
        self.assertEqual(self.log(), ["setup z_base", "setup a_user"])
        self.assertTrue(all(s.loaded for s in self.fx.manager.states()))

    async def test_unload_dependency_unloads_dependents_first(self):
        self.fx.write("a_user", source("a_user", requires=("z_base",)))
        self.fx.write("z_base", source("z_base"))
        await self.fx.manager.load_all()
        self.fx.app.events_log.clear()
        await self.fx.manager.unload("z_base")
        self.assertEqual(self.log(), ["teardown a_user", "teardown z_base"])
        self.assertFalse(any(s.loaded for s in self.fx.manager.states()))

    async def test_unload_all_goes_in_reverse_order(self):
        self.fx.write("a_user", source("a_user", requires=("z_base",)))
        self.fx.write("z_base", source("z_base"))
        await self.fx.manager.load_all()
        self.fx.app.events_log.clear()
        await self.fx.manager.unload_all()
        self.assertEqual(self.log(), ["teardown a_user", "teardown z_base"])

    async def test_missing_dependency(self):
        self.fx.write("user", source("user", requires=("ghost",)))
        with self.assertLogs("candy", level="ERROR") as logs:
            self.assertFalse(await self.fx.manager.load("user"))
        self.assertIn("ghost", self.fx.manager.states()[0].error)
        self.assertNotIn("Traceback", "\n".join(logs.output))  # понятная причина без traceback
        self.assertEqual(self.fx.commands, {"help", "stop"})

    async def test_failing_dependency_blocks_dependent(self):
        self.fx.write("base", source("base", setup_tail='raise RuntimeError("base boom")'))
        self.fx.write("user", source("user", requires=("base",)))
        with self.assertLogs("candy", level="ERROR"):
            await self.fx.manager.load_all()
        states = {s.name: s for s in self.fx.manager.states()}
        self.assertFalse(states["user"].loaded)
        self.assertIn("base", states["user"].error)
        self.assertIn("base boom", states["user"].error)
        self.assertEqual(self.fx.commands, {"help", "stop"})

    async def test_cycle_is_reported_without_hanging(self):
        self.fx.write("a", source("a", requires=("b",)))
        self.fx.write("b", source("b", requires=("a",)))
        with self.assertLogs("candy", level="ERROR"):
            await asyncio.wait_for(self.fx.manager.load_all(), 5)
        self.assertFalse(any(s.loaded for s in self.fx.manager.states()))
        self.assertEqual(self.fx.commands, {"help", "stop"})
        self.assertEqual(self.fx.task_names, [])

    async def test_self_dependency_and_string_requires(self):
        self.fx.write("me", source("me", requires=("me",)))
        with self.assertLogs("candy", level="ERROR"):
            self.assertFalse(await self.fx.manager.load("me"))
        self.assertIn("самого себя", self.fx.manager.states()[0].error)
        text = source("str").replace("requires = ()", "requires = 'base'")
        self.fx.write("str", text)
        with self.assertLogs("candy", level="ERROR"):
            self.assertFalse(await self.fx.manager.load("str"))
        self.assertIn("кортежем", self.fx.manager.info("str")["error"])

    async def test_dependency_shared_by_two_plugins_loads_once(self):
        self.fx.write("base", source("base"))
        self.fx.write("u1", source("u1", requires=("base",)))
        self.fx.write("u2", source("u2", requires=("base",)))
        await self.fx.manager.load_all()
        self.assertEqual(self.log().count("setup base"), 1)
        await self.fx.manager.unload("base")
        self.assertFalse(any(s.loaded for s in self.fx.manager.states()))


class CoreVersionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = Fixture()

    async def asyncTearDown(self):
        await self.fx.app.background.stop_all()
        self.fx.close()

    async def test_satisfied_requirement(self):
        self.fx.write("ok", source("ok", min_core="0.1"))
        self.assertTrue(await self.fx.manager.load("ok"))
        self.fx.write("same", source("same", min_core=CORE_VERSION))
        self.assertTrue(await self.fx.manager.load("same"))

    async def test_too_new_requirement_is_refused_with_clear_message(self):
        self.fx.write("future", source("future", min_core="99.0"))
        with self.assertLogs("candy", level="ERROR") as logs:
            self.assertFalse(await self.fx.manager.load("future"))
        error = self.fx.manager.states()[0].error
        self.assertIn("99.0", error)
        self.assertIn(CORE_VERSION, error)
        self.assertNotIn("Traceback", "\n".join(logs.output))
        self.assertEqual(self.fx.commands, {"help", "stop"})

    async def test_garbage_requirement(self):
        self.fx.write("junk", source("junk", min_core="abc"))
        with self.assertLogs("candy", level="ERROR"):
            self.assertFalse(await self.fx.manager.load("junk"))
        self.assertIn("min_core", self.fx.manager.states()[0].error)

    async def test_version_helpers(self):
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertTrue(at_least("1.2", "1.2.0"))
        self.assertTrue(at_least("1.10", "1.9"))
        self.assertFalse(at_least("1.2", "1.2.1"))
        self.assertTrue(at_least("2", "1.99"))
        for bad in ("", "x", "1..2", "-1"):
            with self.assertRaises(ValueError):
                parse_version(bad)


class ExamplePluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_shipped_example_works(self):
        example = Path(__file__).resolve().parent.parent / "store" / "plugins" / "example.py"
        fx = Fixture()
        try:
            fx.write("example", example.read_text(encoding="utf-8"))
            self.assertTrue(await fx.manager.load("example"))
            self.assertIn("hello", fx.commands)
            self.assertEqual(fx.task_names, ["plugin.example.tick"])
            event = SimpleNamespace(raw_text=".hello", edit=AsyncMock())
            await fx.app.dispatcher.dispatch_command(event)
            event.edit.assert_awaited_once_with("Привет из плагина! 🍬")
            self.assertTrue(await fx.manager.reload("example"))
            self.assertEqual(await PluginAPI(fx.app, "example").storage.get("runs"), 2)
            await fx.manager.unload_all()
            self.assertEqual(fx.task_names, [])
            self.assertEqual(fx.commands, {"help", "stop"})
        finally:
            await fx.app.background.stop_all()
            fx.close()


class InfoTests(unittest.IsolatedAsyncioTestCase):
    async def test_info_and_errors(self):
        fx = Fixture()
        try:
            fx.write("demo", source(description="описание", requires=()))
            await fx.manager.load("demo")
            info = fx.manager.info("demo")
            self.assertEqual(info["description"], "описание")
            self.assertTrue(info["loaded"])
            self.assertEqual(info["error"], None)
            with self.assertRaises(KeyError):
                fx.manager.info("nope")
            await fx.manager.unload_all()
            await fx.app.background.stop_all()
            info = fx.manager.info("demo")
            self.assertFalse(info["loaded"])
            self.assertEqual((info["commands"], info["tasks"]), (0, 0))
        finally:
            fx.close()


if __name__ == "__main__":
    unittest.main()
