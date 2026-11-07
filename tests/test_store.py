"""Магазин плагинов: индекс, проверка файлов, установка, откат, удаление и команда .store."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.errors import UserError
from core.store import (
    MAX_PLUGIN_BYTES,
    PluginStore,
    StoreError,
    build_entry,
    build_index,
    dump_index,
    parse_index,
    read_metadata,
)
from core.updater import UpdateError
from modules.system.store import StoreModule
from tests.test_plugins import Fixture

ROOT = Path(__file__).resolve().parent.parent

PLUGIN = '''\
from core.plugin import Plugin


class Demo(Plugin):
    name = "{name}"
    version = "{version}"
    description = "Демо {name}"
    min_core = "{min_core}"
    requires = {requires}

    async def setup(self) -> None:
        self.api.register_command("{name}", self.run, description="демо", usage=".{name}")

    async def run(self, event) -> None:
        await event.edit("ok")


def create_plugin(app):
    return Demo(app)
'''


def source(name="demo", version="1.0.0", min_core="", requires="()") -> str:
    return PLUGIN.format(name=name, version=version, min_core=min_core, requires=requires)


class FakeRepo:
    """Подмена сети: отдаёт индекс и файлы плагинов, считает запросы."""

    def __init__(self, files: dict[str, str]):
        self.files = {name: text.encode("utf-8") for name, text in files.items()}
        self.override: dict[str, bytes] = {}
        self.urls: list[str] = []

    def index(self) -> bytes:
        plugins = {}
        for name, data in self.files.items():
            text = data.decode("utf-8")
            meta = read_metadata(text, name)
            plugins[meta["name"]] = {
                "version": meta["version"],
                "description": meta["description"],
                "min_core": meta.get("min_core", ""),
                "requires": list(meta.get("requires", ())),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        return json.dumps({"schema": 1, "plugins": plugins}).encode()

    def __call__(self, url: str, limit: int) -> bytes:
        self.urls.append(url)
        prefix = "https://raw.githubusercontent.com/Vital-Candy/Candy-userbot/main/store/"
        assert url.startswith(prefix), url
        relative = url[len(prefix):]
        if relative in self.override:
            return self.override[relative]
        if relative == "index.json":
            return self.index()
        name = relative.removeprefix("plugins/").removesuffix(".py")
        if name not in self.files:
            raise UpdateError("Репозиторий или ветка не найдены (HTTP 404).")
        return self.files[name]


class MetadataTests(unittest.TestCase):
    def test_reads_metadata_without_running_code(self):
        meta = read_metadata(source("a_b", "1.2.3", "2.0", '("x",)') + "\nraise SystemExit('не запускать')\n")
        self.assertEqual((meta["name"], meta["version"], meta["min_core"], meta["requires"]), ("a_b", "1.2.3", "2.0", ("x",)))

    def test_rejects_bad_sources(self):
        cases = {
            "def broken(:\n": "синтаксиса",
            "class P:\n    pass\n": "create_plugin",
            "def create_plugin(app): pass\n": "ровно один",
            source().replace('version = "1.0.0"', "version = compute()"): "литералом",
        }
        for text, expected in cases.items():
            with self.subTest(expected=expected), self.assertRaises(StoreError) as ctx:
                read_metadata(text)
            self.assertIn(expected, str(ctx.exception))

    def test_build_entry_checks_name_version_description(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        def write(name, text):
            (tmp / f"{name}.py").write_text(text, encoding="utf-8")
            return tmp / f"{name}.py"
        entry = build_entry(write("demo", source()))
        self.assertEqual(entry["sha256"], hashlib.sha256(source().encode()).hexdigest())
        bad = {
            "other": source("demo"),  # имя в файле не совпадает с именем файла
            "Upper": source("Upper"),
            "ver": source("ver", version="x.y"),
            "mc": source("mc", min_core="abc"),
            "req": source("req", requires='("Bad Name",)'),
            "nodesc": source("nodesc").replace('description = "Демо nodesc"', 'description = ""'),
            "big": source("big") + "#" + "x" * MAX_PLUGIN_BYTES,
        }
        for name, text in bad.items():
            with self.subTest(name=name), self.assertRaises(StoreError):
                build_entry(write(name, text))


class IndexTests(unittest.TestCase):
    def test_repository_index_is_fresh(self):
        """Индекс в репозитории совпадает с файлами: если упало, запустите tools/build_store_index.py."""
        expected = dump_index(build_index(ROOT / "store"))
        self.assertEqual((ROOT / "store" / "index.json").read_text(encoding="utf-8"), expected)

    def test_build_and_parse_roundtrip(self):
        entries = parse_index((ROOT / "store" / "index.json").read_text(encoding="utf-8"))
        self.assertIn("example", entries)
        self.assertEqual(entries["example"].file, "plugins/example.py")

    def test_parse_rejects_broken_indexes(self):
        good = {"schema": 1, "plugins": {"demo": {
            "version": "1.0", "description": "d", "sha256": "a" * 64, "size": 10}}}
        parse_index(json.dumps(good))
        def variant(**changes):
            item = dict(good["plugins"]["demo"], **changes)
            return json.dumps({"schema": 1, "plugins": {"demo": item}})
        bad = [
            "не json", "[]", json.dumps({"schema": 2, "plugins": {}}), json.dumps({"schema": 1}),
            variant(sha256="xyz"), variant(sha256="A" * 64), variant(size=0), variant(size=True),
            variant(size=MAX_PLUGIN_BYTES + 1), variant(version="v"), variant(min_core="q"),
            variant(requires="demo"), variant(requires=["Bad"]), variant(description="x" * 500),
            json.dumps({"schema": 1, "plugins": {"../evil": good["plugins"]["demo"]}}),
            json.dumps({"schema": 1, "plugins": {"demo": "строка"}}),
        ]
        for text in bad:
            with self.subTest(text=text[:50]), self.assertRaises(StoreError):
                parse_index(text)


class StoreClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.plugins = self.tmp / "plugins"
        self.repo = FakeRepo({"demo": source("demo", "1.0.0"), "other": source("other", "2.0.0")})
        self.store = self.make_store()

    def make_store(self, **kwargs):
        return PluginStore(self.plugins, self.tmp / "data" / "store.json", fetch=self.repo, **kwargs)

    def test_install_writes_file_and_state(self):
        result = self.store.install("demo")
        self.assertEqual(result.entry.version, "1.0.0")
        self.assertEqual((self.plugins / "demo.py").read_text(encoding="utf-8"), source("demo", "1.0.0"))
        self.assertEqual(self.store.installed()["demo"]["version"], "1.0.0")
        self.assertEqual(self.store.status(self.store.entry("demo")), "current")
        self.assertEqual(list(self.plugins.glob(".*")), [])  # временных файлов нет

    def test_wrong_checksum_or_size_is_refused_and_nothing_is_written(self):
        for tampered in (source("demo", "1.0.0") + "# подмена", source("demo", "1.0.0").replace("ok", "ko")):
            self.repo.override["plugins/demo.py"] = tampered.encode("utf-8")
            with self.assertRaises(StoreError) as ctx:
                self.store.install("demo")
            self.assertTrue("сумма" in str(ctx.exception) or "слишком большой" in str(ctx.exception))
        self.assertFalse((self.plugins / "demo.py").exists())
        self.assertEqual(self.store.installed(), {})

    def test_syntax_error_in_file_with_valid_checksum_is_refused(self):
        broken = "class P:\n    x = (\n"
        repo = FakeRepo({})
        data = broken.encode()
        repo.files["demo"] = data
        with patch.object(FakeRepo, "index", lambda self: json.dumps({"schema": 1, "plugins": {"demo": {
                "version": "1.0", "description": "d", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}}).encode()):
            store = PluginStore(self.plugins, self.tmp / "s.json", fetch=repo)
            with self.assertRaises(StoreError):
                store.install("demo")
        self.assertFalse((self.plugins / "demo.py").exists())

    def test_foreign_file_is_never_overwritten(self):
        self.plugins.mkdir()
        (self.plugins / "demo.py").write_text("мой плагин", encoding="utf-8")
        for force in (False, True):
            with self.assertRaises(StoreError) as ctx:
                self.store.install("demo", force=force)
            self.assertIn("не из магазина", str(ctx.exception))
        self.assertEqual((self.plugins / "demo.py").read_text(encoding="utf-8"), "мой плагин")
        with self.assertRaises(StoreError):
            self.store.remove("demo")
        self.assertTrue((self.plugins / "demo.py").exists())

    def test_locally_modified_file_needs_force(self):
        self.store.install("demo")
        (self.plugins / "demo.py").write_text(source("demo", "1.0.0") + "# моя правка\n", encoding="utf-8")
        self.assertEqual(self.store.status(self.store.entry("demo")), "modified")
        with self.assertRaises(StoreError) as ctx:
            self.store.install("demo")
        self.assertIn("--force", str(ctx.exception))
        self.store.install("demo", force=True)
        self.assertEqual((self.plugins / "demo.py").read_text(encoding="utf-8"), source("demo", "1.0.0"))

    def test_update_flow_and_status(self):
        self.store.install("demo")
        self.repo.files["demo"] = source("demo", "1.1.0").encode()
        entry = self.store.entry("demo")
        self.assertEqual(self.store.status(entry), "update")
        result = self.store.install("demo")
        self.assertEqual(result.previous_version, "1.0.0")
        self.assertEqual(self.store.installed()["demo"]["version"], "1.1.0")

    def test_rollback_restores_previous_or_removes_new(self):
        first = self.store.install("demo")
        self.store.rollback(first)
        self.assertFalse((self.plugins / "demo.py").exists())
        self.assertEqual(self.store.installed(), {})
        self.store.install("demo")
        self.repo.files["demo"] = source("demo", "1.1.0").encode()
        second = self.store.install("demo")
        self.store.rollback(second)
        self.assertEqual((self.plugins / "demo.py").read_text(encoding="utf-8"), source("demo", "1.0.0"))
        self.assertEqual(self.store.installed()["demo"]["version"], "1.0.0")

    def test_remove(self):
        self.assertFalse(self.store.remove("demo"))
        self.store.install("demo")
        self.assertTrue(self.store.remove("demo"))
        self.assertFalse((self.plugins / "demo.py").exists())
        self.assertEqual(self.store.installed(), {})

    def test_core_version_and_requires_are_checked(self):
        self.repo.files["new"] = source("new", min_core="9.0").encode()
        with self.assertRaises(StoreError) as ctx:
            self.store.install("new")
        self.assertIn("9.0", str(ctx.exception))
        self.repo.files["dep"] = source("dep", requires='("demo",)').encode()
        with self.assertRaises(StoreError) as ctx:
            self.store.install("dep")
        self.assertIn("demo", str(ctx.exception))
        self.store.install("demo")
        self.store.install("dep")

    def test_names_are_validated_against_path_tricks(self):
        for name in ("../evil", "a/b", "Demo", "", ".", "a b", "x" * 40, "1abc"):
            with self.subTest(name=name):
                with self.assertRaises(StoreError):
                    self.store.install(name)
                with self.assertRaises(StoreError):
                    self.store.remove(name)
        self.assertEqual(self.repo.urls, [])  # в сеть с такими именами не ходим

    def test_unknown_plugin_and_missing_store(self):
        with self.assertRaises(StoreError) as ctx:
            self.store.entry("nothing")
        self.assertIn("нет плагина", str(ctx.exception))
        def no_store(url, limit):
            raise UpdateError("Репозиторий или ветка не найдены (HTTP 404).")
        with self.assertRaises(StoreError) as ctx:
            PluginStore(self.plugins, self.tmp / "s.json", fetch=no_store).index()
        self.assertIn("нет магазина", str(ctx.exception))

    def test_state_file_damage_is_survivable(self):
        state = self.tmp / "data" / "store.json"
        state.parent.mkdir()
        for text in ("не json", "[]", json.dumps({"demo": "строка", "../x": {"version": "1"}})):
            state.write_text(text, encoding="utf-8")
            self.assertEqual(self.store.installed(), {})

    def test_bad_repo_or_branch_is_refused(self):
        for repo, branch in (("не/репо ", "main"), ("a/b", "../x"), ("a/b", "x y")):
            with self.assertRaises(StoreError):
                PluginStore(self.plugins, self.tmp / "s.json", repo=repo, branch=branch)

    def test_urls_only_point_to_store_directory_of_the_repository(self):
        self.store.install("demo")
        self.assertTrue(all("/store/" in url for url in self.repo.urls))


class StoreCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = Fixture()
        self.repo = FakeRepo({"demo": source("demo", "1.0.0"), "other": source("other", "2.0.0")})
        self.state = self.fx.root.parent / "store.json"
        self.fx.app.dispatcher.gate = None
        self.module = StoreModule(self.fx.app)
        self.fx.app.plugins = self.fx.manager
        self.module._store = lambda: PluginStore(self.fx.root, self.state, fetch=self.repo)
        await self.module.start()

    async def asyncTearDown(self):
        await self.fx.manager.unload_all()
        await self.fx.app.background.stop_all()
        self.fx.close()

    async def run_cmd(self, text):
        event = SimpleNamespace(raw_text=text, edit=AsyncMock(), out=True, sender_id=1)
        await self.fx.app.dispatcher.dispatch_command(event)
        return event.edit.await_args.args[0]

    async def test_list_and_info(self):
        text = await self.run_cmd(".store")
        self.assertIn("demo v1.0.0", text)
        self.assertIn("[не установлен]", text)
        info = await self.run_cmd(".store info demo")
        self.assertIn("SHA-256", info)
        self.assertIn(".store install demo", info)
        self.assertIn("нет плагина", await self.run_cmd(".store info nothing"))

    async def test_install_requires_confirmation_then_loads_plugin(self):
        ask = await self.run_cmd(".store install demo")
        self.assertIn("--yes", ask)
        self.assertFalse((self.fx.root / "demo.py").exists())
        done = await self.run_cmd(".store install demo --yes")
        self.assertIn("установлен и загружен", done)
        self.assertIn("demo", self.fx.commands)
        self.assertIn("[установлен]", await self.run_cmd(".store"))
        self.assertIn("уже установлен", await self.run_cmd(".store install demo"))

    async def test_update_reloads_the_new_version(self):
        await self.run_cmd(".store install demo --yes")
        self.assertIn("Обновлений плагинов нет", await self.run_cmd(".store update"))
        self.repo.files["demo"] = source("demo", "1.1.0").encode()
        self.assertIn("demo v1.1.0", await self.run_cmd(".store update"))
        self.assertIn("--yes", await self.run_cmd(".store update demo"))
        done = await self.run_cmd(".store update demo --yes")
        self.assertIn("1.0.0 → v1.1.0", done)
        self.assertEqual(self.fx.manager.info("demo")["version"], "1.1.0")
        self.assertIn("последняя версия", await self.run_cmd(".store update demo"))

    async def test_update_of_not_installed_plugin_is_refused(self):
        self.assertIn("не установлен", await self.run_cmd(".store update demo --yes"))

    async def test_failed_load_rolls_everything_back(self):
        bad = source("demo", "1.0.0").replace("async def setup(self) -> None:", "async def setup(self) -> None:\n        raise RuntimeError('сломан')\n        ")
        self.repo.files["demo"] = bad.encode()
        with self.assertLogs("candy", level="ERROR"):
            text = await self.run_cmd(".store install demo --yes")
        self.assertIn("не загрузился", text)
        self.assertFalse((self.fx.root / "demo.py").exists())
        self.assertEqual(PluginStore(self.fx.root, self.state, fetch=self.repo).installed(), {})
        self.assertEqual(self.fx.manager.states(), [])

    async def test_failed_update_restores_old_working_version(self):
        await self.run_cmd(".store install demo --yes")
        bad = source("demo", "1.1.0").replace("async def setup(self) -> None:", "async def setup(self) -> None:\n        raise RuntimeError('сломан')\n        ")
        self.repo.files["demo"] = bad.encode()
        with self.assertLogs("candy", level="ERROR"):
            text = await self.run_cmd(".store update demo --yes")
        self.assertIn("Прежнее состояние возвращено", text)
        self.assertEqual(self.fx.manager.info("demo")["version"], "1.0.0")
        self.assertIn("demo", self.fx.commands)

    async def test_remove(self):
        await self.run_cmd(".store install demo --yes")
        self.assertIn("удалён", await self.run_cmd(".store remove demo"))
        self.assertFalse((self.fx.root / "demo.py").exists())
        self.assertNotIn("demo", self.fx.commands)
        self.assertEqual(self.fx.manager.states(), [])
        self.assertIn("не установлен", await self.run_cmd(".store remove demo"))

    async def test_own_plugin_is_not_removed(self):
        self.fx.write("mine", source("mine"))
        self.assertIn("не из магазина", await self.run_cmd(".store remove mine"))
        self.assertTrue((self.fx.root / "mine.py").exists())

    async def test_force_flag_replaces_modified_file(self):
        await self.run_cmd(".store install demo --yes")
        (self.fx.root / "demo.py").write_text(source("demo") + "# правка\n", encoding="utf-8")
        self.assertIn("--force", await self.run_cmd(".store install demo --yes"))
        self.assertIn("установлен и загружен", await self.run_cmd(".store install demo --yes --force"))

    async def test_usage_and_errors(self):
        for text in (".store install", ".store remove", ".store info", ".store frob", ".store list demo",
                     ".store install demo --bad", ".store install a b", ".store -x"):
            with self.subTest(text=text):
                self.assertIn("Использование", await self.run_cmd(text))
        self.assertIn("Имя плагина", await self.run_cmd(".store install ../evil --yes"))

    async def test_network_error_is_shown_not_raised(self):
        def offline(url, limit):
            raise UpdateError("Нет связи с GitHub: timeout")
        self.module._store = lambda: PluginStore(self.fx.root, self.state, fetch=offline)
        self.assertIn("Нет связи", await self.run_cmd(".store"))

    async def test_store_command_is_owner_only_and_not_shareable(self):
        command = self.fx.app.dispatcher.commands["store"]
        self.assertIsNone(command.level)
        self.assertFalse(command.allow_incoming)


class StoreAndUserErrorTests(unittest.TestCase):
    def test_store_error_is_user_error(self):
        self.assertTrue(issubclass(StoreError, UserError))


if __name__ == "__main__":
    unittest.main()
