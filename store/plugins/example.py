# Пример плагина: команда .hello и фоновая задача. Ставится из магазина: .store install example
# Загрузка и выгрузка без перезапуска: .plugin unload example / .plugin reload example
from __future__ import annotations

from core.plugin import Plugin


class Example(Plugin):
    name = "example"
    version = "1.0.0"
    description = "Пример: команда .hello и фоновая задача"
    min_core = "0.6"       # минимальная версия ядра
    requires = ()          # имена других плагинов (файлов без .py)

    async def setup(self) -> None:
        # Всё, что зарегистрировано через self.api, снимается при выгрузке само,
        # даже если teardown() упадёт или setup() не дойдёт до конца.
        self.api.register_command(
            "hello", self.hello, description="поздороваться", usage=".hello"
        )
        await self.api.add_background_task("tick", self.tick, interval=60)
        runs = (await self.api.storage.get("runs", 0)) + 1  # своё хранилище плагина
        await self.api.storage.set("runs", runs)
        self.api.logger.info("Плагин запущен уже %d раз", runs)

    async def hello(self, event) -> None:
        await event.edit("Привет из плагина! 🍬")

    async def tick(self) -> None:
        self.api.logger.debug("tick")


def create_plugin(app):
    return Example(app)
