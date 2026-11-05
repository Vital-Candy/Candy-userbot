"""Этап 8: меню терминала, обновление из GitHub (.update), буфер логов."""
from __future__ import annotations

import asyncio
import io
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config.settings import load_settings
from core import menu as menu_mod
from core.dispatcher import Dispatcher
from core.errors import UserError
from core.menu import DEFAULT_LOGO, MenuUI, load_logo
from core.updater import (
    MAX_ARCHIVE_BYTES,
    UpdateError,
    UpdateInfo,
    Updater,
    default_fetch,
    parse_remote_version,
)
from modules.system.update import UpdateModule, describe
from utils import logger as log_utils

ROOT_NAME = "Candy-Userbot-main"


def version_py(version: str) -> str:
    return f'"""v"""\nCORE_VERSION = "{version}"\n'


def make_zip(files: dict[str, str], *, root: str | None = ROOT_NAME, extra=None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(f"{root}/{name}" if root else name, content)
        for info, content in extra or []:
            archive.writestr(info, content)
    return buffer.getvalue()


def good_tree(version: str = "1.1.0", **more: str) -> dict[str, str]:
    tree = {
        "main.py": "print('new')\n",
        "core/version.py": version_py(version),
        "core/new.py": "X = 1\n",
        "requirements.txt": "telethon\nextra\n",
        "plugins/example.py.disabled": "x\n",
        "data/leak.txt": "x\n",
        "logo.txt": "REPO LOGO\n",
    }
    tree.update(more)
    return tree


class FakeFetch:
    """Подменяет сеть: отдаёт заранее заданные байты и запоминает запросы."""

    def __init__(self, version: str | None = "1.1.0", archive: bytes | None = None):
        self.version = version
        self.archive = archive
        self.urls: list[str] = []

    def __call__(self, url: str, limit: int) -> bytes:
        self.urls.append(url)
        if url.endswith("/core/version.py"):
            if self.version is None:
                return b"nothing here"
            return version_py(self.version).encode()
        if "codeload.github.com" in url:
            return self.archive if self.archive is not None else make_zip(good_tree(self.version or "1.1.0"))
        raise AssertionError(url)


def make_root(tmp: Path) -> Path:
    root = tmp / "app"
    for name, content in {
        "main.py": "print('old')\n",
        "core/version.py": version_py("1.0.0"),
        "core/old_module.py": "OLD = 1\n",
        "requirements.txt": "telethon\n",
        "data/secret.txt": "keep",
        "plugins/mine.py": "mine",
        "logs/candy.log": "log",
        ".env": "CANDY_LOG_LEVEL=DEBUG\n",
        "logo.txt": "MY LOGO\n",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


# --------------------------------------------------------------------- логи


class RingBufferTests(unittest.TestCase):
    def test_keeps_last_lines_and_notifies(self):
        handler = log_utils.RingBufferHandler(capacity=3)
        handler.setFormatter(__import__("logging").Formatter("%(message)s"))
        seen: list[str] = []
        unsubscribe = handler.subscribe(seen.append)
        import logging

        for i in range(5):
            handler.emit(logging.LogRecord("c", logging.INFO, "", 0, f"m{i}", None, None))
        self.assertEqual(handler.lines(), ["m2", "m3", "m4"])
        self.assertEqual(handler.lines(2), ["m3", "m4"])
        self.assertEqual(seen, ["m0", "m1", "m2", "m3", "m4"])
        unsubscribe()
        handler.emit(logging.LogRecord("c", logging.INFO, "", 0, "m5", None, None))
        self.assertEqual(len(seen), 5)

    def test_broken_listener_does_not_break_logging(self):
        import logging

        handler = log_utils.RingBufferHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.subscribe(lambda line: 1 / 0)
        handler.emit(logging.LogRecord("c", logging.INFO, "", 0, "ok", None, None))
        self.assertEqual(handler.lines(), ["ok"])

    def test_mute_and_unmute_console(self):
        before = list(log_utils.logger.handlers)
        log_utils.mute_console()
        try:
            self.assertFalse(any(type(h) is __import__("logging").StreamHandler for h in log_utils.logger.handlers))
            self.assertIn(log_utils.ring, log_utils.logger.handlers)
        finally:
            log_utils.unmute_console()
        self.assertEqual(sorted(map(id, log_utils.logger.handlers)), sorted(map(id, before)))

    def test_muted_logs_still_reach_ring(self):
        log_utils.mute_console()
        try:
            log_utils.logger.info("тихая-строка-7")
        finally:
            log_utils.unmute_console()
        self.assertTrue(any("тихая-строка-7" in line for line in log_utils.ring.lines()))


# ----------------------------------------------------------------- настройки


class UpdateSettingsTests(unittest.TestCase):
    def load(self, **env):
        return load_settings(env, Path("/base"))

    def test_defaults(self):
        s = self.load()
        self.assertEqual((s.update_repo, s.update_branch, s.ui), ("Vital-Candy/Candy-userbot", "main", "menu"))
        self.assertEqual(s.warnings, ())

    def test_valid_overrides(self):
        s = self.load(CANDY_UPDATE_REPO="me/fork", CANDY_UPDATE_BRANCH="dev/x", CANDY_UI="CONSOLE")
        self.assertEqual((s.update_repo, s.update_branch, s.ui), ("me/fork", "dev/x", "console"))

    def test_invalid_values_fall_back_with_warnings(self):
        s = self.load(CANDY_UPDATE_REPO="evil.com/../x y", CANDY_UPDATE_BRANCH="a..b", CANDY_UI="gui")
        self.assertEqual((s.update_repo, s.update_branch, s.ui), ("Vital-Candy/Candy-userbot", "main", "menu"))
        self.assertEqual(len(s.warnings), 3)


# ---------------------------------------------------------------- обновление


class VersionCheckTests(unittest.TestCase):
    def info(self, local, remote):
        return UpdateInfo(local, remote, "a/b", "main")

    def test_states(self):
        self.assertEqual(self.info("1.0.0", "1.0.1").state, "newer")
        self.assertEqual(self.info("1.0.0", "1.0").state, "current")
        self.assertEqual(self.info("1.1.0", "1.0.9").state, "older")

    def test_numeric_not_textual_comparison(self):
        self.assertEqual(self.info("1.9.0", "1.10.0").state, "newer")
        self.assertEqual(self.info("1.10.0", "1.9.0").state, "older")

    def test_parse_remote_version(self):
        self.assertEqual(parse_remote_version('CORE_VERSION = "2.1.0"'), "2.1.0")
        with self.assertRaises(UpdateError):
            parse_remote_version("nothing")
        with self.assertRaises(UpdateError):
            parse_remote_version('CORE_VERSION = "abc"')

    def test_describe(self):
        self.assertIn("Доступна", describe(self.info("1.0.0", "1.1.0")))
        self.assertIn("последняя", describe(self.info("1.0.0", "1.0.0")))
        self.assertIn("Понижать", describe(self.info("1.1.0", "1.0.0")))


class FetchSafetyTests(unittest.TestCase):
    def test_rejects_plain_http_and_foreign_hosts_without_network(self):
        for url in (
            "http://raw.githubusercontent.com/a/b/main/core/version.py",
            "https://evil.example.com/a.zip",
            "file:///etc/passwd",
            "https://github.com.evil.com/x",
        ):
            with self.subTest(url=url), self.assertRaises(UpdateError):
                default_fetch(url, 1000)

    def test_rejects_bad_repo_and_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            for kwargs in ({"repo": "no-slash"}, {"repo": "a/b/c"}, {"branch": "a..b"}, {"branch": "x y"}):
                with self.subTest(**kwargs), self.assertRaises(UpdateError):
                    Updater(Path(tmp), Path(tmp), **kwargs)


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = make_root(self.tmp)

    def updater(self, fetch=None, **kw):
        return Updater(self.root, self.root / "data", local_version="1.0.0", fetch=fetch or FakeFetch(), **kw)

    def snapshot(self):
        return {
            str(p.relative_to(self.root)): p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(self.root.rglob("*"))
            if p.is_file() and "update" not in p.relative_to(self.root).parts
        }

    # ------------------------------------------------------------- проверка

    def test_check_reads_remote_version(self):
        fetch = FakeFetch("1.2.3")
        info = self.updater(fetch).check()
        self.assertEqual((info.local, info.remote, info.state), ("1.0.0", "1.2.3", "newer"))
        self.assertIn("raw.githubusercontent.com/Vital-Candy/Candy-userbot/main/core/version.py", fetch.urls[0])

    def test_check_with_old_repo_layout_fails_clearly(self):
        with self.assertRaises(UpdateError) as ctx:
            self.updater(FakeFetch(None)).check()
        self.assertIn("core/version.py", str(ctx.exception))

    def test_update_error_is_user_error(self):
        self.assertTrue(issubclass(UpdateError, UserError))

    # ------------------------------------------------------------ установка

    def test_apply_installs_and_protects_user_data(self):
        result = self.updater().apply()
        self.assertEqual((result.old, result.new), ("1.0.0", "1.1.0"))
        self.assertEqual((self.root / "main.py").read_text(), "print('new')\n")
        self.assertIn("1.1.0", (self.root / "core/version.py").read_text())
        self.assertTrue((self.root / "core/new.py").exists())
        # личное не тронуто
        self.assertEqual((self.root / "data/secret.txt").read_text(), "keep")
        self.assertFalse((self.root / "data/leak.txt").exists())
        self.assertEqual((self.root / "plugins/mine.py").read_text(), "mine")
        self.assertFalse((self.root / "plugins/example.py.disabled").exists())
        self.assertEqual((self.root / ".env").read_text(), "CANDY_LOG_LEVEL=DEBUG\n")
        self.assertEqual((self.root / "logs/candy.log").read_text(), "log")
        self.assertEqual((self.root / "logo.txt").read_text(), "MY LOGO\n")

    def test_apply_removes_stale_package_files_and_backs_them_up(self):
        result = self.updater().apply()
        self.assertFalse((self.root / "core/old_module.py").exists())
        self.assertEqual((result.backup / "core/old_module.py").read_text(), "OLD = 1\n")
        self.assertEqual((result.backup / "main.py").read_text(), "print('old')\n")
        self.assertEqual(result.removed, 1)

    def test_new_logo_is_installed_when_user_has_none(self):
        (self.root / "logo.txt").unlink()
        self.updater().apply()
        self.assertEqual((self.root / "logo.txt").read_text(), "REPO LOGO\n")

    def test_requirements_change_is_reported(self):
        result = self.updater().apply()
        self.assertTrue(any("requirements" in note for note in result.notes))

    def test_no_note_when_requirements_same(self):
        fetch = FakeFetch(archive=make_zip(good_tree(**{"requirements.txt": "telethon\n"})))
        self.assertEqual(self.updater(fetch).apply().notes, [])

    def test_stale_files_in_package_dirs_only(self):
        (self.root / "README.md").write_text("mine")  # верхний уровень не чистим
        self.updater().apply()
        self.assertEqual((self.root / "README.md").read_text(), "mine")

    def test_same_or_lower_version_refused_and_nothing_changes(self):
        before = self.snapshot()
        for remote in ("1.0.0", "0.9.0"):
            with self.subTest(remote=remote), self.assertRaises(UpdateError) as ctx:
                self.updater(FakeFetch(remote)).apply()
            self.assertIn("понижать", str(ctx.exception))
        self.assertEqual(self.snapshot(), before)

    def test_downgrade_refused_even_if_check_was_stale(self):
        """Версию из check() не берём на веру: в самом архиве она проверяется заново."""
        archive = make_zip(good_tree("0.5.0"))
        info = UpdateInfo("1.0.0", "1.1.0", "a/b", "main")
        before = self.snapshot()
        with self.assertRaises(UpdateError):
            self.updater(FakeFetch(archive=archive)).apply(info)
        self.assertEqual(self.snapshot(), before)

    def test_bad_archives_change_nothing(self):
        trees = {
            "traversal": make_zip(good_tree(), extra=[("../evil.py", "x")]),
            "absolute": make_zip(good_tree(), extra=[("/etc/evil.py", "x")]),
            "no main": make_zip({k: v for k, v in good_tree().items() if k != "main.py"}),
            "no version": make_zip({k: v for k, v in good_tree().items() if k != "core/version.py"}),
            "syntax": make_zip(good_tree(**{"core/new.py": "def (:\n"})),
            "two roots": make_zip(good_tree(), extra=[("other/x.py", "x")]),
            "no root": make_zip(good_tree(), root=None),
            "empty": make_zip({}),
            "not zip": b"hello",
        }
        link = zipfile.ZipInfo(f"{ROOT_NAME}/core/link.py")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        trees["symlink"] = make_zip(good_tree(), extra=[(link, "/etc/passwd")])
        before = self.snapshot()
        for name, archive in trees.items():
            with self.subTest(case=name), self.assertRaises(UpdateError):
                self.updater(FakeFetch(archive=archive)).apply()
            self.assertEqual(self.snapshot(), before, name)
        self.assertFalse((self.tmp / "evil.py").exists())

    def test_no_stage_directories_left_behind(self):
        self.updater().apply()
        left = [p.name for p in (self.root / "data/update").iterdir() if p.name.startswith("stage-")]
        self.assertEqual(left, [])

    def test_failure_midway_rolls_everything_back(self):
        before = self.snapshot()
        original = Updater._place
        calls = {"n": 0}

        def flaky(source, target):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("диск переполнен")
            original(source, target)

        with patch.object(Updater, "_place", staticmethod(flaky)), self.assertLogs("candy", level="ERROR"):
            with self.assertRaises(UpdateError) as ctx:
                self.updater().apply()
        self.assertIn("восстановлена", str(ctx.exception))
        self.assertEqual(self.snapshot(), before)

    def test_installed_files_are_replaced_not_edited_in_place(self):
        """run.sh может быть запущен: файл заменяется новым inode, а не переписывается."""
        script = self.root / "run.sh"
        script.write_text("old")
        inode = script.stat().st_ino
        fetch = FakeFetch(archive=make_zip(good_tree(**{"run.sh": "new"})))
        self.updater(fetch).apply()
        self.assertEqual(script.read_text(), "new")
        self.assertNotEqual(script.stat().st_ino, inode)

    def test_second_update_to_same_version_is_refused(self):
        self.updater().apply()
        up = Updater(self.root, self.root / "data", local_version="1.1.0", fetch=FakeFetch("1.1.0"))
        with self.assertRaises(UpdateError):
            up.apply()

    def test_archive_size_limit_is_passed_to_fetch(self):
        limits = []

        def fetch(url, limit):
            limits.append(limit)
            return FakeFetch()(url, limit)

        self.updater(fetch).apply()
        self.assertIn(MAX_ARCHIVE_BYTES, limits)


# -------------------------------------------------------------- команда .update


def make_event(text: str):
    edits: list[str] = []

    async def edit(value):
        edits.append(value)

    return SimpleNamespace(raw_text=text, out=True, edit=edit), edits


class UpdateCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = SimpleNamespace(
            dispatcher=Dispatcher(),
            access=None,
            check_update=AsyncMock(),
            install_update=AsyncMock(),
            request_restart=AsyncMock(),
        )
        self.module = UpdateModule(self.app)
        await self.module.start()

    async def run_command(self, text):
        event, edits = make_event(text)
        await self.app.dispatcher.dispatch_command(event)
        return edits

    async def test_is_optional_module(self):
        self.assertFalse(self.module.required)

    async def test_check_only(self):
        self.app.check_update.return_value = UpdateInfo("1.0.0", "1.1.0", "a/b", "main")
        edits = await self.run_command(".update check")
        self.assertIn("Доступна", edits[-1])
        self.app.install_update.assert_not_awaited()
        self.app.request_restart.assert_not_awaited()

    async def test_installs_and_restarts(self):
        info = UpdateInfo("1.0.0", "1.1.0", "a/b", "main")
        self.app.check_update.return_value = info
        self.app.install_update.return_value = SimpleNamespace(
            old="1.0.0", new="1.1.0", files=7, notes=["requirements.txt изменился"]
        )
        edits = await self.run_command(".update")
        self.app.install_update.assert_awaited_once_with(info)
        self.app.request_restart.assert_awaited_once()
        self.assertIn("1.0.0 → 1.1.0", edits[-1])
        self.assertIn("requirements", edits[-1])

    async def test_already_current_does_nothing(self):
        self.app.check_update.return_value = UpdateInfo("1.1.0", "1.1.0", "a/b", "main")
        edits = await self.run_command(".update")
        self.assertIn("последняя", edits[-1])
        self.app.install_update.assert_not_awaited()

    async def test_older_remote_is_not_installed(self):
        self.app.check_update.return_value = UpdateInfo("1.1.0", "1.0.0", "a/b", "main")
        edits = await self.run_command(".update")
        self.assertIn("Понижать", edits[-1])
        self.app.install_update.assert_not_awaited()

    async def test_user_error_is_shown_without_restart(self):
        self.app.check_update.side_effect = UpdateError("Нет связи с GitHub: timeout")
        edits = await self.run_command(".update")
        self.assertIn("Нет связи", edits[-1])
        self.app.request_restart.assert_not_awaited()

    async def test_unexpected_error_is_hidden_and_logged(self):
        self.app.check_update.side_effect = RuntimeError("секрет внутри")
        with self.assertLogs("candy", level="ERROR"):
            edits = await self.run_command(".update")
        self.assertNotIn("секрет", edits[-1])
        self.app.request_restart.assert_not_awaited()

    async def test_bad_arguments_show_usage(self):
        edits = await self.run_command(".update now please")
        self.assertIn("Использование", edits[-1])
        self.app.check_update.assert_not_awaited()

    async def test_foreign_user_is_refused(self):
        event, edits = make_event(".update")
        event.out = False
        event.sender_id = 999
        await self.app.dispatcher.dispatch_command(event, incoming=True)
        self.app.check_update.assert_not_awaited()


# ---------------------------------------------------------------------- меню


class Script:
    """Подменяет ввод: отдаёт заготовленные ответы, затем EOFError."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def __call__(self, prompt, **kw):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        value = self.answers.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def make_menu_app(**over):
    accounts = [
        SimpleNamespace(account_id=1, display_name="@alice"),
        SimpleNamespace(account_id=2, display_name="@bob"),
    ]
    control = SimpleNamespace(
        describe=lambda: "● @alice · 1 (активный)\n○ @bob · 2",
        accounts=SimpleNamespace(list=lambda: accounts, get_active=lambda: accounts[0]),
        backup_create=lambda: Path("backup-alice.zip"),
    )
    app = SimpleNamespace(
        account=accounts[0],
        health=lambda: SimpleNamespace(status="ok", uptime=3725.0),
        account_control=control,
        stop=AsyncMock(),
        is_stopping=False,
        switch_account=AsyncMock(),
        onboard_account=AsyncMock(return_value=SimpleNamespace(display_name="@carol")),
        request_restart=AsyncMock(),
        check_update=AsyncMock(return_value=UpdateInfo("1.0.0", "1.1.0", "a/b", "main")),
        install_update=AsyncMock(
            return_value=SimpleNamespace(old="1.0.0", new="1.1.0", files=5, notes=[])
        ),
    )
    for key, value in over.items():
        setattr(app, key, value)
    return app


def make_menu(app, *answers, **kw):
    script = Script(*answers)
    out: list[str] = []
    kw.setdefault("clear", False)
    kw.setdefault("color", False)
    kw.setdefault("width", 80)
    menu = MenuUI(app, ask=script, write=out.append, **kw)
    return menu, script, out


class LogoTests(unittest.TestCase):
    def test_default_logo_is_plain_ascii_and_fits_80_columns(self):
        lines = load_logo(None, 80)
        self.assertEqual("\n".join(lines), DEFAULT_LOGO)
        self.assertTrue(all(len(line) <= 80 for line in lines))
        DEFAULT_LOGO.encode("ascii")

    def test_custom_logo_file_is_used_when_it_fits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logo.txt"
            path.write_text("\n\nAB\nCD\n\n\n", encoding="utf-8")
            self.assertEqual(load_logo(path, 40), ["AB", "CD"])

    def test_too_wide_custom_logo_falls_back_instead_of_being_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logo.txt"
            path.write_text("A" * 200 + "\nB\n", encoding="utf-8")
            self.assertEqual("\n".join(load_logo(path, 80)), DEFAULT_LOGO)

    def test_tiny_screen_gets_plain_title(self):
        self.assertEqual(load_logo(None, 20), [menu_mod.FALLBACK_TITLE])

    def test_empty_or_missing_logo_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logo.txt"
            path.write_text("  \n\n", encoding="utf-8")
            self.assertEqual("\n".join(load_logo(path, 80)), DEFAULT_LOGO)
            self.assertEqual("\n".join(load_logo(Path(tmp) / "none.txt", 80)), DEFAULT_LOGO)

    def test_too_tall_logo_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logo.txt"
            path.write_text("\n".join(f"line{i}" for i in range(100)), encoding="utf-8")
            self.assertEqual("\n".join(load_logo(path, 80)), DEFAULT_LOGO)

    def test_shipped_logo_file_fits_a_phone_or_falls_back_cleanly(self):
        shipped = Path(__file__).resolve().parent.parent / "logo.txt"
        self.assertTrue(shipped.is_file())
        wide = load_logo(shipped, 80)
        self.assertGreater(len(wide), 1)
        narrow = load_logo(shipped, 36)  # узкий экран телефона: запасной логотип целиком
        self.assertEqual("\n".join(narrow), DEFAULT_LOGO)


class MenuScreenTests(unittest.TestCase):
    def test_main_screen_shows_account_state_version_and_items(self):
        menu, _, _ = make_menu(make_menu_app())
        text = menu.screen()
        for expected in ("@alice", "работает", "1 ч 2 мин", "1  Аккаунты", "2  Логи", "3  Обновление", "4  Остановить"):
            self.assertIn(expected, text)
        self.assertIn(menu_mod.CORE_VERSION, text)

    def test_degraded_and_broken_health(self):
        menu, _, _ = make_menu(make_menu_app(health=lambda: SimpleNamespace(status="degraded", uptime=None)))
        self.assertIn("с проблемами", menu.screen())

        def boom():
            raise RuntimeError("x")

        menu, _, _ = make_menu(make_menu_app(health=boom))
        self.assertIn("неизвестно", menu.screen())

    def test_no_ansi_without_color(self):
        menu, _, _ = make_menu(make_menu_app())
        self.assertNotIn("\033", menu.screen())

    def test_ansi_with_color(self):
        menu, _, _ = make_menu(make_menu_app(), color=True)
        self.assertIn("\033[", menu.screen())

    def test_clear_sequence_written_when_enabled(self):
        menu, _, out = make_menu(make_menu_app(), clear=True)
        asyncio.run(menu.run())
        self.assertEqual(out[0], menu_mod.CLEAR)

    def test_custom_logo_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logo.txt"
            path.write_text("МОЙ-ЛОГО\n", encoding="utf-8")
            menu, _, _ = make_menu(make_menu_app(), logo_path=path)
            self.assertIn("МОЙ-ЛОГО", menu.screen())


class MenuFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_eof_ends_menu(self):
        menu, _, _ = make_menu(make_menu_app())
        with self.assertLogs("candy", level="INFO"):
            await asyncio.wait_for(menu.run(), 2)

    async def test_enter_redraws_and_unknown_choice_is_reported(self):
        menu, script, out = make_menu(make_menu_app(), "", "9", "")
        with self.assertLogs("candy", level="INFO"):
            await menu.run()
        self.assertEqual(sum("Аккаунт" in chunk and "Версия" in chunk for chunk in out), 3)
        self.assertTrue(any("Нет пункта «9»" in chunk for chunk in out))

    async def test_stop_needs_confirmation(self):
        app = make_menu_app()
        menu, _, _ = make_menu(app, "4", "n")
        with self.assertLogs("candy", level="INFO"):
            await menu.run()
        app.stop.assert_not_awaited()

        menu, _, _ = make_menu(app, "4", "y")
        await asyncio.wait_for(menu.run(), 2)
        app.stop.assert_awaited_once()

    async def test_accounts_list_and_backup(self):
        menu, _, out = make_menu(make_menu_app(), "1", "3", "", "0", "")
        with self.assertLogs("candy", level="INFO"):
            await menu.run()
        text = "\n".join(out)
        self.assertIn("@bob · 2", text)
        self.assertIn("backup-alice.zip", text)

    async def test_switch_account_by_number(self):
        app = make_menu_app()
        menu, _, _ = make_menu(app, "1", "1", "2", "y")
        app.switch_account.side_effect = lambda ref: setattr(app, "is_stopping", True)
        await asyncio.wait_for(menu.run(), 2) if False else None
        with self.assertLogs("candy", level="INFO"):
            await menu.run()
        app.switch_account.assert_awaited_once_with("2")

    async def test_switch_rejects_bad_number_and_single_account(self):
        app = make_menu_app()
        menu, _, out = make_menu(app, "1", "1", "7", "", "0", "")
        with self.assertLogs("candy", level="INFO"):
            await menu.run()
        app.switch_account.assert_not_awaited()
        self.assertTrue(any("Нет такого номера" in chunk for chunk in out))

        app = make_menu_app()
        app.account_control.accounts.list = lambda: [SimpleNamespace(account_id=1, display_name="@a")]
        menu, _, out = make_menu(app, "1", "1", "", "0", "")
        with self.assertLogs("candy", level="INFO"):
            await menu.run()
        self.assertTrue(any("один аккаунт" in chunk for chunk in out))

    async def test_add_account_offers_restart(self):
        app = make_menu_app()
        menu, _, out = make_menu(app, "1", "2", "y", "0", "")
        with self.assertLogs("candy", level="INFO"):
            await menu.run()
        app.onboard_account.assert_awaited_once_with(confirm_replace=True)
        app.request_restart.assert_awaited_once()
        self.assertTrue(any("@carol" in chunk for chunk in out))

    async def test_update_installs_after_confirmation_and_restarts(self):
        app = make_menu_app()
        menu, _, out = make_menu(app, "3", "y")
        with self.assertLogs("candy", level="INFO"):
            await menu.run()
        app.install_update.assert_awaited_once()
        app.request_restart.assert_awaited_once()
        self.assertTrue(any("1.0.0 → 1.1.0" in chunk for chunk in out))

    async def test_update_declined_or_not_available(self):
        app = make_menu_app()
        menu, _, _ = make_menu(app, "3", "n")
        with self.assertLogs("candy", level="INFO"):
            await menu.run()
        app.install_update.assert_not_awaited()

        app = make_menu_app(check_update=AsyncMock(return_value=UpdateInfo("1.1.0", "1.0.0", "a/b", "main")))
        menu, script, out = make_menu(app, "3", "")
        with self.assertLogs("candy", level="INFO"):
            await menu.run()
        app.install_update.assert_not_awaited()
        self.assertTrue(any("Понижать" in chunk for chunk in out))

    async def test_update_error_is_shown_and_menu_continues(self):
        app = make_menu_app(check_update=AsyncMock(side_effect=UpdateError("Нет связи с GitHub: x")))
        menu, _, out = make_menu(app, "3", "")
        with self.assertLogs("candy", level="INFO"):
            await menu.run()
        self.assertTrue(any("Нет связи с GitHub" in chunk for chunk in out))

    async def test_unexpected_error_does_not_kill_menu(self):
        app = make_menu_app(check_update=AsyncMock(side_effect=RuntimeError("секрет")))
        menu, _, out = make_menu(app, "3", "", "")
        with self.assertLogs("candy", level="ERROR"):
            await menu.run()
        self.assertFalse(any("секрет" in chunk for chunk in out))
        self.assertTrue(any("Ошибка" in chunk for chunk in out))

    async def test_live_logs_show_backlog_new_lines_and_enter_returns(self):
        release = asyncio.Event()
        calls = []

        async def ask(prompt, **kw):
            calls.append(prompt)
            if prompt == "":  # ожидание Enter на экране логов
                await release.wait()
                return ""
            if len(calls) == 1:
                return "2"
            raise EOFError

        out: list[str] = []
        menu = MenuUI(make_menu_app(), ask=ask, write=out.append, clear=False, color=False, width=80)
        log_utils.mute_console()
        log_utils.logger.info("строка-из-прошлого-31")
        log_utils.unmute_console()
        # без assertLogs: он подменяет обработчики логгера, и буфер ничего бы не получил
        log_utils.mute_console()
        try:
            task = asyncio.create_task(menu.run())
            await asyncio.sleep(0.05)
            log_utils.logger.info("живая-строка-31")
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.wait_for(task, 2)
        finally:
            log_utils.unmute_console()
        text = "\n".join(out)
        self.assertIn("строка-из-прошлого-31", text)
        self.assertIn("живая-строка-31", text)
        # после выхода из логов подписка снята
        self.assertEqual(log_utils.ring._listeners, [])

    async def test_cancel_during_logs_cleans_up(self):
        async def ask(prompt, **kw):
            if prompt == "":
                await asyncio.Event().wait()
            return "2"

        menu = MenuUI(make_menu_app(), ask=ask, write=lambda t: None, clear=False, color=False, width=80)
        task = asyncio.create_task(menu.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(log_utils.ring._listeners, [])


class ConsoleMenuIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_mode_mutes_console_and_stop_restores(self):
        from core.console import ConsoleManager

        started = asyncio.Event()

        class FakeMenu:
            def __init__(self, *a, **k):
                pass

            async def run(self):
                started.set()
                await asyncio.Event().wait()

        before = list(log_utils.logger.handlers)
        console = ConsoleManager(SimpleNamespace(), ui="menu")
        with patch.object(ConsoleManager, "_has_tty", staticmethod(lambda: True)), patch(
            "core.console.MenuUI", FakeMenu
        ):
            await console.start()
            await asyncio.wait_for(started.wait(), 1)
            import logging

            self.assertFalse(any(type(h) is logging.StreamHandler for h in log_utils.logger.handlers))
            await console.stop()
        self.assertEqual(sorted(map(id, log_utils.logger.handlers)), sorted(map(id, before)))

    async def test_no_menu_without_terminal(self):
        from core.console import ConsoleManager

        console = ConsoleManager(SimpleNamespace(), ui="menu")
        with patch.object(ConsoleManager, "_has_tty", staticmethod(lambda: False)), self.assertLogs("candy", level="INFO"):
            await console.start()
        self.assertIsNone(console._task)

    async def test_console_logs_come_back_when_menu_ends_by_itself(self):
        from core.console import ConsoleManager
        import logging

        class QuickMenu:
            def __init__(self, *a, **k):
                pass

            async def run(self):
                return

        console = ConsoleManager(SimpleNamespace(), ui="menu")
        with patch.object(ConsoleManager, "_has_tty", staticmethod(lambda: True)), patch(
            "core.console.MenuUI", QuickMenu
        ):
            await console.start()
            await asyncio.wait_for(console._task, 1)
        self.assertTrue(any(type(h) is logging.StreamHandler for h in log_utils.logger.handlers))
        await console.stop()


if __name__ == "__main__":
    unittest.main()
