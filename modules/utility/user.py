"""Команда .user: сведения об аккаунте Telegram.

Синтаксис: ``.user <id или @username> [--not-private]`` (или ответом на сообщение без аргумента).
Показываются только те данные, которые Telegram сам отдаёт вашему аккаунту. Телефон выводится
только с флагом ``--not-private`` и только если Telegram его раскрывает.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from core.errors import UserError
from core.module import Module
from core.reply import answer
from storage.permissions import PermissionLevel
from modules.utility.text import clean, code
from utils.logger import logger

PRIVATE_FLAG = "--not-private"
USAGE = "Использование: .user <id или @username> [--not-private]\nИли ответом на сообщение: .user"

DETAILS = """\
Показывает всё, что Telegram сообщает вашему аккаунту об этом пользователе: ID, имя, username, био, статус, язык, общие чаты, день рождения, данные Premium, флаги (бот, проверенный, подозрительный, ограничен) и другое. Все значения выводятся моноширинным шрифтом: нажмите на значение, чтобы скопировать.

.user <id или @username> — сведения без номера телефона
.user <id или @username> --not-private — то же и номер телефона (если Telegram его показывает вам)
.user — ответом на сообщение: про автора этого сообщения

Что видит каждый уровень:
владелец — всё, а телефон только с --not-private;
admin и trusted — то же, но без телефона, без признаков вашей адресной книги (контакт, близкий друг, заблокирован), без общих чатов и без имени, если оно взято из ваших контактов;
user — только ID, username, ссылка и признаки аккаунта (бот, Premium, проверенный и т. п.). Без имени, био и номера.
Телефон и имя из ваших контактов другим не показываются никогда, флаг --not-private у них не действует.

Поиск по числовому ID работает, если ваш аккаунт уже встречал этого человека (общий чат, переписка). Если пользователь у вас в контактах, Telegram отдаёт имя так, как оно записано в ваших контактах: имя из его профиля через API получить нельзя. Поэтому для контактов показывается и имя при пересылке сообщений, если Telegram его раскрывает.

Права: только владелец аккаунта."""

# Названия статусов Telegram по имени класса (UserStatus*).
_STATUS = {
    "UserStatusOnline": "в сети",
    "UserStatusRecently": "был(а) недавно",
    "UserStatusLastWeek": "был(а) на этой неделе",
    "UserStatusLastMonth": "был(а) в этом месяце",
    "UserStatusEmpty": "скрыт или давно",
}

# Флаги пользователя: (атрибут, подпись).
_FLAGS = (
    ("bot", "бот"),
    ("premium", "Premium"),
    ("verified", "проверенный"),
    ("scam", "подозрение на мошенника"),
    ("fake", "помечен как фейк"),
    ("support", "поддержка Telegram"),
    ("deleted", "аккаунт удалён"),
    ("restricted", "ограничен"),
    ("contact", "в ваших контактах"),
    ("mutual_contact", "взаимный контакт"),
    ("close_friend", "близкий друг"),
)
# Признаки, которые видят все уровни: описывают сам аккаунт, а не вашу адресную книгу.
_PUBLIC_FLAGS = frozenset({"bot", "premium", "verified", "scam", "fake", "support", "deleted", "restricted"})

# Что показывать кому: "owner" всё, "trusted" без личного, "user" только справочный минимум.
VIEWS = ("owner", "trusted", "user")


def status_text(status) -> str | None:
    """Статус пользователя словами; для «был(а) в сети» добавляет время по UTC."""
    if status is None:
        return None
    kind = type(status).__name__
    if kind == "UserStatusOffline":
        moment = getattr(status, "was_online", None)
        if isinstance(moment, datetime):
            return "был(а) в сети " + moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return "не в сети"
    return _STATUS.get(kind, kind)


def _birthday(value) -> str | None:
    """День рождения ``ДД.ММ`` или ``ДД.ММ.ГГГГ``."""
    day, month = getattr(value, "day", None), getattr(value, "month", None)
    if not day or not month:
        return None
    year = getattr(value, "year", None)
    return f"{day:02d}.{month:02d}" + (f".{year}" if year else "")


def _usernames(user) -> list[str]:
    """Все активные username пользователя (основной первым), без повторов."""
    names: list[str] = []
    if getattr(user, "username", None):
        names.append(user.username)
    for item in getattr(user, "usernames", None) or ():
        if getattr(item, "active", True) and getattr(item, "username", None) and item.username not in names:
            names.append(item.username)
    return names


def _restriction(user) -> str | None:
    """Причины ограничения аккаунта в одну строку."""
    reasons = [
        getattr(item, "reason", None) or getattr(item, "text", None) or ""
        for item in getattr(user, "restriction_reason", None) or ()
    ]
    reasons = [item for item in reasons if item]
    return ", ".join(reasons) if reasons else None


def view_for(level) -> str:
    """Вид ответа для уровня вызывающего: владелец, trusted/admin или user (всё неизвестное строже всего)."""
    if level == PermissionLevel.OWNER:
        return "owner"
    if level in (PermissionLevel.TRUSTED, PermissionLevel.ADMIN):
        return "trusted"
    return "user"


def format_user(user, full=None, *, private: bool = False, view: str = "owner") -> str:
    """Собирает текст ответа. ``full``: UserFull (может быть None); ``private``: показать телефон.

    ``view`` ограничивает набор полей: у ``trusted`` нет телефона, общих чатов и признаков адресной
    книги, а имя скрыто, если оно из ваших контактов; у ``user`` остаются ID, username, ссылка и
    признаки аккаунта. Телефон показывается только владельцу. Пустые поля пропускаются.
    """
    if view not in VIEWS:
        view = "user"
    owner = view == "owner"
    private = private and owner
    full = full or SimpleNamespace()
    lines = ["👤 Информация о пользователе"]

    def add(label: str, value) -> None:
        """Добавляет строку «подпись: значение», если значение есть."""
        if value is None or value == "" or value == []:
            return
        lines.append(f"{label}: {code(value)}")

    add("ID", getattr(user, "id", None))
    name = " ".join(
        part for part in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if part
    )
    if owner or (view == "trusted" and not getattr(user, "contact", False)):
        add("Имя", name)  # у контакта имя взято из вашей записной книжки: другим его не отдаём
    if owner and getattr(user, "contact", False):
        add("Имя при пересылке", getattr(full, "private_forward_name", None))
    names = _usernames(user)
    if names:
        lines.append("Username: " + ", ".join(code("@" + item) for item in names))
    add("Ссылка", f"tg://user?id={user.id}" if getattr(user, "id", None) else None)
    if view != "user":
        add("Био", getattr(full, "about", None))
    if private:
        phone = getattr(user, "phone", None)
        lines.append("Телефон: " + (code("+" + str(phone).lstrip("+")) if phone else "скрыт или недоступен"))
    if view != "user":
        add("Статус", status_text(getattr(user, "status", None)))
        add("Язык", getattr(user, "lang_code", None))
        add("День рождения", _birthday(getattr(full, "birthday", None)))
    if owner:
        add("Общих чатов", getattr(full, "common_chats_count", None))
    photo = getattr(user, "photo", None)
    if photo is not None and view != "user":
        add("Дата-центр фото", getattr(photo, "dc_id", None))
    if view != "user":
        add("Личный канал (ID)", getattr(full, "personal_channel_id", None))
        add("Подарков в профиле", getattr(full, "stargifts_count", None))
        add("Автоудаление сообщений (с)", getattr(full, "ttl_period", None))
    add("Ограничение", _restriction(user))

    flags = [
        label for attr, label in _FLAGS if getattr(user, attr, False) and (owner or attr in _PUBLIC_FLAGS)
    ]
    if owner and getattr(full, "blocked", False):
        flags.append("заблокирован вами")
    if view != "user" and getattr(full, "phone_calls_available", False):
        flags.append("доступны звонки")
    if view != "user" and getattr(full, "voice_messages_forbidden", False):
        flags.append("голосовые запрещены")
    if flags:
        lines.append("Признаки: " + ", ".join(clean(item) for item in flags))

    bot_info = getattr(full, "bot_info", None)
    if bot_info is not None and view != "user":
        add("Описание бота", getattr(bot_info, "description", None))
        commands = [
            f"/{item.command}" for item in getattr(bot_info, "commands", None) or () if getattr(item, "command", None)
        ]
        add("Команды бота", " ".join(commands))
        add("Пользователей бота в месяц", getattr(user, "bot_active_users", None))
    return "\n".join(lines)


class UserModule(Module):
    """Команда ``.user``: подробные сведения об аккаунте."""

    name = "utility.user"
    description = "Команда .user: информация об аккаунте"
    # Необязательный: без него userbot работает, сбой запуска только пришлёт уведомление.

    async def start(self) -> None:
        """Регистрирует команду ``.user``."""
        self.app.dispatcher.register_command(
            "user",
            self.user_command,
            description="информация об аккаунте",
            usage=".user <id или @username> [--not-private]",
            priority=50,
            details=DETAILS,
            level=PermissionLevel.USER,
        )

    async def stop(self) -> None:
        """Снимает регистрацию команды ``.user``."""
        self.app.dispatcher.unregister_command("user")

    async def user_command(self, event) -> None:
        """Разбирает аргументы, находит пользователя и отвечает сведениями."""
        command = self.app.dispatcher.get_command(event)
        args = list(command.args) if command else []
        view = view_for(self.app.dispatcher.caller_level(event))
        private = any(item.lower() == PRIVATE_FLAG for item in args)
        if private and view != "owner":
            await answer(event, "❌ Номер телефона показывается только владельцу аккаунта.")
            return
        refs = [item for item in args if item.lower() != PRIVATE_FLAG]
        try:
            if len(refs) > 1 or any(item.startswith("-") for item in refs):
                raise UserError(USAGE)
            user = await self._find(event, refs[0] if refs else None)
            full = await self._full(user)
        except UserError as exc:
            await answer(event, f"❌ {exc}")
            return
        except Exception:
            logger.exception("Ошибка .user.")
            await answer(event, "❌ Не удалось получить сведения. Подробности в логе.")
            return
        await answer(event, format_user(user, full, private=private, view=view))

    async def _find(self, event, ref: str | None):
        """Находит пользователя по ID, username или по сообщению, на которое ответили."""
        client = self.app.client
        try:
            if ref is None:
                reply = await event.get_reply_message()
                if reply is None:
                    raise UserError(USAGE)
                entity = await reply.get_sender()
            else:
                target = int(ref) if ref.lstrip("-").isdigit() else ref
                entity = await client.get_entity(target)
        except UserError:
            raise
        except Exception as exc:
            logger.debug("Не нашёл пользователя %r: %s", ref, exc)
            raise UserError(
                "Не нашёл такого пользователя. По числовому ID находятся только те, "
                "кого ваш аккаунт уже встречал; по username — любые."
            ) from None
        if entity is None or type(entity).__name__ != "User":
            raise UserError("Это не пользователь (чат или канал).")
        return entity

    async def _full(self, user):
        """Полные сведения (био, общие чаты и т. д.); при ошибке возвращает None."""
        try:
            from telethon.tl.functions.users import GetFullUserRequest

            result = await self.app.client(GetFullUserRequest(user))
            return getattr(result, "full_user", result)
        except Exception as exc:
            logger.warning("Не удалось получить полные сведения о пользователе: %s", exc)
            return None
