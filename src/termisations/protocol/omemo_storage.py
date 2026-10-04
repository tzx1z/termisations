"""Хранилище ключевого материала OMEMO поверх базы клиента.

``omemo.Storage`` - это плоское хранилище произвольных JSON-значений по
строковому ключу. Библиотека держит схему ключей внутренней и складывает туда
все сразу: идентификатор устройства, пару ключей, списки устройств собеседников,
решения о доверии и состояния Double Ratchet.

Реализация кладет это в ту же базу SQLite, что и переписку, а не в отдельный
файл. Причина: доверие к устройству и история сообщений должны сниматься одним
резервным копированием и удаляться одной командой. Права на файл базы уже 0600,
отдельный файл ключей потребовал бы повторять это правило второй раз.

Контракт библиотеки требует, чтобы запись и удаление были завершены к моменту
возврата из метода, буферизовать нельзя. Хранилище клиента это обеспечивает:
каждый вызов доходит до диска в потоке базы.
"""

import json
from typing import Any

from omemo.storage import Just, Maybe, Nothing, Storage, StorageException

from termisations.core.i18n import _
from termisations.core.storage import Storage as ClientStorage

__all__ = ["SqliteOmemoStorage"]


class SqliteOmemoStorage(Storage):
    """``omemo.Storage`` поверх ``core.storage.Storage``.

    Учетная запись задается идентификатором строки таблицы ``accounts``: ключи
    библиотеки не префиксуются адресом, и без разделения по учетной записи два
    аккаунта в одном файле затерли бы устройства друг друга.
    """

    def __init__(self, storage: ClientStorage, account: int) -> None:
        """Собрать хранилище. Кэш библиотеки оставлен включенным."""
        super().__init__()
        self._storage = storage
        self._account = account

    async def _load(self, key: str) -> Maybe[Any]:
        """Прочитать значение. ``Nothing`` означает, что ключа нет.

        Различать отсутствие ключа и значение ``null`` обязательно: библиотека
        на этом различии строит логику первого запуска.
        """
        try:
            raw = await self._storage.omemo_load(self._account, key)
        except Exception as error:
            raise StorageException(
                _("OMEMO key {key} cannot be read: {error}").format(key=key, error=error)
            ) from error
        if raw is None:
            return Nothing()
        try:
            return Just(json.loads(raw))
        except ValueError as error:
            raise StorageException(
                _("OMEMO key {key} is corrupted: {error}").format(key=key, error=error)
            ) from error

    async def _store(self, key: str, value: Any) -> None:
        """Записать значение. К возврату оно уже на диске."""
        try:
            await self._storage.omemo_save(
                self._account, key, json.dumps(value, ensure_ascii=False)
            )
        except Exception as error:
            raise StorageException(
                _("OMEMO key {key} was not written: {error}").format(key=key, error=error)
            ) from error

    async def _delete(self, key: str) -> None:
        """Удалить значение. Отсутствие ключа не ошибка."""
        try:
            await self._storage.omemo_delete(self._account, key)
        except Exception as error:
            raise StorageException(
                _("OMEMO key {key} was not deleted: {error}").format(key=key, error=error)
            ) from error
