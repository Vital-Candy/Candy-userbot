"""Команда .store: магазин плагинов (список, сведения, установка, обновление, удаление).

Синтаксис: ``.store [list]``, ``.store info <имя>``, ``.store install <имя> [--yes] [--force]``,
``.store update [<имя>] [--yes] [--force]``, ``.store remove <имя>``. Только владелец аккаунта.
Логика, проверки и защита: core/store.py. Плагины скачиваются отдельно от userbot.
"""
from __future__ import annotations

import asyncio

from core.access import require
from core.errors import UserError
from core.module import Module
from core.reply import answer
from core.store import NAME_PATTERN, PluginStore, StoreEntry, StoreError
from storage.permissions import PermissionLevel
from utils.logger import logger
from utils.text import escape_markdown

USAGE = (
    "Использование:\n"
    ".store [list]\n"
    ".store info <имя>\n"
    ".store install <имя> [--yes] [--force]\n"
    ".store update [<имя>] [--yes] [--force]\n"
    ".store remove <имя>"
)
DETAILS = """\
Магазин плагинов. Плагины лежат в репозитории отдельно от userbot и скачиваются только по вашей команде; в сам userbot и в .update они не входят.

.store — список плагинов магазина и их состояние
.store info <имя> — версия, описание, зависимости, размер и контрольная сумма
.store install <имя> — показать, что будет установлено; чтобы установить, повторите с --yes
.store install <имя> --yes — скачать, проверить и загрузить плагин
.store update — какие поставленные плагины можно обновить
.store update <имя> --yes — обновить плагин
.store remove <имя> — выгрузить и удалить плагин, поставленный из магазина
--force — заменить файл, который вы изменили сами

Установка идёт только из репозитория обновлений: файл ставится, если его SHA-256 и размер совпали с индексом, а код компилируется. Файл с таким же именем, положенный не из магазина, не затирается. Ничего не обновляется само.
Плагины выполняют код с теми же правами, что и ваш аккаунт: ставьте только те, чьему автору доверяете.

Права: только владелец аккаунта."""

STATUS_TEXT = {
    "new": "не установлен",
    "current": "установлен",
    "update": "есть обновление",
    "modified": "изменён вами",
    "foreign": "файл есть, но не из магазина",
}


def entry_line(entry: StoreEntry, status: str, installed_version: str | None) -> str:
    """Строка списка: имя, версия, описание и состояние."""
    state = STATUS_TEXT.get(status, status)
    if status == "update" and installed_version:
        state += f" {installed_version} → {entry.version}"
    return f"• {entry.name} v{entry.version} — {escape_markdown(entry.description)} [{state}]"


class StoreModule(Module):
    """Команда ``.store``: магазин плагинов."""

    name = "system.store"
    description = "Команда .store: магазин плагинов"
    # Необязательный: без него userbot работает, просто нет установки плагинов из Telegram.

    def __init__(self, app) -> None:
        super().__init__(app)
        self._lock = asyncio.Lock()  # одна операция установки за раз

    async def start(self) -> None:
        """Регистрирует команду ``.store`` (только владелец)."""
        self.app.dispatcher.register_command(
            "store",
            self.store_command,
            description="магазин плагинов",
            usage=".store [list | info | install | update | remove <имя>]",
            priority=95,
            details=DETAILS,
            filters=(require(self.app, PermissionLevel.ADMIN),),
        )

    async def stop(self) -> None:
        """Снимает регистрацию команды ``.store``."""
        self.app.dispatcher.unregister_command("store")

    def _store(self) -> PluginStore:
        """Клиент магазина с текущими настройками репозитория и папками."""
        from config.settings import BASE_DIR, DATA_DIR, UPDATE_BRANCH, UPDATE_REPO

        return PluginStore(
            BASE_DIR / "plugins", DATA_DIR / "store.json", repo=UPDATE_REPO, branch=UPDATE_BRANCH
        )

    async def store_command(self, event) -> None:
        """Разбирает ``.store`` и выполняет действие; ошибки показывает текстом."""
        command = self.app.dispatcher.get_command(event)
        args = list(command.args) if command else []
        flags = {item.lower() for item in args if item.startswith("--")}
        words = [item for item in args if not item.startswith("--")]
        if flags - {"--yes", "--force"} or any(item.startswith("-") for item in words):
            await answer(event, USAGE)
            return
        action = words[0].lower() if words else "list"
        rest = words[1:]
        try:
            if action == "list" and not rest and not flags:
                text = await self._list()
            elif action == "info" and len(rest) == 1 and not flags:
                text = await self._info(rest[0].lower())
            elif action in ("install", "update") and len(rest) <= 1 and (rest or action == "update"):
                text = await self._install(action, rest[0].lower() if rest else None, "--yes" in flags, "--force" in flags)
            elif action == "remove" and len(rest) == 1 and not flags:
                text = await self._remove(rest[0].lower())
            else:
                text = USAGE
        except UserError as exc:
            text = f"❌ {exc}"
        except Exception:
            logger.exception("Ошибка .store.")
            text = "❌ Не удалось выполнить команду. Подробности в логе."
        await answer(event, text)

    # ------------------------------------------------------------------ действия

    async def _list(self) -> str:
        """Список записей магазина с состоянием каждой."""
        store = self._store()
        entries = await asyncio.to_thread(store.index)
        if not entries:
            return "🛒 Магазин пуст."
        state = store.installed()
        lines = [
            entry_line(entry, store.status(entry, state), state.get(entry.name, {}).get("version"))
            for entry in sorted(entries.values(), key=lambda item: item.name)
        ]
        return "🛒 Плагины магазина:\n" + "\n".join(lines) + "\n\nПодробнее: .store info <имя>"

    async def _info(self, name: str) -> str:
        """Подробности о плагине: версия, зависимости, размер и сумма."""
        store = self._store()
        entry = await asyncio.to_thread(store.entry, name)
        status = store.status(entry)
        lines = [
            f"🔌 {entry.name} v{entry.version}",
            escape_markdown(entry.description),
            f"Состояние: {STATUS_TEXT[status]}",
            f"Нужна версия ядра: {entry.min_core or 'любая'}",
            "Зависит от: " + (", ".join(entry.requires) or "ничего"),
            f"Размер: {entry.size} байт",
            f"SHA-256: {entry.sha256[:16]}…",
        ]
        if status in ("new", "update"):
            lines.append(f"Установить: .store install {entry.name}")
        return "\n".join(lines)

    async def _install(self, action: str, name: str | None, confirmed: bool, force: bool) -> str:
        """Установка или обновление: без ``--yes`` только описывает, что будет сделано."""
        store = self._store()
        if name is None:  # «.store update» без имени: что можно обновить
            return await self._updates(store)
        entry = await asyncio.to_thread(store.entry, name)
        status = store.status(entry)
        if action == "update" and status == "new":
            raise StoreError(f"Плагин {name} не установлен. Поставить: .store install {name}")
        if action == "install" and status == "current":
            return f"ℹ️ Плагин {name} v{entry.version} уже установлен."
        if action == "update" and status == "current":
            return f"✅ У плагина {name} последняя версия (v{entry.version})."
        if not confirmed:
            return (
                f"⚠️ Будет {'обновлён' if status == 'update' else 'установлен'} плагин {entry.name} v{entry.version} "
                f"({entry.size} байт, SHA-256 {entry.sha256[:12]}…).\n"
                "Он выполняется с правами вашего аккаунта. Если доверяете, повторите команду с --yes:\n"
                f".store {action} {entry.name} --yes"
            )
        async with self._lock:
            return await self._do_install(store, name, force)

    async def _updates(self, store: PluginStore) -> str:
        """Список установленных плагинов, у которых в магазине версия новее."""
        entries = await asyncio.to_thread(store.index)
        state = store.installed()
        lines = [
            entry_line(entry, "update", state[entry.name]["version"])
            for entry in sorted(entries.values(), key=lambda item: item.name)
            if entry.name in state and store.status(entry, state) == "update"
        ]
        if not lines:
            return "✅ Обновлений плагинов нет."
        return "🆕 Можно обновить:\n" + "\n".join(lines) + "\n\nОбновить: .store update <имя> --yes"

    async def _do_install(self, store: PluginStore, name: str, force: bool) -> str:
        """Скачивает и записывает файл, затем загружает плагин; при сбое возвращает прежнее состояние."""
        manager = self.app.plugins
        was_loaded = any(item.name == name and item.loaded for item in manager.states())
        if was_loaded:
            await manager.unload(name)
        result = await asyncio.to_thread(store.install, name, force=force)
        manager.discover()
        try:
            loaded = await manager.load(name)
        except KeyError:
            loaded = False
        if loaded:
            old = f"{result.previous_version} → " if result.previous_version else ""
            return f"✅ Плагин {name} {old}v{result.entry.version} установлен и загружен."
        error = manager.info(name)["error"] if any(item.name == name for item in manager.states()) else None
        await asyncio.to_thread(store.rollback, result)
        manager.forget(name)
        manager.discover()
        if result.previous_data is not None:
            try:
                await manager.load(name)
            except KeyError:
                pass
        return f"❌ Плагин {name} не загрузился ({escape_markdown(str(error or 'см. лог'))}). Прежнее состояние возвращено."

    async def _remove(self, name: str) -> str:
        """Выгружает и удаляет плагин, поставленный из магазина."""
        store = self._store()
        if NAME_PATTERN.match(name) is None:
            raise StoreError("Имя плагина: латинские буквы a-z, цифры и _.")
        if name not in store.installed():
            existing = store.target(name).exists()
            raise StoreError(
                f"Файл plugins/{name}.py поставлен не из магазина: удалите его сами." if existing
                else f"Плагин {name} не установлен."
            )
        manager = self.app.plugins
        await manager.unload(name)
        await asyncio.to_thread(store.remove, name)
        manager.forget(name)
        return f"✅ Плагин {name} удалён."
