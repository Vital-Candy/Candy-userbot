"""Команда .mute: тихо удалять новые сообщения, пока режим включён.

Синтаксис: ``.mute [длительность] [--all]``, ``.mute off``, ``.mute list``.
Режимы (кто чьи сообщения теряет):

* личный чат: удаляются все новые сообщения (и ваши, и собеседника);
* группа без флага: только ваши новые сообщения;
* группа с ``--all``: сообщения всех, кроме ваших (нужны права на удаление, два подтверждения);
* группа, команда ответом на сообщение: сообщения этого пользователя (нужны права на удаление).

Флаг уровня ``-user``, ``-trusted`` или ``-admin`` (для ``--all`` и личного чата) оставляет право писать
тем, у кого выдан этот уровень или выше; их сообщения не удаляются. Без флага писать может только владелец.

Остановка: ответить на сообщение с командой словом ``stop`` (или «стоп»). Режимы сохраняются в
базе и возобновляются после перезапуска userbot. Сообщения удаляются пачками (до 100 за запрос),
а наблюдатель за сообщениями подключён, только пока есть активный режим.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from core.errors import UserError
from core.module import Module
from core.reply import answer
from modules.utility.purge import can_delete_others
from utils.logger import logger

try:
    from telethon.errors import FloodWaitError
except ImportError:  # Telethon не установлен
    FloodWaitError = None

PLUGIN = "utility.mute"
KEY = "active"
MIN_SECONDS = 5
MAX_SECONDS = 365 * 86400
FLUSH_DELAY = 0.2  # сбор сообщений в пачку перед удалением
CHUNK = 100
CONFIRM_TTL = 120  # секунд на каждое подтверждение
MAX_FLOOD_WAIT = 60
MAX_FAILURES = 3  # подряд неудачных удалений до остановки режима
STOP_WORDS = frozenset({"stop", "стоп"})
CANCEL_WORDS = frozenset({"stop", "стоп", "no", "нет", "cancel", "отмена"})
FIRST_WORDS = frozenset({"да", "yes"})
SECOND_WORDS = frozenset({"подтверждаю", "confirm"})
LEVEL_FLAGS = {f"{dashes}{name}": name for name in ("user", "trusted", "admin") for dashes in ("-", "--")}
USAGE = "Использование: .mute [длительность] [--all] [-user|-trusted|-admin] | .mute off | .mute list"

DETAILS = f"""\
Тихо удаляет новые сообщения в чате, пока режим включён. Сообщение с командой остаётся в чате как «пульт»: на него отвечают, чтобы остановить режим.

.mute — в личном чате удаляет все новые сообщения (и ваши, и собеседника); в группе только ваши
.mute <длительность> — то же на время: 30s, 10m, 2h, 1d, 1h30m (число без буквы считается минутами; от {MIN_SECONDS} секунд до 365 дней). Без длительности режим работает, пока его не остановят
.mute --all — в группе удалять сообщения всех, кроме ваших. Проверяются права на удаление, затем нужно дважды подтвердить ответом на сообщение с командой: сначала да, потом подтверждаю
.mute --all -trusted — то же, но писать могут ещё и те, кому вы выдали уровень trusted или выше (admin тоже). Так же работают -user и -admin; можно и в личном чате. Кто пишет ниже указанного уровня или без выданного уровня, того сообщения удаляются. Без флага писать может только владелец
.mute (ответом на сообщение пользователя в группе) — удалять все новые сообщения именно этого пользователя; нужны права на удаление
.mute off — остановить режим в этом чате
.mute list — показать все активные режимы

Остановка: ответьте на сообщение с командой словом stop (или стоп). Сообщение с командой и ваш ответ удалятся. Режим сохраняется и после перезапуска userbot. По окончании или при ошибке (например, пропали права) в «Избранное» приходит сводка с числом удалённых сообщений. В каналах команда не работает. Удалённое вернуть нельзя.

Права: только владелец аккаунта."""

_UNITS = {
    "s": 1, "sec": 1, "с": 1, "сек": 1,
    "m": 60, "min": 60, "м": 60, "мин": 60,
    "h": 3600, "hr": 3600, "ч": 3600, "час": 3600,
    "d": 86400, "day": 86400, "д": 86400, "дн": 86400,
}


@dataclass(slots=True, frozen=True)
class MuteRequest:
    """Разобранная команда: действие, длительность (None: бессрочно), ``--all`` и уровень «кому можно писать»."""

    action: str  # "start", "off", "list"
    seconds: int | None = None
    everyone: bool = False
    allow: str | None = None  # "user", "trusted", "admin" или None: писать может только владелец


def parse_duration(tokens: list[str]) -> int:
    """Складывает токены длительности (``1h``, ``30m``, ``10``); при ошибке бросает ``UserError``."""
    total = 0
    for token in tokens:
        token = token.lower()
        parts = re.findall(r"(\d+)([a-zа-яё]*)", token)
        if not parts or "".join(number + unit for number, unit in parts) != token:
            raise UserError(f"Не понял длительность «{token}». Примеры: 30s, 10m, 2h, 1d, 1h30m.")
        for number, unit in parts:
            if unit and unit not in _UNITS:
                raise UserError(f"Неизвестная единица «{unit}». Допустимы s, m, h, d (с, м, ч, д).")
            total += int(number) * _UNITS.get(unit, 60)
    if not MIN_SECONDS <= total <= MAX_SECONDS:
        raise UserError(f"Длительность от {MIN_SECONDS} секунд до 365 дней.")
    return total


def parse_mute(args: list[str]) -> MuteRequest:
    """Разбирает аргументы команды; при ошибке бросает ``UserError``."""
    lowered = [item.lower() for item in args]
    if lowered == ["off"]:
        return MuteRequest("off")
    if lowered == ["list"]:
        return MuteRequest("list")
    everyone = "--all" in lowered
    levels = [LEVEL_FLAGS[item] for item in lowered if item in LEVEL_FLAGS]
    rest = [item for item in args if item.lower() != "--all" and item.lower() not in LEVEL_FLAGS]
    if lowered.count("--all") > 1 or len(levels) > 1:
        raise UserError(USAGE)
    if any(item.startswith("-") for item in rest) or any(item.lower() in {"off", "list"} for item in rest):
        raise UserError(USAGE)
    seconds = parse_duration(rest) if rest else None
    return MuteRequest("start", seconds, everyone, levels[0] if levels else None)


def format_left(seconds: float | None) -> str:
    """Человекочитаемый остаток времени (``1 ч 5 мин``) или «пока не остановят»."""
    if seconds is None:
        return "пока не остановят"
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {secs} с"
    return f"{secs} с"


@dataclass(slots=True)
class Mute:
    """Активный режим в одном чате."""

    chat_id: int
    anchor_id: int  # сообщение с командой: на него отвечают, чтобы остановить
    mode: str  # "chat" (личный чат), "own", "all" или "user"
    target_id: int | None = None
    label: str = ""
    until: float | None = None  # время окончания по time.time(); None: бессрочно
    deleted: int = 0
    allow: str | None = None  # уровень, начиная с которого писать можно ("user"/"trusted"/"admin")
    failures: int = 0
    timer: asyncio.Task | None = field(default=None, repr=False)

    def as_dict(self) -> dict:
        """Запись для базы (без таймера и счётчика ошибок)."""
        return {
            "chat_id": self.chat_id, "anchor_id": self.anchor_id, "mode": self.mode,
            "target_id": self.target_id, "label": self.label, "until": self.until, "deleted": self.deleted,
            "allow": self.allow,
        }


@dataclass(slots=True)
class Pending:
    """Режим ``--all``, ожидающий подтверждений."""

    chat_id: int
    anchor_id: int
    seconds: int | None
    allow: str | None = None
    stage: int = 1
    timer: asyncio.Task | None = field(default=None, repr=False)


SCOPE_TEXT = {
    "chat": "все сообщения в чате",
    "own": "только ваши сообщения",
    "all": "сообщения всех участников, кроме ваших",
}


def scope_text(mute: "Mute") -> str:
    """Описание режима для ответов; с флагом уровня добавляет, кому можно писать."""
    who = f" ({mute.label})" if mute.label else ""
    text = SCOPE_TEXT.get(mute.mode, f"сообщения пользователя{who}")
    if mute.allow and mute.mode in {"chat", "all"}:
        text += f"; писать могут {mute.allow} и выше"
    return text


class MuteModule(Module):
    """Команда ``.mute``: режим тихого удаления новых сообщений."""

    name = "utility.mute"
    description = "Команда .mute: тихо удалять новые сообщения"
    # Необязательный: без него userbot работает, сбой запуска только пришлёт уведомление.

    def __init__(self, app) -> None:
        super().__init__(app)
        self._active: dict[int, Mute] = {}
        self._pending: dict[int, Pending] = {}
        self._queues: dict[int, list[int]] = {}
        self._flushers: dict[int, asyncio.Task] = {}
        self._handler = None
        self._account_id: int | None = None
        self._sleep = asyncio.sleep  # подменяется в тестах
        self._now = time.time  # подменяется в тестах

    # ------------------------------------------------------------- запуск

    async def start(self) -> None:
        """Регистрирует команду и возобновляет сохранённые режимы."""
        self._account_id = await self.app.identity.account_id()
        self.app.dispatcher.register_command(
            "mute",
            self.mute_command,
            description="тихо удалять новые сообщения (stop: остановить)",
            usage=".mute [длительность] [--all] [-user|-trusted|-admin] | off | list",
            priority=25,
            details=DETAILS,
        )
        await self._restore()

    async def stop(self) -> None:
        """Снимает команду и все таймеры; сохранённые режимы остаются для следующего запуска."""
        self.app.dispatcher.unregister_command("mute")
        tasks = [item.timer for item in (*self._active.values(), *self._pending.values()) if item.timer]
        tasks += list(self._flushers.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._active.clear()
        self._pending.clear()
        self._queues.clear()
        self._flushers.clear()
        self._release_handler()

    async def _restore(self) -> None:
        """Возобновляет режимы, сохранённые до перезапуска (истёкшие отбрасываются)."""
        saved = self.app.storage.plugin_data.get(account_id=self._account_id, plugin=PLUGIN, key=KEY) or []
        restored = 0
        for record in saved if isinstance(saved, list) else []:
            try:
                mute = Mute(
                    int(record["chat_id"]), int(record["anchor_id"]), str(record["mode"]),
                    record.get("target_id"), str(record.get("label") or ""), record.get("until"),
                    int(record.get("deleted") or 0), allow=record.get("allow"),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if mute.mode not in {"chat", "own", "all", "user"}:
                continue
            if mute.allow not in (None, *LEVEL_FLAGS.values()):
                continue
            if mute.until is not None and mute.until <= self._now():
                continue
            self._begin(mute)
            restored += 1
        self._save()
        if restored:
            await self.app.notify_owner(f"🔇 .mute: возобновлено режимов после запуска: {restored}. Список: .mute list")

    # -------------------------------------------------------------- команда

    async def mute_command(self, event) -> None:
        """``.mute [длительность] [--all]``, ``.mute off``, ``.mute list``."""
        command = self.app.dispatcher.get_command(event)
        try:
            request = parse_mute(list(command.args) if command else [])
            if request.action == "list":
                await answer(event, self._list_text())
            elif request.action == "off":
                await self._off(event)
            else:
                await self._start(event, request)
        except UserError as exc:
            await answer(event, f"❌ {exc}")

    def _list_text(self) -> str:
        """Текст ``.mute list``: все активные режимы и ожидающие подтверждения."""
        if not self._active and not self._pending:
            return "ℹ️ Активных режимов .mute нет."
        lines = ["🔇 Режимы .mute:"]
        for mute in self._active.values():
            left = None if mute.until is None else mute.until - self._now()
            scope = scope_text(mute)
            lines.append(f"чат {mute.chat_id}: {scope}, {format_left(left)}, удалено {mute.deleted}")
        for pending in self._pending.values():
            lines.append(f"чат {pending.chat_id}: ждёт подтверждения (шаг {pending.stage} из 2)")
        return "\n".join(lines)

    async def _off(self, event) -> None:
        """``.mute off``: останавливает режим в этом чате; итог приходит в «Избранное», чтобы не светиться в чате."""
        chat_id = getattr(event, "chat_id", None)
        pending = self._pending.pop(chat_id, None)
        if pending is not None:
            self._cancel_timer(pending)
            self._release_handler()
        mute = self._active.get(chat_id)
        if mute is None and pending is None:
            await answer(event, "ℹ️ В этом чате нет активного режима .mute.")
            return
        deleted = 0
        if mute is not None:
            await self._finish(mute, reason="остановлен командой off", quiet=True)
            deleted = mute.deleted
        await self._delete(int(chat_id), [event.id] + ([pending.anchor_id] if pending else []))
        text = f"✅ .mute в чате {chat_id} остановлен, удалено сообщений: {deleted}." if mute else (
            f"✅ .mute в чате {chat_id}: ожидание подтверждения отменено."
        )
        await self.app.notify_owner(text)

    async def _start(self, event, request: MuteRequest) -> None:
        """Определяет режим по чату и ответу, проверяет права и запускает (или просит подтверждение)."""
        chat_id = getattr(event, "chat_id", None)
        anchor_id = getattr(event, "id", None)
        if chat_id is None or anchor_id is None:
            raise UserError("Не удалось определить чат.")
        chat_id, anchor_id = int(chat_id), int(anchor_id)
        if getattr(event, "is_channel", False) and not getattr(event, "is_group", False):
            raise UserError("В каналах .mute не работает: только в личных чатах и группах.")
        if chat_id in self._active or chat_id in self._pending:
            raise UserError("В этом чате режим уже включён или ждёт подтверждения. Остановить: .mute off")
        private = bool(getattr(event, "is_private", False))
        replied = getattr(event, "reply_to_msg_id", None)
        if request.allow and not (private or request.everyone):
            raise UserError("Флаг уровня нужен с --all или в личном чате: в других режимах чужие сообщения не удаляются.")
        if request.allow and replied:
            raise UserError("Флаг уровня и ответ на сообщение нельзя совмещать.")
        if private and request.everyone:
            raise UserError("Флаг --all нужен только в группах: в личном чате и так удаляется всё.")
        if request.everyone and replied:
            raise UserError("--all и ответ на сообщение нельзя совмещать: выберите что-то одно.")

        mute = Mute(chat_id, anchor_id, "chat" if private else "own", allow=request.allow)
        if request.seconds is not None:
            mute.until = self._now() + request.seconds
        if not private and request.everyone:
            await self._check_rights(chat_id)
            await self._ask_confirmation(event, chat_id, anchor_id, request.seconds, request.allow)
            return
        if not private and replied:
            await self._target_from_reply(event, mute)
            if mute.mode == "user":
                await self._check_rights(chat_id)
        await self._activate(mute)

    async def _target_from_reply(self, event, mute: Mute) -> None:
        """Режим «сообщения одного пользователя»: цель берётся из сообщения, на которое ответили."""
        replied = await event.get_reply_message()
        sender_id = getattr(replied, "sender_id", None)
        if sender_id is None:
            raise UserError("У этого сообщения нет автора (анонимный админ или канал): выберите другое.")
        me = await self.app.client.get_me()
        if sender_id == getattr(me, "id", None):
            return  # свои сообщения: режим «own»
        mute.mode = "user"
        mute.target_id = int(sender_id)
        try:
            sender = await replied.get_sender()
        except Exception:
            sender = None
        username = getattr(sender, "username", None)
        mute.label = f"@{username}" if username else str(sender_id)

    async def _check_rights(self, chat_id: int) -> None:
        """Нужны права на удаление чужих сообщений; иначе ``UserError`` с объяснением."""
        try:
            permissions = await self.app.client.get_permissions(chat_id, "me")
        except Exception as exc:
            logger.debug("get_permissions: %s", exc)
            raise UserError("Не удалось проверить права в этом чате. Ничего не включено.") from None
        if not can_delete_others(permissions):
            raise UserError("Нет прав на удаление чужих сообщений в этом чате. Ничего не включено.")

    # ------------------------------------------------------- подтверждения

    async def _ask_confirmation(
        self, event, chat_id: int, anchor_id: int, seconds: int | None, allow: str | None = None
    ) -> None:
        """Первое подтверждение режима ``--all``: текст появляется на месте команды."""
        pending = Pending(chat_id, anchor_id, seconds, allow)
        self._pending[chat_id] = pending
        pending.timer = asyncio.get_running_loop().create_task(self._confirm_timeout(pending))
        self._ensure_handler()
        await answer(
            event,
            "⚠️ Режим --all удаляет ВСЕ новые сообщения ВСЕХ участников этой группы"
            + (f" (кроме вас и тех, у кого уровень {allow} или выше).\n" if allow else " (кроме ваших).\n") +
            "Подтверждение 1 из 2: ответьте на это сообщение словом да. Отмена: ответ стоп.",
        )

    async def _confirm_timeout(self, pending: Pending) -> None:
        """Отменяет подтверждение, если ответа не было вовремя."""
        try:
            await self._sleep(CONFIRM_TTL)
        except asyncio.CancelledError:
            raise
        if self._pending.get(pending.chat_id) is pending:
            del self._pending[pending.chat_id]
            self._release_handler()
            await self._edit(pending.chat_id, pending.anchor_id, "⌛ Время подтверждения вышло. Режим .mute не включён.")

    async def _on_confirm(self, pending: Pending, event, text: str) -> None:
        """Обрабатывает ответ на сообщение с запросом подтверждения."""
        if text in CANCEL_WORDS:
            del self._pending[pending.chat_id]
            self._cancel_timer(pending)
            self._release_handler()
            await self._delete(pending.chat_id, [event.id, pending.anchor_id])
            return
        if pending.stage == 1 and text in FIRST_WORDS:
            pending.stage = 2
            self._restart_confirm_timer(pending)
            await self._delete(pending.chat_id, [event.id])
            await self._edit(
                pending.chat_id, pending.anchor_id,
                "⚠️ Подтверждение 2 из 2, последнее. Режим удалит сообщения всех, вернуть их нельзя.\n"
                "Ответьте на это сообщение словом подтверждаю. Отмена: ответ стоп.",
            )
        elif pending.stage == 2 and text in SECOND_WORDS:
            del self._pending[pending.chat_id]
            self._cancel_timer(pending)
            await self._delete(pending.chat_id, [event.id])
            mute = Mute(pending.chat_id, pending.anchor_id, "all", allow=pending.allow)
            if pending.seconds is not None:
                mute.until = self._now() + pending.seconds
            await self._activate(mute)

    def _restart_confirm_timer(self, pending: Pending) -> None:
        """Даёт новое время на второе подтверждение."""
        self._cancel_timer(pending)
        pending.timer = asyncio.get_running_loop().create_task(self._confirm_timeout(pending))

    # -------------------------------------------------------- активный режим

    async def _activate(self, mute: Mute) -> None:
        """Включает режим, сохраняет его и обновляет сообщение-«пульт»."""
        self._begin(mute)
        self._save()
        scope = scope_text(mute)
        left = None if mute.until is None else mute.until - self._now()
        await self._edit(
            mute.chat_id, mute.anchor_id,
            f"🔇 mute: {scope}, {format_left(left)}. Остановить: ответьте на это сообщение словом stop",
        )

    def _begin(self, mute: Mute) -> None:
        """Запускает наблюдение и таймер окончания (без сохранения)."""
        self._active[mute.chat_id] = mute
        if mute.until is not None:
            mute.timer = asyncio.get_running_loop().create_task(self._expire(mute))
        self._ensure_handler()

    async def _expire(self, mute: Mute) -> None:
        """Ждёт окончания срока и останавливает режим."""
        try:
            await self._sleep(max(0.0, mute.until - self._now()))
        except asyncio.CancelledError:
            raise
        await self._finish(mute, reason="срок вышел")

    async def _finish(self, mute: Mute, *, reason: str, quiet: bool = False, extra_ids=()) -> None:
        """Останавливает режим: удаляет остаток очереди, «пульт» и лишние сообщения, шлёт сводку."""
        if self._active.get(mute.chat_id) is not mute:
            return
        del self._active[mute.chat_id]
        if mute.timer is not None and mute.timer is not asyncio.current_task():
            mute.timer.cancel()
        await self._flush_now(mute)
        await self._delete(mute.chat_id, [mute.anchor_id, *extra_ids])
        self._save()
        self._release_handler()
        if not quiet:
            await self.app.notify_owner(
                f"🔇 .mute в чате {mute.chat_id} остановлен ({reason}). Удалено сообщений: {mute.deleted}."
            )

    # ---------------------------------------------------------- наблюдатель

    def _ensure_handler(self) -> None:
        """Подключает наблюдателя за сообщениями (только пока есть режимы)."""
        if self._handler is None:
            self._handler = self.app.dispatcher.register("new_message", self._on_message, priority=1000, name="mute.watch")

    def _release_handler(self) -> None:
        """Отключает наблюдателя, когда режимов и подтверждений не осталось."""
        if self._handler is not None and not self._active and not self._pending:
            self.app.dispatcher.unregister(self._handler)
            self._handler = None

    async def _on_message(self, event) -> None:
        """Новое сообщение: подтверждение, остановка или постановка на удаление."""
        chat_id = getattr(event, "chat_id", None)
        mute = self._active.get(chat_id)
        pending = self._pending.get(chat_id)
        if mute is None and pending is None:
            return
        out = bool(getattr(event, "out", False))
        text = (getattr(event, "raw_text", "") or "").strip().lower()
        replied = getattr(event, "reply_to_msg_id", None)
        if pending is not None and out and replied == pending.anchor_id:
            await self._on_confirm(pending, event, text)
            return
        if mute is None or event.id <= mute.anchor_id:
            return
        if out and replied == mute.anchor_id and text in STOP_WORDS:
            await self._finish(mute, reason="остановлен ответом stop", quiet=True, extra_ids=[event.id])
            return
        if out and text.split(" ", 1)[0] == self.app.dispatcher.parser.prefix + "mute":
            return  # управление режимом не удаляем
        if await self._matches(mute, event, out):
            self._enqueue(mute, event.id)

    async def _allowed_to_write(self, mute: Mute, event) -> bool:
        """Может ли автор писать: уровень не ниже выбранного флагом. Ошибка проверки означает «нельзя»."""
        if mute.allow is None:
            return False
        sender = getattr(event, "sender_id", None)
        access = getattr(self.app, "access", None)
        if sender is None or access is None:
            return False
        try:
            return await access.has_level(int(sender), mute.allow)
        except Exception:
            logger.exception(".mute: не удалось проверить уровень %s.", sender)
            return False

    async def _matches(self, mute: Mute, event, out: bool) -> bool:
        """Подлежит ли это сообщение удалению в данном режиме."""
        if mute.mode == "all":
            return not out and not await self._allowed_to_write(mute, event)
        if mute.mode == "chat":
            return out or not await self._allowed_to_write(mute, event)
        if mute.mode == "own":
            return out
        return getattr(event, "sender_id", None) == mute.target_id

    def _enqueue(self, mute: Mute, message_id: int) -> None:
        """Добавляет сообщение в пачку на удаление; пачка уходит через короткую паузу."""
        self._queues.setdefault(mute.chat_id, []).append(message_id)
        if mute.chat_id not in self._flushers:
            self._flushers[mute.chat_id] = asyncio.get_running_loop().create_task(self._flush(mute))

    async def _flush(self, mute: Mute) -> None:
        """Удаляет накопленные сообщения пачками по 100; при повторных сбоях останавливает режим."""
        try:
            await self._sleep(FLUSH_DELAY)
            queue = self._queues.get(mute.chat_id, [])
            while queue:
                ids = queue[:CHUNK]
                del queue[:CHUNK]
                if await self._delete_counted(mute, ids) is False:
                    break
        except asyncio.CancelledError:
            raise
        finally:
            self._flushers.pop(mute.chat_id, None)
        if mute.failures >= MAX_FAILURES and self._active.get(mute.chat_id) is mute:
            self._queues.pop(mute.chat_id, None)
            await self._finish(mute, reason="не удаётся удалять сообщения (проверьте права)")

    async def _flush_now(self, mute: Mute) -> None:
        """Сразу удаляет всё, что осталось в очереди чата (перед остановкой режима)."""
        task = self._flushers.pop(mute.chat_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        queue = self._queues.pop(mute.chat_id, [])
        for start in range(0, len(queue), CHUNK):
            await self._delete_counted(mute, queue[start:start + CHUNK])

    async def _delete_counted(self, mute: Mute, ids: list[int]) -> bool:
        """Удаляет пачку и считает результат; возвращает False при неудаче."""
        if await self._delete(mute.chat_id, ids):
            mute.deleted += len(ids)
            mute.failures = 0
            return True
        mute.failures += 1
        return False

    # ----------------------------------------------------------- вспомогательное

    async def _delete(self, chat_id: int, ids: list[int]) -> bool:
        """Удаляет сообщения; на короткий FloodWait ждёт. Ошибки только в лог, результат: True/False."""
        for attempt in range(4):
            try:
                await self.app.client.delete_messages(chat_id, ids)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if FloodWaitError is not None and isinstance(exc, FloodWaitError):
                    seconds = int(getattr(exc, "seconds", 0) or 0)
                    if seconds <= MAX_FLOOD_WAIT and attempt < 3:
                        logger.warning(".mute: FloodWait %s с, жду.", seconds)
                        await self._sleep(seconds + 1)
                        continue
                logger.warning(".mute: не удалось удалить сообщения в чате %s: %s", chat_id, type(exc).__name__)
                return False
        return False

    async def _edit(self, chat_id: int, message_id: int, text: str) -> None:
        """Меняет текст сообщения-«пульта»; ошибки только в лог."""
        try:
            await self.app.client.edit_message(chat_id, message_id, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(".mute: не удалось изменить сообщение: %s", exc)

    @staticmethod
    def _cancel_timer(item) -> None:
        """Отменяет таймер записи (если он ещё идёт)."""
        if item.timer is not None and item.timer is not asyncio.current_task():
            item.timer.cancel()
        item.timer = None

    def _save(self) -> None:
        """Сохраняет активные режимы в базе (для возобновления после перезапуска)."""
        records = [mute.as_dict() for mute in self._active.values()]
        plugin_data = self.app.storage.plugin_data
        if records:
            plugin_data.set(account_id=self._account_id, plugin=PLUGIN, key=KEY, value=records)
        else:
            plugin_data.delete(account_id=self._account_id, plugin=PLUGIN, key=KEY)
