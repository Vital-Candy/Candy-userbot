"""Хранилище прав доступа (таблица permissions) и уровни USER < TRUSTED < ADMIN < OWNER.

Используется модулем прав (.perm) для проверки, кто может вызывать команды. Уровень OWNER
принадлежит только самому аккаунту: он нигде не хранится и никому не выдаётся.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from storage.database import Database
from storage.models import Permission


class PermissionLevel(StrEnum):
    """Уровни доступа. Сравниваются по рангу (OWNER > ADMIN > TRUSTED > USER), а не по строке."""

    USER = "user"
    TRUSTED = "trusted"
    ADMIN = "admin"
    OWNER = "owner"  # только сам аккаунт; выдать этот уровень нельзя

    @property
    def rank(self) -> int:
        """Числовой ранг уровня для сравнений."""
        return _RANKS[self]

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, PermissionLevel):
            return NotImplemented
        return self.rank >= other.rank

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, PermissionLevel):
            return NotImplemented
        return self.rank > other.rank

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, PermissionLevel):
            return NotImplemented
        return self.rank <= other.rank

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, PermissionLevel):
            return NotImplemented
        return self.rank < other.rank


_RANKS = {
    PermissionLevel.USER: 0,
    PermissionLevel.TRUSTED: 1,
    PermissionLevel.ADMIN: 2,
    PermissionLevel.OWNER: 3,
}

# Уровни, которые можно выдать другим людям.
GRANTABLE = (PermissionLevel.USER, PermissionLevel.TRUSTED, PermissionLevel.ADMIN)


class PermissionStore:
    """Выдача, отзыв и чтение прав. Все права изолированы по account_id."""

    def __init__(self, database: Database) -> None:
        self.db = database

    def grant(
        self,
        *,
        account_id: int,
        subject_id: int,
        permission: PermissionLevel | str,
        granted_by: int,
    ) -> Permission:
        """Выдаёт право (повторная выдача обновляет ``granted_by`` и время). Неверный уровень: ValueError."""
        level = PermissionLevel(str(permission))
        if level not in GRANTABLE:
            raise ValueError("уровень owner принадлежит только самому аккаунту")
        created_at = datetime.now(timezone.utc)
        with self.db.transaction():
            self.db.execute(
                """
                INSERT INTO permissions(account_id, subject_id, permission, granted_by, created_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(account_id, subject_id, permission)
                DO UPDATE SET granted_by = excluded.granted_by, created_at = excluded.created_at
                """,
                (account_id, subject_id, level.value, granted_by, created_at.isoformat()),
            )
        return Permission(account_id, subject_id, level.value, granted_by, created_at)

    def revoke(self, *, account_id: int, subject_id: int, permission: PermissionLevel | str) -> bool:
        """Снимает одно право; True, если оно было выдано."""
        level = PermissionLevel(str(permission))
        with self.db.transaction():
            cursor = self.db.execute(
                "DELETE FROM permissions WHERE account_id = ? AND subject_id = ? AND permission = ?",
                (account_id, subject_id, level.value),
            )
        return cursor.rowcount > 0

    def revoke_all(self, *, account_id: int, subject_id: int) -> int:
        """Снимает все права субъекта. Возвращает, сколько записей удалено."""
        with self.db.transaction():
            cursor = self.db.execute(
                "DELETE FROM permissions WHERE account_id = ? AND subject_id = ?",
                (account_id, subject_id),
            )
        return cursor.rowcount

    def highest_level(self, *, account_id: int, subject_id: int) -> PermissionLevel | None:
        """Высший выданный уровень; без записей ``None``: такого человека бот игнорирует."""
        best: PermissionLevel | None = None
        for item in self.list_for_subject(account_id=account_id, subject_id=subject_id):
            try:
                level = PermissionLevel(item.permission)
            except ValueError:  # запись от будущей версии: игнорируем, не повышаем права
                continue
            if level not in GRANTABLE:  # запись owner в базе не даёт ничего: владелец определяется по ID
                continue
            if best is None or level > best:
                best = level
        return best

    def list_all(self, *, account_id: int) -> list[Permission]:
        """Все права аккаунта, по пользователю и уровню."""
        rows = self.db.execute(
            "SELECT * FROM permissions WHERE account_id = ? ORDER BY subject_id, permission",
            (account_id,),
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def has(self, *, account_id: int, subject_id: int, permission: PermissionLevel | str) -> bool:
        """Выдан ли именно этот уровень. Иерархию не учитывает, для неё есть highest_level."""
        level = PermissionLevel(str(permission))
        row = self.db.execute(
            "SELECT 1 FROM permissions WHERE account_id = ? AND subject_id = ? AND permission = ?",
            (account_id, subject_id, level.value),
        ).fetchone()
        return row is not None

    def list_for_subject(self, *, account_id: int, subject_id: int) -> list[Permission]:
        """Все права одного пользователя."""
        rows = self.db.execute(
            """
            SELECT * FROM permissions
            WHERE account_id = ? AND subject_id = ?
            ORDER BY permission
            """,
            (account_id, subject_id),
        ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row) -> Permission:
        """Строка БД в модель Permission."""
        return Permission(
            account_id=int(row["account_id"]),
            subject_id=int(row["subject_id"]),
            permission=str(row["permission"]),
            granted_by=int(row["granted_by"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )
