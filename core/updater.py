"""Обновление из GitHub: проверка версии, скачивание архива, безопасная замена файлов.

Без git и без сторонних библиотек (только urllib и zipfile). Правила:

* версия берётся из ``core/version.py`` в ветке репозитория; понижать и «обновляться»
  на ту же версию нельзя;
* скачивание только по HTTPS и только с доменов GitHub;
* архив проверяется целиком до первой замены: пути, ссылки, размер, версия,
  синтаксис всех ``.py``;
* ``data/``, ``logs/``, ``plugins/``, ``.env`` и ``.git`` не затрагиваются;
* старые файлы сохраняются в ``data/update/backup-<версия>``; при сбое замены
  всё возвращается обратно.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from core.errors import UserError
from core.version import CORE_VERSION, parse_version
from utils.logger import logger

DEFAULT_REPO = "Vital-Candy/Candy-userbot"
DEFAULT_BRANCH = "main"

# Белый список хостов: защита от подмены адреса и переадресации на сторонний сервер.
ALLOWED_HOSTS = frozenset({"raw.githubusercontent.com", "codeload.github.com"})
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9_./-]{1,100}$")

# Лимиты защищают от подмены и «архивных бомб».
MAX_VERSION_BYTES = 64 * 1024  # размер core/version.py с сервера, байты
MAX_ARCHIVE_BYTES = 30 * 1024 * 1024  # размер скачиваемого архива, байты
MAX_UNPACKED_BYTES = 100 * 1024 * 1024  # суммарный размер после распаковки, байты
MAX_FILES = 3000  # максимум записей в архиве
TIMEOUT = 30.0  # таймаут сетевого запроса, секунды

# Никогда не перезаписываются и не удаляются обновлением.
PROTECTED = frozenset({"data", "logs", "plugins", ".env", ".git", "__pycache__"})
# Каталоги репозитория, которые в установленный userbot не копируются: магазин плагинов
# скачивается отдельно (команда .store), а не вместе с ядром.
NOT_INSTALLED = frozenset({"store"})
# Личные файлы: если такой уже есть у пользователя, обновление его не перезаписывает.
KEEP_LOCAL = ("logo.txt",)

_VERSION_LINE = re.compile(r'^\s*CORE_VERSION\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)

# Загрузчик ``(url, максимум_байт) -> содержимое``; в тестах подменяется заглушкой.
Fetcher = Callable[[str, int], bytes]


class UpdateError(UserError):
    """Обновление не выполнено; сообщение понятно пользователю."""


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    """Результат проверки: версии локальная и удалённая, репозиторий и ветка."""

    local: str
    remote: str
    repo: str
    branch: str

    @property
    def state(self) -> str:
        """``newer`` (есть обновление), ``current`` (то же) или ``older`` (на сервере ниже)."""
        have, other = parse_version(self.local), parse_version(self.remote)
        size = max(len(have), len(other))
        pad = lambda v: v + (0,) * (size - len(v))  # noqa: E731
        if pad(other) > pad(have):
            return "newer"
        return "current" if pad(other) == pad(have) else "older"

    @property
    def available(self) -> bool:
        """True, если на сервере версия выше локальной."""
        return self.state == "newer"


@dataclass(slots=True)
class UpdateResult:
    """Итог установки: версии, число записанных и удалённых файлов, backup и заметки."""

    old: str
    new: str
    files: int = 0
    removed: int = 0
    backup: Path | None = None
    notes: list[str] = field(default_factory=list)


def parse_remote_version(text: str) -> str:
    """Достаёт ``CORE_VERSION`` из текста version.py. Ошибка формата: UpdateError."""
    match = _VERSION_LINE.search(text)
    if match is None:
        raise UpdateError("В репозитории не найден core/version.py с CORE_VERSION: обновление отменено.")
    version = match.group(1).strip()
    try:
        parse_version(version)
    except ValueError:
        raise UpdateError(f"В репозитории неверная версия: {version!r}.") from None
    return version


def default_fetch(url: str, limit: int) -> bytes:
    """Скачивает по HTTPS с доменов GitHub, не больше ``limit`` байт."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise UpdateError(f"Адрес не разрешён для обновления: {parsed.hostname or url}")
    request = urllib.request.Request(url, headers={"User-Agent": "Candy-Userbot-Updater"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 - схема и домен проверены
            final = urlparse(response.geturl())
            if final.scheme != "https" or final.hostname not in ALLOWED_HOSTS:
                raise UpdateError(f"Переадресация на неразрешённый адрес: {final.hostname}")
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("Репозиторий или ветка не найдены (HTTP 404).") from None
        raise UpdateError(f"GitHub ответил ошибкой HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateError(f"Нет связи с GitHub: {reason}") from None
    if len(data) > limit:
        raise UpdateError("Файл с сервера слишком большой, обновление отменено.")
    return data


class Updater:
    """Проверяет и устанавливает обновления. Все методы блокирующие (звать через ``to_thread``)."""

    def __init__(
        self,
        root: Path,
        data_dir: Path,
        *,
        repo: str = DEFAULT_REPO,
        branch: str = DEFAULT_BRANCH,
        local_version: str = CORE_VERSION,
        fetch: Fetcher = default_fetch,
    ) -> None:
        """Проверяет имена репозитория и ветки (UpdateError), чтобы они не попали в URL как есть."""
        if not REPO_PATTERN.match(repo):
            raise UpdateError(f"Неверное имя репозитория: {repo!r} (нужно владелец/имя).")
        if not BRANCH_PATTERN.match(branch) or ".." in branch:
            raise UpdateError(f"Неверное имя ветки: {branch!r}.")
        self.root = Path(root)
        self.data_dir = Path(data_dir)
        self.repo = repo
        self.branch = branch
        self.local_version = local_version
        self._fetch = fetch

    # ---------------------------------------------------------------- проверка

    def check(self) -> UpdateInfo:
        """Читает версию из ветки репозитория и сравнивает с локальной. Ошибки: UpdateError."""
        url = f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/core/version.py"
        text = self._fetch(url, MAX_VERSION_BYTES).decode("utf-8", errors="replace")
        return UpdateInfo(self.local_version, parse_remote_version(text), self.repo, self.branch)

    # ------------------------------------------------------------- обновление

    def apply(self, info: UpdateInfo | None = None) -> UpdateResult:
        """Скачивает и устанавливает обновление. Блокирующая: звать через ``to_thread``."""
        info = info or self.check()
        self._require_newer(info.remote)

        url = f"https://codeload.github.com/{self.repo}/zip/refs/heads/{self.branch}"
        archive = self._fetch(url, MAX_ARCHIVE_BYTES)

        # Рабочий каталог внутри data/: он в PROTECTED и не затрагивается самим обновлением.
        work = self.data_dir / "update"
        work.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="stage-", dir=work))
        try:
            tree = self._unpack(archive, staging)
            self._validate(tree, info.remote)
            return self._install(tree, work, info.remote)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _require_newer(self, remote: str) -> None:
        """Отказывает, если удалённая версия не выше локальной (защита от понижения)."""
        if UpdateInfo(self.local_version, remote, self.repo, self.branch).state != "newer":
            raise UpdateError(
                f"Версия на сервере ({remote}) не выше текущей ({self.local_version}): "
                "понижать версию нельзя."
            )

    # --------------------------------------------------------------- проверки

    @staticmethod
    def _unpack(data: bytes, target: Path) -> Path:
        """Проверяет архив (пути, ссылки, размеры, одна корневая папка) и распаковывает его.

        Возвращает корневую папку проекта внутри ``target``. Проверка идёт до ``extractall``.
        """
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise UpdateError("Скачанный файл не является архивом.") from None
        with archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_FILES:
                raise UpdateError("Архив пуст или содержит слишком много файлов.")
            if sum(item.file_size for item in infos) > MAX_UNPACKED_BYTES:
                raise UpdateError("Архив слишком большой после распаковки.")
            roots: set[str] = set()
            for item in infos:
                name = item.filename
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in path.parts[0]:
                    raise UpdateError(f"Небезопасный путь в архиве: {name!r}")
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise UpdateError(f"Символические ссылки в архиве запрещены: {name!r}")
                roots.add(path.parts[0])
            if len(roots) != 1:
                raise UpdateError("Неожиданная структура архива (нужна одна корневая папка).")
            archive.extractall(target)
        root = target / next(iter(roots))
        if not root.is_dir():
            raise UpdateError("Неожиданная структура архива.")
        return root

    @staticmethod
    def _validate(tree: Path, expected: str) -> None:
        """Убеждается, что в дереве настоящий Candy Userbot нужной версии без синтаксических ошибок."""
        for required in ("main.py", "core/version.py"):
            if not (tree / required).is_file():
                raise UpdateError(f"В архиве нет {required}: это не Candy Userbot.")
        found = parse_remote_version((tree / "core" / "version.py").read_text(encoding="utf-8"))
        if found != expected:
            raise UpdateError(f"Версия в архиве ({found}) не совпадает с объявленной ({expected}).")
        for path in sorted(tree.rglob("*.py")):
            if path.relative_to(tree).parts[0] in NOT_INSTALLED:
                continue  # плагины магазина в установку не входят и ядро обновлению не мешают
            try:
                compile(path.read_bytes(), str(path.relative_to(tree)), "exec")
            except (SyntaxError, ValueError) as exc:
                raise UpdateError(f"Ошибка синтаксиса в {path.relative_to(tree)}: {exc}") from None

    # --------------------------------------------------------------- установка

    def _managed(self, tree: Path) -> list[Path]:
        """Относительные пути всех файлов архива, которые разрешено устанавливать."""
        result = []
        for path in sorted(tree.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(tree)
            if relative.parts[0] in PROTECTED or relative.parts[0] in NOT_INSTALLED or "__pycache__" in relative.parts:
                continue
            if len(relative.parts) == 1 and relative.name in KEEP_LOCAL and (self.root / relative).exists():
                continue
            result.append(relative)
        return result

    def _stale(self, tree: Path, new_files: set[Path]) -> list[Path]:
        """Локальные файлы внутри пакетов, которых больше нет в новой версии."""
        stale = []
        for entry in sorted(tree.iterdir()):
            if not entry.is_dir() or entry.name in PROTECTED or entry.name in NOT_INSTALLED:
                continue
            local_dir = self.root / entry.name
            if not local_dir.is_dir():
                continue
            for path in sorted(local_dir.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(self.root)
                if "__pycache__" in relative.parts or relative in new_files:
                    continue
                stale.append(relative)
        return stale

    def _install(self, tree: Path, work: Path, new_version: str) -> UpdateResult:
        """Сохраняет старые файлы в backup, заменяет их новыми, удаляет устаревшие.

        При любом сбое (включая отмену) возвращает прежнее состояние.
        """
        files = self._managed(tree)
        new_set = set(files)
        stale = self._stale(tree, new_set)

        backup = work / f"backup-{self.local_version}"
        if backup.exists():
            shutil.rmtree(backup)
        backup.mkdir(parents=True)

        touched: list[Path] = []  # то, что уже изменено: для отката
        notes: list[str] = []
        old_requirements = self._read(self.root / "requirements.txt")
        try:
            # Сначала копируем всё в backup и только потом меняем: откат должен иметь из чего восстанавливать.
            for relative in files + stale:
                source = self.root / relative
                if source.is_file():
                    saved = backup / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, saved)
            for relative in files:
                self._place(tree / relative, self.root / relative)
                touched.append(relative)
            removed = 0
            for relative in stale:
                (self.root / relative).unlink(missing_ok=True)
                touched.append(relative)
                removed += 1
        except BaseException as exc:  # BaseException: откатываемся и при KeyboardInterrupt/отмене
            logger.error("Обновление не удалось (%s), возвращаю прежние файлы.", exc)
            self._rollback(backup, touched, set(files))
            if isinstance(exc, (OSError, shutil.Error)):
                raise UpdateError(f"Не удалось заменить файлы: {exc}. Прежняя версия восстановлена.") from None
            raise

        new_requirements = self._read(tree / "requirements.txt")
        if new_requirements is not None and new_requirements != old_requirements:
            notes.append("requirements.txt изменился: выполните pip install -r requirements.txt")
        # Старый байт-код удаляем, чтобы не остался от удалённых модулей.
        for pycache in self.root.rglob("__pycache__"):
            if pycache.parent.name not in PROTECTED and "data" not in pycache.relative_to(self.root).parts:
                shutil.rmtree(pycache, ignore_errors=True)
        logger.info("Обновление %s → %s установлено: файлов %d, удалено %d.", self.local_version, new_version, len(files), removed)
        return UpdateResult(self.local_version, new_version, len(files), removed, backup, notes)

    @staticmethod
    def _read(path: Path) -> str | None:
        """Текст файла или None, если его нельзя прочитать."""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    @staticmethod
    def _place(source: Path, target: Path) -> None:
        """Атомарная замена через временный файл: запущенный скрипт (run.sh) не ломается."""
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".upd-", dir=target.parent)
        os.close(fd)
        try:
            shutil.copyfile(source, temp_name)
            mode = stat.S_IMODE(source.stat().st_mode)
            os.chmod(temp_name, mode | (stat.S_IRUSR | stat.S_IWUSR))
            os.replace(temp_name, target)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def _rollback(self, backup: Path, touched: list[Path], new_files: set[Path]) -> None:
        """Возвращает изменённые файлы из backup; ошибки отдельных файлов только логируются."""
        for relative in touched:
            saved = backup / relative
            target = self.root / relative
            try:
                if saved.is_file():
                    self._place(saved, target)
                elif relative in new_files:
                    target.unlink(missing_ok=True)  # файл появился только в новой версии
            except Exception:
                logger.exception("Не удалось восстановить %s из резервной копии (%s).", relative, backup)
