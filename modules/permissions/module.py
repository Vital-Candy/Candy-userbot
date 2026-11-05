"""Модуль команды .perm: уровни людей и уровни доступа к командам.

Синтаксис: ``.perm list [id]``, ``.perm grant <id | ответом> <user|trusted|admin>``,
``.perm revoke <id | ответом> [user|trusted|admin]``, ``.perm cmd [команда уровень | reset]``.

Кто что может:

* владелец (сам аккаунт): всё;
* admin: смотреть списки, выдавать и снимать уровни user и trusted людям ниже admin. Выдавать admin,
  трогать других admin и менять доступ к командам он не может;
* остальные эту команду не видят.

Данные людей хранит storage/permissions.py, ручные уровни команд хранит core/policy.py.
"""
from __future__ import annotations

import sqlite3

from core.module import Module
from core.reply import answer
from storage.permissions import GRANTABLE, PermissionLevel
from utils.logger import logger

USAGE = (
    "Использование:\n"
    ".perm list [id]\n"
    ".perm grant <id | ответом> <user|trusted|admin>\n"
    ".perm revoke <id | ответом> [user|trusted|admin]\n"
    ".perm cmd [<команда> <user|trusted|admin|owner|default> | reset]"
)
DETAILS = """\
Кто из других людей что может. Уровни: user (самый скромный: безобидные команды вроде .ping, .help и урезанный .user) < trusted (доверенный) < admin (управляет людьми, но не аккаунтом). Уровень нужно выдать каждому человеку отдельно: у кого его нет, того userbot игнорирует. Владелец аккаунта выше всех, от этих настроек не зависит и ничем не ограничен.

Люди:
.perm list — все выданные уровни
.perm list <id> — уровень одного человека
.perm grant <id> <user|trusted|admin> — выдать уровень (одному человеку действует высший)
.perm revoke <id> [уровень] — снять один уровень или все

Вместо ID можно ответить командой на сообщение человека: .perm grant trusted. Имена @username не поддерживаются, нужен числовой ID.

Доступ к командам (меняет только владелец):
.perm cmd — какие команды открыты другим и с какого уровня
.perm cmd <команда> <user|trusted|admin> — открыть команду с этого уровня
.perm cmd <команда> owner — закрыть команду для всех, кроме владельца
.perm cmd <команда> default — вернуть уровень по умолчанию
.perm cmd reset — сбросить все ручные настройки

Открыть можно только команды, написанные с расчётом на чужие сообщения (в списке .perm cmd). Остальные, например .update, .restart, .account, .backup, .plugin, .spam, .purge, .mute, .time, останутся только у владельца при любых настройках. Ручные настройки хранятся в каталоге data и при .update не сбрасываются.

Что может admin: смотреть списки, выдавать и снимать уровни user и trusted людям ниже admin. Выдать admin, тронуть другого admin или поменять доступ к командам он не может. Номер телефона в .user не показывается никому, кроме владельца.

Права: владелец аккаунта и admin (ограниченно, см. выше)."""

OWNER_WORDS = {"owner", "владелец"}
RESET_WORDS = {"default", "reset", "по-умолчанию"}


class PermissionsModule(Module):
    """Команда ``.perm``: выдача уровней людям и настройка доступа к командам."""

    name = "permissions"
    description = "Команда .perm: уровни людей и доступ к командам"

    async def start(self) -> None:
        """Регистрирует команду ``.perm`` (по умолчанию доступна admin, но с ограничениями)."""
        self.app.dispatcher.register_command(
            "perm",
            self.perm_command,
            description="уровни людей и доступ к командам",
            usage=".perm list | grant | revoke | cmd",
            priority=40,
            details=DETAILS,
            level=PermissionLevel.ADMIN,
        )

    async def stop(self) -> None:
        """Снимает регистрацию команды ``.perm``."""
        self.app.dispatcher.unregister_command("perm")

    async def perm_command(self, event) -> None:
        """Разбирает ``.perm`` и вызывает нужное действие; ошибки ввода и БД отвечает текстом."""
        caller = self.app.dispatcher.caller_level(event)
        if caller is None:
            return
        command = self.app.dispatcher.get_command(event)
        args = list(command.args) if command else []
        action = args[0].lower() if args else ""
        try:
            if action == "list" and len(args) <= 2:
                await self._list(event, args[1:])
            elif action == "grant" and 1 <= len(args) <= 3:
                await self._grant(event, args[1:], caller)
            elif action == "revoke" and 1 <= len(args) <= 3:
                await self._revoke(event, args[1:], caller)
            elif action == "cmd":
                await self._cmd(event, args[1:], caller)
            else:
                await answer(event, USAGE)
        except ValueError as exc:
            await answer(event, f"❌ {exc}")
        except sqlite3.Error:
            logger.exception("Ошибка базы данных в .perm")
            await answer(event, "❌ Ошибка базы данных.")

    # ----------------------------------------------------------------- люди

    async def _list(self, event, rest: list[str]) -> None:
        """``.perm list [id]``: все выданные уровни либо уровень одного человека."""
        account_id = await self.app.access.account_id()
        store = self.app.storage.permissions
        if rest:
            subject = int(await self._subject(event, rest[0], allow_reply=False))
            level = await self.app.access.level_of(subject)
            shown = level.value if level is not None else "нет (игнорируется)"
            lines = [f"👤 {subject}: {shown}"]
            for item in store.list_for_subject(account_id=account_id, subject_id=subject):
                lines.append(f"выдано: {item.permission}")
        else:
            items = store.list_all(account_id=account_id)
            if not items:
                lines = ["🔐 Уровни пока никому не выданы, поэтому все, кроме вас, игнорируются."]
            else:
                lines = ["🔐 Выданные уровни:"] + [f"{item.subject_id} — {item.permission}" for item in items]
        await answer(event, "\n".join(lines))

    async def _grant(self, event, rest: list[str], caller: PermissionLevel) -> None:
        """``.perm grant``: выдаёт уровень; admin выдаёт только user и trusted людям ниже admin."""
        subject, level = await self._subject_and_level(event, rest, need_level=True)
        account_id = await self.app.access.account_id()
        if subject == account_id:
            raise ValueError("аккаунт и так владелец: ему уровни не нужны")
        await self._check_target(event, subject, level, caller)
        self.app.storage.permissions.grant(
            account_id=account_id, subject_id=subject, permission=level, granted_by=await self._caller_id(event, account_id)
        )
        await answer(event, f"✅ {subject}: выдан уровень {level.value}.")

    async def _revoke(self, event, rest: list[str], caller: PermissionLevel) -> None:
        """``.perm revoke``: снимает один уровень или все; права владельца снять нельзя."""
        subject, level = await self._subject_and_level(event, rest, need_level=False)
        account_id = await self.app.access.account_id()
        if subject == account_id:
            raise ValueError("права владельца снять нельзя")
        await self._check_target(event, subject, level, caller)
        store = self.app.storage.permissions
        if level is None:
            removed = store.revoke_all(account_id=account_id, subject_id=subject)
            text = f"✅ {subject}: все уровни сняты." if removed else f"❌ У {subject} нет выданных уровней."
        else:
            done = store.revoke(account_id=account_id, subject_id=subject, permission=level)
            text = (
                f"✅ {subject}: уровень {level.value} снят."
                if done
                else f"❌ У {subject} нет уровня {level.value}."
            )
        await answer(event, text)

    async def _check_target(self, event, subject: int, level: PermissionLevel | None, caller: PermissionLevel) -> None:
        """Ограничения admin: только user и trusted, только людям ниже admin, не себе."""
        if caller == PermissionLevel.OWNER:
            return
        if level is not None and level >= PermissionLevel.ADMIN:
            raise ValueError("admin не может выдавать и снимать уровень admin: это может только владелец")
        if subject == getattr(event, "sender_id", None):
            raise ValueError("свои уровни менять нельзя")
        current = await self.app.access.level_of(subject)
        if current is not None and current >= PermissionLevel.ADMIN:
            raise ValueError("admin не может менять уровни другого admin: это может только владелец")

    async def _caller_id(self, event, account_id: int) -> int:
        """ID того, кто выдаёт уровень (для записи в базу): отправитель или владелец."""
        sender = getattr(event, "sender_id", None)
        return int(sender) if sender is not None and not getattr(event, "out", False) else account_id

    # ------------------------------------------------------- доступ к командам

    async def _cmd(self, event, rest: list[str], caller: PermissionLevel) -> None:
        """``.perm cmd``: показывает доступ к командам; владелец ещё и меняет его."""
        if not rest:
            await answer(event, self._cmd_table())
            return
        if caller != PermissionLevel.OWNER:
            raise ValueError("доступ к командам меняет только владелец")
        policy = self.app.policy
        if len(rest) == 1 and rest[0].lower() == "reset":
            count = await policy.reset()
            await answer(event, f"✅ Ручные настройки сброшены (было: {count}). Действуют уровни по умолчанию.")
            return
        if len(rest) != 2:
            raise ValueError("укажите команду и уровень: .perm cmd <команда> <user|trusted|admin|owner|default>")
        name, word = rest[0], rest[1].strip().lower()
        if word in RESET_WORDS:
            level = None
        elif word in OWNER_WORDS:
            level = PermissionLevel.OWNER
        else:
            level = self._parse_level(word)
            if level is None or level not in GRANTABLE:
                raise ValueError("уровень: user, trusted, admin, owner или default")
        await policy.set_level(name, level)
        command = self.app.dispatcher.commands[name.strip().lower().lstrip(".")]
        needed = self.app.dispatcher.required_level(command)
        now = "только владелец" if needed is None else f"уровень {needed.value} и выше"
        await answer(event, f"✅ .{command.name}: теперь {now}.")

    def _cmd_table(self) -> str:
        """Таблица: какие команды открыты другим людям и с какого уровня."""
        dispatcher = self.app.dispatcher
        shareable = [item for item in dispatcher.command_help() if item.level is not None]
        closed = sorted(item.name for item in dispatcher.commands.values() if item.level is None)
        lines = ["🔐 Доступ к командам (кроме владельца):"]
        for command in shareable:
            needed = dispatcher.required_level(command)
            now = "только владелец" if needed is None else f"{needed.value} и выше"
            manual = ""
            if command.name in dispatcher.level_overrides:
                manual = f" (вручную; по умолчанию {command.level.value})"
            lines.append(f".{command.name} — {now}{manual}")
        if closed:
            lines += ["", "Только владелец, открыть нельзя: " + ", ".join("." + name for name in closed)]
        lines += ["", "Изменить (владелец): .perm cmd <команда> <user|trusted|admin|owner|default>"]
        return "\n".join(lines)

    # ----------------------------------------------------------------- parsing

    async def _subject_and_level(self, event, rest: list[str], *, need_level: bool):
        """Разбирает ``[<id>] [<уровень>]``: без id берётся автор сообщения, на которое ответили."""
        # Уровень ищем в конце списка: id необязателен (при reply), уровень всегда последний.
        level: PermissionLevel | None = None
        words = list(rest)
        if words and self._parse_level(words[-1]) is not None:
            level = self._parse_level(words.pop())
        if len(words) > 1:
            raise ValueError("слишком много аргументов")
        if need_level and level is None:
            raise ValueError("укажите уровень: user, trusted или admin")
        if level is not None and level not in GRANTABLE:
            raise ValueError("доступны уровни user, trusted и admin")
        subject = await self._subject(event, words[0] if words else None, allow_reply=True)
        return subject, level

    @staticmethod
    def _parse_level(text: str) -> PermissionLevel | None:
        """Возвращает уровень по тексту или ``None``, если такого уровня нет."""
        try:
            return PermissionLevel(text.strip().lower())
        except ValueError:
            return None

    async def _subject(self, event, ref: str | None, *, allow_reply: bool) -> int:
        """Возвращает ID пользователя из аргумента или автора сообщения, на которое ответили.

        Бросает ``ValueError``, если ID не удалось определить. Имена (@username) не поддерживаются.
        """
        if ref is not None:
            ref = ref.strip()
            if ref.lstrip("-").isdigit():
                value = int(ref)
                if value <= 0:
                    raise ValueError("ID пользователя должен быть положительным числом")
                return value
            raise ValueError(f"не понял «{ref}»: нужен числовой ID пользователя")
        if allow_reply:
            reply = await event.get_reply_message()
            sender = getattr(reply, "sender_id", None) if reply is not None else None
            if sender is not None:
                return int(sender)
        raise ValueError("укажите ID пользователя или ответьте на его сообщение")
