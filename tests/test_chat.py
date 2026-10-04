"""Панель беседы: что попадает в ленту, что в заголовок и что не попадает никуда.

Тесты закрывают требование читаемости основной панели продукта. Проверяется не
внутреннее устройство, а то, что видно на экране: панель монтируется в
приложение-обертку и читается результат ее отрисовки.
"""

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Final

import pytest
from textual.app import App
from textual.geometry import Region
from textual.pilot import Pilot
from textual.widget import Widget

from termisations.core import i18n
from termisations.core.events import (
    ActiveConversationChanged,
    CommandFeedback,
    ConnectionStageChanged,
    ConversationsUpdated,
    EventBus,
    MessageAdded,
    MessageUpdated,
    Notice,
    XepActivity,
)
from termisations.core.models import (
    ConnectionStage,
    Conversation,
    DeliveryState,
    Direction,
    Encryption,
    Message,
    OmemoInfo,
    XepEvent,
)
from termisations.ui.chat import ChatPanel
from termisations.xeps import compact_legend, presence_text, xep_title

HostFactory = Callable[[Widget], App[None]]

# Терминал теста шире реального: длинная строка иначе усекается по правому краю.
TEST_SIZE: Final = (140, 40)

BOB: Final = "bob@example.org"
ERIN: Final = "erin@example.org"


def _panel_text(panel: ChatPanel, selector: str) -> str:
    """Текст, видимый в узле панели."""
    node = panel.query_one(selector)
    size = node.size
    if not size.width or not size.height:
        return ""
    strips = node.render_lines(Region(0, 0, size.width, size.height))
    return "\n".join(strip.text for strip in strips)


def _log_text(panel: ChatPanel) -> str:
    """Текст ленты сообщений."""
    return _panel_text(panel, "#chat-log")


def _header_text(panel: ChatPanel) -> str:
    """Текст заголовка беседы."""
    return _panel_text(panel, "#chat-header")


def _message(conversation: str, body: str, index: int = 0) -> Message:
    """Входящее сообщение беседы."""
    return Message(
        message_id=f"msg-{conversation}-{index}",
        conversation=conversation,
        sender=conversation,
        body=body,
        ts=time.time(),
        direction=Direction.IN,
        state=DeliveryState.RECEIVED,
    )


async def _settle(pilot: Pilot[None]) -> None:
    """Дать панели отрисоваться."""
    await pilot.pause()
    await pilot.pause()


@pytest.fixture
def panel(bus: EventBus) -> ChatPanel:
    """Панель беседы на чистой шине."""
    return ChatPanel(bus, id="chat")


async def test_transport_events_stay_out_of_the_log(
    bus: EventBus, panel: ChatPanel, panel_host: HostFactory
) -> None:
    """Пинги и подтверждения потока в ленту беседы не попадают.

    Их место - панель сырого потока, статус-бар и вывод /sm, /ping. В ленте они
    шли со скоростью около восьми строк в секунду и вытесняли переписку.
    """
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        bus.publish(ConversationsUpdated((Conversation(jid=BOB, title="Bob"),)))
        bus.publish(ActiveConversationChanged(BOB))
        bus.publish(MessageAdded(_message(BOB, "живое сообщение")))
        for _ in range(20):
            bus.publish(XepActivity(XepEvent("0199", "ping-sent", peer="example.org")))
            bus.publish(XepActivity(XepEvent("0198", "acked", detail={"h": "9"})))
        await _settle(pilot)

        text = _log_text(panel)
        assert "живое сообщение" in text
        assert "Ping" not in text
        assert "Acked" not in text


async def test_chat_state_goes_to_header(
    bus: EventBus, panel: ChatPanel, panel_host: HostFactory
) -> None:
    """Состояние набора текста показывается строкой заголовка, а не историей."""
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        bus.publish(ConversationsUpdated((Conversation(jid=BOB, title="Bob"),)))
        bus.publish(ActiveConversationChanged(BOB))
        bus.publish(XepActivity(XepEvent("0085", "composing", peer=f"{BOB}/res")))
        await _settle(pilot)

        assert "печатает" in _header_text(panel)
        assert "Composing" not in _log_text(panel)


async def test_panel_texts_follow_language(
    bus: EventBus, panel: ChatPanel, panel_host: HostFactory
) -> None:
    """После смены языка заголовок, подсказка и состояние собеседника идут по-английски."""
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await _settle(pilot)
        assert "беседа не выбрана" in _header_text(panel)
        assert "войти в комнату" in _log_text(panel)

        i18n.set_language("en")
        panel.apply_language()
        await _settle(pilot)
        assert "no conversation selected" in _header_text(panel)
        assert "join a room" in _log_text(panel)
        assert "войти в комнату" not in _log_text(panel), "подсказка осталась прежней"

        bus.publish(ConversationsUpdated((Conversation(jid=BOB, title="Bob"),)))
        bus.publish(ActiveConversationChanged(BOB))
        bus.publish(XepActivity(XepEvent("0085", "composing", peer=f"{BOB}/res")))
        await _settle(pilot)
        assert "typing" in _header_text(panel)
        assert "no history" in _log_text(panel)


def test_xep_texts_follow_language() -> None:
    """Тексты справочника расширений переводятся в момент вызова.

    Официальные названия расширений английские на любом языке интерфейса.
    """
    event = XepEvent("0085", "composing", peer=BOB)
    assert presence_text(event) == "печатает"
    assert dict(compact_legend())["↑"] == "XEP-0198: строфа подтверждена сервером"

    i18n.set_language("en")
    assert presence_text(event) == "typing"
    assert dict(compact_legend())["↑"] == "XEP-0198: stanza acknowledged by the server"
    assert xep_title("0184") == "Message Delivery Receipts"


async def test_foreign_conversation_events_are_not_shown(
    bus: EventBus, panel: ChatPanel, panel_host: HostFactory
) -> None:
    """События чужой беседы в открытое окно не попадают."""
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        bus.publish(
            ConversationsUpdated(
                (Conversation(jid=BOB, title="Bob"), Conversation(jid=ERIN, title="Erin"))
            )
        )
        bus.publish(ActiveConversationChanged(BOB))
        bus.publish(XepActivity(XepEvent("0333", "displayed", peer=ERIN)))
        bus.publish(XepActivity(XepEvent("0333", "displayed", peer=BOB)))
        await _settle(pilot)

        text = _log_text(panel)
        assert BOB in text
        assert ERIN not in text


async def test_switching_conversation_shows_its_history(
    bus: EventBus, panel: ChatPanel, panel_host: HostFactory
) -> None:
    """Переключение беседы показывает ее историю, а не журнал сессии."""
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        bus.publish(
            ConversationsUpdated(
                (Conversation(jid=BOB, title="Bob"), Conversation(jid=ERIN, title="Erin"))
            )
        )
        bus.publish(ActiveConversationChanged(BOB))
        bus.publish(Notice("ответ команды в беседе bob"))
        bus.publish(MessageAdded(_message(BOB, "сообщение бобу")))
        await _settle(pilot)

        bus.publish(ActiveConversationChanged(ERIN))
        bus.publish(MessageAdded(_message(ERIN, "сообщение эрин")))
        await _settle(pilot)

        text = _log_text(panel)
        assert "сообщение эрин" in text
        assert "ответ команды в беседе bob" not in text
        assert "сообщение бобу" not in text


async def test_service_flood_does_not_evict_history(panel_host: HostFactory) -> None:
    """Служебный поток не вытесняет сообщения: лимиты раздельные.

    С одним общим лимитом окно беседы через две минуты работы не содержало ни
    одного сообщения: служебные строки идут на порядок чаще.
    """
    bus = EventBus()
    panel = ChatPanel(bus, id="chat", history_limit=10, system_limit=5)
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        bus.publish(ConversationsUpdated((Conversation(jid=BOB, title="Bob"),)))
        bus.publish(ActiveConversationChanged(BOB))
        for index in range(5):
            bus.publish(MessageAdded(_message(BOB, f"сообщение {index}", index)))
        for index in range(50):
            bus.publish(Notice(f"служебная строка {index}"))
        await _settle(pilot)

        # Лента длиннее экрана, поэтому смотрим ее начало.
        panel.query_one("#chat-log").scroll_home(animate=False, immediate=True)
        await _settle(pilot)

        text = _log_text(panel)
        for index in range(5):
            assert f"сообщение {index}" in text


async def test_clear_hides_messages_and_late_update_does_not_return_them(
    bus: EventBus, panel: ChatPanel, panel_host: HostFactory
) -> None:
    """Очищенное сообщение не возвращается поздней квитанцией.

    Раньше очистка выбрасывала сообщение из индекса, и пришедшее следом
    обновление создавало в пустом окне новый блок.
    """
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        bus.publish(ConversationsUpdated((Conversation(jid=BOB, title="Bob"),)))
        bus.publish(ActiveConversationChanged(BOB))
        message = _message(BOB, "сообщение перед очисткой")
        bus.publish(MessageAdded(message))
        await _settle(pilot)

        hidden = panel.clear_conversation()
        await _settle(pilot)
        assert hidden == 1

        bus.publish(MessageUpdated(message.with_state(DeliveryState.DISPLAYED)))
        await _settle(pilot)

        assert "сообщение перед очисткой" not in _log_text(panel)


async def test_empty_panel_shows_hint(
    bus: EventBus, panel: ChatPanel, panel_host: HostFactory
) -> None:
    """Пока беседа не выбрана, панель подсказывает, с чего начать."""
    del bus
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        await _settle(pilot)
        assert "/chat" in _log_text(panel)


async def test_command_output_keeps_columns(
    bus: EventBus, panel: ChatPanel, panel_host: HostFactory
) -> None:
    """Продолжение описания уходит под описание, а не под колонку имени.

    Справка и список флагов сверстаны двумя колонками. Раньше перенос вставал под
    имя команды, и продолжение читалось как следующий пункт списка.
    """
    async with panel_host(panel).run_test(size=(60, 20)) as pilot:
        bus.publish(Notice("/mam        " + "слово " * 12))
        await _settle(pilot)

        columns = {line.index("слово") for line in _log_text(panel).splitlines() if "слово" in line}
        assert len(columns) == 1


async def test_connection_summary_is_printed(
    bus: EventBus, panel: ChatPanel, panel_host: HostFactory
) -> None:
    """Итог каждой стадии подключения с длительностью остается в ленте."""
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        bus.publish(ConnectionStageChanged(ConnectionStage.TLS_HANDSHAKE, "DirectTLS:5223"))
        bus.publish(
            ConnectionStageChanged(ConnectionStage.TLS_HANDSHAKE, "TLS1.3, сертификат", 92.4)
        )
        bus.publish(ConnectionStageChanged(ConnectionStage.READY, "сессия готова"))
        await _settle(pilot)

        text = _log_text(panel)
        assert "TLS handshake" in text
        assert "92 мс" in text
        assert "итого" in text


async def test_message_badge_does_not_take_current_device_count(
    bus: EventBus, panel: ChatPanel, panel_host: HostFactory
) -> None:
    """Метка шифрования сообщения собирается из данных сообщения.

    Число доверенных устройств - величина текущая. Подставлять ее в старое
    сообщение значит переписывать историю задним числом.
    """
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        conversation = Conversation(
            jid=BOB,
            title="Bob",
            encryption=Encryption.OMEMO,
            omemo=OmemoInfo(enabled=True, trusted_devices=4, total_devices=4),
        )
        bus.publish(ConversationsUpdated((conversation,)))
        bus.publish(ActiveConversationChanged(BOB))
        message = replace(_message(BOB, "шифрованное сообщение"), encryption=Encryption.OMEMO)
        bus.publish(MessageAdded(message))
        await _settle(pilot)

        assert "[OMEMO]" in _log_text(panel)
        assert "OMEMO ✓ 4 dev" in _header_text(panel)


async def test_command_answer_stays_in_its_conversation(
    bus: EventBus, panel: ChatPanel, panel_host: HostFactory
) -> None:
    """Ответ команды остается в той беседе, где команду ввели."""
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        bus.publish(
            ConversationsUpdated(
                (Conversation(jid=BOB, title="Bob"), Conversation(jid=ERIN, title="Erin"))
            )
        )
        bus.publish(ActiveConversationChanged(BOB))
        bus.publish(CommandFeedback("ответ команды"))
        await _settle(pilot)
        assert "ответ команды" in _log_text(panel)

        bus.publish(ActiveConversationChanged(ERIN))
        await _settle(pilot)
        assert "ответ команды" not in _log_text(panel)


@pytest.mark.parametrize(
    ("trusted", "total", "expected"),
    [(4, 4, "OMEMO ✓ 4 dev"), (3, 4, "OMEMO ! 3/4 dev"), (0, 4, "OMEMO ✗ 0/4 dev")],
)
async def test_header_trust_mark_matches_status_bar(
    bus: EventBus,
    panel: ChatPanel,
    panel_host: HostFactory,
    trusted: int,
    total: int,
    expected: str,
) -> None:
    """Знак доверия в заголовке беседы тот же, что в статус-баре.

    Полное доверие, частичное и его отсутствие обязаны выглядеть одинаково в обоих
    местах: иначе один и тот же факт читается как два разных.
    """
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        bus.publish(
            ConversationsUpdated(
                (
                    Conversation(
                        jid=BOB,
                        title="Bob",
                        encryption=Encryption.OMEMO,
                        omemo=OmemoInfo(enabled=True, trusted_devices=trusted, total_devices=total),
                    ),
                )
            )
        )
        bus.publish(ActiveConversationChanged(BOB))
        await _settle(pilot)

        assert expected in _header_text(panel)


async def test_routine_badges_collapse_into_marks(
    bus: EventBus, panel: ChatPanel, panel_host: HostFactory
) -> None:
    """Расширения, срабатывающие на каждом сообщении, показываются знаком.

    Полная метка у них одинакова у всей ленты: она не описывает конкретное
    сообщение и удваивает высоту истории. Знак остается в строке сообщения,
    полный вид расширения виден в панели сырого потока.
    """
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        bus.publish(ConversationsUpdated((Conversation(jid=BOB, title="Bob"),)))
        bus.publish(ActiveConversationChanged(BOB))
        message = _message(BOB, "рутинное сообщение")
        for xep, action in (("0359", "stanza-id"), ("0184", "receipt-sent")):
            message = message.with_xep(
                XepEvent(xep=xep, action=action, direction=Direction.IN, peer=BOB, ts=time.time())
            )
        bus.publish(MessageAdded(message))
        await _settle(pilot)

        text = _log_text(panel)
        assert "рутинное сообщение" in text
        # Полных меток у рутинных расширений нет, вместо них знаки.
        assert "Stanza ID" not in text
        assert "Receipt Sent" not in text
        assert "#" in text
        assert "✓" in text


async def test_significant_badges_stay_visible(
    bus: EventBus, panel: ChatPanel, panel_host: HostFactory
) -> None:
    """Значимые расширения остаются полной меткой: свертка их не затрагивает."""
    async with panel_host(panel).run_test(size=TEST_SIZE) as pilot:
        bus.publish(ConversationsUpdated((Conversation(jid=BOB, title="Bob"),)))
        bus.publish(ActiveConversationChanged(BOB))
        message = _message(BOB, "исправленное сообщение").with_xep(
            XepEvent(
                xep="0308", action="corrected", direction=Direction.IN, peer=BOB, ts=time.time()
            )
        )
        bus.publish(MessageAdded(message))
        await _settle(pilot)

        assert "Corrected" in _log_text(panel)
