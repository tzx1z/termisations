"""Статус-бар: что он берет из беседы, а что из состояния клиента.

Проверяется не устройство виджета, а то, что видно на экране: бар монтируется в
приложение-обертку и читается результат его отрисовки. Бар обязан показывать шифрование
активной беседы, поэтому зеленый OMEMO на открытой незашифрованной беседе - прямая
дезинформация о защищенности канала.
"""

from collections.abc import Callable
from typing import Final

import pytest
from textual.app import App
from textual.geometry import Region
from textual.pilot import Pilot
from textual.widget import Widget

from termisations.core.events import (
    ActiveConversationChanged,
    ConversationsUpdated,
    EventBus,
    StateUpdated,
)
from termisations.core.models import ClientState, Conversation, Encryption, OmemoInfo
from termisations.ui.statusbar import StatusBar

HostFactory = Callable[[Widget], App[None]]

TEST_SIZE: Final = (140, 10)

BOB: Final = "bob@example.org"
ERIN: Final = "erin@example.org"


@pytest.fixture
def status(bus: EventBus) -> StatusBar:
    """Статус-бар на общей шине."""
    return StatusBar(bus, id="statusbar")


def _text(bar: StatusBar) -> str:
    """Обе строки статус-бара в том виде, в котором они на экране."""
    size = bar.size
    if not size.width or not size.height:
        return ""
    strips = bar.render_lines(Region(0, 0, size.width, size.height))
    return "\n".join(strip.text for strip in strips)


async def _settle(pilot: Pilot[None]) -> None:
    """Дать бару отрисоваться."""
    await pilot.pause()
    await pilot.pause()


@pytest.mark.parametrize(
    ("trusted", "total", "expected"),
    [(4, 4, "OMEMO✓ 4 dev"), (3, 4, "OMEMO! 3/4 dev"), (0, 4, "OMEMO✗ 0/4 dev")],
)
async def test_trust_mark_matches_chat_header(
    bus: EventBus,
    status: StatusBar,
    panel_host: HostFactory,
    trusted: int,
    total: int,
    expected: str,
) -> None:
    """Знаки доверия те же, что в заголовке беседы: ✓ полное, ! частичное, ✗ нет."""
    async with panel_host(status).run_test(size=TEST_SIZE) as pilot:
        bus.publish(
            ConversationsUpdated(
                (
                    Conversation(
                        jid=BOB,
                        encryption=Encryption.OMEMO,
                        omemo=OmemoInfo(enabled=True, trusted_devices=trusted, total_devices=total),
                    ),
                )
            )
        )
        bus.publish(ActiveConversationChanged(BOB))
        await _settle(pilot)

        assert expected in _text(status)


async def test_encryption_follows_active_conversation(
    bus: EventBus, status: StatusBar, panel_host: HostFactory
) -> None:
    """Шифрование берется из активной беседы, а не из глобального флага OMEMO.

    Клиент держит OMEMO включенным для одной беседы и открытой другую. Показывать
    в этот момент зеленый OMEMO значит сообщать о защите, которой в открытой
    беседе нет.
    """
    async with panel_host(status).run_test(size=TEST_SIZE) as pilot:
        bus.publish(
            ConversationsUpdated(
                (
                    Conversation(
                        jid=BOB,
                        encryption=Encryption.OMEMO,
                        omemo=OmemoInfo(enabled=True, trusted_devices=4, total_devices=4),
                    ),
                    Conversation(jid=ERIN, encryption=Encryption.PLAIN),
                )
            )
        )
        bus.publish(ActiveConversationChanged(BOB))
        await _settle(pilot)
        assert "OMEMO✓ 4 dev" in _text(status)

        bus.publish(ActiveConversationChanged(ERIN))
        await _settle(pilot)
        text = _text(status)
        assert "plain" in text
        assert "OMEMO" not in text


async def test_unsafe_marker_survives_narrow_terminal(
    bus: EventBus, status: StatusBar, panel_host: HostFactory
) -> None:
    """Маркер UNSAFE не срезается вместе с концом строки на узком терминале.

    Он стоит сразу за присутствием, а не в конце: признак отключенного
    маскирования нужен всегда, а длинный JID пользователь и так знает.
    """
    async with panel_host(status).run_test(size=(60, 10)) as pilot:
        bus.publish(
            StateUpdated(
                ClientState(
                    jid="alexander.petrov@messaging.internal.example/termisations-01",
                    unsafe_xml=True,
                )
            )
        )
        await _settle(pilot)

        assert "UNSAFE" in _text(status)
