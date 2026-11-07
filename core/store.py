"""Магазин плагинов: индекс в репозитории, проверенная установка и удаление.

Плагины лежат в репозитории в ``store/plugins/<имя>.py``, список с версиями и контрольными суммами в
``store/index.json``. В сам userbot (установка и ``.update``) папка ``store/`` не попадает: плагины
скачиваются отдельно командой ``.store`` и кладутся в ``plugins/``, которую обновление не трогает.

Защита, потому что плагин выполняется с правами вашего аккаунта:
* адреса берутся только из репозитория обновлений, на разрешённых хостах GitHub (core/updater.py);
* файл ставится, только если его SHA-256 и размер совпали с индексом, а код компилируется;
* имя плагина и путь проверяются жёстко, выйти из ``plugins/`` нельзя;
* чужой файл с тем же именем (положенный не магазином или изменённый вами) молча не затирается;
* ничего не ставится и не обновляется само.

Все методы блокирующие: звать через ``asyncio.to_thread``. Метаданные плагина читаются из исходника
без его запуска (``ast``).
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.errors import UserError
from core.updater import DEFAULT_BRANCH, DEFAULT_REPO, BRANCH_PATTERN, REPO_PATTERN, UpdateError, default_fetch
from core.version import CORE_VERSION, at_least, parse_version

SCHEMA = 1
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_INDEX_BYTES = 256 * 1024
MAX_PLUGIN_BYTES = 200 * 1024
MAX_DESCRIPTION = 200
STORE_DIR = "store"  # каталог магазина в репозитории; в установленный userbot не копируется
_META_FIELDS = ("name", "version", "description", "min_core", "requires")


class StoreError(UserError):
    """Операция с магазином не выполнена; сообщение понятно пользователю."""


@dataclass(frozen=True, slots=True)
class StoreEntry:
    """Одна запись индекса."""

    name: str
    version: str
    description: str
    sha256: str
    size: int
    min_core: str = ""
    requires: tuple[str, ...] = ()

    @property
    def file(self) -> str:
        """Путь файла внутри ``store/`` (всегда ``plugins/<имя>.py``)."""
        return f"plugins/{self.name}.py"


@dataclass(slots=True)
class InstallResult:
    """Итог установки: запись, прежняя версия и прежний файл (для отката)."""

    entry: StoreEntry
    previous_version: str | None = None
    previous_data: bytes | None = None
    previous_record: dict | None = None
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------- метаданные и индекс


def read_metadata(source: str, filename: str = "plugin.py") -> dict:
    """Достаёт ``name``/``version``/``description``/``min_core``/``requires`` из класса плагина (без запуска).

    Нужен класс, наследующий ``Plugin``, с литеральными значениями полей, и функция ``create_plugin``.
    Ошибки: ``StoreError``.
    """
    try:
        tree = ast.parse(source, filename)
    except SyntaxError as exc:
        raise StoreError(f"{filename}: ошибка синтаксиса: {exc}") from None
    if not any(isinstance(node, ast.FunctionDef) and node.name == "create_plugin" for node in tree.body):
        raise StoreError(f"{filename}: нет функции create_plugin(app).")
    classes = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and any(
            (isinstance(base, ast.Name) and base.id == "Plugin") or (isinstance(base, ast.Attribute) and base.attr == "Plugin")
            for base in node.bases
        )
    ]
    if len(classes) != 1:
        raise StoreError(f"{filename}: нужен ровно один класс, наследующий Plugin.")
    meta: dict = {}
    for node in classes[0].body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            key = node.targets[0].id
            if key in _META_FIELDS:
                try:
                    meta[key] = ast.literal_eval(node.value)
                except ValueError:
                    raise StoreError(f"{filename}: поле {key} должно быть литералом.") from None
    return meta


def build_entry(path: Path) -> dict:
    """Запись индекса для файла плагина: проверяет метаданные, размер и считает SHA-256."""
    data = path.read_bytes()
    if len(data) > MAX_PLUGIN_BYTES:
        raise StoreError(f"{path.name}: файл больше {MAX_PLUGIN_BYTES // 1024} КБ.")
    meta = read_metadata(data.decode("utf-8"), path.name)
    name = meta.get("name")
    if name != path.stem or not NAME_PATTERN.match(str(name)):
        raise StoreError(f"{path.name}: name плагина ({name!r}) должен совпадать с именем файла (a-z, цифры, _).")
    version = str(meta.get("version", ""))
    try:
        parse_version(version)
    except ValueError:
        raise StoreError(f"{path.name}: неверная версия {version!r}.") from None
    description = str(meta.get("description", "")).strip()
    if not description or len(description) > MAX_DESCRIPTION or "\n" in description:
        raise StoreError(f"{path.name}: description: одна строка до {MAX_DESCRIPTION} символов.")
    min_core = str(meta.get("min_core", "") or "")
    if min_core:
        try:
            parse_version(min_core)
        except ValueError:
            raise StoreError(f"{path.name}: неверная min_core {min_core!r}.") from None
    requires = meta.get("requires", ())
    if not isinstance(requires, (tuple, list)) or not all(isinstance(item, str) and NAME_PATTERN.match(item) for item in requires):
        raise StoreError(f"{path.name}: requires: список имён плагинов.")
    return {
        "version": version,
        "description": description,
        "min_core": min_core,
        "requires": list(requires),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def build_index(store_dir: Path) -> dict:
    """Индекс по всем ``store_dir/plugins/*.py`` (кроме начинающихся с ``_``)."""
    plugins = {}
    for path in sorted((store_dir / "plugins").glob("*.py")):
        if path.name.startswith("_"):
            continue
        plugins[path.stem] = build_entry(path)
    return {"schema": SCHEMA, "plugins": plugins}


def dump_index(index: dict) -> str:
    """Текст ``index.json``: стабильный порядок ключей, чтобы в git менялись только настоящие правки."""
    return json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def parse_index(text: str) -> dict[str, StoreEntry]:
    """Разбирает и строго проверяет индекс. Любая неточность: ``StoreError`` (частично доверять нельзя)."""
    try:
        raw = json.loads(text)
    except ValueError:
        raise StoreError("Индекс магазина повреждён (не JSON).") from None
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not isinstance(raw.get("plugins"), dict):
        raise StoreError("Индекс магазина неизвестного формата: обновите userbot командой .update.")
    result: dict[str, StoreEntry] = {}
    for name, item in raw["plugins"].items():
        try:
            if not NAME_PATTERN.match(name) or not isinstance(item, dict):
                raise ValueError
            sha, size = item["sha256"], item["size"]
            if not (isinstance(sha, str) and SHA_PATTERN.match(sha)):
                raise ValueError
            if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_PLUGIN_BYTES:
                raise ValueError
            parse_version(item["version"])
            description = item["description"]
            min_core = item.get("min_core", "") or ""
            if min_core:
                parse_version(min_core)
            requires = item.get("requires", [])
            if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
                raise ValueError
            if not isinstance(requires, list) or not all(isinstance(req, str) and NAME_PATTERN.match(req) for req in requires):
                raise ValueError
            result[name] = StoreEntry(name, item["version"], description, sha, size, min_core, tuple(requires))
        except (KeyError, ValueError, TypeError):
            raise StoreError(f"Индекс магазина: неверная запись плагина {name!r}.") from None
    return result


def sha256_of(data: bytes) -> str:
    """SHA-256 в виде строки hex."""
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- клиент


class PluginStore:
    """Читает индекс магазина и ставит плагины в ``plugins_dir`` с проверкой суммы."""

    def __init__(
        self,
        plugins_dir: Path,
        state_file: Path,
        *,
        repo: str = DEFAULT_REPO,
        branch: str = DEFAULT_BRANCH,
        core_version: str = CORE_VERSION,
        fetch=default_fetch,
    ) -> None:
        if not REPO_PATTERN.match(repo):
            raise StoreError(f"Неверное имя репозитория: {repo!r}.")
        if not BRANCH_PATTERN.match(branch) or ".." in branch:
            raise StoreError(f"Неверное имя ветки: {branch!r}.")
        self.plugins_dir = Path(plugins_dir)
        self.state_file = Path(state_file)
        self.repo, self.branch = repo, branch
        self.core_version = core_version
        self._fetch = fetch

    # ---------------------------------------------------------------- состояние

    def installed(self) -> dict[str, dict]:
        """Что поставлено из магазина: ``{имя: {"version", "sha256"}}``. Повреждённый файл считается пустым."""
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            name: {"version": str(item.get("version", "")), "sha256": str(item.get("sha256", ""))}
            for name, item in raw.items()
            if isinstance(name, str) and NAME_PATTERN.match(name) and isinstance(item, dict)
        }

    def _save_state(self, state: dict[str, dict]) -> None:
        """Записывает состояние атомарно (временный файл и замена)."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def target(self, name: str) -> Path:
        """Путь ``plugins/<имя>.py``; имя проверено, выйти за папку нельзя."""
        if not NAME_PATTERN.match(name):
            raise StoreError("Имя плагина: латинские буквы a-z, цифры и _, начинается с буквы.")
        return self.plugins_dir / f"{name}.py"

    def status(self, entry: StoreEntry, state: dict[str, dict] | None = None) -> str:
        """``new`` не стоит, ``current`` стоит последняя, ``update`` есть новее, ``modified`` файл изменён вами,
        ``foreign`` файл с таким именем положен не магазином."""
        state = self.installed() if state is None else state
        path = self.target(entry.name)
        record = state.get(entry.name)
        if not path.exists():
            return "new"
        if record is None:
            return "foreign"
        if sha256_of(path.read_bytes()) != record.get("sha256"):
            return "modified"
        return "current" if record.get("sha256") == entry.sha256 else "update"

    # ------------------------------------------------------------------ сеть

    def _get(self, relative: str, limit: int) -> bytes:
        """Скачивает файл из ``store/`` репозитория (не больше ``limit`` байт); сетевые ошибки становятся ``StoreError``."""
        url = f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/{STORE_DIR}/{relative}"
        try:
            return self._fetch(url, limit)
        except UpdateError as exc:
            if "404" in str(exc):
                raise StoreError("В репозитории нет магазина плагинов (store/index.json).") from None
            raise StoreError(str(exc)) from None

    def index(self) -> dict[str, StoreEntry]:
        """Скачивает и проверяет индекс магазина."""
        return parse_index(self._get("index.json", MAX_INDEX_BYTES).decode("utf-8", errors="replace"))

    def entry(self, name: str) -> StoreEntry:
        """Запись о плагине из свежего индекса; нет такого: ``StoreError``."""
        self.target(name)
        entries = self.index()
        if name not in entries:
            raise StoreError(f"В магазине нет плагина «{name}».")
        return entries[name]

    # ------------------------------------------------------------- установка

    def install(self, name: str, *, force: bool = False) -> InstallResult:
        """Скачивает, проверяет и записывает плагин. Загрузку в работающий userbot делает вызывающий.

        ``force`` разрешает затереть файл, изменённый вами (но не чужой, поставленный не магазином).
        """
        entry = self.entry(name)
        if entry.min_core and not at_least(self.core_version, entry.min_core):
            raise StoreError(f"Плагину {name} нужна версия ядра {entry.min_core}, у вас {self.core_version}: сначала .update.")
        state = self.installed()
        status = self.status(entry, state)
        if status == "foreign":
            raise StoreError(f"Файл plugins/{name}.py уже есть, и поставлен не из магазина. Переименуйте или удалите его сами.")
        if status == "modified" and not force:
            raise StoreError(f"Файл plugins/{name}.py изменён вами. Чтобы заменить версией из магазина, добавьте --force.")
        missing = [
            req for req in entry.requires
            if not (self.plugins_dir / f"{req}.py").exists()
        ]
        if missing:
            raise StoreError("Сначала поставьте нужные плагины: " + ", ".join(missing) + ".")
        data = self._get(entry.file, min(entry.size, MAX_PLUGIN_BYTES))
        if len(data) != entry.size or sha256_of(data) != entry.sha256:
            raise StoreError("Контрольная сумма не совпала с индексом: файл повреждён или подменён, установка отменена.")
        meta = read_metadata(data.decode("utf-8", errors="replace"), f"{name}.py")  # также проверяет синтаксис
        if meta.get("name") != name:
            raise StoreError("Имя в файле плагина не совпадает с индексом: установка отменена.")
        try:
            compile(data, f"{name}.py", "exec")
        except (SyntaxError, ValueError) as exc:
            raise StoreError(f"В плагине ошибка синтаксиса: {exc}") from None

        path = self.target(name)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        result = InstallResult(
            entry,
            previous_version=state.get(name, {}).get("version") if status != "new" else None,
            previous_data=path.read_bytes() if path.exists() else None,
            previous_record=state.get(name),
        )
        tmp = path.with_name(f".{name}.py.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        state[name] = {"version": entry.version, "sha256": entry.sha256}
        self._save_state(state)
        return result

    def rollback(self, result: InstallResult) -> None:
        """Возвращает прежнее состояние после неудачной загрузки плагина."""
        name = result.entry.name
        path = self.target(name)
        state = self.installed()
        if result.previous_data is None:
            path.unlink(missing_ok=True)
            state.pop(name, None)
        else:
            path.write_bytes(result.previous_data)
            if result.previous_record is not None:
                state[name] = result.previous_record
        self._save_state(state)

    def remove(self, name: str) -> bool:
        """Удаляет плагин, поставленный из магазина. Чужие файлы не трогает (``StoreError``). False: не стоял."""
        path = self.target(name)
        state = self.installed()
        if name not in state:
            if path.exists():
                raise StoreError(f"Файл plugins/{name}.py поставлен не из магазина: удалите его сами.")
            return False
        path.unlink(missing_ok=True)
        del state[name]
        self._save_state(state)
        return True
