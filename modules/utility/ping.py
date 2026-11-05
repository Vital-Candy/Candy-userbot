"""Команда .ping: настоящий пинг до серверов Telegram.

Меряется время MTProto-запроса ``ping`` (туда и обратно), а не задержка редактирования
сообщения. Берётся середина из нескольких замеров, чтобы один всплеск не искажал результат.
"""
from __future__ import annotations

import random
import statistics
import time

from core.module import Module
from core.reply import answer
from storage.permissions import PermissionLevel
from utils.logger import logger

SAMPLES = 3  # замеров за один вызов

DETAILS = f"""\
Проверяет связь с серверами Telegram: отправляет служебный запрос ping и меряет, за сколько приходит ответ. Показывается среднее из {SAMPLES} замеров (медиана) и разброс.

.ping — измерить пинг

Права: только владелец аккаунта."""


def format_ping(samples_ms: list[float]) -> str:
    """Текст ответа по списку замеров в миллисекундах."""
    median = statistics.median(samples_ms)
    text = f"🏓 Pong!\n⚡ Пинг: `{median:.0f} мс`"
    if len(samples_ms) > 1:
        text += f"\n📊 Лучший `{min(samples_ms):.0f}` / худший `{max(samples_ms):.0f}` мс"
    return text


class PingModule(Module):
    """Команда ``.ping``."""

    name = "utility.ping"
    description = "Команда .ping: пинг до Telegram"
    # Необязательный: без него userbot работает, сбой запуска только пришлёт уведомление.

    def __init__(self, app) -> None:
        super().__init__(app)
        self._clock = time.perf_counter  # подменяется в тестах

    async def start(self) -> None:
        """Регистрирует команду ``.ping``."""
        self.app.dispatcher.register_command(
            "ping",
            self.ping_command,
            description="пинг до Telegram",
            usage=".ping",
            priority=70,
            details=DETAILS,
            level=PermissionLevel.USER,
        )

    async def stop(self) -> None:
        """Снимает регистрацию команды ``.ping``."""
        self.app.dispatcher.unregister_command("ping")

    async def measure(self) -> list[float]:
        """Делает несколько запросов ping и возвращает время каждого в миллисекундах."""
        from telethon.tl.functions import PingRequest

        samples: list[float] = []
        for _ in range(SAMPLES):
            started = self._clock()
            await self.app.client(PingRequest(ping_id=random.getrandbits(63)))
            samples.append((self._clock() - started) * 1000)
        return samples

    async def ping_command(self, event) -> None:
        """``.ping``: заменяет сообщение на pong и время ответа сервера."""
        try:
            text = format_ping(await self.measure())
        except Exception:
            logger.exception("Ошибка .ping.")
            text = "❌ Не удалось измерить пинг. Подробности в логе."
        await answer(event, text)
