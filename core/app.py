"""Главное приложение ``UserbotApp``: владеет сервисами и управляет запуском и остановкой.

Связывает аккаунты, Telethon-клиент, диспетчер, модули, плагины и консоль; вызывается из ``main``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from telethon import TelegramClient

from accounts.account import Account
from accounts.auth import AuthManager
from accounts.backup import BackupManager
from accounts.control import AccountControl
from accounts.manager import AccountManager
from config.settings import (
    ACCOUNTS_DIR,
    COMMAND_TIMEOUT,
    DATABASE_PATH,
    HANDLER_TIMEOUT,
    BASE_DIR,
    DATA_DIR,
    STOP_TIMEOUT,
    UPDATE_BRANCH,
    UPDATE_REPO,
    ensure_directories,
)
from core.access import AccessService
from core.policy import AccessPolicy
from core.background import BackgroundManager
from core.client import create_client
from core.console import ConsoleManager
from core.dispatcher import Dispatcher
from core.errors import UserError
from core.events import EventBridge
from core.health import HealthReport, build_health
from core.identity import IdentityService
from core.lifecycle import FAILED, RUNNING, STARTING, STOPPED, STOPPING, Lifecycle
from core.modules import ModuleManager
from core.plugins import PluginManager
from core.shutdown import ShutdownSequence
from core.summary import failed_summary
from core.commands import register_commands
from modules.system.loader import register_system_modules
from storage.service import Storage
from utils.logger import logger
from utils.prompt import ask

if TYPE_CHECKING:  # updater тянет urllib и zipfile (около 4 МБ ОЗУ): грузим только при обновлении
    from core.updater import UpdateInfo, UpdateResult, Updater


# Сколько секунд ждём отправки уведомления владельцу, прежде чем отказаться от него.
NOTIFY_TIMEOUT = 15.0


class UserbotApp:
    """Главное приложение Candy Userbot.

    API ID/Hash не являются глобальной настройкой приложения.
    Они принадлежат Telegram-аккаунту и сохраняются в его profile.json.
    При первом запуске аккаунт добавляется через интерактивный onboarding.
    """

    def __init__(self) -> None:
        """Создаёт каталоги данных и все сервисы. Сеть не трогает: подключение идёт в ``start``."""
        ensure_directories()

        self.accounts = AccountManager(ACCOUNTS_DIR)
        self.auth = AuthManager(self.accounts)
        self.backups = BackupManager(self.accounts)
        self.account_control = AccountControl(self.accounts, self.backups)
        self.account: Account | None = None
        self.client: TelegramClient | None = None

        self.dispatcher = Dispatcher(
            command_prefix=".",
            handler_timeout=HANDLER_TIMEOUT,
            command_timeout=COMMAND_TIMEOUT,
        )
        self.events: EventBridge | None = None
        self.background = BackgroundManager(on_failing=self._task_failing)
        self._notify_tasks: set[asyncio.Task] = set()  # ссылки, чтобы уведомления не собрал сборщик мусора
        self.identity: IdentityService | None = None
        self.access: AccessService | None = None
        self.policy: AccessPolicy | None = None
        self.storage = Storage(DATABASE_PATH)
        self.modules = ModuleManager(self)
        self.plugins = PluginManager(self, Path(__file__).resolve().parent.parent / "plugins")
        self.console = ConsoleManager(self)
        self.lifecycle = Lifecycle()
        self._update_lock = asyncio.Lock()
        self._stop_task: asyncio.Future[None] | None = None
        self.restart_requested = False

    async def _ensure_account(self) -> Account:
        """Возвращает активный аккаунт; если аккаунтов нет, запускает первичный вход."""
        self.accounts.load()
        active = self.accounts.get_active()
        if active is not None:
            return active

        logger.info("Аккаунтов ещё нет. Запускаем добавление первого аккаунта.")
        print("\n=== Первый запуск Candy Userbot ===")
        return await self.onboard_account()

    async def onboard_account(self, *, confirm_replace: bool = False) -> Account:
        """Интерактивный вход в Telegram-аккаунт и сохранение его профиля.

        ``confirm_replace=True``: если аккаунт уже сохранён, спросить, заменить ли сессию.
        """
        print("Сейчас нужно один раз указать API ID и API Hash Telegram.")
        print("Их можно получить на https://my.telegram.org/apps\n")

        api_id = await self._read_int("API ID: ")
        api_hash = await self._read_secret("API Hash: ")
        phone = await self._read_value("Номер телефона (+...): ")

        async def ask_replace(account_id: int) -> bool:
            """Спрашивает, заменять ли уже сохранённую сессию аккаунта."""
            answer = await ask(f"Аккаунт {account_id} уже сохранён. Заменить его сессию? [y/N]: ")
            return answer.strip().lower() in {"y", "yes", "д", "да"}

        account = await self.auth.add_account(
            api_id=api_id,
            api_hash=api_hash,
            phone=phone,
            code_reader=lambda: self._read_value("Код из Telegram: "),
            password_reader=lambda: self._read_value("Пароль 2FA: ", secret=True),
            confirm_replace=ask_replace if confirm_replace else None,
        )
        self.accounts.set_active(account)
        print(f"\n✓ Аккаунт {account.display_name} добавлен и выбран активным.\n")
        return account

    async def cli_add_account(self) -> None:
        """``python main.py --add-account``: добавить аккаунт или войти заново."""
        self.accounts.load()
        try:
            await self.onboard_account(confirm_replace=True)
        except FileExistsError as exc:
            raise UserError(f"{exc}. Замена отменена.") from None

    async def cli_restore(self, source: str) -> None:
        """``python main.py --restore FILE``: восстановить аккаунт из backup."""
        self.accounts.load()
        account = self.backups.restore(source)
        if self.accounts.active_account_id is None or len(self.accounts.list()) == 1:
            self.accounts.set_active(account)
        print(f"✓ Аккаунт {account.display_name} восстановлен из backup.")

    def _updater(self) -> Updater:
        """Свежий ``Updater`` с текущими настройками репозитория и ветки."""
        from core.updater import Updater

        return Updater(BASE_DIR, DATA_DIR, repo=UPDATE_REPO, branch=UPDATE_BRANCH)

    async def check_update(self) -> UpdateInfo:
        """Сравнивает версию с GitHub. Ошибки: UpdateError (текст для пользователя)."""
        return await asyncio.to_thread(self._updater().check)

    async def install_update(self, info: UpdateInfo | None = None) -> UpdateResult:
        """Устанавливает обновление (один за раз). Перезапуск вызывает тот, кто позвал."""
        if self._update_lock.locked():
            raise UserError("Обновление уже выполняется.")
        async with self._update_lock:
            updater = self._updater()
            return await asyncio.to_thread(updater.apply, info)

    async def request_restart(self) -> None:
        """Останавливает приложение и просит main.py запустить процесс заново."""
        self.restart_requested = True
        await self.stop()

    async def switch_account(self, ref: str) -> Account:
        """Делает аккаунт активным и перезапускает процесс под ним."""
        account = self.account_control.resolve(ref)
        if self.account is not None and account.account_id == self.account.account_id:
            raise ValueError(f"аккаунт {account.display_name} уже активен")
        self.accounts.set_active(account)
        logger.info("Переключение на аккаунт %s, перезапуск.", account.display_name)
        await self.request_restart()
        return account

    async def _read_value(self, prompt: str, *, secret: bool = False) -> str:
        """Спрашивает строку, пока пользователь не введёт непустое значение."""
        while True:
            value = (await ask(prompt, secret=secret)).strip()
            if value:
                return value
            print("Значение не может быть пустым.")

    async def _read_secret(self, prompt: str) -> str:
        """То же, что ``_read_value``, но без отображения ввода (API Hash, пароль)."""
        return await self._read_value(prompt, secret=True)

    async def _read_int(self, prompt: str) -> int:
        """Спрашивает положительное целое (API ID), повторяя вопрос при ошибке ввода."""
        while True:
            value = await ask(prompt)
            try:
                number = int(value.strip())
                if number > 0:
                    return number
            except ValueError:
                pass
            print("API ID должен быть положительным числом.")

    async def start(self) -> None:
        """Запускает приложение: вход в аккаунт, подключение, модули, плагины, консоль.

        При любой ошибке (в том числе отмене) состояние жизненного цикла становится FAILED,
        исключение пробрасывается вызывающему.
        """
        logger.info("Запуск Candy Userbot...")
        self.lifecycle.move(STARTING)
        try:
            await self._start()
        except BaseException:
            self.lifecycle.try_move(FAILED)
            raise

    async def _start(self) -> None:
        """Тело запуска. Порядок важен: база и соединение поднимаются до модулей и плагинов."""
        self.account = await self._ensure_account()
        self.client = create_client(self.account)
        self.events = EventBridge(self.client, self.dispatcher)
        self.identity = IdentityService(self.client)
        self.access = AccessService(self.storage.permissions, self.identity)
        self.policy = AccessPolicy(self.access, self.storage.plugin_data, self.dispatcher)
        self.dispatcher.gate = self.policy.gate

        self.storage.open()
        try:
            # connect(), а не start(): start() при невалидной session сам спросит
            # телефон через input() и повиснет в фоне (Termux, nohup).
            await self.client.connect()
            if not await self.client.is_user_authorized():
                raise UserError(
                    "Сохранённая Telegram-сессия недействительна. "
                    "Войдите заново: python main.py --add-account"
                )

            await self._load_policy()
            self.events.register()
            register_commands(self.dispatcher, self.stop)
            register_system_modules(self)
            await self.modules.start_all()
            await self.plugins.load_all()
            await self.console.start()
        except Exception:
            # Откатываем уже запущенное, иначе клиент и база останутся открытыми.
            await self._rollback_startup()
            raise

        self.lifecycle.try_move(RUNNING)
        logger.info("Module System: активна")
        logger.info("Plugin System: активна")
        logger.info("Console: активна")
        logger.info("Аккаунт: %s", self.account.display_name)
        logger.info("Userbot запущен.")

        summary = failed_summary(self)
        if summary:
            await self.notify_owner(summary)

    async def _load_policy(self) -> None:
        """Читает ручные уровни команд. При сбое доступ посторонним закрывается целиком (fail-closed)."""
        try:
            await self.policy.load()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Не удалось прочитать настройки доступа: команды доступны только владельцу.")
            self.dispatcher.gate = None

    def _task_failing(self, item) -> None:
        """Фоновая задача стала сбоить: шлёт владельцу короткое уведомление (в фоне, без ожидания)."""
        if self.client is None or not self.lifecycle.is_running:
            return
        text = (
            f"⚠️ Фоновая задача {item.name} сбоит ({item.consecutive_failures} раз подряд): "
            f"{item.last_error}\nПодробности: .status all"
        )
        task = asyncio.get_running_loop().create_task(self.notify_owner(text))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    async def notify_owner(self, text: str) -> bool:
        """Присылает владельцу сообщение в «Избранное». Никогда не бросает исключений."""
        client = self.client
        if client is None:
            return False
        try:
            await asyncio.wait_for(client.send_message("me", text), NOTIFY_TIMEOUT)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Не удалось отправить уведомление владельцу: %s", exc or type(exc).__name__)
            return False

    def _shutdown_sequence(self) -> ShutdownSequence:
        """Шаги остановки в порядке, обратном запуску. Общие для .stop и отката запуска."""
        seq = ShutdownSequence(STOP_TIMEOUT)
        seq.add("Console", self.console.stop)
        seq.add("Plugins", self.plugins.unload_all)
        seq.add("Modules", self.modules.stop_all)
        seq.add("Background", self.background.stop_all)
        if self.events is not None:
            seq.add("EventBridge", self.events.unregister)
        client = self.client
        if client is not None:
            async def disconnect() -> None:
                """Отключает Telegram-клиент, если он ещё подключён."""
                if client.is_connected():
                    logger.info("Остановка userbot...")
                    await client.disconnect()

            seq.add("Telegram", disconnect)
        # База закрывается после отключения клиента: пока он подключён,
        # обработчики событий ещё могут обращаться к storage.
        seq.add("Storage", self.storage.close)
        return seq

    async def _rollback_startup(self) -> None:
        """Откат неудачного запуска: те же шаги, что и при обычной остановке."""
        await self._shutdown_sequence().run()

    def health(self) -> HealthReport:
        """Сводка о состоянии для .health, .status и консоли. Не бросает исключений."""
        return build_health(self)

    async def run(self) -> None:
        """Блокируется до потери соединения или остановки клиента. До ``start`` бросает RuntimeError."""
        if self.client is None:
            raise RuntimeError("Userbot ещё не запущен")
        await self.client.run_until_disconnected()

    @property
    def is_stopping(self) -> bool:
        """True, как только остановка запрошена (командой, консолью или сигналом)."""
        return self._stop_task is not None

    async def stop(self) -> None:
        """Идемпотентная остановка.

        Реальная работа идёт в отдельной задаче: повторные вызовы (команда
        .stop, консоль, finally в main) ждут её же, а отмена вызывающего не
        обрывает остановку на полпути.
        """
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._do_stop())
        await asyncio.shield(self._stop_task)

    async def _do_stop(self) -> None:
        """Выполняет шаги остановки; STOPPED выставляется даже при сбое шага."""
        self.lifecycle.try_move(STOPPING)
        try:
            await self._shutdown_sequence().run()
        finally:
            self.lifecycle.try_move(STOPPED)
        logger.info("Userbot остановлен.")
