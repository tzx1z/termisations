"""Общие фикстуры тестов termisations.

Здесь только то, что нужно нескольким файлам: шина, сбор событий, фабрика строф,
ожидание условия, запуск мок-сессии и приложение-обертка для монтирования одной
панели.

Импорты на уровне модуля ограничены фундаментом (``core`` и ``textual``). Модуль
``mock`` подключается внутри фикстуры: он нужен не всем тестам, а его отсутствие не
должно ломать сбор всего каталога.
"""

import asyncio
import contextlib
import importlib.util
import inspect
import itertools
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from textual.app import App, ComposeResult
from textual.widget import Widget

from termisations.core import i18n, paths, profile
from termisations.core.events import ConnectionStageChanged, Event, EventBus, StanzaLogged
from termisations.core.models import ConnectionStage, Direction, RawStanza, StanzaKind

if TYPE_CHECKING:
    from termisations.mock import MockSession

# Типы вспомогательных вызовов. Фикстуры отдают функции, потому что импортировать
# conftest из тестового модуля нельзя.
Waiter = Callable[[Callable[[], bool], float], Awaitable[bool]]
StanzaFactory = Callable[..., RawStanza]
MockFactory = Callable[[EventBus, float, str, int], "MockSession"]
MockRunner = Callable[["MockSession", Callable[[], bool], float], Awaitable[None]]
HostFactory = Callable[[Widget], App[None]]

# Шаг опроса при ожидании условия. Мельче нет смысла: таймер отрисовки панели
# срабатывает не чаще 20 раз в секунду.
POLL_STEP: Final = 0.01

# Перечни ниже составлены по RFC 6120, XEP-0388 и XEP-0384, а не по реализации
# маскирования, и шире ее. Если тест берет список секретных элементов из того же
# места, что и код, он не способен заметить пропущенный элемент, например
# <initial-response/> из SASL2.
_SASL_NAMESPACES: Final[tuple[str, ...]] = (
    "urn:ietf:params:xml:ns:xmpp-sasl",
    "urn:xmpp:sasl:2",
)

# Тела этих элементов обязаны исчезать целиком.
_FULL_MASK_ELEMENTS: Final[frozenset[str]] = frozenset(
    {
        "auth",
        "authenticate",
        "response",
        "initial-response",
        "challenge",
        "success",
        "additional-data",
        "password",
        "passwd",
    }
)

# Тела ключей усекаются, поэтому полное значение исчезает при длине больше границы
# усечения. Более короткое тело остается целым на законных основаниях.
_TRUNCATED_ELEMENTS: Final[frozenset[str]] = frozenset(
    {
        "key",
        "payload",
        "spk",
        "spks",
        "ik",
        "pk",
        "prekeypublic",
        "signedprekeypublic",
    }
)
_TRUNCATION_LIMIT: Final = 16

# Тело в форме base64 по RFC 4648: так выглядит нагрузка любого механизма SASL, как
# бы ни назывался несущий ее элемент. Дефис и подчеркивание в алфавит не входят:
# иначе под признак попадали бы имена механизмов и типов привязки канала, которые в
# логе обязаны оставаться видимыми.
_B64_BODY_RE: Final = re.compile(r"[A-Za-z0-9+/]{20,}={0,2}")

# Открывающий тег вместе с непосредственным текстовым узлом. Значения атрибутов
# пропускаются целиком, поэтому ">" внутри кавычек тег не обрывает.
_NODE_RE: Final = re.compile(
    r"<(?P<name>[A-Za-z_][\w.:-]*)(?P<attrs>(?:[^>\"']|\"[^\"]*\"|'[^']*')*)>(?P<body>[^<]*)"
)


# Модули сетевой сессии импортируют slixmpp на уровне файла: без пакета падает
# сбор всего каталога, а не отдельный тест.
collect_ignore: list[str] = []
if importlib.util.find_spec("slixmpp") is None:  # pragma: no cover - зависит от окружения
    collect_ignore += [
        "test_live_session.py",
        "test_live_protocol.py",
        "test_connect_task_factory.py",
        "test_omemo.py",
        "test_caps.py",
        "test_sasl2.py",
    ]


def pytest_configure(config: pytest.Config) -> None:
    """Зарегистрировать собственные маркеры."""
    config.addinivalue_line("markers", "slow: длительный тест производительности")


@pytest.fixture(autouse=True)
def ui_language(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Зафиксировать русский язык интерфейса.

    Проверки текста написаны по русскому выводу, а русский каталог повторяет
    прежние строки дословно. Поэтому прогон на русском заодно проверяет каталог:
    строка без перевода вернется по-английски, и проверка не пройдет. Английский
    вывод проверяют тесты, которые явно вызывают ``i18n.set_language("en")``.
    Переменная окружения нужна дочерним процессам и разбору аргументов в cli.
    """
    monkeypatch.setenv(i18n.ENV_LANG, "ru")
    i18n.set_language("ru")
    yield
    i18n.set_language("ru")


@pytest.fixture(autouse=True)
def clean_profile(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Изолировать выбор профиля: переменная окружения и состояние процесса.

    Профиль - состояние процесса: его выбирает ``cli.main`` один раз на запуск.
    В тестах запусков много, и выбор, оставшийся от предыдущего, увел бы пути
    следующего теста в чужой каталог. Переменная окружения убирается по той же
    причине: на машине разработчика она вполне может быть выставлена, и тогда
    тесты проверяли бы его учетную запись, а не свой сценарий.
    """
    monkeypatch.delenv(profile.ENV_JID, raising=False)
    yield
    paths.use_profile("")


@pytest.fixture(autouse=True)
def xdg_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Увести каталоги XDG во временный каталог теста.

    Тесты запускают ``cli.main``, открывают базу профиля и пишут журнал. Без
    подмены они делали бы это в настоящих каталогах того, кто запустил pytest, а
    перенос данных прежней раскладки трогал бы его файлы.
    """
    for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(variable, str(tmp_path / "xdg" / variable.lower()))


@pytest.fixture(params=[False, True], ids=["lazy", "eager"])
async def task_factory(request: pytest.FixtureRequest) -> AsyncIterator[bool]:
    """Цикл событий с обычной и с жадной фабрикой задач.

    Жадную фабрику ставит Textual в ``App.run_async``, а ``App.run_test`` нет.
    Тест, которому важен способ создания задач, обязан проходить в обоих
    режимах: иначе он проверяет не то, что работает у пользователя.
    """
    loop = asyncio.get_running_loop()
    saved = loop.get_task_factory()
    eager = bool(request.param)
    if eager:
        loop.set_task_factory(asyncio.eager_task_factory)
    try:
        yield eager
    finally:
        loop.set_task_factory(saved)


class _PanelHost(App[None]):
    """Приложение-обертка: монтирует одну панель без остального интерфейса."""

    def __init__(self, panel: Widget) -> None:
        super().__init__()
        self._panel = panel

    def compose(self) -> ComposeResult:
        yield self._panel


@pytest.fixture
def bus() -> EventBus:
    """Чистая шина событий на каждый тест."""
    return EventBus()


@pytest.fixture
def events(bus: EventBus) -> list[Event]:
    """Все события шины в порядке публикации.

    Подписка идет на базовый ``Event``, обработчик дешевый, как требует контракт шины.
    """
    collected: list[Event] = []
    bus.subscribe(Event, collected.append)
    return collected


@pytest.fixture
def make_stanza() -> StanzaFactory:
    """Фабрика строф с уникальным идентификатором и осмысленным телом."""
    counter = itertools.count(1)

    def factory(
        xml: str | None = None,
        *,
        direction: Direction = Direction.IN,
        kind: StanzaKind = StanzaKind.MESSAGE,
        peer: str | None = "bob@srv",
        is_error: bool = False,
    ) -> RawStanza:
        number = next(counter)
        stanza_id = f"s{number:05d}"
        body = (
            xml
            if xml is not None
            else f"<message id='{stanza_id}' from='{peer}'><body>строка {number}</body></message>"
        )
        return RawStanza.make(
            direction,
            kind,
            body,
            stanza_id=stanza_id,
            peer=peer,
            is_error=is_error,
        )

    return factory


@pytest.fixture
def wait_for() -> Waiter:
    """Ожидание условия опросом. Возвращает False, если время вышло."""

    async def waiter(predicate: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if predicate():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(POLL_STEP)

    return waiter


@pytest.fixture
def make_mock() -> MockFactory:
    """Фабрика мок-сессии.

    Параметр seed передается только если конструктор его принимает: контракт его не
    требует, а воспроизводимость проверяется по структуре потока.
    """

    def factory(bus: EventBus, rate: float, scenario: str, seed: int) -> "MockSession":
        from termisations.mock import MockSession

        kwargs: dict[str, Any] = {"rate": rate, "scenario": scenario}
        if "seed" in inspect.signature(MockSession.__init__).parameters:
            kwargs["seed"] = seed
        return MockSession(bus, **kwargs)

    return factory


@pytest.fixture
def run_mock(wait_for: Waiter) -> MockRunner:
    """Проиграть сценарий мока до выполнения условия и остановить сессию."""

    async def runner(session: "MockSession", until: Callable[[], bool], timeout: float) -> None:
        task = asyncio.create_task(session.run())
        try:
            await wait_for(lambda: until() or task.done(), timeout)
        finally:
            session.stop()
            if not task.done():
                task.cancel()
            # Исключение сценария поднимается наружу: это точная диагностика.
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return runner


def scenario_done(events: list[Event]) -> bool:
    """Сценарий по умолчанию отыгран: соединение готово и ошибочная строфа получена."""
    ready = any(
        isinstance(event, ConnectionStageChanged) and event.stage is ConnectionStage.READY
        for event in events
    )
    has_error = any(isinstance(event, StanzaLogged) and event.stanza.is_error for event in events)
    return ready and has_error


@pytest.fixture
async def mock_stanzas(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> list[RawStanza]:
    """Строфы полного прогона сценария по умолчанию."""
    session = make_mock(bus, 500.0, "default", 20260912)
    await run_mock(session, lambda: scenario_done(events), 12.0)
    return [event.stanza for event in events if isinstance(event, StanzaLogged)]


@pytest.fixture
def panel_host() -> HostFactory:
    """Фабрика приложения-обертки для монтирования одной панели."""

    def factory(panel: Widget) -> App[None]:
        return _PanelHost(panel)

    return factory


@pytest.fixture
def secret_bodies() -> Callable[[str], list[str]]:
    """Извлечь из строфы тела, которые обязаны исчезнуть после маскирования.

    Отбор идет по трем независимым признакам: имя элемента из перечня спецификаций,
    длина тела относительно границы усечения ключей и форма значения внутри строфы
    SASL. Третий признак не привязан к именам вовсе, поэтому нагрузка механизма
    считается секретом даже в элементе, о котором реализация не знает.
    """

    def extract(xml: str) -> list[str]:
        in_sasl = any(namespace in xml for namespace in _SASL_NAMESPACES)
        bodies: list[str] = []
        for match in _NODE_RE.finditer(xml):
            body = match.group("body").strip()
            if not body:
                continue
            local = match.group("name").rpartition(":")[2].lower()
            by_name = local in _FULL_MASK_ELEMENTS
            by_length = local in _TRUNCATED_ELEMENTS and len(body) > _TRUNCATION_LIMIT
            by_shape = in_sasl and _B64_BODY_RE.fullmatch(body) is not None
            if by_name or by_length or by_shape:
                bodies.append(body)
        return bodies

    return extract
