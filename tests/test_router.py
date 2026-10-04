"""Общий роутер слэш-команд.

Главная проверка здесь табличная: каждый ключ реестра обязан дойти до владельца.
Раньше роутеров было три, и расхождение находилось только руками - так, ``/sub``
в сетевом режиме не работала вовсе, потому что роутер сверялся с ключом
``roster.sub``, которого в реестре нет.
"""

import inspect
from collections.abc import Sequence
from typing import Any, Final

import pytest

from termisations.core import commands, i18n, router
from termisations.core.commands import REGISTRY, ParsedCommand
from termisations.core.events import EventBus
from termisations.core.i18n import _
from termisations.core.models import PresenceShow
from termisations.core.router import SessionOps, UnsupportedError

# Строка, которой можно вызвать каждый ключ реестра. Значения подобраны так,
# чтобы разбор проходил: иначе тест проверял бы не роутинг, а сообщение об ошибке.
LINES: Final[dict[str, str]] = {
    "conn.connect": "/connect",
    "conn.disconnect": "/disconnect",
    "conn.reconnect": "/reconnect",
    "conn.account": "/account",
    "conn.presence": "/presence away отошел",
    "chat.open": "/chat bob@example.org",
    "chat.close": "/close",
    "chat.join": "/join devops@conference.example.org --nick tester",
    "chat.leave": "/leave",
    "chat.topic": "/topic новая тема",
    "chat.nick": "/nick tester",
    "chat.reply": "/reply msg-1 и вот ответ",
    "chat.react": "/react msg-1 \N{THUMBS UP SIGN}",
    "chat.retract": "/retract msg-1",
    "chat.clear": "/clear",
    "roster.show": "/roster",
    "roster.add": "/add bob@example.org",
    "roster.remove": "/remove bob@example.org",
    "roster.subscribe": "/sub bob@example.org",
    "roster.unsubscribe": "/unsub bob@example.org",
    "roster.block": "/block bob@example.org",
    "roster.unblock": "/unblock bob@example.org",
    "debug.ping": "/ping example.org",
    "debug.disco": "/disco example.org",
    "debug.caps": "/caps bob@example.org",
    "debug.features": "/features",
    "debug.sm": "/sm",
    "debug.tls": "/tls",
    "debug.trace": "/trace id-1",
    "debug.mam": "/mam bob@example.org --limit 10",
    "debug.iq": "/iq example.org jabber:iq:roster get",
    "debug.send": "/send <presence/>",
    "debug.stats": "/stats",
    "debug.panel": "/debug",
    "debug.xml": "/xml both",
    "debug.xml.filter": "/xml filter iq",
    "crypto.omemo.status": "/omemo status bob@example.org",
    "crypto.omemo.enable": "/omemo enable bob@example.org",
    "crypto.omemo.disable": "/omemo disable bob@example.org",
    "crypto.omemo.fingerprints": "/omemo fingerprints bob@example.org",
    "crypto.omemo.trust": "/omemo trust AABBCCDD bob@example.org",
    "crypto.omemo.distrust": "/omemo distrust AABBCCDD bob@example.org",
    "crypto.omemo.purge": "/omemo purge bob@example.org",
    "crypto.omemo.rotate": "/omemo rotate",
    "crypto.ox.status": "/ox status",
    "crypto.ox.enable": "/ox enable",
    "crypto.ox.disable": "/ox disable",
    "files.upload": "/upload /tmp/file.bin",
    "files.slot": "/slot",
    "sys.help": "/help",
    "sys.keys": "/keys",
    "sys.lang": "/lang ru",
    "sys.theme": "/theme nord",
    "sys.log.save": "/log save /tmp/log.xml",
    "sys.quit": "/quit",
}


class RecordingOps:
    """Реализация ``SessionOps``, которая только записывает вызовы.

    Поведения у нее нет: тест проверяет, что ключ дошел до операции, а не то, что
    операция делает. За поведение отвечают tests/test_mock.py и живые тесты сетевой
    сессии.
    """

    def __init__(self, *, online: bool = True) -> None:
        self.calls: list[str] = []
        self.texts: list[tuple[str, bool]] = []
        self._online = online

    def _record(self, name: str) -> None:
        self.calls.append(name)

    # Общее.

    def feedback(self, text: str, ok: bool = True) -> None:
        self.texts.append((text, ok))

    def table(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        self.texts.append((title, True))
        self._record("table")

    def is_online(self) -> bool:
        return self._online

    def active_conversation(self) -> str | None:
        return "bob@example.org"

    def default_target(self) -> str:
        return "example.org"

    # Соединение.

    async def connect(self) -> None:
        self._record("connect")

    async def disconnect(self) -> None:
        self._record("disconnect")

    async def reconnect(self) -> None:
        self._record("reconnect")

    def show_account(self) -> None:
        self._record("show_account")

    def set_presence(self, show: PresenceShow, status: str) -> None:
        self._record(f"set_presence:{show.value}:{status}")

    def stop_session(self) -> None:
        self._record("stop_session")

    # Беседы.

    def open_conversation(self, jid: str) -> None:
        self._record(f"open_conversation:{jid}")

    def close_conversation(self, jid: str | None) -> None:
        self._record(f"close_conversation:{jid}")

    async def join_room(self, room: str, nick: str) -> None:
        self._record(f"join_room:{room}:{nick}")

    def leave_room(self) -> None:
        self._record("leave_room")

    def set_topic(self, subject: str) -> None:
        self._record(f"set_topic:{subject}")

    def set_nick(self, nick: str) -> None:
        self._record(f"set_nick:{nick}")

    async def reply_to(self, message_id: str, text: str) -> None:
        self._record(f"reply_to:{message_id}:{text}")

    async def react_to(self, message_id: str, emoji: Sequence[str]) -> None:
        self._record(f"react_to:{message_id}:{' '.join(emoji)}")

    async def retract(self, message_id: str) -> None:
        self._record(f"retract:{message_id}")

    # Контакт-лист.

    def show_roster(self, *, raw: bool, groups: bool) -> None:
        self._record(f"show_roster:{raw}:{groups}")

    async def roster_add(self, jid: str) -> None:
        self._record(f"roster_add:{jid}")

    async def roster_remove(self, jid: str) -> None:
        self._record(f"roster_remove:{jid}")

    async def subscribe(self, jid: str) -> None:
        self._record(f"subscribe:{jid}")

    async def unsubscribe(self, jid: str) -> None:
        self._record(f"unsubscribe:{jid}")

    async def set_blocked(self, jid: str, *, blocked: bool) -> None:
        self._record(f"set_blocked:{jid}:{blocked}")

    # Отладка.

    async def ping(self, target: str) -> float | None:
        self._record(f"ping:{target}")
        return 12.0

    async def disco(self, jid: str, node: str) -> None:
        self._record(f"disco:{jid}:{node}")

    async def show_caps(self, jid: str) -> None:
        self._record(f"show_caps:{jid}")

    def show_features(self) -> None:
        self._record("show_features")

    def show_sm(self) -> None:
        self._record("show_sm")

    def show_tls(self) -> None:
        self._record("show_tls")

    def show_trace(self, stanza_id: str) -> None:
        self._record(f"show_trace:{stanza_id}")

    async def fetch_mam(self, jid: str, limit: int, before: str) -> None:
        self._record(f"fetch_mam:{jid}:{limit}:{before}")

    async def send_iq(self, target: str, namespace: str, kind: str) -> None:
        self._record(f"send_iq:{target}:{namespace}:{kind}")

    # Шифрование.

    def show_omemo(self, jid: str | None) -> None:
        self._record(f"show_omemo:{jid}")

    def set_omemo(self, jid: str | None, *, enabled: bool) -> None:
        self._record(f"set_omemo:{jid}:{enabled}")

    def show_fingerprints(self, jid: str | None) -> None:
        self._record(f"show_fingerprints:{jid}")

    def set_trust(self, jid: str | None, fingerprint: str, *, trusted: bool) -> None:
        self._record(f"set_trust:{jid}:{fingerprint}:{trusted}")

    def omemo_purge(self, jid: str | None) -> None:
        self._record(f"omemo_purge:{jid}")

    def omemo_rotate(self, jid: str | None) -> None:
        self._record(f"omemo_rotate:{jid}")

    def show_ox(self, action: str) -> None:
        self._record(f"show_ox:{action}")

    # Файлы.

    async def upload(self, source: str) -> None:
        self._record(f"upload:{source}")

    def show_slot(self) -> None:
        self._record("show_slot")


def parse(line: str) -> ParsedCommand:
    """Разобрать строку и убедиться, что разбор прошел."""
    parsed = commands.parse(line)
    assert parsed is not None, line
    assert parsed.error is None, f"{line}: {parsed.error}"
    return parsed


def test_recording_ops_satisfies_protocol() -> None:
    """Заглушка теста сама обязана удовлетворять протоколу."""
    assert isinstance(RecordingOps(), SessionOps)


def test_every_registry_key_has_an_owner() -> None:
    """Каждый ключ реестра обработан таблицей или объявлен ключом интерфейса.

    Роутер проверяет это на импорте, тест повторяет проверку явно: сообщение
    падающего теста читается лучше, чем ошибка импорта в середине сборки.
    """
    assert router.registry_keys() <= frozenset(router._TABLE) | router.UI_KEYS


def test_lines_cover_every_registry_key() -> None:
    """В таблице строк этого файла есть каждый ключ: иначе проверка ниже дырявая."""
    assert set(LINES) == set(router.registry_keys())


@pytest.mark.parametrize("key", sorted(router.registry_keys()))
async def test_key_reaches_its_owner(key: str) -> None:
    """Ключ доходит до операции сессии либо до отказа в пользу интерфейса."""
    ops = RecordingOps()
    await router.route(ops, parse(LINES[key]))
    if key in router.UI_KEYS and key not in router._TABLE:
        assert ops.calls == []
        assert ops.texts and ops.texts[-1][1] is False
        assert "слоем интерфейса" in ops.texts[-1][0]
        return
    assert ops.calls, f"ключ {key} не дошел до операции"


async def test_offline_refusal_is_one_text() -> None:
    """Команды отладки в offline отвечают одним и тем же текстом."""
    ops = RecordingOps(online=False)
    for key in sorted(router.ONLINE_KEYS):
        await router.route(ops, parse(LINES[key]))
    assert ops.calls == []
    assert {text for text, _ok in ops.texts} == {_(router.OFFLINE_REFUSAL)}
    assert _(router.OFFLINE_REFUSAL) == "нет соединения, сначала /connect"


async def test_unsupported_is_printed_as_feedback() -> None:
    """Отказ операции показывается пользователю, а не роняет сессию."""

    class Refusing(RecordingOps):
        async def join_room(self, room: str, nick: str) -> None:
            raise UnsupportedError("комнат тут нет")

    ops = Refusing()
    await router.route(ops, parse(LINES["chat.join"]))
    assert ops.texts[-1] == ("комнат тут нет", False)


async def test_invalid_jid_is_refused_before_the_operation() -> None:
    """Адрес проверяется до отправки строфы: /add на "not-a-jid" не рапортует успех."""
    ops = RecordingOps()
    await router.route(ops, parse("/add not-a-jid"))
    assert ops.calls == []
    assert ops.texts[-1][1] is False
    assert "некорректный JID" in ops.texts[-1][0]


async def test_roster_flags_are_mutually_exclusive() -> None:
    """--raw и --groups вместе дают отказ с ожидаемой сигнатурой."""
    ops = RecordingOps()
    await router.route(ops, parse("/roster --raw --groups"))
    assert ops.calls == []
    assert "/roster" in ops.texts[-1][0]


async def test_crypto_target_falls_back_to_active_conversation() -> None:
    """Без адреса команда шифрования относится к активной беседе."""
    ops = RecordingOps()
    await router.route(ops, parse("/omemo enable"))
    assert ops.calls == ["set_omemo:bob@example.org:True"]


async def test_trust_separates_fingerprint_from_jid() -> None:
    """Отпечаток и адрес различаются по виду, а не по позиции."""
    ops = RecordingOps()
    await router.route(ops, parse("/omemo trust AABB CCDD carol@example.org"))
    assert ops.calls == ["set_trust:carol@example.org:AABB CCDD:True"]


async def test_ping_prints_round_trip_once() -> None:
    """Ответ на /ping печатает роутер, текст выводится один раз."""
    ops = RecordingOps()
    await router.route(ops, parse("/ping example.org"))
    assert ops.calls == ["ping:example.org"]
    assert ops.texts[-1] == ("pong от example.org: 12ms", True)


async def test_ping_without_answer_prints_nothing() -> None:
    """Сессия сама называет причину неудачного пинга, второй строки быть не должно."""

    class Silent(RecordingOps):
        async def ping(self, target: str) -> float | None:
            self._record(f"ping:{target}")
            return None

    ops = Silent()
    await router.route(ops, parse("/ping example.org"))
    assert ops.texts == []


def test_app_local_keys_match_ui_keys() -> None:
    """Карта команд интерфейса совпадает с объявленным набором ключей.

    Расхождение означает команду без хозяина: интерфейс ее не перехватит, а
    сессия ответит отказом в пользу интерфейса.
    """
    from termisations.app import TermisationsApp
    from termisations.mock import MockSession

    bus = EventBus()
    app = TermisationsApp(bus, MockSession(bus))
    assert set(app._local) == router.UI_KEYS


def test_sessions_satisfy_session_ops() -> None:
    """Обе сессии подходят под протокол роутера.

    Проверка структурная: наследования у эмулятора и адаптера нет, общее у них
    только это API.
    """
    from termisations.mock import MockSession

    bus = EventBus()
    assert isinstance(MockSession(bus), SessionOps)

    slixmpp_session = pytest.importorskip(
        "termisations.protocol.session", reason="сетевая сессия требует slixmpp"
    )
    from termisations.protocol.account import Account

    session = slixmpp_session.SlixmppSession(EventBus(), Account(jid="alice@example.org"))
    assert isinstance(session, SessionOps)


def test_session_ops_methods_are_implemented_by_both() -> None:
    """Каждая операция протокола есть у обеих сессий с совместимой сигнатурой.

    ``isinstance`` у ``runtime_checkable`` протокола сверяет только имена, а
    расхождение сигнатур обнаруживалось бы уже во время работы.
    """
    from termisations.mock import MockSession
    from termisations.protocol.session import SlixmppSession

    expected = {
        name: inspect.signature(member)
        for name, member in vars(SessionOps).items()
        if callable(member) and not name.startswith("_")
    }
    for owner in (MockSession, SlixmppSession):
        for name, signature in expected.items():
            method: Any = getattr(owner, name, None)
            assert method is not None, f"{owner.__name__} без операции {name}"
            assert inspect.signature(method) == signature, f"{owner.__name__}.{name}"


def test_registry_keys_skips_unreachable_parents() -> None:
    """Ключ команды с обязательной подкомандой до роутера не доходит."""
    keys = router.registry_keys()
    assert "crypto.omemo" not in keys
    assert "sys.log" not in keys
    # У /xml подкоманда необязательна, поэтому ключ самой команды достижим.
    assert "debug.xml" in keys
    assert {spec.handler_key for spec in REGISTRY if not spec.sub_specs} <= keys


def test_validate_stanza_accepts_one_element() -> None:
    """Корректная строфа проходит, в том числе с префиксом без объявления xmlns."""
    assert router.validate_stanza("<presence/>") is None
    assert router.validate_stanza(" <message><body>привет</body></message> ") is None
    # Префикс stream: объявлен в открытом потоке, а не в самой строфе.
    assert router.validate_stanza("<stream:features/>") is None


@pytest.mark.parametrize(
    ("payload", "part"),
    [
        ("", "пуста"),
        ("не xml", "элементом XML"),
        ("<iq>", "не разбирается"),
        ("<message><body>x</message>", "не разбирается"),
        ("<a/><b/>", "не разбирается"),
    ],
)
def test_validate_stanza_names_the_problem(payload: str, part: str) -> None:
    """Отказ называет причину: общий текст "неверная строфа" бесполезен при отладке."""
    problem = router.validate_stanza(payload)
    assert problem is not None
    assert part in problem


async def test_lang_is_executed_by_the_interface() -> None:
    """/lang меняет язык процесса, поэтому сессия отвечает отказом в пользу интерфейса."""
    assert "sys.lang" in router.UI_KEYS
    assert "sys.lang" not in router._TABLE
    ops = RecordingOps()
    await router.route(ops, parse("/lang en"))
    assert ops.calls == []
    assert ops.texts == [("команда sys.lang исполняется слоем интерфейса", False)]


async def test_router_texts_in_english() -> None:
    """После смены языка ответы роутера выводятся по-английски."""
    i18n.set_language("en")
    ops = RecordingOps(online=False)
    await router.route(ops, parse("/help"))
    await router.route(ops, parse("/sm"))
    await router.route(ops, parse("/add not-a-jid"))
    await router.route(ops, parse("/roster --raw --groups"))
    assert [text for text, _ok in ops.texts] == [
        "command sys.help is handled by the interface layer",
        "not connected, run /connect first",
        "invalid JID: not-a-jid",
        "--raw and --groups are mutually exclusive, expected: /roster [--raw] [--groups]",
    ]

    online = RecordingOps()
    await router.route(online, parse("/ping example.org"))
    assert online.texts[-1] == ("pong from example.org: 12ms", True)
    assert router.validate_stanza("") == "stanza is empty"
    assert _(router.RETRACTED_BODY) == "[message retracted]"
