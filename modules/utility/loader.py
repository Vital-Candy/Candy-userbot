"""Регистрация модулей-утилит (.user, .ping, .tag, .purge, .mute); вызывается из modules/system/loader.py."""
from __future__ import annotations

from modules.utility.mute import MuteModule
from modules.utility.ping import PingModule
from modules.utility.purge import PurgeModule
from modules.utility.tag import TagModule
from modules.utility.user import UserModule


def register_utility_modules(app) -> None:
    """Регистрирует модули команд ``.user``, ``.ping``, ``.tag``, ``.purge`` и ``.mute``."""
    for module in (UserModule, PingModule, TagModule, PurgeModule, MuteModule):
        app.modules.register(module(app))
