"""Панель сырого XML: кольцевой буфер, pause, фильтры, маскирование.

Панель проверяется на уровне виджета и монтируется в приложение-обертку, потому что
таймер отрисовки живет только в дереве Textual.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Final

import pytest
from textual.app import App
from textual.geometry import Region
from textual.pilot import Pilot

from termisations.core import i18n
from termisations.core.events import EventBus, StanzaLogged, XmlMode
from termisations.core.models import Direction, RawStanza, StanzaKind
from termisations.ui.xmllog import XmlLogPanel

Waiter = Callable[[Callable[[], bool], float], Awaitable[bool]]
StanzaFactory = Callable[..., RawStanza]
HostFactory = Callable[[XmlLogPanel], App[None]]

# Размер буфера по умолчанию.
DEFAULT_CAPACITY: Final = 2000

# Терминал теста шире реального: строка строфы усекается по ширине панели,
# и узкий терминал срезал бы проверяемые маркеры.
TEST_SIZE: Final = (240, 50)

# Имена, под которыми может быть объявлен параметр емкости буфера. Контракт
# требует параметр конструктора, но не фиксирует его имя.
_CAPACITY_NAMES: Final[tuple[str, ...]] = (
    "capacity",
    "buffer_size",
    "max_stanzas",
    "buffer",
    "maxlen",
    "size",
)

AUTH_SECRET: Final = "biwsbj1hbGljZSxyPXJPcHJOR2Z3RWJlUldnYlNRPT0="
SASL_AUTH: Final = (
    f"<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl' mechanism='SCRAM-SHA-256'>{AUTH_SECRET}</auth>"
)


def _make_panel(bus: EventBus, capacity: int | None = None) -> tuple[XmlLogPanel, int]:
    """Создать панель и вернуть ее вместе с фактической емкостью буфера."""
    if capacity is None:
        return XmlLogPanel(bus), DEFAULT_CAPACITY
    params = inspect.signature(XmlLogPanel.__init__).parameters
    for name in _CAPACITY_NAMES:
        if name in params:
            kwargs: dict[str, Any] = {name: capacity}
            return XmlLogPanel(bus, **kwargs), capacity
    # Параметра с ожидаемым именем нет: проверяем вытеснение на емкости по умолчанию.
    return XmlLogPanel(bus), DEFAULT_CAPACITY


def _log_text(panel: XmlLogPanel) -> str:
    """Текст, видимый в области строф.

    Читается результат отрисовки виджета, а не его внутренние структуры: так тест
    не зависит от того, на чем именно построена область строф.
    """
    body = panel.query_one("#xmllog-body")
    size = body.size
    if not size.width or not size.height:
        return ""
    strips = body.render_lines(Region(0, 0, size.width, size.height))
    return "\n".join(strip.text for strip in strips)


async def _settle(pilot: Pilot[None]) -> None:
    """Дать таймеру отрисовки отработать несколько тиков."""
    await asyncio.sleep(0.15)
    await pilot.pause()


# Кольцевой буфер.


def test_push_fills_buffer(bus: EventBus, make_stanza: StanzaFactory) -> None:
    """push кладет строфу в буфер без монтирования и без рендера."""
    panel, _ = _make_panel(bus)
    for _ in range(10):
        panel.push(make_stanza())

    stats = panel.stats()
    assert stats.buffered == 10
    assert stats.dropped == 0


def test_ring_buffer_evicts_oldest(bus: EventBus, make_stanza: StanzaFactory) -> None:
    """Буфер вытесняет старые строфы и считает вытесненные."""
    panel, capacity = _make_panel(bus, 64)
    overflow = 25
    for _ in range(capacity + overflow):
        panel.push(make_stanza())

    stats = panel.stats()
    assert stats.buffered == capacity
    assert stats.dropped == overflow


async def test_clear_resets_counters(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Очистка обнуляет и буфер, и счетчик вытесненных."""
    panel, capacity = _make_panel(bus, 32)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        for _ in range(capacity * 2):
            panel.push(make_stanza())
        assert panel.stats().dropped > 0

        panel.clear()
        await pilot.pause()

        stats = panel.stats()
        assert stats.buffered == 0
        assert stats.dropped == 0
        assert stats.pending == 0
        # Пустая панель объясняет причину: пустой буфер, выключенный показ и
        # фильтр без совпадений выводят разный текст.
        assert _log_text(panel).strip() == "строф нет"


async def test_stanza_arrives_through_bus(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Панель подписана на StanzaLogged и принимает строфы из шины."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        bus.publish(StanzaLogged(make_stanza("<message id='BUSMARK'><body>b</body></message>")))
        await _settle(pilot)

        assert panel.stats().buffered == 1
        assert "BUSMARK" in _log_text(panel)


# Pause.


async def test_pause_does_not_lose_stanzas(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
    wait_for: Waiter,
) -> None:
    """В режиме pause строфы копятся в буфере, счетчик непоказанных растет."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()

        follow = panel.toggle_follow()
        if follow:
            follow = panel.toggle_follow()
        assert follow is False, "toggle_follow не выключает follow"
        assert panel.stats().paused is True

        count = 50
        for _ in range(count):
            panel.push(make_stanza())
        await _settle(pilot)

        paused_stats = panel.stats()
        assert paused_stats.paused is True
        assert paused_stats.buffered == count, "строфы потеряны в режиме pause"
        assert paused_stats.dropped == 0
        assert paused_stats.pending > 0, "счетчик непоказанных строф не растет"

        assert panel.toggle_follow() is True
        drained = await wait_for(lambda: panel.stats().pending == 0, 3.0)
        await pilot.pause()

        resumed = panel.stats()
        assert drained, f"очередь не слита, осталось {resumed.pending}"
        assert resumed.paused is False
        assert resumed.buffered == count
        assert resumed.dropped == 0


async def test_pause_counter_matches_pushed(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Счетчик непоказанных строф не превышает числа накопленного."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        if panel.toggle_follow():
            panel.toggle_follow()

        for _ in range(30):
            panel.push(make_stanza())
        await _settle(pilot)

        stats = panel.stats()
        assert 0 < stats.pending <= 30


# Фильтры.


async def test_filter_by_kind(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Фильтр kind оставляет строфы только указанного типа."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.set_filter("kind:iq")
        panel.push(
            make_stanza(
                "<iq id='QMARK' type='get'><ping xmlns='urn:xmpp:ping'/></iq>",
                kind=StanzaKind.IQ,
            )
        )
        panel.push(make_stanza("<message id='MMARK'><body>текст</body></message>"))
        await _settle(pilot)

        text = _log_text(panel)
        assert "QMARK" in text
        assert "MMARK" not in text


async def test_filter_by_jid(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Фильтр jid оставляет строфы только указанного собеседника."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.set_filter("jid:bob@srv")
        panel.push(make_stanza("<message id='BOBMARK' from='bob@srv'/>", peer="bob@srv"))
        panel.push(make_stanza("<message id='CAROLMARK' from='carol@srv'/>", peer="carol@srv"))
        await _settle(pilot)

        text = _log_text(panel)
        assert "BOBMARK" in text
        assert "CAROLMARK" not in text


async def test_filter_by_namespace(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Фильтр ns оставляет строфы с указанным пространством имен."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.set_filter("ns:urn:xmpp:sm:3")
        panel.push(
            make_stanza(
                "<enable xmlns='urn:xmpp:sm:3' resume='true'/>",
                kind=StanzaKind.STREAM,
                direction=Direction.OUT,
            )
        )
        panel.push(make_stanza("<message id='MMARK'><body>текст</body></message>"))
        await _settle(pilot)

        text = _log_text(panel)
        assert "urn:xmpp:sm:3" in text
        assert "MMARK" not in text


async def test_filter_errors_only(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Фильтр err оставляет только ошибочные строфы."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.set_filter("err:true")
        panel.push(
            make_stanza(
                "<iq id='ERRMARK' type='error'><error type='cancel'/></iq>",
                kind=StanzaKind.IQ,
                is_error=True,
            )
        )
        panel.push(make_stanza("<message id='OKMARK'><body>текст</body></message>"))
        await _settle(pilot)

        text = _log_text(panel)
        assert "ERRMARK" in text
        assert "OKMARK" not in text


async def test_empty_filter_shows_everything(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Пустое выражение снимает фильтр и возвращает скрытые строфы."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.set_filter("kind:iq")
        panel.push(make_stanza("<iq id='QMARK' type='get'/>", kind=StanzaKind.IQ))
        panel.push(make_stanza("<message id='MMARK'><body>текст</body></message>"))
        await _settle(pilot)
        assert "MMARK" not in _log_text(panel)

        panel.set_filter("")
        await _settle(pilot)

        text = _log_text(panel)
        assert "QMARK" in text
        assert "MMARK" in text


# Режимы.


async def test_mode_off_keeps_history(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Режим OFF не очищает буфер: история сохраняется при возврате."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        for _ in range(10):
            bus.publish(StanzaLogged(make_stanza()))
        await _settle(pilot)
        assert panel.stats().buffered == 10

        panel.set_mode(XmlMode.OFF)
        await _settle(pilot)
        for _ in range(5):
            bus.publish(StanzaLogged(make_stanza()))
        await _settle(pilot)

        panel.set_mode(XmlMode.BOTH)
        await _settle(pilot)

        stats = panel.stats()
        assert stats.buffered >= 10, "история потеряна при выключении лога"
        assert stats.dropped == 0


async def test_mode_in_filters_direction(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Режим IN пропускает только входящие строфы."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.set_mode(XmlMode.IN)
        bus.publish(StanzaLogged(make_stanza("<message id='INMARK'/>", direction=Direction.IN)))
        bus.publish(StanzaLogged(make_stanza("<message id='OUTMARK'/>", direction=Direction.OUT)))
        await _settle(pilot)

        text = _log_text(panel)
        assert "INMARK" in text
        assert "OUTMARK" not in text


# Маскирование.


async def test_sasl_masked_on_render(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """SASL-строфа показывается замаскированной, исходного base64 на экране нет."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.push(make_stanza(SASL_AUTH, kind=StanzaKind.SASL, direction=Direction.OUT))
        await _settle(pilot)

        text = _log_text(panel)
        assert AUTH_SECRET not in text
        assert "redacted" in text


async def test_unsafe_toggle_rerenders_buffer(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Переключение unsafe меняет вид уже накопленных строф без их повторной подачи."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.push(make_stanza(SASL_AUTH, kind=StanzaKind.SASL, direction=Direction.OUT))
        await _settle(pilot)
        assert AUTH_SECRET not in _log_text(panel)

        panel.set_unsafe(True)
        await _settle(pilot)
        assert AUTH_SECRET in _log_text(panel), "unsafe не перерисовал накопленный буфер"

        panel.set_unsafe(False)
        await _settle(pilot)
        assert AUTH_SECRET not in _log_text(panel)

        # Буфер хранит сырой текст: маскирование выполняется только при рендере.
        assert panel.stats().buffered == 1


def test_buffer_keeps_raw_xml(bus: EventBus) -> None:
    """В буфер попадает сырой текст: маскирование не выполняется на горячем пути."""
    panel, _ = _make_panel(bus)
    stanza = RawStanza.make(Direction.OUT, StanzaKind.SASL, SASL_AUTH)
    panel.push(stanza)

    assert stanza.xml == SASL_AUTH
    assert panel.stats().buffered == 1


@pytest.mark.parametrize(
    "expression", ["kind:message", "jid:bob@srv", "ns:jabber:client", "err:true", "err:false"]
)
async def test_set_filter_does_not_raise(
    bus: EventBus,
    panel_host: HostFactory,
    expression: str,
) -> None:
    """Любое поддерживаемое выражение фильтра принимается без исключения."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.set_filter(expression)
        await pilot.pause()
        panel.set_filter("")
        await pilot.pause()


async def test_filter_does_not_match_masked_content(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Фильтр ищет по показанному тексту, а не по сырому.

    Поиск по сырой строфе дает оракул: показ или скрытие строки - это ответ "да"
    или "нет" на подстроку, и замаскированное тело подбирается по символу без
    включения unsafe. Проверяется и обратное: после включения unsafe та же
    подстрока строфу находит.
    """
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.push(make_stanza(SASL_AUTH, direction=Direction.OUT, kind=StanzaKind.SASL))
        panel.push(make_stanza("<message id='OTHERMARK'><body>текст</body></message>"))
        await _settle(pilot)

        panel.set_filter(AUTH_SECRET[:12].lower())
        await _settle(pilot)
        text = _log_text(panel)
        assert "auth" not in text.lower(), "фильтр нашел строфу по замаскированному телу"
        assert "OTHERMARK" not in text

        panel.set_unsafe(True)
        await _settle(pilot)
        assert "auth" in _log_text(panel).lower(), "в режиме unsafe фильтр обязан находить тело"


async def test_filter_matches_redaction_marker(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """По тексту замены строфа находится: искать по показанному можно."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.push(make_stanza(SASL_AUTH, direction=Direction.OUT, kind=StanzaKind.SASL))
        panel.push(make_stanza("<message id='OTHERMARK'><body>текст</body></message>"))
        await _settle(pilot)

        panel.set_filter("redacted")
        await _settle(pilot)
        text = _log_text(panel)
        assert "redacted" in text
        assert "OTHERMARK" not in text


async def test_unknown_filter_field_is_rejected(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Опечатка в значении фильтра не применяется, а объясняется.

    Раньше неизвестный тип строфы не совпадал ни с чем и молча опустошал панель:
    пользователь не мог отличить опечатку от остановки потока.
    """
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.push(make_stanza("<message id='VISIBLE'><body>текст</body></message>"))
        await _settle(pilot)

        result = panel.set_filter("kind:zzz")
        await _settle(pilot)

        assert not result.ok
        assert "msg" in result.text, "в ответе нет перечня допустимых значений"
        assert "VISIBLE" in _log_text(panel), "неверный фильтр не должен опустошать панель"


async def test_applied_filter_reports_matches(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Примененный фильтр сообщает, сколько строф буфера ему подошло."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.push(make_stanza("<iq type='get' id='1'/>", kind=StanzaKind.IQ))
        panel.push(make_stanza("<message id='2'><body>текст</body></message>"))
        await _settle(pilot)

        result = panel.set_filter("kind:iq")
        await _settle(pilot)

        assert result.ok
        assert "1 из 2" in result.text


async def test_panel_texts_follow_language(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """После смены языка подсказка пустой панели и ответ фильтра идут по-английски."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await _settle(pilot)
        assert "строф нет" in _log_text(panel)

        i18n.set_language("en")
        panel.apply_language()
        await _settle(pilot)
        assert "no stanzas" in _log_text(panel)

        panel.push(make_stanza("<iq type='get' id='1'/>", kind=StanzaKind.IQ))
        await _settle(pilot)
        result = panel.set_filter("kind:iq")
        assert result.text == "log filter: kind:iq - 1 of 1 stanza"


async def test_error_stanza_keeps_highlighting_under_background(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Ошибочная строфа выделяется фоном, а подсветка внутри нее сохраняется.

    Если красить красным все токены, имя элемента, имя атрибута и пространство
    имен внутри строфы становятся неразличимы, то есть пропадает сама подсветка.
    """
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.push(
            make_stanza(
                "<iq xmlns='jabber:client' type='error' id='e1'>"
                "<error type='cancel'><service-unavailable "
                "xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'/></error></iq>",
                kind=StanzaKind.IQ,
                is_error=True,
            )
        )
        await _settle(pilot)

        body = panel.query_one("#xmllog-body")
        strips = body.render_lines(Region(0, 0, body.size.width, 1))
        segments = [segment for segment in strips[0] if segment.text.strip()]
        backgrounds = {str(segment.style.bgcolor) for segment in segments if segment.style}
        colors = {str(segment.style.color) for segment in segments if segment.style}
        assert len(backgrounds) > 1, "у ошибочной строки нет отдельного фона"
        assert len(colors) > 2, "подсветка внутри ошибочной строфы потеряна"


async def test_cursor_marks_only_its_own_line(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Инверсией выделяется строка под курсором, а не вся развернутая строфа."""
    panel, _ = _make_panel(bus)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await pilot.pause()
        panel.push(
            make_stanza(
                "<message xmlns='jabber:client' id='m1'><body>текст</body>"
                "<store xmlns='urn:xmpp:hints'/></message>"
            )
        )
        await _settle(pilot)

        panel.action_cursor_up()
        panel.action_toggle_expand()
        await _settle(pilot)

        body = panel.query_one("#xmllog-body")
        strips = body.render_lines(Region(0, 0, body.size.width, body.size.height))
        reversed_rows = [
            index
            for index, strip in enumerate(strips)
            if any(segment.style is not None and segment.style.reverse for segment in strip)
        ]
        assert reversed_rows == [0], f"в инверсии строки {reversed_rows}"
