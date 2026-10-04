"""Хранилище истории: SQLite в режиме WAL.

Таблицы добавляются вместе со своими потребителями, а не пустыми впрок:
миграция 1 несет учетные записи, контакт-лист, беседы и сообщения, миграция 2 -
хранилище ключей OMEMO, миграция 3 - курсор архива ``mam_sync_state``.

Вся работа с диском идет в одном выделенном потоке. Причина не в
производительности, а в корректности: объект соединения SQLite нельзя
использовать из двух потоков одновременно, а ``asyncio.to_thread`` отдает задачу
произвольному потоку пула. Один поток и очередь исполнителя разом снимают и
гонку, и блокировку цикла событий.

Колонка ``account_id`` стоит во всех таблицах с первой миграции. Это не задел
впрок: таблица ``accounts`` есть в схеме, и без внешнего ключа на нее переход к
нескольким учетным записям означал бы переписывание схемы вместе с данными.
"""

import asyncio
import json
import sqlite3
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final, Self

from termisations.core.i18n import _
from termisations.core.models import Conversation, Direction, Encryption, Message
from termisations.core.paths import FILE_MODE, data_file, secure_file

__all__ = ["MEMORY", "SCHEMA_VERSION", "RosterRow", "Storage", "StoredMessage"]

# Запись контакт-листа в базе: адрес, имя, подписка, группы. Кортеж, а не
# доменная модель: присутствие и статус приходят строфами и на диске не живут.
type RosterRow = tuple[str, str, str, Sequence[str]]

# Путь базы в памяти. Общий кэш и имя нужны, чтобы соединение переживало
# закрытие курсора и чтобы разные тесты не видели чужих данных.
MEMORY: Final = ":memory:"

# Версия схемы. Сверяется с PRAGMA user_version при открытии.
SCHEMA_VERSION: Final = 4

# Имя файла базы в каталоге данных.
DB_NAME: Final = "history.db"

_MIGRATIONS: Final[tuple[str, ...]] = (
    # Миграция 1: учетные записи, контакт-лист, беседы, сообщения.
    """
    CREATE TABLE accounts (
        id         INTEGER PRIMARY KEY,
        jid        TEXT NOT NULL UNIQUE,
        created_at REAL NOT NULL
    );

    CREATE TABLE roster (
        account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        jid          TEXT NOT NULL,
        name         TEXT NOT NULL DEFAULT '',
        subscription TEXT NOT NULL DEFAULT 'none',
        groups       TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (account_id, jid)
    );

    CREATE TABLE conversations (
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        jid        TEXT NOT NULL,
        title      TEXT NOT NULL DEFAULT '',
        is_muc     INTEGER NOT NULL DEFAULT 0,
        encryption TEXT NOT NULL DEFAULT 'plain',
        topic      TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (account_id, jid)
    );

    CREATE TABLE messages (
        id           INTEGER PRIMARY KEY,
        account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        conversation TEXT NOT NULL,
        message_id   TEXT NOT NULL,
        origin_id    TEXT NOT NULL DEFAULT '',
        stanza_id    TEXT NOT NULL DEFAULT '',
        sender       TEXT NOT NULL,
        body         TEXT NOT NULL,
        ts           REAL NOT NULL,
        direction    TEXT NOT NULL,
        encryption   TEXT NOT NULL,
        state        TEXT NOT NULL,
        corrected    INTEGER NOT NULL DEFAULT 0
    );

    -- Идентификатор строфы уникален в пределах учетной записи: копия из Carbons,
    -- запись из архива и живая доставка не должны давать три строки на одно
    -- сообщение. Пустое значение из проверки исключено: до XEP-0359 его не будет.
    CREATE UNIQUE INDEX messages_by_stanza
        ON messages(account_id, stanza_id) WHERE stanza_id <> '';

    CREATE UNIQUE INDEX messages_by_id
        ON messages(account_id, conversation, message_id);

    CREATE INDEX messages_by_time
        ON messages(account_id, conversation, ts);
    """,
    # Миграция 2: хранилище OMEMO.
    #
    # Одна таблица ключ-значение вместо отдельных таблиц для устройств, сессий и
    # доверия (`omemo_devices`, `omemo_sessions`, `omemo_trust`). Причина в
    # интерфейсе библиотеки: `omemo.Storage` - это плоское хранилище произвольных
    # JSON-значений по строковому ключу, и схему ключей библиотека держит
    # внутренней. Разложить ее по трем таблицам значит разбирать приватные пути
    # вида "/{namespace}/{jid}/{device}/double_ratchet" и переписывать разбор на
    # каждое обновление пакета. Списки устройств, сессии и доверие лежат здесь
    # же, каждое под своим ключом библиотеки.
    """
    CREATE TABLE omemo_store (
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        key        TEXT NOT NULL,
        value      TEXT NOT NULL,
        PRIMARY KEY (account_id, key)
    );
    """,
    # Миграция 3: курсор синхронизации архива.
    #
    # Без курсора на диске постраничный MAM превращается в повторную выкачку
    # архива при каждом запуске: клиент либо тянет все заново, либо теряет
    # промежуток между запусками.
    """
    CREATE TABLE mam_sync_state (
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        jid        TEXT NOT NULL,
        last_id    TEXT NOT NULL DEFAULT '',
        complete   INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL,
        PRIMARY KEY (account_id, jid)
    );
    """,
    # Миграция 4: счетчик непрочитанного в беседе.
    #
    # Таблица бесед существовала с первой миграции и не использовалась: список
    # выводился из сообщений через GROUP BY. Из-за этого перезапуск терял
    # название беседы, тему, признак комнаты, тип шифрования и непрочитанное, а
    # личные беседы возвращались голыми адресами.
    """
    ALTER TABLE conversations ADD COLUMN unread INTEGER NOT NULL DEFAULT 0;
    """,
)


class StoredMessage:
    """Сообщение вместе с идентификаторами строфы.

    Отдельный тип, а не поля в ``Message``: ``origin-id`` и ``stanza-id`` нужны
    хранилищу и команде /trace, а области беседы - нет. Расширять доменную модель
    ради базы значит тащить детали протокола в интерфейс.
    """

    __slots__ = ("message", "origin_id", "stanza_id")

    def __init__(self, message: Message, *, origin_id: str = "", stanza_id: str = "") -> None:
        self.message = message
        self.origin_id = origin_id
        self.stanza_id = stanza_id

    def __repr__(self) -> str:
        """Представление для отладки и сообщений падающих тестов."""
        return (
            f"StoredMessage(message_id={self.message.message_id!r}, "
            f"origin_id={self.origin_id!r}, stanza_id={self.stanza_id!r})"
        )


class Storage:
    """Хранилище истории. Все методы асинхронные, диск - в отдельном потоке."""

    def __init__(self, connection: sqlite3.Connection, executor: ThreadPoolExecutor) -> None:
        """Собрать хранилище поверх открытого соединения. Открывать через ``open``."""
        self._connection = connection
        self._executor = executor
        self._closed = False

    @classmethod
    async def open(cls, path: Path | str | None = None) -> Self:
        """Открыть базу и применить миграции.

        ``None`` означает путь по умолчанию - каталог данных выбранного профиля,
        то есть у каждой учетной записи своя история. ``MEMORY`` - базу в памяти
        для тестов и для режима эмулятора.
        """
        target = data_file(DB_NAME) if path is None else path
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="storage")
        loop = asyncio.get_running_loop()
        connection = await loop.run_in_executor(executor, _connect, target)
        await loop.run_in_executor(executor, _migrate, connection)
        if target != MEMORY:
            secure_file(Path(target))
        return cls(connection, executor)

    async def close(self) -> None:
        """Закрыть базу и погасить поток. Повторный вызов безопасен."""
        if self._closed:
            return
        # Признак ставится после закрытия, а не до: _run отказывает закрытому
        # хранилищу, и при обратном порядке соединение не закрылось бы вовсе.
        await self._run(self._connection.close)
        self._closed = True
        self._executor.shutdown(wait=False)

    async def _run(self, work: Any, *args: Any) -> Any:
        """Выполнить работу в потоке хранилища."""
        if self._closed:
            message = _("storage is closed")
            raise RuntimeError(message)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, work, *args)

    # Учетные записи.

    async def account_id(self, jid: str) -> int:
        """Идентификатор учетной записи. Запись создается при первом обращении."""
        return int(await self._run(_account_id, self._connection, jid))

    # Сообщения.

    async def save_message(self, account: int, stored: StoredMessage) -> bool:
        """Записать сообщение. ``False`` означает, что оно уже было в базе.

        Повтор - штатный случай, а не сбой: то же сообщение приходит живой
        доставкой, копией с другого устройства и записью из архива.
        """
        return bool(await self._run(_save_message, self._connection, account, stored))

    async def update_message(self, account: int, message: Message) -> None:
        """Обновить текст и состояние доставки уже записанного сообщения."""
        await self._run(_update_message, self._connection, account, message)

    async def load_messages(self, account: int, conversation: str, limit: int) -> list[Message]:
        """Последние сообщения беседы в порядке от старых к новым."""
        rows = await self._run(_load_messages, self._connection, account, conversation, limit)
        return [_message_of(row) for row in rows]

    async def conversations(self, account: int) -> list[str]:
        """Адреса бесед, по которым есть сообщения, от свежих к старым."""
        return list(await self._run(_conversations, self._connection, account))

    async def save_conversation(self, account: int, conversation: Conversation) -> None:
        """Запомнить беседу целиком: название, тему, тип и непрочитанное.

        Без этого перезапуск возвращал голые адреса: список бесед выводился из
        сообщений, а название, тема, признак комнаты и счетчик непрочитанного
        нигде не хранились.
        """
        await self._run(_save_conversation, self._connection, account, conversation)

    async def load_conversations(self, account: int) -> list[Conversation]:
        """Беседы учетной записи в том виде, в каком их закрыли."""
        rows = await self._run(_load_conversations, self._connection, account)
        return [_conversation_of(row) for row in rows]

    async def forget_conversation(self, account: int, jid: str) -> None:
        """Убрать закрытую беседу: она не должна возвращаться при запуске."""
        await self._run(_forget_conversation, self._connection, account, jid)

    # Хранилище OMEMO.

    async def omemo_load(self, account: int, key: str) -> str | None:
        """Значение ключа OMEMO или ``None``, если его нет.

        Значение отдается строкой JSON как есть: разбор делает вызывающий, а
        библиотека различает "значения нет" и "значение равно null".
        """
        row = await self._run(_omemo_load, self._connection, account, key)
        return None if row is None else str(row)

    async def omemo_save(self, account: int, key: str, value: str) -> None:
        """Записать значение ключа OMEMO. Запись обязана быть завершена к возврату."""
        await self._run(_omemo_save, self._connection, account, key, value)

    async def omemo_delete(self, account: int, key: str) -> None:
        """Удалить ключ OMEMO. Отсутствие ключа не ошибка."""
        await self._run(_omemo_delete, self._connection, account, key)

    async def omemo_clear(self, account: int) -> None:
        """Удалить весь ключевой материал учетной записи: /omemo rotate с нуля."""
        await self._run(_omemo_clear, self._connection, account)

    # Синхронизация архива.

    async def mam_cursor(self, account: int, jid: str) -> tuple[str, bool] | None:
        """Курсор архива беседы: последний известный stanza-id и признак полноты.

        ``None`` означает, что беседу еще не синхронизировали ни разу.
        """
        row = await self._run(_mam_cursor, self._connection, account, jid)
        return None if row is None else (str(row[0]), bool(row[1]))

    async def save_mam_cursor(
        self, account: int, jid: str, last_id: str, *, complete: bool
    ) -> None:
        """Записать курсор архива беседы."""
        await self._run(_save_mam_cursor, self._connection, account, jid, last_id, complete)

    async def mam_cursors(self, account: int) -> list[tuple[str, str, bool]]:
        """Все курсоры учетной записи: беседа, последний stanza-id, полнота."""
        return list(await self._run(_mam_cursors, self._connection, account))

    # Контакт-лист и беседы.

    async def save_roster(self, account: int, items: Sequence[RosterRow]) -> None:
        """Переписать контакт-лист учетной записи целиком.

        Контакт-лист приходит от сервера снимком, поэтому слияние по одной записи
        оставило бы в базе контакты, удаленные с другого устройства.
        """
        await self._run(_save_roster, self._connection, account, items)

    async def load_roster(self, account: int) -> list[tuple[str, str, str, list[str]]]:
        """Контакт-лист из базы: jid, имя, подписка, группы."""
        return list(await self._run(_load_roster, self._connection, account))


# Работа с диском: все вызывается в потоке хранилища.


def _connect(target: Path | str) -> sqlite3.Connection:
    """Открыть соединение и выставить режимы."""
    if target != MEMORY:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Файл создается сразу с нужными правами: между open и chmod он иначе
        # существует с правами по umask, и это окно наблюдаемо.
        if not path.exists():
            path.touch(mode=FILE_MODE)
    connection = sqlite3.connect(target, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # WAL: читатели не блокируют писателя. NORMAL вместо FULL - компромисс,
    # допустимый при WAL: теряется не база, а последняя транзакция при сбое ядра.
    if target != MEMORY:
        connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    """Применить недостающие миграции по PRAGMA user_version."""
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        message = _(
            "the database is newer than the program: schema version {current}, "
            "supported {supported}"
        ).format(current=current, supported=SCHEMA_VERSION)
        raise RuntimeError(message)
    for index in range(current, SCHEMA_VERSION):
        # Транзакция объявлена внутри скрипта: executescript сам
        # завершает начатую снаружи транзакцию, и внешний BEGIN до COMMIT не
        # доживает. Отметка версии идет той же транзакцией, что и схема: иначе
        # сбой между ними оставил бы таблицы без номера версии.
        script = f"BEGIN;\n{_MIGRATIONS[index]}\nPRAGMA user_version = {index + 1};\nCOMMIT;"
        try:
            connection.executescript(script)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


def _account_id(connection: sqlite3.Connection, jid: str) -> int:
    """Найти или создать учетную запись."""
    row = connection.execute("SELECT id FROM accounts WHERE jid = ?", (jid,)).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = connection.execute(
        "INSERT INTO accounts (jid, created_at) VALUES (?, ?)", (jid, time.time())
    )
    return int(cursor.lastrowid or 0)


def _save_message(connection: sqlite3.Connection, account: int, stored: StoredMessage) -> bool:
    """Вставить сообщение, пропустив дубликат по идентификатору строфы."""
    message = stored.message
    try:
        connection.execute(
            "INSERT INTO messages (account_id, conversation, message_id, origin_id, stanza_id,"
            " sender, body, ts, direction, encryption, state, corrected)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account,
                message.conversation,
                message.message_id,
                stored.origin_id,
                stored.stanza_id,
                message.sender,
                message.body,
                message.ts,
                message.direction.value,
                message.encryption.value,
                message.state.value,
                int(message.corrected),
            ),
        )
    except sqlite3.IntegrityError:
        return False
    return True


def _update_message(connection: sqlite3.Connection, account: int, message: Message) -> None:
    """Обновить текст, состояние доставки и признак корректировки."""
    connection.execute(
        "UPDATE messages SET body = ?, state = ?, corrected = ?"
        " WHERE account_id = ? AND conversation = ? AND message_id = ?",
        (
            message.body,
            message.state.value,
            int(message.corrected),
            account,
            message.conversation,
            message.message_id,
        ),
    )


def _load_messages(
    connection: sqlite3.Connection, account: int, conversation: str, limit: int
) -> list[sqlite3.Row]:
    """Последние сообщения беседы. Выборка с конца, возврат в прямом порядке."""
    rows = connection.execute(
        "SELECT * FROM messages WHERE account_id = ? AND conversation = ?"
        " ORDER BY ts DESC, id DESC LIMIT ?",
        (account, conversation, limit),
    ).fetchall()
    return list(reversed(rows))


def _conversations(connection: sqlite3.Connection, account: int) -> list[str]:
    """Беседы с сообщениями, от свежих к старым."""
    rows = connection.execute(
        "SELECT conversation, MAX(ts) AS last FROM messages WHERE account_id = ?"
        " GROUP BY conversation ORDER BY last DESC",
        (account,),
    ).fetchall()
    return [str(row["conversation"]) for row in rows]


def _save_conversation(
    connection: sqlite3.Connection, account: int, conversation: Conversation
) -> None:
    """Записать беседу, заменив прежнюю запись."""
    connection.execute(
        "INSERT INTO conversations (account_id, jid, title, is_muc, encryption, topic, unread)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (account_id, jid) DO UPDATE SET"
        " title = excluded.title, is_muc = excluded.is_muc,"
        " encryption = excluded.encryption, topic = excluded.topic,"
        " unread = excluded.unread",
        (
            account,
            conversation.jid,
            conversation.title,
            int(conversation.is_muc),
            conversation.encryption.value,
            conversation.topic,
            conversation.unread,
        ),
    )
    connection.commit()


def _load_conversations(connection: sqlite3.Connection, account: int) -> list[sqlite3.Row]:
    """Беседы учетной записи, свежие первыми по последнему сообщению."""
    return connection.execute(
        "SELECT c.jid, c.title, c.is_muc, c.encryption, c.topic, c.unread,"
        " (SELECT MAX(ts) FROM messages m"
        "  WHERE m.account_id = c.account_id AND m.conversation = c.jid) AS last"
        " FROM conversations c WHERE c.account_id = ?"
        " ORDER BY last DESC NULLS LAST, c.jid",
        (account,),
    ).fetchall()


def _forget_conversation(connection: sqlite3.Connection, account: int, jid: str) -> None:
    """Удалить запись беседы. Сообщения остаются: история беседы не пропадает."""
    connection.execute("DELETE FROM conversations WHERE account_id = ? AND jid = ?", (account, jid))
    connection.commit()


def _conversation_of(row: sqlite3.Row) -> Conversation:
    """Собрать беседу из строки базы."""
    return Conversation(
        jid=str(row["jid"]),
        title=str(row["title"]) or str(row["jid"]),
        is_muc=bool(row["is_muc"]),
        unread=int(row["unread"]),
        encryption=_encryption_of(str(row["encryption"])),
        topic=str(row["topic"]),
    )


def _encryption_of(value: str) -> Encryption:
    """Тип шифрования из базы. Неизвестное значение считается открытым текстом."""
    try:
        return Encryption(value)
    except ValueError:
        return Encryption.PLAIN


def _save_roster(
    connection: sqlite3.Connection,
    account: int,
    items: Sequence[RosterRow],
) -> None:
    """Переписать контакт-лист одной транзакцией."""
    connection.execute("BEGIN")
    try:
        connection.execute("DELETE FROM roster WHERE account_id = ?", (account,))
        connection.executemany(
            "INSERT INTO roster (account_id, jid, name, subscription, groups)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (account, jid, name, subscription, json.dumps(list(groups), ensure_ascii=False))
                for jid, name, subscription, groups in items
            ],
        )
    except Exception:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def _load_roster(
    connection: sqlite3.Connection, account: int
) -> list[tuple[str, str, str, list[str]]]:
    """Контакт-лист из базы."""
    rows = connection.execute(
        "SELECT jid, name, subscription, groups FROM roster WHERE account_id = ? ORDER BY jid",
        (account,),
    ).fetchall()
    return [
        (str(row["jid"]), str(row["name"]), str(row["subscription"]), _groups(row["groups"]))
        for row in rows
    ]


def _mam_cursor(connection: sqlite3.Connection, account: int, jid: str) -> tuple[str, int] | None:
    """Курсор архива беседы."""
    row = connection.execute(
        "SELECT last_id, complete FROM mam_sync_state WHERE account_id = ? AND jid = ?",
        (account, jid),
    ).fetchone()
    return None if row is None else (str(row["last_id"]), int(row["complete"]))


def _save_mam_cursor(
    connection: sqlite3.Connection, account: int, jid: str, last_id: str, complete: bool
) -> None:
    """Записать курсор архива беседы."""
    connection.execute(
        "INSERT INTO mam_sync_state (account_id, jid, last_id, complete, updated_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (account_id, jid) DO UPDATE SET"
        " last_id = excluded.last_id, complete = excluded.complete,"
        " updated_at = excluded.updated_at",
        (account, jid, last_id, int(complete), time.time()),
    )


def _mam_cursors(connection: sqlite3.Connection, account: int) -> list[tuple[str, str, bool]]:
    """Все курсоры учетной записи."""
    rows = connection.execute(
        "SELECT jid, last_id, complete FROM mam_sync_state WHERE account_id = ? ORDER BY jid",
        (account,),
    ).fetchall()
    return [(str(row["jid"]), str(row["last_id"]), bool(row["complete"])) for row in rows]


def _omemo_load(connection: sqlite3.Connection, account: int, key: str) -> str | None:
    """Прочитать значение ключа OMEMO."""
    row = connection.execute(
        "SELECT value FROM omemo_store WHERE account_id = ? AND key = ?", (account, key)
    ).fetchone()
    return None if row is None else str(row["value"])


def _omemo_save(connection: sqlite3.Connection, account: int, key: str, value: str) -> None:
    """Записать значение ключа OMEMO."""
    connection.execute(
        "INSERT INTO omemo_store (account_id, key, value) VALUES (?, ?, ?)"
        " ON CONFLICT (account_id, key) DO UPDATE SET value = excluded.value",
        (account, key, value),
    )


def _omemo_delete(connection: sqlite3.Connection, account: int, key: str) -> None:
    """Удалить ключ OMEMO."""
    connection.execute("DELETE FROM omemo_store WHERE account_id = ? AND key = ?", (account, key))


def _omemo_clear(connection: sqlite3.Connection, account: int) -> None:
    """Удалить весь ключевой материал учетной записи."""
    connection.execute("DELETE FROM omemo_store WHERE account_id = ?", (account,))


def _groups(value: str) -> list[str]:
    """Разобрать список групп. Испорченное значение не должно ронять загрузку."""
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _message_of(row: sqlite3.Row) -> Message:
    """Собрать доменную модель из строки базы.

    Метки расширений не восстанавливаются: они относятся к протокольному пути
    строфы, а не к сообщению, и хранить их снимком значит показывать вчерашнее
    состояние доставки как сегодняшнее.
    """
    return Message(
        message_id=str(row["message_id"]),
        conversation=str(row["conversation"]),
        sender=str(row["sender"]),
        body=str(row["body"]),
        ts=float(row["ts"]),
        direction=Direction(str(row["direction"])),
        encryption=Encryption(str(row["encryption"])),
        state=_state(str(row["state"])),
        corrected=bool(row["corrected"]),
    )


def _state(value: str) -> Any:
    """Состояние доставки из базы. Неизвестное значение не должно ронять загрузку."""
    from termisations.core.models import DeliveryState

    try:
        return DeliveryState(value)
    except ValueError:
        return DeliveryState.RECEIVED
