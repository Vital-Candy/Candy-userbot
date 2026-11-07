#!/usr/bin/env bash
# Установщик Candy Userbot для Termux и Linux (Debian/Ubuntu).
#
#   bash installer.sh
#
# Что делает: ставит Python и git (если их нет), скачивает проект из GitHub,
# устанавливает зависимости и проверяет окружение. Ничего не запускает и не просит
# входа в Telegram: вход выполняется при первом запуске ./run.sh.
#
# Переменные (необязательные):
#   CANDY_DIR   куда ставить проект (по умолчанию ~/userbot)
#   CANDY_REPO  адрес репозитория для git clone

set -u

CANDY_DIR="${CANDY_DIR:-$HOME/userbot}"
CANDY_REPO="${CANDY_REPO:-https://github.com/Vital-Candy/Candy-userbot.git}"
MIN_PY_MAJOR=3
MIN_PY_MINOR=11

# Цвета только в терминале: в логах и пайпах лишние escape-коды мешают читать вывод.
if [ -t 1 ]; then
    RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; NC=""
fi

info() { printf '%s\n' "${YELLOW}$*${NC}"; }
ok()   { printf '%s\n' "${GREEN}$*${NC}"; }
fail() { printf '%s\n' "${RED}$*${NC}" >&2; exit 1; }

echo "${GREEN}Candy Userbot: установка${NC}"
echo "=============================="

# --- Среда: Termux или обычный Linux --------------------------------------------------
if [ -n "${TERMUX_VERSION:-}" ] || [ -d /data/data/com.termux ]; then
    IS_TERMUX=1
    info "Обнаружен Termux."
    install_packages() { pkg update -y && pkg install -y "$@"; }
else
    IS_TERMUX=0
    # Без root apt не работает: берём sudo, если он есть.
    SUDO=""
    if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    fi
    install_packages() { $SUDO apt-get update -y && $SUDO apt-get install -y "$@"; }
fi

# --- Python и git ---------------------------------------------------------------------
missing=""
command -v python3 >/dev/null 2>&1 || missing="$missing python3"
command -v git >/dev/null 2>&1 || missing="$missing git"
if [ -n "$missing" ]; then
    info "Устанавливаю:$missing"
    if [ "$IS_TERMUX" -eq 1 ]; then
        packages="python git"
    else
        packages="python3 python3-pip python3-venv git"
    fi
    install_packages $packages || fail "Не удалось установить пакеты:$missing"
fi

# Проект использует возможности Python 3.11+.
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)"; then
    version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    fail "Нужен Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR} или новее, сейчас ${version}."
fi

# --- Скачивание проекта ---------------------------------------------------------------
if [ -d "$CANDY_DIR/.git" ]; then
    ok "Candy Userbot уже установлен: $CANDY_DIR"
    info "Обновить его можно командой .update в Telegram или пунктом «Обновление» в меню."
elif [ -e "$CANDY_DIR" ]; then
    # Не затираем чужие файлы: папка может содержать данные аккаунтов.
    fail "Папка $CANDY_DIR уже существует и это не установка из git. Укажите другую: CANDY_DIR=/путь bash installer.sh"
else
    info "Скачиваю проект..."
    git clone "$CANDY_REPO" "$CANDY_DIR" || fail "Не удалось скачать репозиторий: $CANDY_REPO"
    # Плагины магазина (store/) в установку не входят: их ставят отдельно командой .store в Telegram.
    (cd "$CANDY_DIR" && { git sparse-checkout set --no-cone '/*' '!/store/' >/dev/null 2>&1 || rm -rf store; })
fi

cd "$CANDY_DIR" || fail "Не удалось открыть $CANDY_DIR"

# --- Зависимости ----------------------------------------------------------------------
# В Termux ставим прямо в системный Python. В Linux (PEP 668) нужно виртуальное окружение:
# run.sh находит .venv сам.
if [ "$IS_TERMUX" -eq 1 ]; then
    PYTHON_BIN="python3"
else
    if [ ! -x .venv/bin/python ]; then
        info "Создаю виртуальное окружение .venv..."
        python3 -m venv .venv || fail "Не удалось создать .venv (нужен пакет python3-venv)."
    fi
    PYTHON_BIN=".venv/bin/python"
fi

info "Устанавливаю зависимости..."
"$PYTHON_BIN" -m pip install -r requirements.txt || fail "Не удалось установить зависимости."

chmod +x run.sh 2>/dev/null || true

# --- Проверка окружения (ничего не меняет, без подключения к Telegram) -----------------
echo
info "Проверка окружения:"
"$PYTHON_BIN" main.py --check || info "Проверка нашла проблемы: прочитайте сообщения выше."

echo
ok "Установка завершена."
echo
echo "Проект: $CANDY_DIR"
echo
echo "Первый запуск (бот спросит API ID, API Hash и телефон):"
echo "  cd \"$CANDY_DIR\" && ./run.sh"
echo
echo "Данные аккаунта лежат в $CANDY_DIR/data. Не отправляйте эту папку никому."
