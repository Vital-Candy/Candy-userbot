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

Пауза (перезарядка) есть у всех, кроме владельца, и зависит от уровня: чем ниже уровень, тем она
длиннее (по умолчанию user 5 с, trusted 3 с, admin 1 с), но даже у admin она не нулевая. После
команды человек ждёт паузу своего уровня, умноженную на вес команды (``Command.pause``: у тяжёлых
команд он больше). Паузы настраивает владелец (``.perm cooldown``), они хранятся в базе. Если человек
с нужным уровнем написал слишком рано, он получает один ответ «подождите», остальные сообщения
в это окно игнорируются.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from core.access import AccessService
from core.dispatcher import Command, Dispatcher
from core.reply import answer
from storage.permissions import GRANTABLE, PermissionLevel
from utils.logger import logger

PLUGIN = "core.access"
KEY = "command_levels"
AUDIT_KEY = "audit_log"
MAX_AUDIT = 100  # сколько последних изменений хранить
PROTECTED = {"perm": PermissionLevel.ADMIN}  # команды, которые нельзя опустить ниже этого уровня
COOLDOWN_KEY = "cooldowns"
# Базовая пауза по уровням: чем ниже уровень, тем длиннее; у admin она тоже есть.
DEFAULT_COOLDOWNS = {PermissionLevel.USER: 5.0, PermissionLevel.TRUSTED: 3.0, PermissionLevel.ADMIN: 1.0}
MIN_COOLDOWN = 0.5
MAX_COOLDOWN = 3600.0
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
    ) -> None:
        self.access = access
        self.plugin_data = plugin_data
        self.dispatcher = dispatcher
        self._clock = clock
        self.cooldowns: dict[PermissionLevel, float] = dict(DEFAULT_COOLDOWNS)
        self._until: dict[int, float] = {}  # до какого момента человек ждёт
        self._warned: dict[int, float] = {}  # в каком окне ожидания ему уже ответили «подождите»

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
        self.cooldowns = self._read_cooldowns(
            self.plugin_data.get(account_id=account_id, plugin=PLUGIN, key=COOLDOWN_KEY, default=None)
        )

    @staticmethod
    def _read_cooldowns(saved: Any) -> dict[PermissionLevel, float]:
        """Паузы из базы; любая порча (не число, вне границ, нарушен порядок) возвращает значения по умолчанию."""
        result = dict(DEFAULT_COOLDOWNS)
        if not isinstance(saved, dict):
            return result
        for level in DEFAULT_COOLDOWNS:
            value = saved.get(level.value)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if MIN_COOLDOWN <= value <= MAX_COOLDOWN:
                result[level] = float(value)
        if not (result[PermissionLevel.USER] >= result[PermissionLevel.TRUSTED] >= result[PermissionLevel.ADMIN]):
            return dict(DEFAULT_COOLDOWNS)
        return result

    async def set_cooldown(self, level: PermissionLevel, seconds: float) -> None:
        """Задаёт базовую паузу уровня. Границы 0.5 до 3600 с; ниже уровень, пауза не короче.

        Нулевой паузы нет ни у кого, даже у admin. Нарушение правил: ``ValueError``.
        """
        if level not in DEFAULT_COOLDOWNS:
            raise ValueError("пауза задаётся для user, trusted и admin: у владельца её нет")
        if not MIN_COOLDOWN <= seconds <= MAX_COOLDOWN:
            raise ValueError(f"пауза от {MIN_COOLDOWN:g} с до {MAX_COOLDOWN:g} с")
        updated = dict(self.cooldowns)
        updated[level] = float(seconds)
        if not (updated[PermissionLevel.USER] >= updated[PermissionLevel.TRUSTED] >= updated[PermissionLevel.ADMIN]):
            raise ValueError("чем ниже уровень, тем пауза длиннее или равна: user ≥ trusted ≥ admin")
        await self._save_cooldowns(updated)

    async def reset_cooldowns(self) -> None:
        """Возвращает паузы по умолчанию."""
        await self._save_cooldowns(dict(DEFAULT_COOLDOWNS))

    async def _save_cooldowns(self, cooldowns: dict[PermissionLevel, float]) -> None:
        """Сохраняет паузы в базе (если они отличаются от умолчаний) и применяет."""
        account_id = await self.access.account_id()
        if cooldowns == DEFAULT_COOLDOWNS:
            self.plugin_data.delete(account_id=account_id, plugin=PLUGIN, key=COOLDOWN_KEY)
        else:
            self.plugin_data.set(
                account_id=account_id, plugin=PLUGIN, key=COOLDOWN_KEY,
                value={level.value: seconds for level, seconds in cooldowns.items()},
            )
        self.cooldowns = cooldowns

    def pause_for(self, level: PermissionLevel, command: Command) -> float:
        """Сколько секунд ждёт человек уровня ``level`` после команды ``command``."""
        return self.cooldowns.get(level, DEFAULT_COOLDOWNS[PermissionLevel.USER]) * command.pause

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
        until = self._until.get(sender)
        if until is not None and self._clock() < until and self._warned.get(sender) == until:
            return None  # уже предупредили в этом окне: молчим и базу не трогаем
        level = await self.access.level_of(sender)
        if level is None or level < needed:
            return None  # без права посторонним не отвечаем вообще
        # Время смотрим уже после ожидания базы: два сообщения подряд не должны пройти оба.
        now = self._clock()
        until = self._until.get(sender)
        if until is not None and now < until:
            if self._warned.get(sender) != until:
                self._warned[sender] = until
                await answer(event, f"⏳ Подождите {math.ceil(until - now)} с.")
            return None
        self._remember(sender, now + self.pause_for(level, command))
        return level

    def _remember(self, sender: int, until: float) -> None:
        """Запоминает, до какого момента человек ждёт; таблицы не растут бесконечно."""
        if len(self._until) >= MAX_TRACKED:
            now = self._clock()
            for table in (self._until, self._warned):
                for key in [key for key, moment in table.items() if moment <= now]:
                    del table[key]
            if len(self._until) >= MAX_TRACKED:
                logger.debug("Таблица пауз переполнена, очищаю.")
                self._until.clear()
                self._warned.clear()
        self._until[sender] = until
        self._warned.pop(sender, None)
