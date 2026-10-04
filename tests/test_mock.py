"""Генератор эмулированного потока: стадии, события, состав строф, воспроизводимость.

Сценарий обязан проиграть путь от резолва SRV до ошибочной строфы и опубликовать по
дороге события всех нужных типов.
"""

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

import pytest

from termisations.core import i18n
from termisations.core.events import (
    ActiveConversationChanged,
    CommandFeedback,
    CommandTable,
    ConnectionStageChanged,
    ConversationsUpdated,
    Event,
    EventBus,
    MessageAdded,
    MessageUpdated,
    RosterUpdated,
    RunCommandLine,
    StanzaLogged,
    StateUpdated,
    XepActivity,
)
from termisations.core.i18n import _
from termisations.core.models import ConnectionStage, Direction, Encryption, RawStanza
from termisations.core.router import RETRACTED_BODY
from termisations.mock import MockSession

MockFactory = Callable[[EventBus, float, str, int], MockSession]
MockRunner = Callable[[MockSession, Callable[[], bool], float], Awaitable[None]]

SEED: Final = 20260912
RATE: Final = 500.0
TIMEOUT: Final = 12.0

# Обязательный порядок стадий. Проверяется как подпоследовательность:
# сценарий вправе добавить между ними свои стадии.
EXPECTED_STAGES: Final[tuple[ConnectionStage, ...]] = (
    ConnectionStage.RESOLVING_SRV,
    ConnectionStage.TLS_HANDSHAKE,
    ConnectionStage.SASL,
    ConnectionStage.BINDING,
    ConnectionStage.SM_ENABLE,
    ConnectionStage.FETCHING_MAM,
    ConnectionStage.READY,
)

# Маркеры, по которым обязательные строфы сценария узнаются в потоке.
SASL_MARKERS: Final[tuple[str, ...]] = ("urn:ietf:params:xml:ns:xmpp-sasl", "urn:xmpp:sasl:2")
OMEMO_MARKERS: Final[tuple[str, ...]] = ("eu.siacs.conversations.axolotl", "urn:xmpp:omemo:2")
RECEIPT_MARKERS: Final[tuple[str, ...]] = ("urn:xmpp:receipts",)
CHAT_MARKER_MARKERS: Final[tuple[str, ...]] = ("urn:xmpp:chat-markers:0",)
CORRECTION_MARKERS: Final[tuple[str, ...]] = ("urn:xmpp:message-correct:0",)
SM_MARKERS: Final[tuple[str, ...]] = ("urn:xmpp:sm:3",)
MAM_MARKERS: Final[tuple[str, ...]] = ("urn:xmpp:mam:2", "urn:xmpp:mam:1")
ROSTER_MARKERS: Final[tuple[str, ...]] = ("jabber:iq:roster",)
# Бандл ключей PEP: без него строка таблицы раздела 5 про замену тел ключей их
# количеством не проверяется ни на одной строфе сценария.
BUNDLE_MARKERS: Final[tuple[str, ...]] = ("<bundle ", "urn:xmpp:omemo:2:bundles")

# Кириллица в тексте: признак строки, которая не переключилась на английский.
CYRILLIC: Final = re.compile("[А-Яа-яЁё]")


def _of[E: Event](events: list[Event], event_type: type[E]) -> list[E]:
    """События указанного типа в порядке публикации."""
    return [event for event in events if isinstance(event, event_type)]


def _stanzas(events: list[Event]) -> list[RawStanza]:
    return [event.stanza for event in _of(events, StanzaLogged)]


def _contains(stanzas: list[RawStanza], markers: tuple[str, ...]) -> bool:
    """Есть ли в потоке строфа хотя бы с одним из маркеров."""
    return any(any(marker in stanza.xml for marker in markers) for stanza in stanzas)


def _ready(events: list[Event]) -> bool:
    """Дошел ли сценарий до стадии READY."""
    stages = _of(events, ConnectionStageChanged)
    return any(event.stage is ConnectionStage.READY for event in stages)


def _full_scenario(events: list[Event]) -> bool:
    """Сценарий отыгран целиком: есть READY и есть ошибочная строфа."""
    return _ready(events) and any(stanza.is_error for stanza in _stanzas(events))


def _stage_sequence(events: list[Event]) -> list[ConnectionStage]:
    """Стадии в порядке входа. События завершения стадии пропускаются."""
    return [
        event.stage for event in _of(events, ConnectionStageChanged) if event.duration_ms is None
    ]


def _is_subsequence(expected: tuple[ConnectionStage, ...], actual: list[ConnectionStage]) -> bool:
    """Идут ли ожидаемые стадии в заданном порядке внутри фактической цепочки."""
    position = 0
    for stage in actual:
        if position < len(expected) and stage is expected[position]:
            position += 1
    return position == len(expected)


def _handshake_signature(events: list[Event]) -> list[tuple[str, str, bool]]:
    """Структура потока до выхода на READY.

    Это скриптовая часть сценария, поэтому она обязана совпадать между прогонами.
    Идентификаторы и время в подпись не входят: они зависят от часов, а не от сценария.
    """
    signature: list[tuple[str, str, bool]] = []
    for event in events:
        if isinstance(event, ConnectionStageChanged) and event.stage is ConnectionStage.READY:
            break
        if isinstance(event, StanzaLogged):
            signature.append(
                (event.stanza.direction.value, event.stanza.kind.value, event.stanza.is_error)
            )
    return signature


# Стадии.


async def test_scenario_runs_all_stages_in_order(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """Сценарий проходит стадии соединения по порядку и доходит до READY."""
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _full_scenario(events), TIMEOUT)

    stages = _stage_sequence(events)
    assert stages, "сценарий не опубликовал ни одной стадии"
    assert _is_subsequence(EXPECTED_STAGES, stages), f"фактический порядок стадий: {stages}"


async def test_stage_completion_reports_duration(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """Стадия завершается отдельным событием с фактической длительностью."""
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _ready(events), TIMEOUT)

    finished = [
        event for event in _of(events, ConnectionStageChanged) if event.duration_ms is not None
    ]
    assert finished, "нет ни одного события завершения стадии"
    assert all(event.duration_ms is not None and event.duration_ms >= 0.0 for event in finished)


# Состав событий.


@pytest.mark.parametrize(
    "event_type",
    [
        StanzaLogged,
        ConnectionStageChanged,
        StateUpdated,
        RosterUpdated,
        ConversationsUpdated,
        ActiveConversationChanged,
        MessageAdded,
        XepActivity,
    ],
    ids=lambda item: item.__name__,
)
async def test_publishes_event_type(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
    event_type: type[Event],
) -> None:
    """Сценарий публикует события всех типов, на которые подписан интерфейс."""
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _full_scenario(events), TIMEOUT)

    assert _of(events, event_type), f"событие {event_type.__name__} не опубликовано"


async def test_state_is_published_with_connection_details(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """К моменту READY состояние заполнено: JID, транспорт, TLS, потоковый менеджмент."""
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _full_scenario(events), TIMEOUT)

    states = _of(events, StateUpdated)
    assert states, "состояние клиента не публиковалось"
    # Берется состояние живого соединения: после остановки сессии метрики
    # закрытого потока обнуляются, недоступная метрика это n/a.
    live = [item.state for item in states if item.state.stage is not ConnectionStage.OFFLINE]
    assert live, "состояние живого соединения не публиковалось"
    last = live[-1]
    assert last.jid, "JID не заполнен"
    assert last.tls.version, "версия TLS не заполнена"
    assert last.sm.enabled, "потоковый менеджмент не включен"


# Состав потока строф.


@pytest.mark.parametrize(
    ("name", "markers"),
    [
        ("SASL", SASL_MARKERS),
        ("OMEMO", OMEMO_MARKERS),
        ("XEP-0184 receipt", RECEIPT_MARKERS),
        ("XEP-0333 marker", CHAT_MARKER_MARKERS),
        ("XEP-0308 correction", CORRECTION_MARKERS),
        ("XEP-0198 stream management", SM_MARKERS),
        ("XEP-0313 MAM", MAM_MARKERS),
        ("roster", ROSTER_MARKERS),
        ("PEP bundle", BUNDLE_MARKERS),
    ],
    ids=lambda item: item if isinstance(item, str) else "markers",
)
async def test_stream_contains_stanza(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
    name: str,
    markers: tuple[str, ...],
) -> None:
    """В потоке есть все обязательные строфы."""
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _full_scenario(events), TIMEOUT)

    assert _contains(_stanzas(events), markers), f"в потоке нет строфы: {name}"


async def test_stream_contains_error_stanza(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """Ошибочная строфа помечена признаком is_error и содержит type='error'."""
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _full_scenario(events), TIMEOUT)

    errors = [stanza for stanza in _stanzas(events) if stanza.is_error]
    assert errors, "в потоке нет ошибочной строфы"
    assert any("type='error'" in stanza.xml or 'type="error"' in stanza.xml for stanza in errors)


async def test_stream_has_both_directions(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """В потоке есть и входящие, и исходящие строфы, размер считается."""
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _full_scenario(events), TIMEOUT)

    stanzas = _stanzas(events)
    directions = {stanza.direction for stanza in stanzas}
    assert Direction.IN in directions
    assert Direction.OUT in directions
    assert all(stanza.size_bytes > 0 for stanza in stanzas)


async def test_xep_activity_covers_required_extensions(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """Протокольные события покрывают обязательные расширения."""
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _full_scenario(events), TIMEOUT)

    numbers = {event.event.xep for event in _of(events, XepActivity)}
    # Маркеры и корректировки навешиваются на существующее сообщение, поэтому они
    # приходят событием обновления, а не отдельной активностью расширения.
    for added in _of(events, MessageAdded):
        numbers.update(item.xep for item in added.message.xeps)
    for updated in _of(events, MessageUpdated):
        numbers.update(item.xep for item in updated.message.xeps)

    required = {"0198", "0184", "0333", "0308", "0384"}
    assert required <= numbers, f"нет событий расширений: {sorted(required - numbers)}"


async def test_incoming_omemo_message_is_marked_encrypted(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """Входящее OMEMO-сообщение приходит с типом шифрования, а не просто текстом."""
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _full_scenario(events), TIMEOUT)

    messages = [event.message for event in _of(events, MessageAdded)]
    assert messages, "мок не добавил ни одного сообщения"
    assert any(message.encryption is Encryption.OMEMO for message in messages)


# Воспроизводимость.


async def _handshake_run(
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> tuple[list[tuple[str, str, bool]], list[ConnectionStage]]:
    """Один прогон сценария до READY на собственной шине."""
    bus = EventBus()
    collected: list[Event] = []
    bus.subscribe(Event, collected.append)
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _ready(collected), TIMEOUT)
    return _handshake_signature(collected), _stage_sequence(collected)


async def test_handshake_is_reproducible(
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """Два прогона с одинаковыми параметрами дают одинаковую структуру потока.

    Сравнивается скриптовая часть до выхода на READY: идентификаторы и время в
    подпись не входят, они зависят от часов, а не от сценария.
    """
    first_signature, first_stages = await _handshake_run(make_mock, run_mock)
    second_signature, second_stages = await _handshake_run(make_mock, run_mock)

    assert first_signature, "первый прогон не дал ни одной строфы до READY"
    assert first_signature == second_signature, "структура потока отличается между прогонами"
    assert first_stages == second_stages, "порядок стадий отличается между прогонами"


def test_scenarios_are_declared() -> None:
    """Список сценариев объявлен и содержит обязательные имена."""
    assert set(MockSession.SCENARIOS) >= {"default", "stress", "error", "muc"}


@pytest.mark.parametrize("scenario", sorted(MockSession.SCENARIOS))
def test_every_scenario_constructs(bus: EventBus, make_mock: MockFactory, scenario: str) -> None:
    """Любой объявленный сценарий создается без исключения."""
    session = make_mock(bus, RATE, scenario, SEED)
    assert isinstance(session, MockSession)
    session.stop()


# Исполнение команд.


@pytest.mark.parametrize("line", ["/help", "/clear", "/keys", "/stats"])
async def test_ui_commands_are_not_duplicated_by_session(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    line: str,
) -> None:
    """Команды слоя интерфейса сессия не исполняет.

    Справка, клавиши, статистика и очистка окна перехватываются приложением: у
    него реестр, буфер панели лога и лента беседы. Вторая реализация в сессии
    означала бы, что правка текста в одной из них до экрана не доходит.
    """
    session = make_mock(bus, RATE, "default", SEED)
    await session.handle_command(RunCommandLine(line))

    feedback = _of(events, CommandFeedback)
    assert feedback, "команда не дала ответа"
    assert not feedback[-1].ok
    assert "слоем интерфейса" in feedback[-1].text


async def test_handle_unknown_command_reports_failure(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
) -> None:
    """Неизвестная команда дает ответ с признаком ошибки и именем команды."""
    session = make_mock(bus, RATE, "default", SEED)
    await session.handle_command(RunCommandLine("/nosuchcommand"))

    feedback = _of(events, CommandFeedback)
    assert feedback, "команда не дала ответа"
    assert feedback[-1].ok is False
    assert "/nosuchcommand" in feedback[-1].text


async def test_handle_command_reports_expected_usage(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
) -> None:
    """Команда без обязательного аргумента возвращает ожидаемую сигнатуру."""
    session = make_mock(bus, RATE, "default", SEED)
    await session.handle_command(RunCommandLine("/chat"))

    feedback = _of(events, CommandFeedback)
    assert feedback, "команда не дала ответа"
    assert feedback[-1].ok is False
    assert "/chat <jid>" in feedback[-1].text


# Маскирование за пределами панели лога.


async def test_slot_command_masks_signature(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    tmp_path: Path,
) -> None:
    """Команда /slot не печатает подпись из query string в панель беседы.

    Строфа слота маскируется панелью лога, а ответ команды идет в область беседы,
    которая маскирование не применяет. Тот же токен служит значением заголовка
    Authorization, поэтому вывод ссылки целиком обходит сразу два правила маскирования.
    """
    source = tmp_path / "upload_probe.bin"
    source.write_bytes(b"x" * 1024)
    session = make_mock(bus, RATE, "default", SEED)

    await session.handle_command(RunCommandLine(f"/upload {source}"))
    await session.handle_command(RunCommandLine("/slot"))

    slot_stanzas = [stanza for stanza in _stanzas(events) if "urn:xmpp:http:upload:0" in stanza.xml]
    assert slot_stanzas, "строфа слота не попала в поток"
    signature = _slot_signature(slot_stanzas[-1].xml)

    answers = [event.text for event in _of(events, CommandFeedback)]
    assert any("put:" in text for text in answers), "команда /slot не дала ответа"
    for text in answers:
        assert signature not in text, f"подпись слота видна в ответе команды: {text}"
    assert any("[signed]" in text for text in answers), "срез подписи не отмечен в ответе"


def _slot_signature(xml: str) -> str:
    """Достать подпись из строфы слота: она же служит токеном авторизации."""
    match = re.search(r"signature=(?P<token>[^'\"&<]+)", xml)
    assert match, f"в строфе слота нет подписи: {xml}"
    return match.group("token")


# Согласованность сведений об устройствах OMEMO.


def _rows(events: list[Event], title_part: str) -> list[tuple[str, str]]:
    """Строки последней таблицы, заголовок которой содержит подстроку."""
    tables = [event for event in _of(events, CommandTable) if title_part in event.title]
    assert tables, f"таблицы с заголовком {title_part!r} нет"
    return list(tables[-1].rows)


def _device_ids(xml: str) -> list[str]:
    """Идентификаторы устройств из devicelist или из заголовка шифрованной строфы."""
    return re.findall(r"<(?:device|key) (?:id|rid)='(\d+)'", xml)


def _peer_trust(events: list[Event]) -> dict[str, str]:
    """Отпечатки устройств собеседника и состояние доверия по последнему выводу."""
    result: dict[str, str] = {}
    for name, value in _rows(events, "отпечатки"):
        if not name.startswith("устройство "):
            continue
        fingerprint, _, state = value.rpartition("  ")
        result[fingerprint] = state
    return result


async def test_omemo_device_ids_match_across_stream_and_command(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """Один набор устройств собеседника виден одинаково в трех местах.

    PEP-строфа devicelist, адресаты ключей в шифрованной строфе и вывод
    /omemo fingerprints обязаны показывать одни и те же идентификаторы: иначе
    клиент, который продается прозрачностью протокола, показывает три разных
    набора устройств для одного собеседника.
    """
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _full_scenario(events), TIMEOUT)
    await session.handle_command(RunCommandLine("/omemo fingerprints bob@example.org"))

    listed = [stanza for stanza in _stanzas(events) if "axolotl.devicelist" in stanza.xml]
    assert listed, "список устройств не попал в поток"
    published = set(_device_ids(listed[-1].xml))
    assert published, "в devicelist нет ни одного устройства"

    outgoing = [
        stanza
        for stanza in _stanzas(events)
        if stanza.direction is Direction.OUT and "<encrypted" in stanza.xml
    ]
    assert outgoing, "исходящая шифрованная строфа не найдена"
    assert set(_device_ids(outgoing[-1].xml)) == published, "rid не совпал с devicelist"

    shown = {
        name.removeprefix("устройство ")
        for name, _ in _rows(events, "отпечатки")
        if name.startswith("устройство ")
    }
    assert shown == published, "в /omemo fingerprints другие устройства, чем в потоке"


async def test_omemo_trust_counter_matches_fingerprints(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """Счетчик доверенных устройств считается по тому же списку, что и отпечатки.

    Отпечаток вводится в том виде, в котором его печатает команда: группами через
    пробел. Снятие доверия обязано менять и список, и счетчик состояния, иначе
    статус-бар и вывод команды расходятся.
    """
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _full_scenario(events), TIMEOUT)
    await session.handle_command(RunCommandLine("/omemo fingerprints"))

    before = _peer_trust(events)
    assert len(before) > 1, "у собеседника меньше двух устройств"
    assert set(before.values()) == {"доверенный"}, "устройства не доверены при первой сессии"

    target = next(iter(before))
    await session.handle_command(RunCommandLine(f"/omemo distrust {target}"))
    await session.handle_command(RunCommandLine("/omemo status"))
    await session.handle_command(RunCommandLine("/omemo fingerprints"))

    trusted = dict(_rows(events, "OMEMO bob@example.org"))["доверенных устройств"]
    assert trusted == f"{len(before) - 1} из {len(before)}"

    after = _peer_trust(events)
    assert after[target] == "недоверенный"
    assert sum(state == "недоверенный" for state in after.values()) == 1


async def test_trace_finds_message_by_correction_id(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """Путь строфы находится по идентификатору корректировки.

    По XEP-0308 корректировка заменяет исходное сообщение и своего сообщения не
    заводит, но в панели лога видна именно ее строфа. Дельты этапов при этом не
    бывают отрицательными: отсчет идет от самого раннего события пути.
    """
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _full_scenario(events), TIMEOUT)

    corrections = [stanza for stanza in _stanzas(events) if "message-correct:0" in stanza.xml]
    assert corrections, "корректировка не попала в поток"
    match = re.search(r"<origin-id xmlns='urn:xmpp:sid:0' id='(?P<id>[^']+)'", corrections[-1].xml)
    assert match, "у корректировки нет origin-id"

    await session.handle_command(RunCommandLine(f"/trace {match.group('id')}"))

    rows = _rows(events, "путь строфы")
    assert dict(rows)["беседа"], "в пути строфы нет беседы"
    assert not any("+-" in value for _, value in rows), "дельта этапа отрицательная"


async def _first_outgoing(session: MockSession, events: list[Event], run_mock: MockRunner) -> str:
    """Отправить сообщение и вернуть его идентификатор."""
    await run_mock(session, lambda: bool(_of(events, ActiveConversationChanged)), TIMEOUT)
    await session.handle_command(RunCommandLine("//проверка расширений"))
    outgoing = [
        event.message
        for event in _of(events, MessageAdded)
        if event.message.direction is Direction.OUT
    ]
    assert outgoing, "исходящее сообщение не появилось"
    return outgoing[-1].message_id


async def test_reaction_replaces_the_mark_and_shows_the_stanza(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """Реакция уходит строфой без тела и заменяет прежнюю метку у цели."""
    session = make_mock(bus, RATE, "default", SEED)
    message_id = await _first_outgoing(session, events, run_mock)

    await session.handle_command(RunCommandLine(f"/react {message_id} \N{THUMBS UP SIGN}"))
    await session.handle_command(RunCommandLine(f"/react {message_id} \N{PARTY POPPER}"))

    stanzas = [stanza for stanza in _stanzas(events) if "urn:xmpp:reactions:0" in stanza.xml]
    assert len(stanzas) == 2
    assert "<body>" not in stanzas[-1].xml

    updated = [
        event.message
        for event in _of(events, MessageUpdated)
        if event.message.message_id == message_id
    ]
    marks = [item for item in updated[-1].xeps if item.xep == "0444"]
    assert len(marks) == 1
    assert marks[0].detail["emoji"] == "\N{PARTY POPPER}"


async def test_empty_reaction_removes_the_mark(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """Команда без эмодзи снимает реакции."""
    session = make_mock(bus, RATE, "default", SEED)
    message_id = await _first_outgoing(session, events, run_mock)

    await session.handle_command(RunCommandLine(f"/react {message_id} \N{THUMBS UP SIGN}"))
    await session.handle_command(RunCommandLine(f"/react {message_id}"))

    updated = [
        event.message
        for event in _of(events, MessageUpdated)
        if event.message.message_id == message_id
    ]
    assert not [item for item in updated[-1].xeps if item.xep == "0444"]


async def test_retract_replaces_the_body(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """Отзыв меняет текст своего сообщения и не убирает запись из ленты."""
    session = make_mock(bus, RATE, "default", SEED)
    message_id = await _first_outgoing(session, events, run_mock)

    await session.handle_command(RunCommandLine(f"/retract {message_id}"))

    stanzas = [stanza for stanza in _stanzas(events) if "message-retract:1" in stanza.xml]
    assert stanzas, "строфа отзыва не попала в поток"
    updated = [
        event.message
        for event in _of(events, MessageUpdated)
        if event.message.message_id == message_id
    ]
    assert updated[-1].body == _(RETRACTED_BODY)
    assert updated[-1].body == "[сообщение отозвано]"


async def test_reply_marks_the_answer(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """Ответ появляется своим сообщением с меткой исходного."""
    session = make_mock(bus, RATE, "default", SEED)
    message_id = await _first_outgoing(session, events, run_mock)

    await session.handle_command(RunCommandLine(f"/reply {message_id} и вот ответ"))

    replies = [
        event.message
        for event in _of(events, MessageUpdated)
        if any(item.xep == "0461" for item in event.message.xeps)
    ]
    assert replies, "у ответа нет метки XEP-0461"
    assert replies[-1].body == "и вот ответ"
    assert replies[-1].xeps[-1].stanza_id == message_id


# Язык интерфейса.


async def test_english_scenario_content(
    bus: EventBus,
    events: list[Event],
    make_mock: MockFactory,
    run_mock: MockRunner,
) -> None:
    """На английском языке переписка эмулятора, статусы и подписи тоже английские.

    Сценарий тот же, что на русском: seed один, меняется только текст.
    """
    i18n.set_language("en")
    session = make_mock(bus, RATE, "default", SEED)
    await run_mock(session, lambda: _full_scenario(events), TIMEOUT)
    await session.handle_command(RunCommandLine("/omemo status bob@example.org"))

    incoming = [
        event.message.body
        for event in _of(events, MessageAdded)
        if event.message.direction is Direction.IN
    ]
    assert incoming, "входящих сообщений нет"
    assert not [body for body in incoming if CYRILLIC.search(body)], incoming
    assert "push went through, keys delivered to three devices" in incoming

    stanzas = _stanzas(events)
    assert not [stanza.xml for stanza in stanzas if CYRILLIC.search(stanza.xml)]
    assert any("xml:lang='en'" in stanza.xml for stanza in stanzas)

    details = [event.detail for event in _of(events, ConnectionStageChanged)]
    assert "session ready" in details
    assert dict(_rows(events, "OMEMO bob@example.org"))["enabled"] == "yes"
