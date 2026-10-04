"""Сессия на настоящем сервере.

Тесты помечены маркером ``live`` и пропускаются, если сервер не поднят. Поднять
его можно так (ejabberd в контейнере, учетки создаются командой register):

    podman run -d --name termisations-xmpp -p 5222:5222 -p 5223:5223 \\
        docker.io/ejabberd/ecs:latest
    podman exec termisations-xmpp ejabberdctl register alice localhost PASSWORD
    podman exec termisations-xmpp ejabberdctl register bob localhost PASSWORD

Адрес и учетные данные берутся из переменных окружения, значения по умолчанию
совпадают с этой командой.
"""

import asyncio
import contextlib
import os
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Final

import pytest

from termisations.core.events import (
    CommandTable,
    ConnectionStageChanged,
    Event,
    EventBus,
    MessageAdded,
    MessageUpdated,
    RunCommandLine,
    StanzaLogged,
)
from termisations.core.models import ConnectionStage, Direction, StanzaKind
from termisations.protocol.account import Account
from termisations.protocol.session import SlixmppSession

pytestmark = pytest.mark.live

HOST: Final = os.environ.get("TERMISATIONS_TEST_HOST", "127.0.0.1")
PORT: Final = int(os.environ.get("TERMISATIONS_TEST_PORT", "5222"))
DOMAIN: Final = os.environ.get("TERMISATIONS_TEST_DOMAIN", "localhost")
USER: Final = os.environ.get("TERMISATIONS_TEST_USER", "alice")
PEER: Final = os.environ.get("TERMISATIONS_TEST_PEER", "bob")
PASSWORD: Final = os.environ.get("TERMISATIONS_TEST_PASSWORD", f"S3cr3t-{USER}-2026")

# Предел ожидания готовности сессии. Локальный сервер отвечает за доли секунды,
# запас взят на медленную машину и на первый запуск контейнера.
READY_TIMEOUT: Final = 20.0


def _server_is_up() -> bool:
    """Проверить, слушает ли сервер порт. Без него тесты пропускаются."""
    try:
        with socket.create_connection((HOST, PORT), timeout=1.0):
            return True
    except OSError:
        return False


pytest.importorskip("slixmpp", reason="реальный транспорт требует slixmpp")

if not _server_is_up():  # pragma: no cover - зависит от окружения
    pytest.skip(
        f"XMPP-сервер на {HOST}:{PORT} не поднят, см. docstring модуля",
        allow_module_level=True,
    )


@asynccontextmanager
async def live_session(
    resource: str, *, peer_ready: bool = False, eager: bool = False
) -> AsyncIterator[tuple[SlixmppSession, EventBus, list[Event]]]:
    """Поднять сессию на настоящем сервере и погасить ее вместе с задачами.

    Ресурс у каждой сессии свой: одинаковый ресурс сервер закрывает конфликтом,
    и тесты начинают мешать друг другу.

    Флаг ``eager`` воспроизводит цикл настоящего запуска: Textual ставит жадную
    фабрику задач в ``App.run_async``. Весь набор под ней не запускается - от
    способа создания задач зависит только подключение, оно покрыто быстрым тестом
    без сервера, а прогон против сервера стоит дорого.
    """
    loop = asyncio.get_running_loop()
    saved_factory = loop.get_task_factory()
    if eager:
        loop.set_task_factory(asyncio.eager_task_factory)
    os.environ["TERMISATIONS_PASSWORD"] = PASSWORD
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(Event, events.append)
    account = Account(
        jid=f"{USER}@{DOMAIN}",
        resource=resource,
        host=HOST,
        port=PORT,
        direct_tls=False,
        tls_verify=False,
    )
    session = SlixmppSession(bus, account)
    tasks = [
        asyncio.create_task(bus.run(), name="bus"),
        asyncio.create_task(session.run(), name="session"),
    ]
    try:
        await _wait_for(lambda: session.state.stage is ConnectionStage.READY, READY_TIMEOUT)
        if peer_ready:
            await asyncio.sleep(0.3)
        yield session, bus, events
    finally:
        session.stop()
        bus.stop()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        loop.set_task_factory(saved_factory)


async def _wait_for(condition: Callable[[], bool], timeout: float) -> bool:
    """Дождаться условия. Возвращает False, если время вышло."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return condition()


async def test_connects_under_eager_task_factory() -> None:
    """Полное рукопожатие с настоящим сервером проходит и под жадной фабрикой.

    Быстрый тест без сервера проверяет факт открытия сокета, этот - весь путь
    целиком: TLS, SASL, привязка ресурса и контакт-лист.
    """
    async with live_session("test-eager", eager=True) as (session, _bus, _events):
        assert session.state.stage is ConnectionStage.READY
        assert session.state.tls.version is not None


async def test_connects_and_reports_stages() -> None:
    """Подключение проходит все стадии по порядку и доходит до готовности."""
    async with live_session("test-stages") as (session, _bus, events):
        stages = [
            event.stage
            for event in events
            if isinstance(event, ConnectionStageChanged) and event.duration_ms is not None
        ]
        assert ConnectionStage.TLS_HANDSHAKE in stages
        assert ConnectionStage.SASL in stages
        assert ConnectionStage.BINDING in stages
        assert ConnectionStage.FETCHING_ROSTER in stages
        assert session.state.stage is ConnectionStage.READY
        # Длительность стадии - фактический замер, а не заглушка.
        assert all(
            event.duration_ms is None or event.duration_ms >= 0
            for event in events
            if isinstance(event, ConnectionStageChanged)
        )


async def test_channel_is_encrypted() -> None:
    """Канал шифруется, версия и шифр видны в состоянии."""
    async with live_session("test-tls") as (session, _bus, _events):
        tls = session.state.tls
        assert tls.version is not None
        assert tls.version.startswith("TLSv1.")
        assert tls.cipher


async def test_stream_management_enabled() -> None:
    """XEP-0198 включается и считает обработанные строфы."""
    async with live_session("test-sm") as (session, _bus, _events):
        assert await _wait_for(lambda: session.state.sm.enabled, 5.0)
        assert session.state.sm.inbound_handled >= 0


async def test_raw_stream_contains_real_stanzas() -> None:
    """В сыром потоке видны настоящие строфы обоих направлений."""
    async with live_session("test-raw") as (_session, _bus, events):
        stanzas = [event.stanza for event in events if isinstance(event, StanzaLogged)]
        assert stanzas
        kinds = {stanza.kind for stanza in stanzas}
        assert StanzaKind.SASL in kinds
        assert StanzaKind.IQ in kinds
        assert {stanza.direction for stanza in stanzas} == {Direction.IN, Direction.OUT}
        # Строфа лежит в буфере сырой: маскирование делает панель при рендере.
        sasl = [item for item in stanzas if item.kind is StanzaKind.SASL]
        assert any("urn:ietf:params:xml:ns:xmpp-sasl" in item.xml for item in sasl)


async def test_ping_measures_latency() -> None:
    """XEP-0199 дает задержку, и она попадает в метрики."""
    async with live_session("test-ping") as (session, bus, _events):
        bus.dispatch(RunCommandLine("/ping"))
        assert await _wait_for(lambda: session.state.metrics.latency_ms is not None, 10.0)
        latency = session.state.metrics.latency_ms
        assert latency is not None
        assert 0 < latency < 5000


async def test_disco_returns_server_features() -> None:
    """XEP-0030 отдает возможности сервера таблицей."""
    async with live_session("test-disco") as (_session, bus, events):
        bus.dispatch(RunCommandLine(f"/disco {DOMAIN}"))
        assert await _wait_for(
            lambda: any(
                isinstance(event, CommandTable) and event.title.startswith("disco")
                for event in events
            ),
            15.0,
        )
        table = next(
            event
            for event in events
            if isinstance(event, CommandTable) and event.title.startswith("disco")
        )
        assert any(row[0] == "возможностей" and int(row[1]) > 10 for row in table.rows)


async def test_message_round_trip() -> None:
    """Сообщение уходит на сервер и возвращается своей же копией.

    Отправка идет самому себе: второй аккаунт для этого не нужен, а путь строфы
    через сервер проверяется полностью. Признак возврата - метка XEP-0359 у
    своего сообщения: ее ставит сервер, и без прохода через него она не
    появится. Второй записи в ленте при этом быть не должно, иначе переписка с
    самим собой двоится.
    """
    text = "проверка доставки"
    async with live_session("test-send") as (session, bus, events):
        bus.dispatch(RunCommandLine(f"/chat {USER}@{DOMAIN}"))
        await asyncio.sleep(0.3)
        bus.dispatch(RunCommandLine(text))
        assert await _wait_for(
            lambda: any(
                isinstance(event, MessageUpdated)
                and event.message.body == text
                and any(mark.xep == "0359" for mark in event.message.xeps)
                for event in events
            ),
            10.0,
        )
        added = [
            event.message
            for event in events
            if isinstance(event, MessageAdded) and event.message.body == text
        ]
        assert len(added) == 1, "своя строфа, вернувшаяся копией, завела вторую запись"
        assert added[0].direction is Direction.OUT
        assert session.stanzas_total > 0


async def test_unknown_peer_does_not_break_session() -> None:
    """Ошибка сервера на запрос к несуществующему адресу не роняет сессию."""
    async with live_session("test-error") as (session, bus, _events):
        bus.dispatch(RunCommandLine(f"/disco nosuch.{DOMAIN}"))
        # Ждать возврата в готовность, а не спать: после ошибки сессия проходит
        # свои стадии, и на удаленном сервере они дольше фиксированной паузы.
        assert await _wait_for(lambda: session.state.stage is ConnectionStage.READY, 20.0)
