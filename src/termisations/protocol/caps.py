"""Кэш возможностей собеседников по XEP-0115.

Хранилище отдельное, в каталоге кэша, а не в основной базе. Причина: кэш можно
удалить в любой момент, и это не должно даже теоретически задевать переписку и
решения о доверии к устройствам. Восстанавливается он сам, одним кругом disco.

Ключ - строка проверки (``ver``) по XEP-0115, а не адрес собеседника. Так и
задумано расширением: одинаковые клиенты дают одинаковый хэш, и ответ disco
скачивается один раз на всех. Отдельная таблица связывает адрес с хэшем: по нему
и определяется, изменился ли набор возможностей у собеседника.

Без кэша каждый ``/caps`` и каждый выбор варианта OMEMO означают круг disco к
собеседнику. На десятке контактов это заметный служебный трафик при каждом
подключении.
"""

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final, Self

from termisations.core.i18n import _
from termisations.core.paths import cache_file

__all__ = ["CAPS_FILE", "CapsCache", "CapsEntry"]

# Имя файла кэша в каталоге кэша.
CAPS_FILE: Final = "caps.db"

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS caps (
    verstring TEXT PRIMARY KEY,
    features  TEXT NOT NULL,
    identities TEXT NOT NULL,
    stored_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS caps_jids (
    jid       TEXT PRIMARY KEY,
    verstring TEXT NOT NULL,
    seen_at   REAL NOT NULL
);
"""


class CapsEntry:
    """Возможности одного набора: список фич и список удостоверений."""

    __slots__ = ("features", "identities", "verstring")

    def __init__(
        self, verstring: str, features: tuple[str, ...], identities: tuple[str, ...]
    ) -> None:
        """Собрать запись кэша."""
        self.verstring = verstring
        self.features = features
        self.identities = identities

    def __repr__(self) -> str:
        """Представление для отладки."""
        return f"CapsEntry(verstring={self.verstring!r}, features={len(self.features)})"


class CapsCache:
    """Кэш возможностей. Диск в отдельном потоке, как и основная база."""

    def __init__(self, connection: sqlite3.Connection, executor: ThreadPoolExecutor) -> None:
        """Собрать кэш поверх открытого соединения. Открывать через ``open``."""
        self._connection = connection
        self._executor = executor
        self._closed = False

    @classmethod
    async def open(cls, path: Path | str | None = None) -> Self:
        """Открыть кэш. ``None`` означает каталог кэша выбранного профиля."""
        target = cache_file(CAPS_FILE) if path is None else path
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="caps")
        loop = asyncio.get_running_loop()
        connection = await loop.run_in_executor(executor, _connect, target)
        return cls(connection, executor)

    async def close(self) -> None:
        """Закрыть кэш. Повторный вызов безопасен."""
        if self._closed:
            return
        await self._run(self._connection.close)
        self._closed = True
        self._executor.shutdown(wait=False)

    async def _run(self, work: Any, *args: Any) -> Any:
        """Выполнить работу в потоке кэша."""
        if self._closed:
            message = _("capabilities cache is closed")
            raise RuntimeError(message)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, work, *args)

    async def verstring_of(self, jid: str) -> str:
        """Известная строка проверки собеседника. Пустая строка, если ее нет."""
        return str(await self._run(_verstring_of, self._connection, jid) or "")

    async def get(self, verstring: str) -> CapsEntry | None:
        """Возможности по строке проверки. ``None``, если их в кэше нет."""
        row = await self._run(_get, self._connection, verstring)
        if row is None:
            return None
        features, identities = row
        return CapsEntry(verstring, _split(features), _split(identities))

    async def put(
        self, verstring: str, features: tuple[str, ...], identities: tuple[str, ...]
    ) -> None:
        """Положить возможности в кэш."""
        await self._run(_put, self._connection, verstring, features, identities)

    async def bind(self, jid: str, verstring: str) -> None:
        """Связать адрес со строкой проверки.

        Смена хэша у собеседника означает смену набора возможностей: запись
        просто перезаписывается, а старый набор остается в кэше - он может быть
        общим с другими контактами.
        """
        await self._run(_bind, self._connection, jid, verstring)

    async def forget(self, jid: str) -> None:
        """Забыть связь адреса с хэшем: следующий запрос пойдет в сеть."""
        await self._run(_forget, self._connection, jid)


def _connect(target: Path | str) -> sqlite3.Connection:
    """Открыть соединение кэша и создать схему."""
    if target != ":memory:":
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, isolation_level=None)
    connection.row_factory = sqlite3.Row
    if target != ":memory:":
        connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.executescript(_SCHEMA)
    return connection


def _verstring_of(connection: sqlite3.Connection, jid: str) -> str | None:
    """Строка проверки, связанная с адресом."""
    row = connection.execute("SELECT verstring FROM caps_jids WHERE jid = ?", (jid,)).fetchone()
    return None if row is None else str(row["verstring"])


def _get(connection: sqlite3.Connection, verstring: str) -> tuple[str, str] | None:
    """Возможности по строке проверки."""
    row = connection.execute(
        "SELECT features, identities FROM caps WHERE verstring = ?", (verstring,)
    ).fetchone()
    return None if row is None else (str(row["features"]), str(row["identities"]))


def _put(
    connection: sqlite3.Connection,
    verstring: str,
    features: tuple[str, ...],
    identities: tuple[str, ...],
) -> None:
    """Записать возможности."""
    connection.execute(
        "INSERT INTO caps (verstring, features, identities, stored_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (verstring) DO UPDATE SET features = excluded.features,"
        " identities = excluded.identities, stored_at = excluded.stored_at",
        (verstring, "\n".join(features), "\n".join(identities), time.time()),
    )


def _bind(connection: sqlite3.Connection, jid: str, verstring: str) -> None:
    """Связать адрес со строкой проверки."""
    connection.execute(
        "INSERT INTO caps_jids (jid, verstring, seen_at) VALUES (?, ?, ?)"
        " ON CONFLICT (jid) DO UPDATE SET verstring = excluded.verstring,"
        " seen_at = excluded.seen_at",
        (jid, verstring, time.time()),
    )


def _forget(connection: sqlite3.Connection, jid: str) -> None:
    """Забыть связь адреса с хэшем."""
    connection.execute("DELETE FROM caps_jids WHERE jid = ?", (jid,))


def _split(value: str) -> tuple[str, ...]:
    """Разобрать список, сохраненный строками."""
    return tuple(item for item in value.split("\n") if item)
