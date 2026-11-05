#!/usr/bin/env bash
# Публикация Candy Userbot на GitHub без личных данных.
#
#   bash tools/publish.sh [--dry-run] [владелец/репозиторий]             первая публикация
#   bash tools/publish.sh --release [--dry-run] [владелец/репозиторий]   выпуск обновления
#
# Скрипт не переписывает чужую историю. Он:
#   1. копирует проект в одну постоянную рабочую папку ~/.candy-publish (без .git, data/, logs/,
#      .env, кэша); при каждом запуске она пересоздаётся, а после успешной отправки удаляется,
#      так что новых папок не копится. Остатки прежних запусков (~/candy-publish-ГГГГММДД-ЧЧММСС,
#      созданные этим скриптом) тоже убираются;
#   2. задаёт анонимного автора (имя Candy, адрес noreply GitHub);
#   3. проверяет, что в публикуемых файлах нет секретов, почты, телефонов, личных файлов и запретных
#      слов (из ~/.candy-forbidden плюс имя и почта из настройки git и профиля GitHub: их
#      добавлять в файл не нужно);
#   4. отправляет код и тег версии.
# Первая публикация: создаёт свежий репозиторий с одним коммитом. Если репозиторий уже
# существует, скрипт останавливается: удалите его вручную на сайте (Settings, Danger Zone),
# иначе в старой истории останутся прежние коммиты.
# Выпуск обновления (--release): скачивает уже опубликованный репозиторий, заменяет его
# файлы текущими и добавляет ОДИН новый коммит сверху (история не переписывается). Версия
# CORE_VERSION должна быть выше опубликованной, иначе .update обновления не увидит.
#
# Публикация ничего не устанавливает: ни в этой папке, ни на ваших устройствах. Скрипт только
# отправляет код на GitHub, а обновление ставится вручную командой .update (автоматической
# установки нет). Папку проекта скрипт лишь читает. Рекомендуется распаковывать выпуск в
# отдельную папку (например ~/candy-release), чтобы работающий ~/userbot не менялся до .update.
# Если скрипт запущен из рабочей установки (там есть сессия Telegram), он остановится: иначе
# распаковка выпуска поверх установки подменила бы файлы локально, и .update не увидел бы новой версии.
#
# --dry-run: выполнить всё, кроме отправки (и создания репозитория).
#
# Запретные слова (например, настоящее имя) скрипт читает из файла ~/.candy-forbidden:
# по одному регулярному выражению в строке, регистр не важен. Сами слова нигде в проекте
# не хранятся, поэтому не попадут в публикацию.
#
# Переменные для тестов и особых случаев:
#   PUBLISH_LOGIN, PUBLISH_ID     логин и числовой ID GitHub (иначе берутся через gh)
#   PUBLISH_FORBIDDEN_FILE        другой путь к файлу запретных слов
#   PUBLISH_WORK                  своя рабочая папка (должна не существовать)
#   PUBLISH_ALLOW_NO_FORBIDDEN=1  не требовать файл запретных слов (не рекомендуется)
#   PUBLISH_KEEP_WORK=1           не удалять рабочую папку после отправки
#   PUBLISH_ALLOW_SAME_VERSION=1  при --release не требовать повышения версии
#   PUBLISH_FROM_INSTALL=1        разрешить запуск из рабочей установки (не рекомендуется)

set -euo pipefail

# Кириллица в проверках (регистр, имена файлов): нужна UTF-8 локаль; если её нет, grep работает побайтно.
export LC_ALL="$(locale -a 2>/dev/null | grep -i -m1 -E '^(c|en_us)\.utf-?8$' || echo C)"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="Vital-Candy/Candy-userbot"
DRY=0
MODE=publish

die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
}

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;;
        --release) MODE=release ;;
        -h|--help) usage; exit 0 ;;
        */*) REPO="$arg" ;;
        *) die "неизвестный аргумент: $arg (нужно --dry-run, --release или владелец/репозиторий)" ;;
    esac
done

command -v git >/dev/null 2>&1 || die "не найден git (pkg install git)."
command -v tar >/dev/null 2>&1 || die "не найден tar."

# --- Личность автора -----------------------------------------------------------------
LOGIN="${PUBLISH_LOGIN:-}"
ID="${PUBLISH_ID:-}"
if [ -z "$LOGIN" ] || [ -z "$ID" ]; then
    command -v gh >/dev/null 2>&1 || die "не найден gh (pkg install gh) и не заданы PUBLISH_LOGIN/PUBLISH_ID."
    gh auth status >/dev/null 2>&1 || die "вы не вошли в GitHub: выполните gh auth login."
    LOGIN="$(gh api user --jq .login)"
    ID="$(gh api user --jq .id)"
fi
case "$ID" in ''|*[!0-9]*) die "числовой ID GitHub получен неверно: '$ID'" ;; esac
AUTHOR_NAME="Candy"
AUTHOR_EMAIL="${ID}+${LOGIN}@users.noreply.github.com"

# --- Версия --------------------------------------------------------------------------
VERSION="$(sed -n 's/^CORE_VERSION *= *"\([^"]*\)".*/\1/p' "$SRC/core/version.py" | head -n 1)"
[ -n "$VERSION" ] || die "не удалось прочитать CORE_VERSION из core/version.py."

# --- Защита рабочей установки ----------------------------------------------------------
# Выпуск нужно готовить в отдельной папке: обновление на устройство ставится только командой .update.
if [ "${PUBLISH_FROM_INSTALL:-0}" != "1" ] && [ -d "$SRC/data" ] \
    && [ -n "$(find "$SRC/data" -name '*.session*' -print -quit 2>/dev/null)" ]; then
    die "$SRC похожа на рабочую установку (в data есть сессия Telegram). Распакуйте выпуск в отдельную папку (например ~/candy-release) и запускайте скрипт оттуда: так работающий userbot не изменится до команды .update. Обойти проверку: PUBLISH_FROM_INSTALL=1."
fi

# --- Файл запретных слов нужен до любой работы (чтобы не скачивать репозиторий зря) ----
FORBIDDEN_FILE="${PUBLISH_FORBIDDEN_FILE:-$HOME/.candy-forbidden}"
if [ ! -s "$FORBIDDEN_FILE" ] && [ "${PUBLISH_ALLOW_NO_FORBIDDEN:-0}" != "1" ] && [ "$DRY" -eq 0 ]; then
    die "нет файла запретных слов $FORBIDDEN_FILE. Создайте его (по слову или регулярному выражению в строке), например:
  printf '%s\n' 'слово1' 'слово2' > ~/.candy-forbidden"
fi

# --- Рабочая папка ---------------------------------------------------------------------
# Одна постоянная папка: прежний запуск стирается, новые папки не накапливаются. Стереть можно
# только то, что создал сам скрипт (внутри есть .git), и никогда не саму папку проекта.
DEFAULT_WORK="$HOME/.candy-publish"
WORK="${PUBLISH_WORK:-$DEFAULT_WORK}"
if [ "$WORK" = "$DEFAULT_WORK" ]; then
    if [ -e "$WORK" ]; then
        [ "$WORK" != "$SRC" ] && [ -d "$WORK/.git" ] || die "$WORK уже существует, но это не рабочая папка скрипта. Удалите её вручную или укажите другую (PUBLISH_WORK)."
        rm -rf "$WORK"
    fi
    # Папки прежних версий скрипта (candy-publish-ГГГГММДД-ЧЧММСС): убираем только свои коммиты автора Candy.
    for old in "$HOME"/candy-publish-[0-9]*-[0-9]*; do
        [ -d "$old/.git" ] || continue
        [ "$old" != "$SRC" ] || continue
        if [ "$(git -C "$old" log -1 --format=%an 2>/dev/null || true)" = "$AUTHOR_NAME" ]; then
            rm -rf "$old"
            say "Удалена прежняя рабочая папка $old."
        fi
    done
else
    [ ! -e "$WORK" ] || die "папка $WORK уже существует; удалите её или укажите другую (PUBLISH_WORK)."
fi

if [ "$MODE" = release ]; then
    # Выпуск: берём опубликованную историю и добавляем в неё один коммит.
    command -v gh >/dev/null 2>&1 || die "не найден gh (pkg install gh)."
    gh repo view "$REPO" >/dev/null 2>&1 || die "репозиторий $REPO не найден. Первую публикацию делайте без --release."
    say "Скачиваю опубликованный репозиторий $REPO в $WORK..."
    gh repo clone "$REPO" "$WORK" -- -q || die "не удалось скачать $REPO."
    OLD_VERSION="$(git -C "$WORK" show HEAD:core/version.py 2>/dev/null | sed -n 's/^CORE_VERSION *= *"\([^"]*\)".*/\1/p' | head -n 1)"
    # Версия обязана расти: иначе .update у пользователей ничего не найдёт.
    if [ "${PUBLISH_ALLOW_SAME_VERSION:-0}" != "1" ] && [ -n "$OLD_VERSION" ]; then
        newest="$(printf '%s\n%s\n' "$OLD_VERSION" "$VERSION" | sort -V | tail -n 1)"
        if [ "$OLD_VERSION" = "$VERSION" ] || [ "$newest" != "$VERSION" ]; then
            die "версия $VERSION не выше опубликованной ($OLD_VERSION). Поднимите CORE_VERSION в core/version.py."
        fi
    fi
    # Старые файлы убираем, чтобы удалённые в проекте файлы исчезли и из репозитория.
    find "$WORK" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
else
    mkdir -p "$WORK"
fi

say "Копирую проект в $WORK (без .git, data, logs, .env, кэша)..."
(
    cd "$SRC"
    tar -cf - \
        --exclude='./.git' --exclude='./data' --exclude='./logs' --exclude='./.env' \
        --exclude='./.venv' --exclude='__pycache__' --exclude='*.session' \
        --exclude='*.session-journal' --exclude='*.zip' --exclude='*.bak' .
) | tar -xf - -C "$WORK"

cd "$WORK"
if [ "$MODE" = publish ]; then
    git init -q
    git checkout -q -b main
fi
git config user.name "$AUTHOR_NAME"
git config user.email "$AUTHOR_EMAIL"
git config commit.gpgsign false
git add -A

# --- Проверки перед коммитом ---------------------------------------------------------
FOUND=0
report() { FOUND=1; printf 'НАЙДЕНО: %s\n' "$1" >&2; }

# 1. Файлы, которых в публикации быть не должно.
bad_paths="$(git -c core.quotepath=off ls-files | grep -Ei '(^|/)(data|logs|downloads|backups?)/|(^|/)\.env($|\.)|\.session|profile\.json|\.zip$|\.bak$|sqlite|\.db$|\.log$|\.pem$|\.key$|\.p12$|\.pyc$|(^|/)\.candy-' | grep -Ev '(^|/)\.env\.example$' || true)"
if [ -n "$bad_paths" ]; then
    report "личные или служебные файлы:"
    printf '%s\n' "$bad_paths" >&2
fi

# 2. Секреты: api_hash, токены ботов, приватные ключи.
secrets="$(git grep --cached -I -n -E \
    "api_hash[[:space:]]*[:=][[:space:]]*['\"][0-9a-fA-F]{32}['\"]|[0-9]{8,10}:[A-Za-z0-9_-]{35}|BEGIN [A-Z ]*PRIVATE KEY" \
    || true)"
if [ -n "$secrets" ]; then
    report "похоже на секреты:"
    printf '%s\n' "$secrets" >&2
fi

# 2a. Номера телефонов и api_id: «+» и 10–15 цифр, номер рядом со словом phone/телефон, api_id из 6+ цифр.
# Исключение: заведомо выдуманный номер 998901234567, который используют тесты.
phones="$(git grep --cached -I -n -E \
    '\+[0-9]{10,15}|([Pp]hone|[Тт]елефон|[Тт]ел\.)[^0-9]{0,20}[0-9]{10,15}|api_id[[:space:]]*[:=][[:space:]]*['\''\"]?[0-9]{6,}' \
    | grep -v '998901234567' || true)"
if [ -n "$phones" ]; then
    report "похоже на номера телефонов или api_id:"
    printf '%s\n' "$phones" >&2
fi

# 3. Адреса электронной почты (разрешён только noreply GitHub).
emails="$(git grep --cached -I -n -E \
    '[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+' | grep -v 'users\.noreply\.github\.com' || true)"
if [ -n "$emails" ]; then
    report "адреса электронной почты:"
    printf '%s\n' "$emails" >&2
fi

# 4. Запретные слова из ~/.candy-forbidden (в содержимом и в именах файлов).
# Кроме файла, автоматически берутся слова из имени и почты в настройке git и в профиле GitHub:
# если там настоящее имя, оно тоже не должно попасть в публикацию.
AUTO_WORDS="$(mktemp)"
trap 'rm -f "$AUTO_WORDS"' EXIT
{
    git config --global user.name 2>/dev/null || true
    git config --global user.email 2>/dev/null | sed 's/@.*//' || true
    if [ -z "${PUBLISH_LOGIN:-}" ] && command -v gh >/dev/null 2>&1; then
        gh api user --jq '(.name // empty), ((.email // "") | sub("@.*"; ""))' 2>/dev/null || true
    fi
} | grep -oE '[[:alnum:]]{4,}' | tr '[:upper:]' '[:lower:]' \
    | grep -Evx 'candy|vital|noreply|users|github|example|test|tester' | sort -u > "$AUTO_WORDS" || true
CHECK_WORDS="$(mktemp)"
trap 'rm -f "$AUTO_WORDS" "$CHECK_WORDS"' EXIT
{ [ -s "$FORBIDDEN_FILE" ] && cat "$FORBIDDEN_FILE"; cat "$AUTO_WORDS"; } > "$CHECK_WORDS" || true
if [ -s "$CHECK_WORDS" ]; then
    FORBIDDEN_FILE="$CHECK_WORDS"
    words="$(git grep --cached -I -n -i -E -f "$FORBIDDEN_FILE" || true)"
    if [ -n "$words" ]; then
        report "запретные слова в файлах:"
        printf '%s\n' "$words" >&2
    fi
    names="$(git -c core.quotepath=off ls-files | grep -i -E -f "$FORBIDDEN_FILE" || true)"
    if [ -n "$names" ]; then
        report "запретные слова в именах файлов:"
        printf '%s\n' "$names" >&2
    fi
elif [ "${PUBLISH_ALLOW_NO_FORBIDDEN:-0}" != "1" ]; then
    say "Предупреждение: нет файла запретных слов, проверка по словам пропущена."
fi

[ "$FOUND" -eq 0 ] || die "в публикуемых файлах найдены личные данные (см. выше). Ничего не отправлено."
say "Проверки пройдены: секретов, почты, телефонов, личных файлов и запретных слов нет."

# --- Коммит и тег --------------------------------------------------------------------
if [ "$MODE" = release ] && git diff --cached --quiet; then
    die "в проекте нет изменений относительно опубликованной версии. Нечего выпускать."
fi
git commit -q -m "Candy Userbot ${VERSION}"
git tag "v${VERSION}"

# Проверяем, что автор и коммиттер действительно анонимны.
identities="$(git log --format='%an <%ae>%n%cn <%ce>' | sort -u)"
expected="${AUTHOR_NAME} <${AUTHOR_EMAIL}>"
if [ "$identities" != "$expected" ]; then
    die "в коммите неожиданный автор: $identities (ожидался $expected)."
fi
say "Автор коммита: $expected"

# --- Отправка ------------------------------------------------------------------------
# После успешной отправки рабочая копия не нужна: удаляем (только стандартную папку).
cleanup_work() {
    if [ "$WORK" = "$DEFAULT_WORK" ] && [ "${PUBLISH_KEEP_WORK:-0}" != "1" ]; then
        cd "$HOME" && rm -rf "$WORK"
    fi
}

if [ "$DRY" -eq 1 ]; then
    say ""
    say "Пробный запуск: ничего не отправлено. Готовая копия: $WORK"
    exit 0
fi

if [ "$MODE" = release ]; then
    say "Отправляю обновление ${VERSION} в $REPO..."
    git push -q origin main
    git push -q origin "v${VERSION}"
    say ""
    say "Готово: версия ${VERSION} опубликована. Пользователи получат её командой .update."
    say "Автоматической установки нет: работающий userbot не изменён, пока вы не выполните .update."
    cleanup_work
    exit 0
fi

command -v gh >/dev/null 2>&1 || die "не найден gh: нечем создать репозиторий."
if gh repo view "$REPO" >/dev/null 2>&1; then
    die "репозиторий $REPO уже существует. Чтобы в нём не осталось старой истории, удалите его на сайте (Settings, Danger Zone, Delete this repository) и запустите скрипт снова. Копия проекта готова: $WORK"
fi

say "Создаю репозиторий $REPO и отправляю код..."
gh repo create "$REPO" --public --source . --remote origin --push \
    --description "Candy Userbot: модульный Telegram userbot на Telethon"
git push -q origin "v${VERSION}"

cleanup_work
say ""
say "Готово: https://github.com/${REPO}"
say "Проверьте на GitHub в Settings, Emails: включено «Keep my email addresses private» и «Block command line pushes that expose my email»."
say "В профиле (Settings, Public profile) в поле Name не должно быть настоящего имени."
