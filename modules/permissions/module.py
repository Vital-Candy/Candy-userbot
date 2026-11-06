"""Модуль команды .perm: уровни людей и уровни доступа к командам.

Синтаксис: ``.perm list [id]``, ``.perm grant <id | ответом> <user|trusted|admin>``,
``.perm revoke <id | ответом> [user|trusted|admin]``, ``.perm audit [N]``,
``.perm cmd [<команды или номера> <уровень> | reset]``, ``.perm cooldown [<уровень> <секунды> | reset]``.

Кто что может:

* владелец (сам аккаунт): всё;
* admin: смотреть списки и аудит, выдавать и снимать уровни user и trusted людям ниже admin. Выдавать admin,
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
    ".perm audit [N]\n"
    ".perm cmd [<команды или номера> <user|trusted|admin|owner|default> | reset]\n"
    ".perm cooldown [<user|trusted|admin> <секунды> | reset]"
)
DETAILS = """\
Кто из других людей что может. Уровни: user (самый скромный: безобидные команды вроде .ping, .help и урезанный .user) < trusted (доверенный) < admin (управляет людьми, но не аккаунтом). Уровень нужно выдать каждому человеку отдельно: у кого его нет, того userbot игнорирует. Владелец аккаунта выше всех, от этих настроек не зависит и ничем не ограничен.

Люди:
.perm list — все выданные уровни
.perm list <id> — уровень одного человека
.perm grant <id> <user|trusted|admin> — выдать уровень (одному человеку действует высший)
.perm revoke <id> [уровень] — снять один уровень или все

Вместо ID можно ответить командой на сообщение человека: .perm grant trusted. Имена @username не поддерживаются, нужен числовой ID.

Аудит:
.perm audit — кто что имеет, какие команды открыты шире обычного, подозрительные места и последние изменения
.perm audit <N> — то же, но последних изменений N (до 50)

Доступ к командам (меняет только владелец):
.perm cmd — пронумерованный список: у какой команды какой уровень
.perm cmd <команда> <user|trusted|admin> — открыть команду с этого уровня
.perm cmd tag ping user — сразу несколько команд (через пробел или запятую); уровень всегда последний
.perm cmd 2 5 trusted — команды можно указывать номерами из списка .perm cmd
.perm cmd all trusted — все открытые команды сразу (кроме .perm: он остаётся не ниже admin)
.perm cmd <команда> owner (или off) — закрыть команду для всех, кроме владельца
.perm cmd <команда> default — вернуть уровень по умолчанию
.perm cmd reset — сбросить все ручные настройки
Если хотя бы одно имя неверное, не меняется ничего.

Паузы (перезарядка между командами чужих людей):
.perm cooldown — паузы по уровням и вес пауз у команд
.perm cooldown trusted 4 — задать базовую паузу уровня в секундах (владелец; от 0.5 до 3600)
.perm cooldown reset — вернуть паузы по умолчанию
Чем ниже уровень, тем пауза длиннее или равна (user ≥ trusted ≥ admin); нулевой нет ни у кого, даже у admin. У тяжёлых команд (например .tag) пауза умножается на вес. У владельца пауз нет.

Открыть можно только команды, написанные с расчётом на чужие сообщения (в списке .perm cmd). Остальные, например .update, .restart, .account, .backup, .plugin, .spam, .purge, .mute, .time, останутся только у владельца при любых настройках. Ручные настройки хранятся в каталоге data и при .update не сбрасываются.

Что может admin: смотреть списки и аудит, выдавать и снимать уровни user и trusted людям ниже admin. Выдать admin, тронуть другого admin или поменять доступ к командам он не может. Номер телефона в .user не показывается никому, кроме владельца.

Права: владелец аккаунта и admin (ограниченно, см. выше)."""

OWNER_WORDS = {"owner", "владелец", "off", "close", "закрыть"}
RESET_WORDS = {"default", "reset", "по-умолчанию"}
ALL_WORDS = {"all", "все"}
LIST_LIMIT = 15  # сколько ID показывать в одном списке аудита
AUDIT_DEFAULT = 10
AUDIT_MAX = 50
MESSAGE_LIMIT = 3800  # запас до лимита Telegram в 4096 символов


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
        if caller is None or caller < PermissionLevel.ADMIN:
            return  # запасная защита: даже если команду по ошибке открыли ниже admin, работать она не будет
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
            elif action == "audit" and len(args) <= 2:
                await self._audit(event, args[1:])
            elif action == "cmd":
                await self._cmd(event, args[1:], caller)
            elif action == "cooldown" and len(args) <= 3:
                await self._cooldown(event, args[1:], caller)
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
        await self._log(event, f"выдан {level.value} → {subject}")
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
        if text.startswith("✅"):
            await self._log(event, f"снят {level.value if level else 'все уровни'} → {subject}")
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

    def _shareable(self) -> list:
        """Открытые другим команды в стабильном порядке (по имени): по нему считаются номера в ``.perm cmd``."""
        return sorted((item for item in self.app.dispatcher.commands.values() if item.level is not None), key=lambda item: item.name)

    async def _log(self, event, text: str) -> None:
        """Записывает изменение в журнал аудита; сбой журнала не мешает самой команде."""
        try:
            account_id = await self.app.access.account_id()
            by = await self._caller_id(event, account_id)
            await self.app.policy.record(by, text)
        except Exception:
            logger.exception("Не удалось записать изменение в журнал .perm audit")

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
            await self._log(event, f"сброшены ручные уровни команд (было {count})")
            await answer(event, f"✅ Ручные настройки сброшены (было: {count}). Действуют уровни по умолчанию.")
            return
        tokens = [part for item in rest for part in item.replace(",", " ").split() if part]
        if len(tokens) < 2:
            raise ValueError("укажите команды и уровень: .perm cmd <команда> <user|trusted|admin|owner|default>")
        word = tokens.pop().lower()
        if word in RESET_WORDS:
            level = None
        elif word in OWNER_WORDS:
            level = PermissionLevel.OWNER
        else:
            level = self._parse_level(word)
            if level is None or level not in GRANTABLE:
                raise ValueError("уровень: user, trusted, admin, owner или default")
        names = self._command_names(tokens)
        changed = await policy.set_levels(names, level)
        dispatcher = self.app.dispatcher
        lines = []
        for name in changed:
            needed = dispatcher.required_level(dispatcher.commands[name])
            lines.append(f".{name}: " + ("только владелец" if needed is None else f"{needed.value} и выше"))
        shown = ", ".join(f".{name}" for name in changed)
        await self._log(event, f"уровень команд {shown}: {word}")
        if len(lines) == 1:
            await answer(event, "✅ " + lines[0].replace(": ", ": теперь ", 1) + ".")
        else:
            await answer(event, "✅ Изменено команд: " + str(len(lines)) + "\n" + "\n".join(lines))

    def _command_names(self, tokens: list[str]) -> list[str]:
        """Имена команд из слов и номеров; ``all`` берёт все открытые команды (кроме защищённых)."""
        from core.policy import PROTECTED  # локально: модуль грузится раньше политики

        shareable = self._shareable()
        names: list[str] = []
        for token in tokens:
            if token.lower() in ALL_WORDS:
                names += [item.name for item in shareable if item.name not in PROTECTED]
            elif token.isdigit():
                number = int(token)
                if not 1 <= number <= len(shareable):
                    raise ValueError(f"номера {number} нет: в списке .perm cmd команд {len(shareable)}")
                names.append(shareable[number - 1].name)
            else:
                names.append(token)
        return list(dict.fromkeys(names))  # без повторов, порядок сохраняется

    def _cmd_table(self) -> str:
        """Пронумерованная таблица: какие команды открыты другим людям и с какого уровня."""
        dispatcher = self.app.dispatcher
        closed = sorted(item.name for item in dispatcher.commands.values() if item.level is None)
        lines = ["🔐 Доступ к командам (кроме владельца):"]
        for number, command in enumerate(self._shareable(), start=1):
            needed = dispatcher.required_level(command)
            now = "только владелец" if needed is None else f"{needed.value} и выше"
            manual = ""
            if command.name in dispatcher.level_overrides:
                manual = f" (вручную; по умолчанию {command.level.value})"
            lines.append(f"{number}. .{command.name} — {now}{manual}")
        if closed:
            lines += ["", "Только владелец, открыть нельзя: " + ", ".join("." + name for name in closed)]
        lines += ["", "Изменить (владелец): .perm cmd <команды или номера> <user|trusted|admin|owner|default>"]
        return "\n".join(lines)

    # ------------------------------------------------------------------ паузы

    def _cooldown_text(self) -> str:
        """Паузы по уровням и команды с повышенным весом (с итоговым временем для каждого уровня)."""
        policy = self.app.policy
        lines = ["⏱ Паузы между командами чужих людей (у владельца их нет):"]
        for level in GRANTABLE:
            lines.append(f"{level.value}: {policy.cooldowns[level]:g} с")
        heavy = sorted(
            (item for item in self.app.dispatcher.commands.values() if item.level is not None and item.pause > 1),
            key=lambda item: item.name,
        )
        if heavy:
            lines += ["", "У тяжёлых команд пауза умножается на вес:"]
            for item in heavy:
                times = ", ".join(f"{level.value} {policy.pause_for(level, item):g} с" for level in GRANTABLE)
                lines.append(f".{item.name} x{item.pause:g}: {times}")
        lines += ["", "Изменить (владелец): .perm cooldown <user|trusted|admin> <секунды> | reset"]
        return "\n".join(lines)

    async def _cooldown(self, event, rest: list[str], caller: PermissionLevel) -> None:
        """``.perm cooldown``: показывает паузы; владелец ещё и меняет их."""
        if not rest:
            await answer(event, self._cooldown_text())
            return
        if caller != PermissionLevel.OWNER:
            raise ValueError("паузы меняет только владелец")
        policy = self.app.policy
        if len(rest) == 1 and rest[0].lower() in RESET_WORDS:
            await policy.reset_cooldowns()
            await self._log(event, "паузы возвращены по умолчанию")
            await answer(event, "✅ Паузы по умолчанию.\n" + self._cooldown_text())
            return
        if len(rest) != 2:
            raise ValueError("использование: .perm cooldown <user|trusted|admin> <секунды> | reset")
        level = self._parse_level(rest[0].lower())
        raw = rest[1].lower().replace(",", ".").removesuffix("с").removesuffix("s")
        try:
            seconds = float(raw)
        except ValueError:
            raise ValueError("секунды нужно записать числом, например 2 или 0.5") from None
        if seconds != seconds or seconds in (float("inf"), float("-inf")):  # nan и бесконечность
            raise ValueError("секунды нужно записать числом, например 2 или 0.5")
        if level is None:
            raise ValueError("уровень: user, trusted или admin")
        await policy.set_cooldown(level, seconds)
        await self._log(event, f"пауза {level.value}: {seconds:g} с")
        await answer(event, f"✅ Пауза {level.value}: {seconds:g} с.\n" + self._cooldown_text())

    # ------------------------------------------------------------------ аудит

    async def _audit(self, event, rest: list[str]) -> None:
        """``.perm audit [N]``: сводка прав, подозрительные места и последние изменения."""
        count = AUDIT_DEFAULT
        if rest:
            if not rest[0].isdigit() or not 1 <= int(rest[0]) <= AUDIT_MAX:
                raise ValueError(f"N это число от 1 до {AUDIT_MAX}")
            count = int(rest[0])
        account_id = await self.app.access.account_id()
        dispatcher = self.app.dispatcher
        items = self.app.storage.permissions.list_all(account_id=account_id)

        holders: dict[int, list[PermissionLevel]] = {}
        for item in items:
            try:
                level = PermissionLevel(item.permission)
            except ValueError:
                continue
            if level in GRANTABLE:
                holders.setdefault(item.subject_id, []).append(level)
        best = {subject: max(levels) for subject, levels in holders.items()}

        lines = ["🔎 Аудит прав", f"Людей с уровнем: {len(best)}"]
        for level in reversed(GRANTABLE):
            ids = sorted(subject for subject, top in best.items() if top == level)
            if ids:
                shown = ", ".join(str(number) for number in ids[:LIST_LIMIT])
                more = f" и ещё {len(ids) - LIST_LIMIT}" if len(ids) > LIST_LIMIT else ""
                lines.append(f"{level.value} ({len(ids)}): {shown}{more}")

        pauses = ", ".join(f"{level.value} {self.app.policy.cooldowns[level]:g} с" for level in GRANTABLE)
        lines += ["", f"Паузы: {pauses}"]
        shareable = self._shareable()
        manual = [item for item in shareable if item.name in dispatcher.level_overrides]
        lines += ["", f"Команд открыто другим: {len(shareable)}, настроено вручную: {len(manual)}"]
        for command in manual:
            needed = dispatcher.required_level(command)
            now = "только владелец" if needed is None else f"{needed.value} и выше"
            lines.append(f".{command.name} — {now} (по умолчанию {command.level.value})")

        warnings: list[str] = []
        for command in manual:
            needed = dispatcher.required_level(command)
            if needed is not None and needed < command.level:
                warnings.append(f".{command.name} открыта шире обычного: {needed.value} вместо {command.level.value}")
        for subject, levels in sorted(holders.items()):
            if len(levels) > 1:
                warnings.append(f"{subject}: выдано несколько уровней, действует высший ({best[subject].value})")
        known = set(best) | {account_id}
        for item in items:
            if item.granted_by not in known and item.subject_id in best:
                warnings.append(f"{item.subject_id}: {item.permission} выдал {item.granted_by}, у которого сейчас нет прав")
        stale = sorted(name for name in dispatcher.level_overrides if name not in dispatcher.commands)
        if stale:
            warnings.append("настройки для команд, которых сейчас нет: " + ", ".join("." + name for name in stale))
        if len([1 for top in best.values() if top == PermissionLevel.ADMIN]) > 3:
            warnings.append("admin больше трёх: проверьте, что все они нужны")
        lines += ["", "Замечания:" if warnings else "Замечаний нет."] + [f"⚠️ {item}" for item in warnings[:LIST_LIMIT]]

        history = await self.app.policy.history(count)
        if history:
            lines += ["", f"Последние изменения ({len(history)}):"]
            for record in reversed(history):
                moment = record["at"].replace("T", " ")[:16]
                lines.append(f"{moment} UTC · {record.get('by', '?')} · {record['what']}")
        else:
            lines += ["", "Журнал изменений пока пуст."]
        text = "\n".join(lines)
        if len(text) > MESSAGE_LIMIT:
            text = text[:MESSAGE_LIMIT].rsplit("\n", 1)[0] + "\n… (обрезано, уменьшите N)"
        await answer(event, text)

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
