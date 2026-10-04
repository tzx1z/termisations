"""Стенд живых проверок: сессия на настоящем сервере.

Модуль не тестовый, в нем нет ни одной проверки. Здесь то, что нужно каждому
живому тесту: адрес сервера, учетные данные, поднятая сессия и разбор событий
шины. Без общего стенда каждый сценарий заводил бы свою копию этого кода, и
расходились бы они уже на третьем.

Чтобы набор прошел целиком, на сервере нужны две учетные записи, взаимные
подписки и комната, см. README.md, раздел "Live tests". Без подписок OMEMO не
забирает списки устройств из PEP.
"""

import asyncio
import contextlib
import os
import socket
import tempfile
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

import pytest

from termisations.core.events import (
    CommandFeedback,
    CommandTable,
    ConnectionStageChanged,
    Event,
    EventBus,
    MessageAdded,
    MessageUpdated,
    Notice,
    RunCommandLine,
    StanzaLogged,
    XepActivity,
)
from termisations.core.models import ConnectionStage, Message
from termisations.core.storage import Storage
from termisations.protocol.account import Account
from termisations.protocol.session import SlixmppSession

__all__ = [
    "DOMAIN",
    "HOST",
    "PEER",
    "PORT",
    "READY_TIMEOUT",
    "ROOM",
    "SELF_PEER",
    "USER",
    "Live",
    "live_session",
    "needs_two_accounts",
    "omemo_store",
    "password_of",
    "server_is_up",
    "stage_details",
    "wait_for",
]

HOST: Final = os.environ.get("TERMISATIONS_TEST_HOST", "127.0.0.1")
PORT: Final = int(os.environ.get("TERMISATIONS_TEST_PORT", "5222"))
DOMAIN: Final = os.environ.get("TERMISATIONS_TEST_DOMAIN", "localhost")
USER: Final = os.environ.get("TERMISATIONS_TEST_USER", "alice")
PEER: Final = os.environ.get("TERMISATIONS_TEST_PEER", "bob")
ROOM: Final = os.environ.get("TERMISATIONS_TEST_ROOM", f"devops@conference.{DOMAIN}")

# Предел ожидания готовности сессии. Локальный сервер отвечает за доли секунды,
# запас взят на медленную машину и на первый запуск контейнера.
READY_TIMEOUT: Final = 20.0

# Сколько ждать, пока строфа дойдет до собеседника и вернется событием.
EXCHANGE_TIMEOUT: Final = 10.0


# Собеседник и владелец сессии совпадают, когда набор запускается на сервере, где
# второй учетной записи нет. Часть сценариев в таком профиле проверить нельзя:
# счетчик непрочитанного не растет от своих сообщений, а доверие OMEMO к
# собственному устройству устроено иначе, чем к чужому.
SELF_PEER: Final = PEER == USER


def needs_two_accounts() -> None:
    """Пропустить сценарий, которому нужен второй участник.

    Заводить постороннюю учетную запись на чужом сервере ради теста нельзя, а
    молча проходить мимо - значит показывать зеленый набор там, где сценарий не
    проверялся.
    """
    if SELF_PEER:
        pytest.skip(f"нужен второй участник, а {PEER} - это сам владелец сессии")


def password_of(user: str) -> str:
    """Пароль учетной записи по соглашению тестового сервера."""
    if user == USER:
        return os.environ.get("TERMISATIONS_TEST_PASSWORD", f"S3cr3t-{user}-2026")
    return os.environ.get(f"TERMISATIONS_TEST_PASSWORD_{user.upper()}", f"S3cr3t-{user}-2026")


def omemo_store(user: str) -> Path:
    """Устойчивый путь базы ключей OMEMO для живых проверок.

    Путь не временный. Каждая новая база - это новое устройство, опубликованное
    в PEP, а старое оттуда никуда не девается: список устройств учетной записи
    растет с каждым прогоном, бандлы удаленных баз становятся недоступны, и рано
    или поздно у собеседника не остается ни одного пригодного устройства.
    Например, за десяток прогонов с временной базой у ``alice`` накопилось 35
    устройств.

    Живой клиент ведет себя так же: ключи лежат на диске и переживают запуск.
    Каталог чистится вручную, если надо начать с нуля, а заодно снимаются и
    списки устройств на сервере.
    """
    root = Path(tempfile.gettempdir()) / "termisations-live-omemo"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root / f"{user}.db"


def server_is_up() -> bool:
    """Слушает ли сервер порт. Без него живые тесты пропускаются."""
    try:
        with socket.create_connection((HOST, PORT), timeout=1.0):
            return True
    except OSError:
        return False


async def wait_for(condition: Callable[[], bool], timeout: float = EXCHANGE_TIMEOUT) -> bool:
    """Дождаться условия опросом. ``False`` означает, что время вышло."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return condition()


class Live:
    """Поднятая сессия вместе с разбором того, что она положила в шину.

    Тесты спрашивают у нее не "какие события были", а "что увидел пользователь":
    отклик команды, таблица, метка расширения, сообщение в ленте. Так проверка
    остается про поведение, а не про внутреннее устройство шины.
    """

    def __init__(self, session: SlixmppSession, bus: EventBus, events: list[Event]) -> None:
        """Собрать обертку поверх запущенной сессии."""
        self.session = session
        self.bus = bus
        self.events = events

    async def run(self, line: str, settle: float = 0.6) -> None:
        """Выполнить команду и дать сессии время отработать ее до конца."""
        await self.session.handle_command(RunCommandLine(line))
        await asyncio.sleep(settle)

    def mark(self) -> int:
        """Отметка в журнале событий: с нее смотрят, что случилось дальше."""
        return len(self.events)

    def feedback(self, since: int = 0) -> list[str]:
        """Текстовые отклики команд."""
        return [item.text for item in self.events[since:] if isinstance(item, CommandFeedback)]

    def notices(self, since: int = 0) -> list[str]:
        """Уведомления сессии."""
        return [item.text for item in self.events[since:] if isinstance(item, Notice)]

    def tables(self, part: str, since: int = 0) -> list[CommandTable]:
        """Таблицы, в заголовке которых есть подстрока."""
        return [
            item
            for item in self.events[since:]
            if isinstance(item, CommandTable) and part in item.title
        ]

    def rows(self, part: str, since: int = 0) -> dict[str, str]:
        """Последняя таблица по подстроке заголовка, разобранная в словарь."""
        found = self.tables(part, since)
        return dict(found[-1].rows) if found else {}

    def messages(self, since: int = 0) -> list[Message]:
        """Сообщения, добавленные в ленту."""
        return [item.message for item in self.events[since:] if isinstance(item, MessageAdded)]

    def stanzas(self, part: str, since: int = 0) -> list[str]:
        """Сырые строфы, содержащие подстроку: то, что реально ушло и пришло."""
        return [
            item.stanza.xml
            for item in self.events[since:]
            if isinstance(item, StanzaLogged) and part in item.stanza.xml
        ]

    def updated(self, since: int = 0) -> list[Message]:
        """Сообщения, которые изменились: доставка, текст, метки расширений."""
        return [item.message for item in self.events[since:] if isinstance(item, MessageUpdated)]

    def xeps(self, number: str, since: int = 0) -> list[str]:
        """Действия расширения в виде ``действие:направление``."""
        return [
            f"{item.event.action}:{item.event.direction.value}"
            for item in self.events[since:]
            if isinstance(item, XepActivity) and item.event.xep == number
        ]

    async def ready(self, timeout: float = READY_TIMEOUT) -> bool:
        """Дождаться готовности сессии."""
        return await wait_for(lambda: self.session.state.stage is ConnectionStage.READY, timeout)


@asynccontextmanager
async def live_session(
    resource: str,
    *,
    user: str = USER,
    storage: Path | str | None = None,
    eager: bool = False,
    wait_ready: bool = True,
) -> AsyncIterator[Live]:
    """Поднять сессию на настоящем сервере и погасить ее вместе с задачами.

    Ресурс у каждой сессии свой: одинаковый сервер закрывает конфликтом, и тесты
    начинают мешать друг другу.

    ``storage`` нужен там, где проверяется то, что обязано пережить перезапуск:
    история, ключи OMEMO, курсор архива. Без него сессия работает в памяти.

    Флаг ``eager`` воспроизводит цикл настоящего запуска: Textual ставит жадную
    фабрику задач в ``App.run_async``. Весь набор под ней не запускается - от
    способа создания задач зависит только подключение, оно покрыто быстрым тестом
    без сервера, а прогон против сервера стоит дорого.
    """
    loop = asyncio.get_running_loop()
    saved_factory = loop.get_task_factory()
    if eager:
        loop.set_task_factory(asyncio.eager_task_factory)
    os.environ["TERMISATIONS_PASSWORD"] = password_of(user)
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(Event, events.append)
    account = Account(
        jid=f"{user}@{DOMAIN}",
        resource=resource,
        host=HOST,
        port=PORT,
        direct_tls=False,
        tls_verify=False,
    )
    store = await Storage.open(storage) if storage is not None else None
    session = SlixmppSession(bus, account, None, store)
    tasks = [
        asyncio.create_task(bus.run(), name=f"bus-{resource}"),
        asyncio.create_task(session.run(), name=f"session-{resource}"),
    ]
    live = Live(session, bus, events)
    try:
        if wait_ready:
            await live.ready()
        yield live
    finally:
        session.stop()
        bus.stop()
        # Сессия закрывает хранилище сама в _shutdown, поэтому ей дается такт на
        # это до отмены задач: иначе фоновые записи упрутся в закрытую базу.
        await asyncio.sleep(0.3)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        loop.set_task_factory(saved_factory)


def stage_details(events: Sequence[Event], part: str) -> list[str]:
    """Подписи стадий подключения, в которых есть подстрока."""
    return [
        item.detail
        for item in events
        if isinstance(item, ConnectionStageChanged) and part in (item.detail or "")
    ]
