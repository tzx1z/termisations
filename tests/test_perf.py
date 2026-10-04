"""Производительность панели лога.

Бюджет: 500 строф в секунду без деградации ввода и отставание панели не более 200 мс.
Пороги в тестах заданы с запасом относительно этого бюджета, чтобы набор не давал ложных
провалов на загруженной или медленной машине. Фактические значения выводятся в тексте
утверждения, поэтому при провале сразу видно, насколько промах велик.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Final

import pytest
from textual.app import App
from textual.geometry import Region
from textual.widget import Widget

from termisations.core.events import EventBus
from termisations.core.models import RawStanza
from termisations.core.redact import redact
from termisations.ui.xmllog import XmlLogPanel

Waiter = Callable[[Callable[[], bool], float], Awaitable[bool]]
StanzaFactory = Callable[..., RawStanza]
HostFactory = Callable[[Widget], App[None]]

# Нагрузка бенчмарка push: десятикратный запас к бюджету 500 строф в секунду.
PUSH_COUNT: Final = 5000
# push - это добавление в кольцевой буфер и счетчик, без рендера и без маскирования.
# Реальная стоимость единицы измеряется единицами микросекунд, порог взят с запасом.
PUSH_TOTAL_BUDGET_S: Final = 1.0
PUSH_AVERAGE_BUDGET_US: Final = 200.0

# Батч-рендер: 500 строф за один слив очереди.
BATCH_COUNT: Final = 500
# Целевой бюджет - 200 мс. К нему добавляется до 50 мс ожидания таймера отрисовки
# и шаг опроса, поэтому граница утверждения поднята до 600 мс.
BATCH_BUDGET_S: Final = 0.6
BATCH_TARGET_S: Final = 0.2

# Маскирование вызывается на каждую отрисованную строку, поэтому оно тоже на
# горячем пути и должно укладываться в бюджет пачки.
REDACT_COUNT: Final = 500
REDACT_BUDGET_S: Final = 0.2

SASL_SAMPLE: Final = (
    "<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl' mechanism='SCRAM-SHA-256'>"
    "biwsbj1hbGljZSxyPXJPcHJOR2Z3RWJlUldnYlNRPT0=</auth>"
)
OMEMO_SAMPLE: Final = (
    "<message from='bob@srv/phone' type='chat' id='omemo-perf'>"
    "<encrypted xmlns='eu.siacs.conversations.axolotl'><header sid='1712345'>"
    "<key rid='4823001'>MwohBS9vL0hKZE1rTnFQclNzVHVWd1h5WjBhMmM0ZTZnOGk=</key>"
    "</header><payload>U29tZUxvbmdDaXBoZXJUZXh0V2l0aFNlY3JldFBheWxvYWREYXRh</payload>"
    "</encrypted></message>"
)
PLAIN_SAMPLE: Final = (
    "<message from='bob@srv/term' type='chat' id='plain-perf'>"
    "<body>перезапусти воркер на втором узле</body>"
    "<request xmlns='urn:xmpp:receipts'/></message>"
)


def _log_text(panel: XmlLogPanel) -> str:
    """Текст, видимый в области строф."""
    body = panel.query_one("#xmllog-body")
    size = body.size
    if not size.width or not size.height:
        return ""
    strips = body.render_lines(Region(0, 0, size.width, size.height))
    return "\n".join(strip.text for strip in strips)


@pytest.mark.slow
async def test_push_throughput(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Горячий путь push выдерживает 5000 строф заметно быстрее бюджета."""
    panel = XmlLogPanel(bus)
    stanzas = [make_stanza() for _ in range(PUSH_COUNT)]

    async with panel_host(panel).run_test(size=(240, 50)) as pilot:
        await pilot.pause()

        start = time.perf_counter()
        for stanza in stanzas:
            panel.push(stanza)
        elapsed = time.perf_counter() - start

        # Очередь отрисовки не нужна дальше: сбрасываем ее до выхода из приложения.
        panel.clear()
        await pilot.pause()

    average_us = elapsed / PUSH_COUNT * 1e6
    assert elapsed < PUSH_TOTAL_BUDGET_S, (
        f"{PUSH_COUNT} строф приняты за {elapsed * 1000:.1f} мс "
        f"при пороге {PUSH_TOTAL_BUDGET_S * 1000:.0f} мс"
    )
    assert average_us < PUSH_AVERAGE_BUDGET_US, (
        f"средняя стоимость push {average_us:.1f} мкс при пороге {PUSH_AVERAGE_BUDGET_US:.0f} мкс"
    )


@pytest.mark.slow
async def test_batch_render_budget(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
    wait_for: Waiter,
) -> None:
    """Пачка из 500 строф доходит до экрана в пределах бюджета отставания.

    Отставание меряется по факту показа: секундомер останавливается, когда в
    области строф появилась последняя строфа пачки.
    """
    panel = XmlLogPanel(bus)
    marker = "LASTMARK"
    stanzas = [make_stanza() for _ in range(BATCH_COUNT - 1)]
    stanzas.append(make_stanza(f"<message id='{marker}'><body>последняя</body></message>"))

    async with panel_host(panel).run_test(size=(240, 50)) as pilot:
        await pilot.pause()
        panel.clear()
        await pilot.pause()

        start = time.perf_counter()
        for stanza in stanzas:
            panel.push(stanza)
        drawn = await wait_for(lambda: marker in _log_text(panel), 5.0)
        elapsed = time.perf_counter() - start

        buffered = panel.stats().buffered

    assert drawn, "последняя строфа пачки не попала на экран за 5 секунд"
    assert buffered == BATCH_COUNT, "строфы потеряны при батч-рендере"
    assert elapsed < BATCH_BUDGET_S, (
        f"пачка из {BATCH_COUNT} строф дошла до экрана за {elapsed * 1000:.1f} мс "
        f"при бюджете {BATCH_TARGET_S * 1000:.0f} мс и пороге теста "
        f"{BATCH_BUDGET_S * 1000:.0f} мс"
    )


@pytest.mark.slow
def test_redact_throughput() -> None:
    """Маскирование пачки строф укладывается в бюджет отставания панели."""
    samples = [SASL_SAMPLE, OMEMO_SAMPLE, PLAIN_SAMPLE]
    batch = [samples[index % len(samples)] for index in range(REDACT_COUNT)]

    start = time.perf_counter()
    for xml in batch:
        redact(xml)
    elapsed = time.perf_counter() - start

    assert elapsed < REDACT_BUDGET_S, (
        f"маскирование {REDACT_COUNT} строф заняло {elapsed * 1000:.1f} мс "
        f"при пороге {REDACT_BUDGET_S * 1000:.0f} мс"
    )


@pytest.mark.slow
async def test_event_loop_stays_responsive_under_load(
    bus: EventBus,
    make_stanza: StanzaFactory,
    panel_host: HostFactory,
) -> None:
    """Под нагрузкой 500 строф в секунду цикл событий продолжает работать.

    Прямой замер отзывчивости ввода требует полного приложения, здесь проверяется
    необходимое условие: подача строф и батч-рендер не блокируют цикл событий.
    """
    panel = XmlLogPanel(bus)
    ticks = 0

    def on_tick() -> None:
        nonlocal ticks
        ticks += 1

    async with panel_host(panel).run_test(size=(240, 50)) as pilot:
        await pilot.pause()
        panel.clear()
        timer = pilot.app.set_interval(0.02, on_tick)

        start = time.perf_counter()
        while time.perf_counter() - start < 1.0:
            for _ in range(10):
                panel.push(make_stanza())
            await asyncio.sleep(0.02)

        timer.stop()
        await pilot.pause()
        buffered = panel.stats().buffered

    # За секунду таймер с интервалом 20 мс обязан сработать хотя бы 20 раз:
    # половина номинала оставлена планировщику и медленной машине.
    assert ticks >= 20, f"цикл событий получил только {ticks} тиков за секунду нагрузки"
    assert buffered > 0
