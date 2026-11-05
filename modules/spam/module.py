"""Модуль команды .spam: отправка одного сообщения несколько раз с паузой.

Синтаксис: ``.spam "<текст>" [повторов] [-d пауза]`` и ``.spam stop``. Разбор: modules/spam/parser.py.
Команда выполняется в фоне (отправка может идти минуты), поэтому её можно остановить.
Вызывает владелец аккаунта.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from core.errors import UserError
from core.module import Module
from core.reply import answer
from modules.spam.parser import (
    DEFAULT_DELAY,
    MAX_COUNT,
    MIN_DELAY,
    SpamRequest,
    parse_spam,
)
from utils.logger import logger

try:
    from telethon.errors import FloodWaitError
except ImportError:  # Telethon не установлен
    FloodWaitError = None

MAX_FLOOD_RETRIES = 3  # сколько раз подряд ждём FloodWait на одном сообщении
MAX_FLOOD_WAIT = 300  # дольше этого FloodWait не ждём, останавливаем отправку (секунды)

DETAILS = f"""\
Отправляет одно и то же сообщение несколько раз с паузой. Сама команда после запуска удаляется, остаются только отправленные сообщения.

.spam "<текст>" — отправить один раз
.spam "<текст>" <повторов> — повторить (от 1 до {MAX_COUNT})
.spam "<текст>" <повторов> -d <пауза> — пауза между сообщениями в секундах (по умолчанию {DEFAULT_DELAY:g}, минимум {MIN_DELAY:g})
.spam stop — остановить все запущенные отправки

Текст берётся между первой и последней кавычкой, кавычки внутри текста не мешают. Пример: .spam "скажи "привет"" 3 -d 0.5 отправит скажи "привет" три раза с паузой полсекунды. Текст уходит как есть, без форматирования.
В одном чате одновременно идёт одна отправка. Если Telegram просит подождать (FloodWait), пауза делается сама; при долгом ожидании или ошибке отправка останавливается и вы получите уведомление в «Избранное».

Права: только владелец аккаунта."""


@dataclass(slots=True)
class SpamJob:
    """Одна идущая отправка."""

    chat_id: int
    request: SpamRequest
    sent: int = 0
    task: asyncio.Task | None = field(default=None, repr=False)


class SpamModule(Module):
    """Команда ``.spam``: повтор сообщения с паузой, остановка через ``.spam stop``."""

    name = "spam.spam"
    description = "Команда .spam: повтор сообщения с паузой"
    # Необязательный: без него userbot работает, сбой запуска только пришлёт уведомление.

    def __init__(self, app) -> None:
        super().__init__(app)
        self._jobs: dict[int, SpamJob] = {}  # chat_id → отправка
        self._sleep = asyncio.sleep  # подменяется в тестах

    async def start(self) -> None:
        """Регистрирует команду ``.spam``."""
        self.app.dispatcher.register_command(
            "spam",
            self.spam_command,
            description="повтор сообщения с паузой",
            usage='.spam "<текст>" [повторов] [-d пауза] | .spam stop',
            priority=40,
            details=DETAILS,
        )

    async def stop(self) -> None:
        """Снимает команду и останавливает все идущие отправки."""
        self.app.dispatcher.unregister_command("spam")
        await self._cancel_all()

    # ------------------------------------------------------------- команда

    async def spam_command(self, event) -> None:
        """``.spam "<текст>" [N] [-d t]`` запускает отправку, ``.spam stop`` останавливает."""
        command = self.app.dispatcher.get_command(event)
        if command is None:
            return
        # Берём исходный текст, а не разобранные аргументы: в тексте могут быть пробелы и кавычки.
        rest = command.raw[len(self.app.dispatcher.parser.prefix):].strip()[len(command.name):].strip()
        if rest.lower() == "stop":
            stopped = await self._cancel_all()
            await answer(event, f"⏹ Остановлено отправок: {stopped}." if stopped else "ℹ️ Сейчас ничего не отправляется.")
            return
        try:
            request = parse_spam(rest)
            chat_id = getattr(event, "chat_id", None)
            if chat_id is None:
                raise UserError("Не удалось определить чат.")
            if chat_id in self._jobs:
                raise UserError("В этом чате уже идёт отправка. Остановить: .spam stop")
        except UserError as exc:
            await answer(event, f"❌ {exc}")
            return

        job = SpamJob(int(chat_id), request)
        self._jobs[job.chat_id] = job
        job.task = asyncio.get_running_loop().create_task(self._run(job))
        # Команду убираем, чтобы в чате остались только сами сообщения.
        try:
            await event.delete()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Не удалось удалить сообщение с командой .spam: %s", exc)

    # ------------------------------------------------------------ отправка

    async def _run(self, job: SpamJob) -> None:
        """Отправляет сообщения по одному с паузой; ошибки сообщает владельцу."""
        request = job.request
        try:
            for number in range(request.count):
                if number:
                    await self._sleep(request.delay)
                await self._send(job)
                job.sent += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Ошибка .spam.")
            reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            await self.app.notify_owner(
                f"⚠️ .spam остановлен: отправлено {job.sent} из {request.count}. {reason}"
            )
        finally:
            if self._jobs.get(job.chat_id) is job:
                del self._jobs[job.chat_id]

    async def _send(self, job: SpamJob) -> None:
        """Отправляет одно сообщение; на короткий FloodWait ждёт и повторяет."""
        for attempt in range(MAX_FLOOD_RETRIES + 1):
            try:
                # parse_mode=None: текст уходит как есть, без Markdown.
                await self.app.client.send_message(job.chat_id, job.request.text, parse_mode=None)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if FloodWaitError is None or not isinstance(exc, FloodWaitError):
                    raise
                seconds = int(getattr(exc, "seconds", 0) or 0)
                if seconds > MAX_FLOOD_WAIT or attempt == MAX_FLOOD_RETRIES:
                    raise
                logger.warning(".spam: FloodWait %s с, жду.", seconds)
                await self._sleep(seconds + 1)

    async def _cancel_all(self) -> int:
        """Останавливает все отправки и возвращает, сколько их было."""
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
