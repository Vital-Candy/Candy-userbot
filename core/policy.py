"""Политика доступа к командам: кто из других людей и какие команды может вызывать.

Схема простая и строгая:

* владелец (сам аккаунт) может всё и ни от какой настройки не зависит;
* остальные люди делятся на user < trusted < admin, но только если им уровень выдан
  (``.perm grant``); без выданного уровня их сообщения игнорируются молча;
* команда открыта для других людей, только если при регистрации у неё указан ``level``
  (команда написана с расчётом на чужие сообщения). Остальные команды (``.update``, ``.restart``,
  ``.account``, ``.backup``, ``.plugin`` и так далее) недоступны никому, кроме владельца, и
  никакая настройка этого не меняет;
* владелец может вручную поменять уровень открытой команды (``.perm cmd``). Настройки лежат в
  базе (каталог data), поэтому ``.update`` их не сбрасывает.

Для защиты от флуда один человек может вызывать команды не чаще раза в ``COOLDOWN`` секунд.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from core.access import AccessService
from core.dispatcher import Command, Dispatcher
from storage.permissions import GRANTABLE, PermissionLevel
from utils.logger import logger

PLUGIN = "core.access"
KEY = "command_levels"
COOLDOWN = 2.0  # секунд между командами одного постороннего человека
MAX_TRACKED = 1000  # сколько людей помнить для паузы


class AccessPolicy:
    """Решает, пустить ли постороннего человека к команде, и хранит ручные настройки уровней."""

    def __init__(
        self,
        access: AccessService,
        plugin_data: Any,
        dispatcher: Dispatcher,
        *,
        clock: Callable[[], float] = time.monotonic,
        cooldown: float = COOLDOWN,
    ) -> None:
        self.access = access
        self.plugin_data = plugin_data
        self.dispatcher = dispatcher
        self._clock = clock
        self._cooldown = cooldown
        self._last: dict[int, float] = {}

    # ---------------------------------------------------------------- настройки

    async def load(self) -> None:
        """Читает ручные настройки уровней из базы и передаёт их диспетчеру."""
        account_id = await self.access.account_id()
        saved = self.plugin_data.get(account_id=account_id, plugin=PLUGIN, key=KEY, default=None)
        overrides: dict[str, str] = {}
        if isinstance(saved, dict):
            allowed = {level.value for level in GRANTABLE} | {PermissionLevel.OWNER.value}
            for name, value in saved.items():
                if isinstance(name, str) and value in allowed:
                    overrides[name] = value
        self.dispatcher.level_overrides = overrides

    async def set_level(self, name: str, level: PermissionLevel | None) -> None:
        """Задаёт уровень команды вручную; ``None`` возвращает уровень по умолчанию.

        ``owner`` закрывает команду для всех, кроме владельца. Открыть можно только команду,
        у которой при регистрации указан уровень; иначе ``ValueError``.
        """
        command = self.dispatcher.commands.get(name.strip().lower().lstrip("."))
        if command is None:
            raise ValueError(f"команды .{name} нет")
        if command.level is None:
            raise ValueError(
                f"команда .{command.name} только для владельца: она не рассчитана на чужие сообщения, открыть её нельзя"
            )
        overrides = dict(self.dispatcher.level_overrides)
        if level is None:
            overrides.pop(command.name, None)
        else:
            overrides[command.name] = level.value
        await self._save(overrides)

    async def reset(self) -> int:
        """Сбрасывает все ручные настройки; возвращает, сколько их было."""
        count = len(self.dispatcher.level_overrides)
        await self._save({})
        return count

    async def _save(self, overrides: dict[str, str]) -> None:
        """Сохраняет настройки в базе и применяет их."""
        account_id = await self.access.account_id()
        if overrides:
            self.plugin_data.set(account_id=account_id, plugin=PLUGIN, key=KEY, value=overrides)
        else:
            self.plugin_data.delete(account_id=account_id, plugin=PLUGIN, key=KEY)
        self.dispatcher.level_overrides = overrides

    # ------------------------------------------------------------------- проверка

    async def gate(self, event: Any, command: Command) -> PermissionLevel | None:
        """Пускает постороннего к команде: возвращает его уровень или ``None`` (игнорировать)."""
        needed = self.dispatcher.required_level(command)
        if needed is None:
            return None
        sender = getattr(event, "sender_id", None)
        if sender is None:  # анонимный админ или канал
            return None
        sender = int(sender)
        now = self._clock()
        last = self._last.get(sender)
        if last is not None and now - last < self._cooldown:
            return None
        level = await self.access.level_of(sender)
        if level is None or level < needed:
            return None
        self._remember(sender, now)
        return level

    def _remember(self, sender: int, now: float) -> None:
        """Запоминает время последней команды человека; список не растёт бесконечно."""
        if len(self._last) >= MAX_TRACKED:
            expired = [key for key, moment in self._last.items() if now - moment >= self._cooldown]
            for key in expired:
                del self._last[key]
            if len(self._last) >= MAX_TRACKED:
                logger.debug("Список пауз переполнен, очищаю.")
                self._last.clear()
        self._last[sender] = now
