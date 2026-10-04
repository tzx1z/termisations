"""Расширения, которые относятся к другому сообщению.

Реакция по XEP-0444 и отзыв по XEP-0424 приходят строфой без тела и меняют уже
показанную запись, а не создают свою. Ответ по XEP-0461 наоборот приходит
вместе с телом и остается меткой на своем сообщении.

Сеть здесь не нужна: строфы собираются вручную, сессия создается без клиента.
"""

import time
from typing import Final

import pytest

from termisations.core import i18n
from termisations.core.events import (
    CommandTable,
    Event,
    EventBus,
    MessageAdded,
    MessageUpdated,
    XepActivity,
)
from termisations.core.i18n import _
from termisations.core.models import DeliveryState, Direction, Message
from termisations.xeps import badge_text, format_xep_detail

pytest.importorskip("slixmpp", reason="разбор строф требует slixmpp")

from slixmpp.plugins.xep_0424 import stanza as retract_stanza
from slixmpp.plugins.xep_0444 import stanza as reaction_stanza
from slixmpp.plugins.xep_0461 import stanza as reply_stanza
from slixmpp.stanza import Message as SlixMessage
from slixmpp.xmlstream import register_stanza_plugin

from termisations.protocol.account import Account
from termisations.protocol.session import RETRACTED_BODY, SlixmppSession

OWN: Final = "alice@example.org"
PEER: Final = "bob@example.org"
ROOM: Final = "devops@conference.example.org"
TARGET: Final = "msg-1"

# Плагины строф регистрирует сам slixmpp при подключении плагина к клиенту.
# Клиента здесь нет, поэтому строфы регистрируются напрямую.
register_stanza_plugin(SlixMessage, reaction_stanza.Reactions)
register_stanza_plugin(reaction_stanza.Reactions, reaction_stanza.Reaction, iterable=True)
register_stanza_plugin(SlixMessage, retract_stanza.Retract)
register_stanza_plugin(SlixMessage, reply_stanza.Reply)


def make_session(events: list[Event]) -> SlixmppSession:
    """Сессия без клиента с одним сообщением в окне истории."""
    bus = EventBus()
    bus.subscribe(Event, events.append)
    session = SlixmppSession(bus, Account(jid=OWN))
    session._messages[TARGET] = Message(
        message_id=TARGET,
        conversation=PEER,
        sender=OWN,
        body="исходное сообщение",
        ts=time.time(),
        direction=Direction.OUT,
        state=DeliveryState.SENT,
    )
    return session


def reaction(target: str, emoji: set[str], *, sender: str = PEER) -> SlixMessage:
    """Строфа реакции: идентификатор цели и полный набор эмодзи участника."""
    stanza = SlixMessage()
    stanza["from"] = sender
    stanza["to"] = OWN
    stanza["reactions"]["id"] = target
    stanza["reactions"]["values"] = emoji
    return stanza


def retraction(target: str, *, reason: str = "", sender: str = PEER) -> SlixMessage:
    """Строфа отзыва сообщения."""
    stanza = SlixMessage()
    stanza["from"] = sender
    stanza["to"] = OWN
    stanza["retract"]["id"] = target
    if reason:
        stanza["retract"]["reason"] = reason
    return stanza


def updates(events: list[Event]) -> list[Message]:
    """Сообщения из событий обновления."""
    return [event.message for event in events if isinstance(event, MessageUpdated)]


def marks(message: Message, xep: str) -> list[str]:
    """Детали меток нужного расширения."""
    return [format_xep_detail(item) for item in message.xeps if item.xep == xep]


async def test_reaction_lands_on_its_target() -> None:
    """Реакция меняет целевое сообщение, а не появляется своей записью."""
    events: list[Event] = []
    session = make_session(events)
    await session._on_message(reaction(TARGET, {"👍"}))
    assert not [event for event in events if isinstance(event, MessageAdded)]
    changed = updates(events)
    assert len(changed) == 1
    assert changed[0].message_id == TARGET
    assert marks(changed[0], "0444") == ["emoji=👍"]


async def test_reaction_replaces_the_previous_one() -> None:
    """Новый набор эмодзи отменяет прежний, а не добавляется к нему.

    XEP-0444 передает полный набор реакций участника в каждой строфе, поэтому
    накопление меток показывало бы снятые реакции как действующие.
    """
    events: list[Event] = []
    session = make_session(events)
    await session._on_message(reaction(TARGET, {"👍"}))
    await session._on_message(reaction(TARGET, {"🎉"}))
    assert marks(updates(events)[-1], "0444") == ["emoji=🎉"]


async def test_empty_reaction_removes_the_mark() -> None:
    """Пустой набор означает снятие реакций."""
    events: list[Event] = []
    session = make_session(events)
    await session._on_message(reaction(TARGET, {"👍"}))
    await session._on_message(reaction(TARGET, set()))
    assert marks(updates(events)[-1], "0444") == []


async def test_reactions_of_different_people_live_together() -> None:
    """Реакции разных участников не вытесняют друг друга."""
    events: list[Event] = []
    session = make_session(events)
    await session._on_message(reaction(TARGET, {"👍"}))
    await session._on_message(reaction(TARGET, {"🎉"}, sender="carol@example.org"))
    assert sorted(marks(updates(events)[-1], "0444")) == ["emoji=🎉", "emoji=👍"]


async def test_reaction_detail_reaches_the_badge() -> None:
    """Эмодзи виден в метке у сообщения, а не только в строке события.

    Ключ детали обязан совпадать с таблицей ``xeps.BADGE_DETAILS``: при
    расхождении метка собирается, а содержимое в нее не попадает.
    """
    events: list[Event] = []
    session = make_session(events)
    await session._on_message(reaction(TARGET, {"👍"}))
    event = next(item for item in updates(events)[-1].xeps if item.xep == "0444")
    assert "👍" in badge_text(event)


async def test_reaction_without_a_known_target_is_shown_as_an_event() -> None:
    """Реакция на сообщение вне окна истории не теряется молча."""
    events: list[Event] = []
    session = make_session(events)
    await session._on_message(reaction("нет-такого", {"👍"}))
    assert not updates(events)
    activity = [event for event in events if isinstance(event, XepActivity)]
    assert activity and activity[-1].event.xep == "0444"


async def test_retraction_replaces_the_body() -> None:
    """Отзыв меняет текст сообщения, но не убирает запись из ленты."""
    events: list[Event] = []
    session = make_session(events)
    await session._on_message(retraction(TARGET))
    changed = updates(events)[-1]
    assert changed.body == _(RETRACTED_BODY)
    assert [item.action for item in changed.xeps if item.xep == "0424"] == ["retracted"]


async def test_retraction_keeps_the_reason() -> None:
    """Названная причина отзыва остается видна."""
    events: list[Event] = []
    session = make_session(events)
    await session._on_message(retraction(TARGET, reason="ошибся адресом"))
    assert updates(events)[-1].body.endswith("ошибся адресом")


async def test_retraction_badge_comes_from_the_table() -> None:
    """Подпись метки берется из таблицы расширений, а не собирается из действия.

    Действие ``retract`` в таблице не зарегистрировано, и подпись для него
    собиралась капитализацией: в ленте стояло ``Retract`` вместо ``Retracted``.
    """
    events: list[Event] = []
    session = make_session(events)
    await session._on_message(retraction(TARGET))
    event = next(item for item in updates(events)[-1].xeps if item.xep == "0424")
    assert badge_text(event) == "XEP-0424: Retracted"


async def test_reply_stays_a_mark_on_its_own_message() -> None:
    """Ответ приходит с телом и остается меткой своей реплики."""
    events: list[Event] = []
    session = make_session(events)
    stanza = SlixMessage()
    stanza["from"] = PEER
    stanza["to"] = OWN
    stanza["body"] = "согласен"
    stanza["reply"]["id"] = TARGET
    stanza["reply"]["to"] = OWN
    await session._on_message(stanza)
    added = [event.message for event in events if isinstance(event, MessageAdded)]
    assert len(added) == 1
    assert added[0].body == "согласен"
    assert [item.action for item in added[0].xeps if item.xep == "0461"] == ["reply"]


async def test_room_reaction_names_the_participant() -> None:
    """В комнате метка реакции называет ник, а не адрес комнаты."""
    events: list[Event] = []
    session = make_session(events)
    session._messages[TARGET] = Message(
        message_id=TARGET,
        conversation=ROOM,
        sender="carol",
        body="реплика в комнате",
        ts=time.time(),
        direction=Direction.IN,
        state=DeliveryState.RECEIVED,
    )
    stanza = reaction(TARGET, {"👍"}, sender=f"{ROOM}/dave")
    stanza["type"] = "groupchat"
    await session._on_groupchat_message(stanza)
    event = next(item for item in updates(events)[-1].xeps if item.xep == "0444")
    assert event.peer == "dave"
    assert "dave" in badge_text(event)


async def test_own_message_coming_back_does_not_duplicate_the_entry() -> None:
    """Своя строфа, вернувшаяся копией, не заводит вторую запись в ленте.

    Так выглядит переписка с самим собой: сообщение уходит на свой же адрес и
    приходит обратно с тем же идентификатором. Раньше оно добавлялось второй
    записью и затирало текст - в частности, воскрешало только что отозванное
    сообщение.
    """
    events: list[Event] = []
    session = make_session(events)
    stanza = SlixMessage()
    stanza["from"] = OWN
    stanza["to"] = OWN
    stanza["id"] = TARGET
    stanza["body"] = "исходное сообщение"
    await session._on_message(stanza)
    assert not [event for event in events if isinstance(event, MessageAdded)]
    assert session._messages[TARGET].body == "исходное сообщение"


async def test_retracted_message_is_not_revived_by_its_own_copy() -> None:
    """Отзыв переживает возврат собственной строфы."""
    events: list[Event] = []
    session = make_session(events)
    session._retract_message(TARGET, OWN, "")
    stanza = SlixMessage()
    stanza["from"] = OWN
    stanza["to"] = OWN
    stanza["id"] = TARGET
    stanza["body"] = "исходное сообщение"
    await session._on_message(stanza)
    assert session._messages[TARGET].body == _(RETRACTED_BODY)


async def test_retraction_mark_uses_the_language_of_the_moment() -> None:
    """Пометка отзыва переводится в момент отзыва и в таком виде уходит в историю."""
    i18n.set_language("en")
    events: list[Event] = []
    session = make_session(events)
    await session._on_message(retraction(TARGET, reason="wrong chat"))
    assert updates(events)[-1].body == f"{RETRACTED_BODY}: wrong chat"


def test_session_text_follows_the_language() -> None:
    """Сводка /account и таблица /sm выводятся на текущем языке интерфейса.

    Русский вывод сверяется с прежними строками дословно, английский - с
    исходными строками кода.
    """
    events: list[Event] = []
    session = make_session(events)

    summary = session.describe()
    assert summary[0].startswith(f"учетная запись: {OWN}")
    assert summary[1] == "сервер: не подключено"
    assert summary[3] == "проверка сертификата: включена"
    assert summary[4] == "источник пароля: еще не запрошен"
    session.show_sm()
    tables = [event for event in events if isinstance(event, CommandTable)]
    assert tables[-1].rows[:2] == (("включен", "нет"), ("возобновлен", "нет"))

    i18n.set_language("en")
    summary = session.describe()
    assert summary[0].startswith(f"account: {OWN}")
    assert summary[1] == "server: not connected"
    assert summary[3] == "certificate verification: enabled"
    assert summary[4] == "password source: not requested yet"
    session.show_sm()
    tables = [event for event in events if isinstance(event, CommandTable)]
    assert tables[-1].rows == (
        ("enabled", "no"),
        ("resumed", "no"),
        ("unacknowledged", "0"),
        ("inbound handled", "0"),
    )
