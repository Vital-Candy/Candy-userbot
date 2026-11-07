"""Менеджер плагинов: поиск файлов в plugins/, загрузка, выгрузка и горячая перезагрузка.

Базовый класс: core/plugin.py, API для плагина: core/plugin_api.py.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from core.plugin import Plugin
from core.plugin_api import PluginAPI
from core.version import CORE_VERSION, at_least
from utils.logger import logger

ERROR_TEXT_LIMIT = 200


class PluginError(RuntimeError):
    """Плагин нельзя загрузить по понятной причине (версия, зависимость). Без traceback в логе."""


@dataclass(slots=True)
class PluginState:
    """Состояние одного плагина: файл, загруженный объект, статус и причина сбоя."""

    name: str
    path: Path
    module: ModuleType | None = None
    plugin: Plugin | None = None
    api: PluginAPI | None = None
    loaded: bool = False
    failed: bool = False
    error: str | None = None
    version: str = ""
    description: str = ""
    requires: tuple[str, ...] = ()


def _describe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if isinstance(exc, PluginError):
        text = str(exc)
    return text[:ERROR_TEXT_LIMIT]


class PluginManager:
    """Загружает плагины из каталога ``root`` с учётом ``requires`` и откатом при сбое."""

    def __init__(self, app: Any, root: Path) -> None:
        self.app, self.root = app, root
        self._plugins: dict[str, PluginState] = {}
        self._order: list[str] = []  # в порядке загрузки: выгружаем в обратном
        self._loading: set[str] = set()

    def discover(self) -> list[PluginState]:
        """Сканирует ``root`` и возвращает состояния плагинов (файлы ``*.py`` без ``_``-префикса).

        Новые файлы добавляются в реестр, у известных обновляется путь. Исчезнувшие не удаляются.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        found = []
        for path in sorted(self.root.glob("*.py")):
            if path.name.startswith("_"):
                continue
            state = self._plugins.get(path.stem)
            if state is None:
                state = PluginState(path.stem, path)
                self._plugins[path.stem] = state
            else:
                state.path = path
            found.append(state)
        return found

    async def load_all(self) -> None:
        """Находит и загружает все плагины. Сбой одного не прерывает остальные."""
        for state in self.discover():
            await self.load(state.name)

    async def load(self, name: str) -> bool:
        """Загружает плагин вместе с зависимостями. False при сбое (причина в ``state.error``).

        Raises:
            KeyError: плагин не найден ни в реестре, ни на диске.
        """
        state = self._plugins.get(name)
        if state is None:
            # Файл мог появиться после старта — пробуем найти его на диске.
            self.discover()
            state = self._plugins.get(name)
        if state is None:
            raise KeyError(name)
        if state.loaded:
            return True
        # Имя уже в процессе загрузки: значит, зависимости замкнулись в круг.
        if name in self._loading:
            state.failed = True
            state.error = "циклическая зависимость плагинов"
            logger.error("Плагин '%s': %s.", name, state.error)
            return False

        self._loading.add(name)
        module: ModuleType | None = None
        plugin: Plugin | None = None
        api: PluginAPI | None = None
        state.error = None
        try:
            module = self._import(state)
            factory = getattr(module, "create_plugin", None)
            if factory is None:
                raise ValueError("plugin должен экспортировать create_plugin(app)")
            plugin = factory(self.app)
            if not isinstance(plugin, Plugin):
                raise TypeError("create_plugin() должен вернуть Plugin")
            api = PluginAPI(self.app, name)
            plugin.api = api
            self._check_core(plugin)
            await self._load_dependencies(name, plugin)
            await self._call(plugin.setup)
        except asyncio.CancelledError:
            # При отмене убираем за собой, иначе остались бы «полузагруженные» регистрации.
            await self._discard(state, module, plugin, api)
            raise
        except Exception as exc:
            state.failed = True
            state.error = _describe(exc)
            if isinstance(exc, PluginError):
                logger.error("Не удалось загрузить плагин '%s': %s", name, exc)
            else:
                logger.exception("Не удалось загрузить плагин '%s'.", name)
            await self._discard(state, module, plugin, api)
            return False
        finally:
            # Снимаем метку при любом исходе, иначе повторная загрузка сочтётся циклом.
            self._loading.discard(name)

        state.module, state.plugin, state.api = module, plugin, api
        state.version = str(getattr(plugin, "version", ""))
        state.description = str(getattr(plugin, "description", ""))
        state.requires = tuple(plugin.requires)
        state.loaded, state.failed = True, False
        self._order.append(name)
        logger.info("Плагин '%s' загружен.", name)
        return True

    @staticmethod
    def _check_core(plugin: Plugin) -> None:
        need = str(plugin.min_core or "").strip()
        if not need:
            return
        try:
            enough = at_least(CORE_VERSION, need)
        except ValueError as exc:
            raise PluginError(f"неверный min_core: {exc}") from None
        if not enough:
            raise PluginError(f"нужно ядро не ниже {need}, сейчас {CORE_VERSION}")

    async def _load_dependencies(self, name: str, plugin: Plugin) -> None:
        requires = plugin.requires
        if isinstance(requires, str):
            raise PluginError("requires должен быть кортежем имён плагинов, а не строкой")
        for dep in requires:
            if dep == name:
                raise PluginError("плагин не может зависеть от самого себя")
            if dep not in self._plugins:
                self.discover()
            dep_state = self._plugins.get(dep)
            if dep_state is None:
                raise PluginError(f"зависимость '{dep}' не найдена в plugins/")
            if not dep_state.loaded and not await self.load(dep):
                raise PluginError(f"зависимость '{dep}' не загрузилась: {dep_state.error or 'см. лог'}")

    async def unload(self, name: str) -> bool:
        """Выгружает плагин (сначала зависящие от него). False, если его ``teardown`` упал.

        Незагруженный или неизвестный плагин считается выгруженным (True).
        """
        state = self._plugins.get(name)
        if state is None or not state.loaded:
            return True
        # Сначала те, кто от него зависит.
        for other in reversed(tuple(self._order)):
            other_state = self._plugins[other]
            if other != name and other_state.loaded and name in other_state.requires:
                await self.unload(other)
        plugin, module = state.plugin, state.module
        ok = True
        try:
            if plugin is not None:
                await self._call(plugin.teardown)
        except Exception as exc:
            ok = False
            state.error = f"teardown: {_describe(exc)}"[:ERROR_TEXT_LIMIT]
            logger.exception("Ошибка выгрузки плагина '%s'.", name)
        finally:
            # Состояние сбрасываем всегда: иначе плагин с падающим teardown
            # навсегда остаётся «loaded» и его нельзя перезагрузить.
            # API снимает регистрации плагина сам, даже если teardown упал.
            await self._discard(state, module, None, state.api)
        if ok:
            logger.info("Плагин '%s' выгружен.", name)
        return ok

    async def reload(self, name: str) -> bool:
        """Выгружает и загружает плагин заново, перечитав файл с диска.

        Raises:
            KeyError: плагин не найден.
        """
        if name not in self._plugins:
            self.discover()
        if name not in self._plugins:
            raise KeyError(name)
        if self._plugins[name].loaded and not await self.unload(name):
            # teardown упал, но состояние уже сброшено — загрузка с нуля возможна.
            logger.warning("Плагин '%s' выгружен с ошибкой, загружаем заново.", name)
        self.discover()
        return await self.load(name)

    async def unload_all(self) -> None:
        """Выгружает все плагины в порядке, обратном загрузке."""
        for name in reversed(tuple(self._order)):
            await self.unload(name)

    def forget(self, name: str) -> bool:
        """Убирает из реестра выгруженный плагин, чей файл удалён. False, если плагин ещё загружен или файл на месте."""
        state = self._plugins.get(name)
        if state is None or state.loaded or state.path.exists():
            return False
        del self._plugins[name]
        return True

    def states(self) -> list[PluginState]:
        """Состояния всех известных плагинов (копия списка)."""
        return list(self._plugins.values())

    @staticmethod
    async def _call(func: Any) -> None:
        result = func()
        if inspect.isawaitable(result):
            await result

    async def _discard(
        self,
        state: PluginState,
        module: ModuleType | None,
        plugin: Plugin | None,
        api: PluginAPI | None = None,
    ) -> None:
        """Откатывает частично загруженный плагин и чистит состояние."""
        if plugin is not None:
            try:
                await self._call(plugin.teardown)
            except Exception:
                logger.exception("Ошибка teardown после неудачной загрузки '%s'.", state.name)
        api = api or state.api
        if api is not None:
            await api.cleanup()  # ошибки шагов логируются внутри, наружу не выходят
        module = module or state.module
        if module is not None:
            sys.modules.pop(module.__name__, None)
        state.module = state.plugin = state.api = None
        state.loaded = False
        if state.name in self._order:
            self._order.remove(state.name)

    def info(self, name: str) -> dict[str, Any]:
        """Сведения о плагине для .plugin info и консоли. KeyError, если такого нет."""
        state = self._plugins.get(name)
        if state is None:
            self.discover()
            state = self._plugins.get(name)
        if state is None:
            raise KeyError(name)
        registrations = state.api.registrations if state.api else {"commands": 0, "handlers": 0, "tasks": 0}
        return {
            "name": state.name,
            "loaded": state.loaded,
            "failed": state.failed,
            "error": state.error,
            "version": state.version,
            "description": state.description,
            "requires": state.requires,
            "path": str(state.path),
            **registrations,
        }

    @staticmethod
    def _import(state: PluginState) -> ModuleType:
        module_name = f"candy_plugin_{state.name}"
        spec = importlib.util.spec_from_file_location(module_name, state.path)
        if spec is None:
            raise ImportError(f"не удалось импортировать {state.path}")
        module = importlib.util.module_from_spec(spec)
        # Модуль должен быть в sys.modules до выполнения: @dataclass вместе с
        # `from __future__ import annotations` ищет там модуль класса.
        # Компилируем исходник напрямую, чтобы reload не брал устаревший .pyc.
        code = compile(state.path.read_text(encoding="utf-8"), str(state.path), "exec")
        sys.modules[module_name] = module
        try:
            exec(code, module.__dict__)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        return module
