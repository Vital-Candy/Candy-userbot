# Candy Userbot 2.0 — архитектура и решения

Документ фиксирует целевое дерево каталогов и принятые решения. Реализация идёт по этапам,
после каждого этапа проект запускается и проходит тесты.

## Принятые решения

1. **Сбой модуля при старте.** Модуль либо обязательный (`required = True`), либо необязательный.
   - Обязательный модуль (системные, хранилище, события) не запустился → запуск откатывается, процесс завершается с ошибкой.
   - Необязательный модуль не запустился → в лог и владельцу в Telegram уходит уведомление, модуль помечается `FAILED`, userbot продолжает работать.
2. **Аккаунты.** Одновременно работает один активный аккаунт. Переключение: остановка клиента и подключение другого.
3. **Управление.** Через Telegram-команды (`.help`, `.status`, `.plugin ...`, `.account ...`, `.backup ...`).
   Команды живут в системных модулях и доступны уровню ADMIN (сам аккаунт всегда ADMIN).
   Локальная консоль остаётся как дополнительный способ, когда есть терминал.

## Правила именования (отличия от первоначального плана)

| Было в плане | Стало | Причина |
|---|---|---|
| `logging/` | `utils/logger.py` | пакет `logging` в корне закрыл бы стандартный модуль |
| `plugins/loader.py`, `registry.py`, `api.py` | `core/plugins.py`, `core/plugin_api.py` | `plugins/` — папка пользовательских плагинов (`plugins/*.py` в `.gitignore`) |
| `console/` | `core/console.py` | отдельный пакет только когда файл перерастёт ~300 строк |
| `.label X` для создания и для обращения | `.label X` создаёт, `.goto X` обращается | одна команда не может делать оба действия |
| `modules/scheduler/` | вне плана | нет этапа, добавим отдельно, если понадобится |

## Целевое дерево

```text
CandyUserbot/
├── main.py
├── core/
│   ├── app.py            # UserbotApp
│   ├── lifecycle.py      # состояния new/starting/running/stopping/stopped/failed
│   ├── shutdown.py       # шаги остановки/отката с таймаутами
│   ├── health.py         # HealthReport: ok / degraded / down
│   ├── doctor.py         # python main.py --check
│   ├── menu.py           # терминальное меню (ANSI, без библиотек)
│   ├── updater.py        # обновление из GitHub: проверка, замена, откат
│   ├── client.py
│   ├── events.py
│   ├── dispatcher.py
│   ├── parser.py
│   ├── filters.py
│   ├── commands.py
│   ├── module.py
│   ├── modules.py
│   ├── background.py
│   ├── plugin.py
│   ├── plugins.py
│   ├── plugin_api.py     # PluginAPI: учёт регистраций плагина
│   ├── version.py        # CORE_VERSION для min_core
│   ├── access.py         # AccessService, require(level)
│   ├── reply.py          # answer(): ответ на команду без падений
│   ├── summary.py        # тексты для .status и консоли
│   ├── console.py
│   ├── identity.py
│   └── errors.py
├── accounts/             # account, manager, auth, backup, migration, control (этап 2)
├── modules/
│   ├── system/           # status, heartbeat, accounts (.account), backup (.backup), plugins (.plugin), update (.update), restart (.restart), prefix (.setprefix)
│   ├── storage/          # .label, .labels, .goto, .unlabel
│   ├── clock/            # .time: часы в имени (styles, service, module)
│   ├── spam/             # .spam: повтор сообщения с паузой (parser, module)
│   ├── utility/          # .user, .ping, .tag, .purge, .mute (text, user, ping, tag, purge, mute)
│   └── permissions/
├── storage/              # database, models, labels, permissions, service, migrations/
├── plugins/              # только пользовательские плагины
├── config/               # settings.py, defaults.py, env.py
├── utils/                # logger.py, prompt.py, files.py (права 600/700)
├── data/                 # accounts/, database/, backups/, cache/  (не в git)
├── logs/                 # candy.log с ротацией, не в git
├── deploy/               # candy-userbot.service (systemd)
├── run.sh                # супервизор: перезапуск только после кода 1
├── installer.sh          # установка в Termux/Linux: git clone + зависимости + проверка
├── tools/publish.sh      # безопасная публикация на GitHub: чистая история, анонимный автор
├── logo.txt              # ASCII-логотип для меню (необязателен, не затирается обновлением)
├── assets/               # картинки репозитория
├── LICENCE               # Apache 2.0
├── tests/
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

Файлы создаются по мере этапов, пустые заготовки не добавляются.

## Этапы и теги

| Этап | Тег |
|---|---|
| 0. Рабочая основа | `v0.0` |
| 1. Foundation | `v0.1` |
| 2. Accounts | `v0.2` |
| 3. Events / Dispatcher | `v0.3` |
| 4. Modules / Background | `v0.4` |
| 5. Storage / Labels / Permissions | `v0.5` |
| 6. Plugins / Console | `v0.6` |
| 7. Hardening / Stable | `v1.0` |
| Меню и обновление из GitHub | `v1.1` |
