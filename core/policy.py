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
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from core.access import AccessService
from core.dispatcher import Command, Dispatcher
from storage.permissions import GRANTABLE, PermissionLevel
from utils.logger import logger

PLUGIN = "core.access"
KEY = "command_levels"
AUDIT_KEY = "audit_log"
MAX_AUDIT = 100  # сколько последних изменений хранить
PROTECTED = {"perm": PermissionLevel.ADMIN}  # команды, которые нельзя опустить ниже этого уровня
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
        """Задаёт уровень одной команды вручную (см. ``set_levels``)."""
        await self.set_levels([name], level)

    async def set_levels(self, names: Iterable[str], level: PermissionLevel | None) -> list[str]:
        """Задаёт уровень сразу нескольким командам; ``None`` возвращает уровень по умолчанию.

        ``owner`` закрывает команду для всех, кроме владельца. Открыть можно только команду,
        у которой при регистрации указан уровень; ``.perm`` нельзя опустить ниже admin. Если хоть
        одно имя не подходит, не меняется ничего (``ValueError``). Возвращает настоящие имена команд.
        """
        found: list[Command] = []
        for name in names:
            command = self.dispatcher.commands.get(name.strip().lower().lstrip("."))
            if command is None:
                raise ValueError(f"команды .{name.strip().lstrip('.')} нет")
            if command.level is None:
                raise ValueError(
                    f"команда .{command.name} только для владельца: она не рассчитана на чужие сообщения, открыть её нельзя"
                )
            floor = PROTECTED.get(command.name)
            if floor is not None and level is not None and level != PermissionLevel.OWNER and level < floor:
                raise ValueError(f"команду .{command.name} нельзя опустить ниже уровня {floor.value}")
            if command not in found:
                found.append(command)
        overrides = dict(self.dispatcher.level_overrides)
        for command in found:
            if level is None:
                overrides.pop(command.name, None)
            else:
                overrides[command.name] = level.value
        await self._save(overrides)
        return [command.name for command in found]

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

    # ----------------------------------------------------------------- журнал изменений

    async def record(self, by: int, text: str) -> None:
        """Записывает в журнал, кто и что изменил (только изменения прав, не обычные команды)."""
        account_id = await self.access.account_id()
        saved = self.plugin_data.get(account_id=account_id, plugin=PLUGIN, key=AUDIT_KEY, default=None)
        records = [item for item in saved if isinstance(item, dict)] if isinstance(saved, list) else []
        records.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": int(by), "what": str(text)})
        self.plugin_data.set(account_id=account_id, plugin=PLUGIN, key=AUDIT_KEY, value=records[-MAX_AUDIT:])

    async def history(self, count: int = 10) -> list[dict]:
        """Последние записи журнала (новые в конце); повреждённые записи пропускаются."""
        account_id = await self.access.account_id()
        saved = self.plugin_data.get(account_id=account_id, plugin=PLUGIN, key=AUDIT_KEY, default=None)
        if not isinstance(saved, list):
            return []
        good = [
            item for item in saved
            if isinstance(item, dict) and isinstance(item.get("at"), str) and isinstance(item.get("what"), str)
        ]
        return good[-max(1, count):]

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
        if self._too_fast(sender):
            return None
        level = await self.access.level_of(sender)
        if level is None or level < needed:
            return None
        # Повторная проверка после ожидания базы: два сообщения подряд не должны пройти оба.
        if self._too_fast(sender):
            return None
        self._remember(sender, self._clock())
        return level

    def _too_fast(self, sender: int) -> bool:
        """True, если этот человек уже вызывал команду меньше ``cooldown`` секунд назад."""
        last = self._last.get(sender)
        return last is not None and self._clock() - last < self._cooldown

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
