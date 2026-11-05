"""Команда .tag: отметить участников группы.

Синтаксис: ``.tag all [-e кого пропустить]``, ``.tag rand <число> [-e ...]``, ``.tag stop``.
Отметки уходят пачками по несколько человек с паузой. У кого есть username, пишется ``@username``,
у остальных имя со ссылкой на аккаунт. Боты, удалённые аккаунты и вы сами пропускаются.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from core.errors import UserError
from core.module import Module
from core.reply import answer
from storage.permissions import PermissionLevel
from modules.utility.text import clean
from utils.logger import logger

try:
    from telethon.errors import FloodWaitError
except ImportError:  # Telethon не установлен
    FloodWaitError = None

PER_MESSAGE = 5  # отметок в одном сообщении
PAUSE = 1.5  # секунд между сообщениями
MAX_ALL = 300  # больше участников за раз не отмечаем
MAX_RAND = 100
MAX_RAND_OTHERS = 10  # потолок для людей с выданным уровнем: отметить всех они не могут
FETCH_LIMIT = 2000  # сколько участников максимум читать из чата
MAX_FLOOD_WAIT = 300
EXCLUDE_FLAGS = ("-e", "-е", "--exclude")  # вторая -е написана русской буквой: так легко ошибиться

USAGE = (
    "Использование:\n"
    ".tag all [-e @юзер ...] — отметить всех\n"
    ".tag rand <число> [-e @юзер ...] — отметить случайных\n"
    ".tag stop — остановить"
)

DETAILS = f"""\
Отмечает участников группы. Работает только в группах, где вы видите список участников.

.tag all — отметить всех (до {MAX_ALL} человек за раз)
.tag all -e @юзер1 @юзер2 — отметить всех, кроме указанных
.tag rand <число> — отметить столько случайных участников (от 1 до {MAX_RAND})
.tag rand <число> -e @юзер — случайных, кроме указанных
.tag stop — остановить идущую отметку

В -e можно писать @username, username или числовой ID, несколько через пробел. У кого есть username, ставится @username, у остальных имя со ссылкой на аккаунт. Боты, удалённые аккаунты и вы сами пропускаются.
Отметки идут по {PER_MESSAGE} в сообщении с паузой {PAUSE:g} с, чтобы не поймать ограничение Telegram. В одном чате идёт одна отметка.

Людям с выданным уровнем (по умолчанию trusted и выше) доступно только .tag rand до {MAX_RAND_OTHERS} человек без -e: .tag all и .tag stop только у владельца.

Права: только владелец аккаунта."""


@dataclass(slots=True)
class TagRequest:
    """Разобранная команда: режим (all/rand), сколько случайных и кого исключить."""

    mode: str
    count: int = 0
    exclude: tuple[str, ...] = ()


def parse_tag(args: list[str]) -> TagRequest:
    """Разбирает аргументы после ``.tag``; при ошибке бросает ``UserError``."""
    if not args or args[0].lower() not in ("all", "rand"):
        raise UserError(USAGE)
    mode = args[0].lower()
    rest = args[1:]
    count = 0
    if mode == "rand":
        if not rest or not rest[0].isdigit():
            raise UserError("Укажите, сколько отметить.\n" + USAGE)
        count = int(rest[0])
        if not 1 <= count <= MAX_RAND:
            raise UserError(f"Число должно быть от 1 до {MAX_RAND}.")
        rest = rest[1:]
    exclude: list[str] = []
    if rest:
        if rest[0].lower() not in EXCLUDE_FLAGS:
            raise UserError(USAGE)
        exclude = [item.lstrip("@").lower() for item in rest[1:] if item.lstrip("@")]
        if not exclude:
            raise UserError("После -e укажите, кого пропустить.\n" + USAGE)
        if any(item.startswith("-") for item in exclude):
            raise UserError(USAGE)
    return TagRequest(mode, count, tuple(exclude))


def is_excluded(user, exclude: tuple[str, ...]) -> bool:
    """Подходит ли пользователь под список исключений (по username или ID)."""
    if str(getattr(user, "id", "")) in exclude:
        return True
    username = (getattr(user, "username", None) or "").lower()
    return bool(username) and username in exclude


def mention(user) -> str:
    """Отметка пользователя: ``@username`` или имя со ссылкой на аккаунт (Markdown)."""
    username = getattr(user, "username", None)
    if username:
        return "@" + username
    name = " ".join(
        part for part in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if part
    )
    # В тексте ссылки убираем всё, что ломает разбор Markdown.
    name = clean(name)
    for char in "[]()*_~|":
        name = name.replace(char, "")
    name = name.strip() or "пользователь"
    return f"[{name}](tg://user?id={user.id})"


def chunks(items: list[str], size: int) -> list[list[str]]:
    """Делит список на пачки по ``size`` элементов."""
    return [items[i : i + size] for i in range(0, len(items), size)]


@dataclass(slots=True)
class TagJob:
    """Одна идущая отметка."""

    chat_id: int
    messages: list[str]
    sent: int = 0
    task: asyncio.Task | None = field(default=None, repr=False)


class TagModule(Module):
    """Команда ``.tag``: отметка всех или случайных участников группы."""

    name = "utility.tag"
    description = "Команда .tag: отметить участников группы"
    # Необязательный: без него userbot работает, сбой запуска только пришлёт уведомление.

    def __init__(self, app) -> None:
        super().__init__(app)
        self._jobs: dict[int, TagJob] = {}
        self._sleep = asyncio.sleep  # подменяется в тестах
        self._random = random.Random()

    async def start(self) -> None:
        """Регистрирует команду ``.tag``."""
        self.app.dispatcher.register_command(
            "tag",
            self.tag_command,
            description="отметить участников группы",
            usage=".tag all | rand <число> [-e @юзер ...] | stop",
            priority=30,
            details=DETAILS,
            level=PermissionLevel.TRUSTED,
        )

    async def stop(self) -> None:
        """Снимает команду и останавливает идущие отметки."""
        self.app.dispatcher.unregister_command("tag")
        await self._cancel_all()

    async def tag_command(self, event) -> None:
        """``.tag all|rand ...`` запускает отметку, ``.tag stop`` останавливает."""
        command = self.app.dispatcher.get_command(event)
        args = list(command.args) if command else []
        owner = self.app.dispatcher.caller_level(event) == PermissionLevel.OWNER
        if [item.lower() for item in args] == ["stop"]:
            if not owner:
                await answer(event, "❌ Остановить отметку может только владелец.")
                return
            stopped = await self._cancel_all()
            await answer(event, f"⏹ Остановлено отметок: {stopped}." if stopped else "ℹ️ Сейчас ничего не отмечается.")
            return
        try:
            request = parse_tag(args)
            if not owner:
                if request.mode != "rand" or request.exclude:
                    raise UserError(f"Вам доступно только: .tag rand <число до {MAX_RAND_OTHERS}>")
                if request.count > MAX_RAND_OTHERS:
                    raise UserError(f"Не больше {MAX_RAND_OTHERS} человек за раз.")
            if not getattr(event, "is_group", False):
                raise UserError("Команда работает только в группах.")
            chat_id = int(event.chat_id)
            if chat_id in self._jobs:
                raise UserError("В этом чате уже идёт отметка. Остановить: .tag stop")
            users = await self._members(chat_id, request)
            messages = self._build(users, request)
        except UserError as exc:
            await answer(event, f"❌ {exc}")
            return
        except Exception:
            logger.exception("Ошибка .tag.")
            await answer(event, "❌ Не удалось получить участников. Подробности в логе.")
            return

        job = TagJob(chat_id, messages)
        self._jobs[chat_id] = job
        job.task = asyncio.get_running_loop().create_task(self._run(job))
        if not owner:
            return  # чужое сообщение не трогаем: удалять его вы не вправе, да и команда не ваша
        try:
            await event.delete()  # в чате остаются только отметки
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Не удалось удалить сообщение с командой .tag: %s", exc)

    async def _members(self, chat_id: int, request: TagRequest) -> list:
        """Участники чата без ботов, удалённых аккаунтов, вас самих и исключённых."""
        me = await self.app.client.get_me()
        my_id = getattr(me, "id", None)
        try:
            members = await self.app.client.get_participants(chat_id, limit=FETCH_LIMIT)
        except Exception as exc:
            logger.debug("get_participants: %s", exc)
            raise UserError("Не удалось получить список участников (возможно, нужны права администратора).") from None
        users = [
            user
            for user in members
            if getattr(user, "id", None) != my_id
            and not getattr(user, "bot", False)
            and not getattr(user, "deleted", False)
            and not is_excluded(user, request.exclude)
        ]
        if not users:
            raise UserError("Некого отмечать: подходящих участников нет.")
        return users

    def _build(self, users: list, request: TagRequest) -> list[str]:
        """Тексты сообщений с отметками для выбранного режима."""
        if request.mode == "rand":
            users = self._random.sample(users, min(request.count, len(users)))
        else:
            users = users[:MAX_ALL]
        return [" ".join(group) for group in chunks([mention(user) for user in users], PER_MESSAGE)]

    async def _run(self, job: TagJob) -> None:
        """Отправляет сообщения с отметками по очереди; ошибку сообщает владельцу."""
        try:
            for number, text in enumerate(job.messages):
                if number:
                    await self._sleep(PAUSE)
                await self._send(job, text)
                job.sent += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Ошибка .tag.")
            reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            await self.app.notify_owner(
                f"⚠️ .tag остановлен: отправлено {job.sent} из {len(job.messages)} сообщений. {reason}"
            )
        finally:
            if self._jobs.get(job.chat_id) is job:
                del self._jobs[job.chat_id]

    async def _send(self, job: TagJob, text: str) -> None:
        """Отправляет одно сообщение; на короткий FloodWait ждёт и повторяет."""
        for attempt in range(4):
            try:
                await self.app.client.send_message(job.chat_id, text, parse_mode="md", link_preview=False)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if FloodWaitError is None or not isinstance(exc, FloodWaitError):
                    raise
                seconds = int(getattr(exc, "seconds", 0) or 0)
                if seconds > MAX_FLOOD_WAIT or attempt == 3:
                    raise
                logger.warning(".tag: FloodWait %s с, жду.", seconds)
                await self._sleep(seconds + 1)

    async def _cancel_all(self) -> int:
        """Останавливает все отметки и возвращает, сколько их было."""
        jobs = list(self._jobs.values())
        for job in jobs:
            if job.task is not None:
                job.task.cancel()
        for job in jobs:
            if job.task is not None:
                try:
                    await job.task
                except asyncio.CancelledError:
                    pass
        self._jobs.clear()
        return len(jobs)
