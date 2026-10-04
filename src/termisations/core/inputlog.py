"""История ввода: общий для всех профилей файл введенных команд.

Профили разводят по каталогам переписку, ключи и кэш, но набранная команда
принадлежит не учетной записи, а человеку за терминалом: заведя второй аккаунт,
он не должен начинать со стрелкой вверх с нуля. Поэтому файл истории один на все
профили и лежит в корне каталога данных, рядом с каталогами профилей.

В общий файл идут только слэш-команды. Тексты сообщений туда писать нельзя:
профили заведены ровно затем, чтобы переписка одной учетной записи не попадала в
данные другой, и общий файл с телами сообщений перечеркнул бы это. Текст
сообщений остается в истории сеанса, в памяти виджета ввода.

Формат - SQLite, а не текстовый файл в духе ``.bash_history``. Причин три: у
клиента есть многострочный ввод, и в тексте его пришлось бы экранировать;
обрезка по лимиту в тексте означает перезапись файла целиком, а перезапись из
второго процесса теряет чужие записи; два клиента разных профилей работают
одновременно, и разводить их записи должна база, а не блокировка поверх файла.

Вся работа с диском идет в одном выделенном потоке, как в ``core/storage.py``:
соединение SQLite нельзя отдавать произвольному потоку пула, а запись в цикле
событий - это пауза на каждый Enter.

Маскирование ``redact`` при записи не применяется: замаскированную команду
нельзя повторить, а повтор - вся польза истории. Защита тут та же, что у
``/log save``, - права 0600 на файле.
"""

import asyncio
import contextlib
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final, Self

from termisations.core.i18n import _
from termisations.core.paths import FILE_MODE, secure_file, shared_data_file

__all__ = [
    "HISTORY_LIMIT",
    "LOG_NAME",
    "MEMORY",
    "SCHEMA_VERSION",
    "InputLog",
    "is_command",
]

# Путь базы в памяти. Нужен тестам и эмулятору без файла истории.
MEMORY: Final = ":memory:"

# Имя файла в корне каталога данных.
LOG_NAME: Final = "input-history.db"

# Сколько строк держится в файле и в памяти. Лимиты одинаковые: поиск Ctrl+R
# идет по памяти, и записи сверх нее не нашлись бы ни одним запросом.
HISTORY_LIMIT: Final = 5000

# Версия схемы. Сверяется с PRAGMA user_version при открытии.
SCHEMA_VERSION: Final = 1

# Через сколько записей выполняется обрезка по лимиту. На каждой вставке она не
# нужна: лишние сто строк в файле не мешают, а лишний DELETE идет в журнал WAL.
_TRIM_EVERY: Final = 100

_log = logging.getLogger(__name__)

_MIGRATIONS: Final[tuple[str, ...]] = (
    # Миграция 1: строки ввода. Время записи хранится для разбора глазами:
    # программе оно не нужно, порядок задает первичный ключ.
    """
    CREATE TABLE input_history (
        id      INTEGER PRIMARY KEY,
        line    TEXT NOT NULL,
        used_at REAL NOT NULL
    );
    """,
)


def is_command(line: str) -> bool:
    """Идет ли строка в общий файл истории.

    Команда - строка, начинающаяся с одиночного слэша. Двойной слэш экранирует
    команду и отправляется как текст, поэтому командой не считается.
    """
    text = line.strip()
    return text.startswith("/") and not text.startswith("//")


class InputLog:
    """История ввода. Все методы асинхронные, диск - в отдельном потоке."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        executor: ThreadPoolExecutor,
        lines: tuple[str, ...],
        *,
        writable: bool,
        limit: int,
    ) -> None:
        """Собрать историю поверх открытого соединения. Открывать через ``open``."""
        self._connection = connection
        self._executor = executor
        self._lines = lines
        self._writable = writable
        self._limit = limit
        self._since_trim = 0
        self._closed = False

    @property
    def lines(self) -> tuple[str, ...]:
        """Строки, прочитанные при открытии: старые первыми, свежие в конце.

        Снимок на момент старта. Записи, сделанные соседним клиентом уже после,
        сюда не приезжают: синхронизация на лету не нужна, история подхватится
        при следующем запуске.
        """
        return self._lines

    @property
    def writable(self) -> bool:
        """Пишется ли история. Ложь у эмулятора: он следов не оставляет."""
        return self._writable

    @classmethod
    async def open(
        cls,
        path: Path | str | None = None,
        *,
        writable: bool = True,
        limit: int = HISTORY_LIMIT,
    ) -> Self:
        """Открыть файл истории и прочитать его в память.

        ``None`` означает общий файл в корне каталога данных. ``writable=False``
        открывает базу только на чтение: эмулятору история видна, но его команды
        в нее не попадают. Отсутствующий файл в этом режиме не создается, и
        история получается пустой.
        """
        target = shared_data_file(LOG_NAME) if path is None else path
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inputlog")
        loop = asyncio.get_running_loop()
        connection = await loop.run_in_executor(executor, _connect, target, writable)
        if writable:
            await loop.run_in_executor(executor, _migrate, connection)
            if target != MEMORY:
                secure_file(Path(target))
            await loop.run_in_executor(executor, _trim, connection, limit)
        lines = await loop.run_in_executor(executor, _load, connection, limit)
        return cls(connection, executor, tuple(lines), writable=writable, limit=limit)

    async def append(self, line: str) -> None:
        """Записать строку. Не команда, пустая строка и повтор подряд не пишутся.

        Отказ записи наружу не идет: история - вспомогательная вещь, и упавший
        диск не должен мешать переписке. Строка теряется, причина остается в
        журнале клиента.
        """
        if not self._writable or self._closed or not is_command(line):
            return
        self._since_trim += 1
        trim = self._since_trim >= _TRIM_EVERY
        if trim:
            self._since_trim = 0
        try:
            await self._run(_append, self._connection, line, self._limit, trim)
        except (OSError, sqlite3.Error, RuntimeError) as error:
            _log.warning(_("input history not written: %s"), error)

    async def close(self) -> None:
        """Закрыть базу и погасить поток. Повторный вызов безопасен."""
        if self._closed:
            return
        # Признак ставится после закрытия: _run отказывает закрытой истории, и
        # обратный порядок не дал бы закрыть соединение вовсе.
        await self._run(self._connection.close)
        self._closed = True
        self._executor.shutdown(wait=False)

    async def _run(self, work: Any, *args: Any) -> Any:
        """Выполнить работу в потоке истории."""
        if self._closed:
            message = _("input history is closed")
            raise RuntimeError(message)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, work, *args)


def _connect(target: Path | str, writable: bool) -> sqlite3.Connection:
    """Открыть соединение и выставить режимы.

    Без права записи отсутствующий файл не создается: эмулятор не должен
    оставлять на диске даже пустую базу. Вместо него открывается база в памяти,
    и история получается пустой.
    """
    if target == MEMORY:
        return _tune(sqlite3.connect(MEMORY, isolation_level=None))
    path = Path(target)
    if not writable:
        if not path.is_file():
            return _tune(sqlite3.connect(MEMORY, isolation_level=None))
        return _tune(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Файл создается сразу с нужными правами: между open и chmod он иначе
    # существует с правами по umask, и это окно наблюдаемо.
    if not path.exists():
        path.touch(mode=FILE_MODE)
    connection = _tune(sqlite3.connect(path, isolation_level=None))
    # WAL: два клиента разных профилей пишут в один файл, и читатель не должен
    # блокировать писателя.
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _tune(connection: sqlite3.Connection) -> sqlite3.Connection:
    """Общие режимы соединения."""
    connection.execute("PRAGMA synchronous = NORMAL")
    # Ожидание чужой записи: соседний клиент держит базу доли миллисекунды,
    # но под нагрузкой отказ вместо ожидания потерял бы строку.
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    """Применить недостающие миграции по PRAGMA user_version."""
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        message = _(
            "the input history is newer than the program: schema version {current}, "
            "supported {supported}"
        ).format(current=current, supported=SCHEMA_VERSION)
        raise RuntimeError(message)
    for index in range(current, SCHEMA_VERSION):
        # Транзакция объявлена внутри скрипта: executescript завершает начатую
        # снаружи, а отметка версии должна идти той же транзакцией, что и схема.
        script = f"BEGIN;\n{_MIGRATIONS[index]}\nPRAGMA user_version = {index + 1};\nCOMMIT;"
        try:
            connection.executescript(script)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


def _load(connection: sqlite3.Connection, limit: int) -> list[str]:
    """Последние строки истории, старые первыми.

    Испорченный или чужой файл не должен мешать запуску, поэтому ошибка чтения
    дает пустую историю: клиент работает, просто листать нечего.
    """
    try:
        rows = connection.execute(
            "SELECT line FROM input_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    except sqlite3.Error:
        return []
    return [str(row[0]) for row in reversed(rows)]


def _append(connection: sqlite3.Connection, line: str, limit: int, trim: bool) -> None:
    """Записать строку и, если пора, обрезать историю по лимиту."""
    last = connection.execute("SELECT line FROM input_history ORDER BY id DESC LIMIT 1").fetchone()
    if last is not None and str(last[0]) == line:
        return
    connection.execute(
        "INSERT INTO input_history (line, used_at) VALUES (?, ?)", (line, time.time())
    )
    if trim:
        _trim(connection, limit)


def _trim(connection: sqlite3.Connection, limit: int) -> None:
    """Оставить в файле последние ``limit`` строк.

    Отказ обрезки не важен: база занята соседним клиентом или открыта на чтение,
    и лишние строки просто дождутся следующего раза.
    """
    with contextlib.suppress(sqlite3.Error):
        connection.execute(
            "DELETE FROM input_history WHERE id <= "
            "(SELECT id FROM input_history ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (limit,),
        )
