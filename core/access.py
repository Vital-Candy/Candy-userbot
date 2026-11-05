"""Уровни доступа: кто что может делать. Сам аккаунт всегда OWNER, остальным уровень нужно выдать."""
from __future__ import annotations

from typing import Any

from core.filters import Filter
from storage.permissions import PermissionLevel, PermissionStore
from utils.logger import logger


class AccessService:
    """Определяет уровень пользователя относительно текущего аккаунта."""

    def __init__(self, store: PermissionStore, identity: Any) -> None:
        self.store = store
        self.identity = identity

    async def account_id(self) -> int:
        """ID текущего аккаунта (делегирует в identity)."""
        return await self.identity.account_id()

    async def level_of(self, subject_id: int) -> PermissionLevel | None:
        """Наивысший уровень ``subject_id``: OWNER для самого аккаунта, ``None``, если ничего не выдано."""
        account_id = await self.account_id()
        if int(subject_id) == account_id:
            return PermissionLevel.OWNER  # владелец: записи в базе не нужно
        return self.store.highest_level(account_id=account_id, subject_id=int(subject_id))

    async def has_level(self, subject_id: int, level: PermissionLevel | str) -> bool:
        """True, если уровень пользователя не ниже ``level``; без выданного уровня всегда False."""
        current = await self.level_of(subject_id)
        return current is not None and current >= PermissionLevel(str(level))


def require(app: Any, level: PermissionLevel | str) -> Filter:
    """Фильтр для команд и обработчиков: пропускает не ниже ``level``.

    * собственные исходящие сообщения всегда проходят (это владелец);
    * уровень OWNER у других людей не бывает: фильтр с ним пускает только владельца;
    * без ``sender_id`` (анонимный админ канала) не проходит;
    * любая ошибка при проверке означает отказ, а не доступ.
    """
    wanted = PermissionLevel(str(level))

    async def check(event: Any) -> bool:
        """Проверка одного события: True, если отправителю разрешено."""
        if getattr(event, "out", False):
            return True
        sender = getattr(event, "sender_id", None)
        access = getattr(app, "access", None)
        if sender is None or access is None:
            return False
        try:
            return await access.has_level(int(sender), wanted)
        except Exception:
            # Fail-closed: сбой базы или Telegram не должен открывать доступ.
            logger.exception("Не удалось проверить права пользователя %s.", sender)
            return False

    # Имя фильтра попадает в логи и справку, поэтому делаем его читаемым.
    check.__name__ = f"require_{wanted.value}"
    return check
