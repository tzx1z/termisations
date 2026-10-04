"""Подключение под жадной фабрикой задач.

Textual ставит циклу ``asyncio.eager_task_factory`` в ``App.run_async``, а
``App.run_test`` этот код обходит. Из-за этого тесты через ``App.run_test``
проверяют подключение в условиях, которых в настоящем запуске не бывает. Тесты
этого модуля проходят и под жадной фабрикой.

Сервер здесь не нужен: слушатель на 127.0.0.1 отвечает за факт открытия сокета,
большего для проверки пути ``connect()`` не требуется. Приватные поля slixmpp не
читаются: признаком служат принятое соединение и байты потока на слушателе.
"""

import asyncio
import contextlib
import inspect
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

from slixmpp.xmlstream import XMLStream
from textual.app import App

from termisations.app import TermisationsApp
from termisations.core.events import Event, EventBus, Notice, NoticeLevel, RunCommandLine
from termisations.core.models import ConnectionStage
from termisations.protocol import session as session_module
from termisations.protocol.account import Account
from termisations.protocol.session import SlixmppSession

# Путь целиком лежит внутри машины, секунды хватает с запасом.
OPEN_TIMEOUT: Final = 2.0

# Размер экрана для приложения: меньше минимальной ширины раскладка сжимается,
# к проверке подключения это отношения не имеет.
APP_SIZE: Final = (240, 50)

# Пароль в этих тестах не проверяется: до SASL дело не доходит ни разу.
PASSWORD: Final = "пароль не проверяется"


@dataclass(slots=True)
class Listener:
    """Слушатель на петле: порт, счетчик соединений и первые байты потока."""

    port: int = 0
    opened: int = 0
    data: bytearray = field(default_factory=bytearray)


@asynccontextmanager
async def listener(*, hold: bool = False) -> AsyncIterator[Listener]:
    """Поднять TCP-слушателя на 127.0.0.1 со свободным портом.

    Рукопожатие XMPP не разыгрывается: сервер либо читает заголовок потока и
    закрывает сокет, либо (``hold``) держит соединение открытым и молчит - так
    проверяется сторожевой таймер.
    """
    state = Listener()
    forever = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        state.opened += 1
        with contextlib.suppress(OSError):
            state.data += await reader.read(4096)
        if hold:
            await forever.wait()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    state.port = int(server.sockets[0].getsockname()[1])
    try:
        yield state
    finally:
        forever.set()
        server.close()
        await server.wait_closed()


def closed_port() -> int:
    """Порт, который точно никто не слушает: занять и сразу освободить."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def make_session(bus: EventBus, port: int, *, resource: str = "factory") -> SlixmppSession:
    """Сессия, нацеленная на локальный слушатель."""
    account = Account(
        jid="alice@localhost",
        resource=resource,
        host="127.0.0.1",
        port=port,
        direct_tls=False,
        tls_verify=False,
    )
    return SlixmppSession(bus, account)


@asynccontextmanager
async def running(session: SlixmppSession) -> AsyncIterator[None]:
    """Запустить сессию публичным ``run()`` и погасить ее вместе с задачами."""
    task = asyncio.create_task(session.run(), name="session")
    try:
        yield
    finally:
        session.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_connect_opens_socket(task_factory, monkeypatch, wait_for) -> None:
    """Сокет к серверу открывается при любой фабрике задач.

    Под жадной фабрикой тело ``_connect_loop`` выполняется до присваивания
    ``_current_connection_attempt``, и без обхода подключение завершается, не
    дойдя до ``create_connection``. Параметризация делает
    проверку обязательной для обоих режимов, а не только для удобного тестам.
    """
    monkeypatch.setenv("TERMISATIONS_PASSWORD", PASSWORD)
    async with listener() as server:
        session = make_session(EventBus(), server.port)
        async with running(session):
            assert await wait_for(lambda: server.opened > 0, OPEN_TIMEOUT)
            assert await wait_for(lambda: b"jabber:client" in bytes(server.data), OPEN_TIMEOUT)


async def test_task_factory_is_restored(task_factory, monkeypatch, wait_for) -> None:
    """Фабрика задач возвращается на место после подключения.

    Жадную фабрику ставит Textual для своих задач: снявший обязан вернуть, иначе
    одно подключение меняет режим планирования всему приложению.
    """
    monkeypatch.setenv("TERMISATIONS_PASSWORD", PASSWORD)
    loop = asyncio.get_running_loop()
    before = loop.get_task_factory()
    async with listener() as server:
        session = make_session(EventBus(), server.port)
        async with running(session):
            assert await wait_for(lambda: server.opened > 0, OPEN_TIMEOUT)
    assert loop.get_task_factory() is before


async def test_refused_port_fails_fast(task_factory, monkeypatch, wait_for) -> None:
    """Закрытый порт дает ошибку подключения сразу, а не по сторожевому таймеру.

    Тест отличает настоящую починку от маскировки сторожем: предел сторожа
    заведомо больше окна ожидания, поэтому ошибка может прийти только из
    ``connection_failed``, то есть из ``create_connection``.
    """
    monkeypatch.setenv("TERMISATIONS_PASSWORD", PASSWORD)
    monkeypatch.setattr(session_module, "CONNECT_DEADLINE", 30.0)
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(Event, events.append)
    session = make_session(bus, closed_port())
    async with running(session):
        assert await wait_for(lambda: session.state.stage is ConnectionStage.ERROR, 3.0)
    assert any(
        isinstance(item, Notice) and "соединение не установлено" in item.text for item in events
    )


async def test_watchdog_breaks_silent_handshake(task_factory, monkeypatch, wait_for) -> None:
    """Молчащий сервер приводит к названной ошибке, а не к вечному спиннеру."""
    monkeypatch.setenv("TERMISATIONS_PASSWORD", PASSWORD)
    monkeypatch.setattr(session_module, "CONNECT_DEADLINE", 0.3)
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(Event, events.append)
    async with listener(hold=True) as server:
        session = make_session(bus, server.port)
        async with running(session):
            assert await wait_for(lambda: session.state.stage is ConnectionStage.ERROR, 3.0)
    assert any(
        isinstance(item, Notice)
        and item.level is NoticeLevel.ERROR
        and "не завершилось" in item.text
        for item in events
    )


async def test_reconnect_opens_new_socket(task_factory, monkeypatch, wait_for) -> None:
    """Команда /reconnect тоже доходит до сокета, а не только первое подключение."""
    monkeypatch.setenv("TERMISATIONS_PASSWORD", PASSWORD)
    async with listener(hold=True) as server:
        session = make_session(EventBus(), server.port)
        async with running(session):
            assert await wait_for(lambda: server.opened >= 1, OPEN_TIMEOUT)
            await session.handle_command(RunCommandLine("/reconnect"))
            assert await wait_for(lambda: server.opened >= 2, OPEN_TIMEOUT)


async def test_app_connects_under_eager_task_factory(monkeypatch, wait_for) -> None:
    """Подключение доходит до сокета и при запуске через приложение.

    ``App.run_test`` жадную фабрику не ставит, поэтому тест ставит ее сам: так
    воспроизводится то, что делает ``App.run_async`` в настоящем терминале,
    вместе со всем путем on_mount - run_worker - session.run.
    """
    monkeypatch.setenv("TERMISATIONS_PASSWORD", PASSWORD)
    loop = asyncio.get_running_loop()
    saved = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        async with listener(hold=True) as server:
            bus = EventBus()
            session = make_session(bus, server.port, resource="app")
            app = TermisationsApp(bus, session, layout="split")
            async with app.run_test(size=APP_SIZE):
                assert await wait_for(lambda: server.opened > 0, OPEN_TIMEOUT)
    finally:
        loop.set_task_factory(saved)


def test_textual_still_sets_eager_task_factory() -> None:
    """Обход рассчитан на то, что Textual ставит жадную фабрику в run_async.

    Если строка из фреймворка исчезнет, тест не пройдет и напомнит, что обход в
    ``_connect`` можно снимать.
    """
    assert "eager_task_factory" in inspect.getsource(App.run_async)


def test_slixmpp_still_assigns_attempt_after_ensure_future() -> None:
    """Причина обхода лежит в порядке действий внутри slixmpp.

    ``connect()`` присваивает ``_current_connection_attempt`` после
    ``ensure_future``, а ``_attempt_connection`` читает его до
    ``create_connection``. Пока это так, обход нужен.
    """
    assert "self._current_connection_attempt = asyncio.ensure_future" in inspect.getsource(
        XMLStream.connect
    )
    assert "if self._current_connection_attempt is None" in inspect.getsource(
        XMLStream._attempt_connection
    )


def test_reschedule_suspends_before_the_check() -> None:
    """Повтор попытки под жадной фабрикой работает сам, обход ему не нужен.

    ``reschedule_connection_attempt`` увеличивает паузу перед созданием задачи,
    поэтому повторный ``_connect_loop`` приостанавливается на ``asyncio.sleep``
    раньше проверки. Тест фиксирует это как причину, по которой обход поставлен
    только на первый вызов ``connect()``.
    """
    source = inspect.getsource(XMLStream._connect_loop)
    assert "if self._connect_loop_wait > 0:" in source
    assert "await asyncio.sleep(self._connect_loop_wait)" in source
    reschedule = inspect.getsource(XMLStream.reschedule_connection_attempt)
    assert reschedule.index("_connect_loop_wait") < reschedule.index("ensure_future")
