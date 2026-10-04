"""Генератор эмулированного потока XMPP-строфов.

Эмулятор - альтернативный источник событий для режима ``--mock`` вместо сетевого
слоя ``termisations.protocol``. ``MockSession`` играет сценарий подключения (резолв
SRV, TLS, SASL2, bind2, XEP-0198, roster, presence, синхронизация MAM), затем
переходит в фоновый режим и обслуживает команды интерфейса.

Свойства, на которые опирается остальной каркас:

* поток идет только через шину событий, сессия ничего не знает про Textual;
* строфы кладутся в ``RawStanza`` без маскирования, маскирует панель при рендере;
* случайные элементы берутся из ``random.Random`` с фиксируемым seed, поэтому
  сценарий воспроизводится в тестах строфа в строфу.

Полезная нагрузка SASL считается по-настоящему: клиентское доказательство
SCRAM-SHA-256 выводится из пароля учетной записи. Так маскирование проверяется
на данных, которые действительно являются учетными, а не на строке-заглушке.
Секрет попадает в элемент ``<response/>``, который ``core.redact`` заменяет на
отметку о длине; в элементе ``<initial-response/>`` по устройству SCRAM лежит
только имя пользователя и клиентский nonce, пароля там нет. Второй настоящий
секрет потока - пароль комнаты в строфе входа по XEP-0045: он уходит в открытом
виде и заменяется целиком.
"""

import asyncio
import base64
import hashlib
import hmac
import math
import re
import time
from collections import deque
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from random import Random
from typing import Any, ClassVar, Final
from xml.sax.saxutils import escape

from termisations.core import commands, i18n, router
from termisations.core.events import (
    ActiveConversationChanged,
    ClearXmlLog,
    CloseConversation,
    Command,
    CommandFeedback,
    CommandTable,
    Connect,
    ConnectionStageChanged,
    ConversationsUpdated,
    Disconnect,
    EventBus,
    MessageAdded,
    MessageUpdated,
    Notice,
    NoticeLevel,
    OccupantsUpdated,
    OpenConversation,
    OperationProgress,
    PingServer,
    Quit,
    Reconnect,
    RequestDisco,
    RequestMam,
    RosterUpdated,
    RunCommandLine,
    SendRawXml,
    SendText,
    SetChatState,
    SetOmemoEnabled,
    SetPresence,
    SetUnsafeXml,
    SetXmlFilter,
    SetXmlMode,
    StanzaLogged,
    StateUpdated,
    UnsafeModeChanged,
    XepActivity,
    XmlLogModeChanged,
)
from termisations.core.i18n import N_, _
from termisations.core.models import (
    NOT_AVAILABLE,
    ClientState,
    ConnectionStage,
    Conversation,
    DeliveryState,
    Direction,
    Encryption,
    Message,
    Metrics,
    OmemoInfo,
    PresenceShow,
    RawStanza,
    RosterItem,
    SmInfo,
    StanzaKind,
    TlsInfo,
    Transport,
    XepEvent,
    humanize_bytes,
    stage_label,
    transport_label,
)
from termisations.core.redact import redact
from termisations.xeps import NAMESPACE_XEPS, format_xep_badge, format_xep_detail, xep_title

__all__ = ["MockSession"]

# Параметры сценария.

# Задержки подобраны близко к реальным замерам живых серверов: резолв DNS -
# десятки миллисекунд, полный TLS-хендшейк - от восьмидесяти до двухсот пятидесяти,
# обмен SASL - около сотни. Стадии специально разнесены по времени: при сумме в
# полсекунды ни одна подпись у спиннера не успевает быть прочитанной.
_DELAY_DNS: Final = 0.06
_DELAY_TLS: Final = 0.18
_DELAY_STREAM: Final = 0.05
_DELAY_SASL: Final = 0.07
_DELAY_BIND: Final = 0.05
_DELAY_ROSTER: Final = 0.12
_DELAY_PRESENCE: Final = 0.02
_DELAY_MAM_PAGE: Final = 0.006
_DELAY_ACTION: Final = 0.25

# Одна версия XEP-0384 на весь сценарий: siacs, та же, что у Conversations и Dino.
# Смешивать в одном обмене узлы urn:xmpp:omemo:2 и тела eu.siacs.conversations.axolotl
# нельзя: это две несовместимые версии расширения с разной структурой бандла.
_OMEMO_NS: Final = "eu.siacs.conversations.axolotl"
_OMEMO_DEVICES_NODE: Final = f"{_OMEMO_NS}.devicelist"
_OMEMO_BUNDLE_NODE: Final = f"{_OMEMO_NS}.bundles"

# Число одноразовых ключей в бандле PEP по XEP-0384. Реальный клиент публикует
# сотню, для потока достаточно нескольких: правило маскирования считает их, а не
# показывает тела.
_OMEMO_PREKEYS: Final = 4

# Границы эмулируемого RTT для XEP-0199.
_RTT_MIN: Final = 0.018
_RTT_MAX: Final = 0.065

# Период публикации StateUpdated и период фонового пинга.
_STATE_PERIOD: Final = 1.0
_PING_PERIOD: Final = 20.0

# Нижняя граница периода сна в нагрузочном режиме. Ниже нее строфы отдаются
# пачками: при 500 строфах в секунду отдельный asyncio.sleep на строфу стоит
# дороже самой строфы и расписание перестает выдерживаться.
_MIN_TICK: Final = 0.01

# Предел хранилища сообщений сессии. В нагрузочном режиме поток не должен
# приводить к неограниченному росту памяти.
_MESSAGE_LIMIT: Final = 500

_ID_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz0123456789"
_ID_LENGTH: Final = 10

_CLIENT_NAME: Final = "termisations"
# Идентификатор установки клиента по XEP-0388: устойчивый UUID, а не новый токен
# на каждую сессию. Смысл поля в том, что установка узнаваема между сессиями.
_CLIENT_AGENT_ID: Final = "d4f1c0a6-3b57-4a29-9c41-6f0b8e2d7c15"
_CLIENT_NODE: Final = "https://codeberg.org/termisations/termisations"
_CLIENT_CAPS_VER: Final = "n7pV0bGm3sQ1yR4tXcA8jLd2WfE="

# Возможности потока. Контейнер механизмов SASL2 по XEP-0388 называется
# <authentication>: имя <mechanisms> принадлежит SASL1 из RFC 6120 и живет в
# другом пространстве имен. Версионирование roster объявлено явно, иначе клиент
# не вправе слать ver в запросе.
_STREAM_FEATURES: Final = (
    "<stream:features xmlns:stream='http://etherx.jabber.org/streams'>"
    "<authentication xmlns='urn:xmpp:sasl:2'>"
    "<mechanism>SCRAM-SHA-256-PLUS</mechanism>"
    "<mechanism>SCRAM-SHA-256</mechanism>"
    "<mechanism>SCRAM-SHA-1</mechanism>"
    "<inline><bind xmlns='urn:xmpp:bind:0'/>"
    "<sm xmlns='urn:xmpp:sm:3'/></inline>"
    "</authentication>"
    "<sasl-channel-binding xmlns='urn:xmpp:sasl-cb:0'>"
    "<channel-binding type='{binding}'/>"
    "<channel-binding type='tls-server-end-point'/>"
    "</sasl-channel-binding>"
    "<sm xmlns='urn:xmpp:sm:3'/>"
    "<csi xmlns='urn:xmpp:csi:0'/>"
    "<ver xmlns='urn:xmpp:features:rosterver'/>"
    "<register xmlns='http://jabber.org/features/iq-register'/>"
    "</stream:features>"
)

# Разбор строфы возможностей для команды /features. Регулярные выражения здесь
# уместнее разбора XML: строфа собрана этим же модулем и ее форма известна.
_MECHANISM_RE: Final = re.compile(r"<mechanism>([^<]+)</mechanism>")
_BINDING_RE: Final = re.compile(r"<channel-binding type='([^']+)'/>")
_INLINE_RE: Final = re.compile(r"<inline>(.*?)</inline>", re.DOTALL)
_ELEMENT_RE: Final = re.compile(r"<([A-Za-z-]+) xmlns='([^']+)'\s*/?>")

# Через сколько строф нагрузочного потока сервер подтверждает прием.
_STRESS_ACK_EVERY: Final = 40

_MAX_RATE: Final = 20000.0
_MAX_MAM_BATCH: Final = 2000


# Пространство имен -> номер расширения. Нужна расшифровке фич в /disco и /caps:
# название расширения берется из справочника, своих текстов здесь нет.
_NAMESPACE_XEPS: Final[Mapping[str, str]] = NAMESPACE_XEPS
"""Таблица живет в справочнике расширений: две копии разошлись бы."""

# Пространства имен, на которые сервер отвечает result. Все остальное дает
# service-unavailable, как на живом сервере.
_KNOWN_NAMESPACES: Final[frozenset[str]] = frozenset(_NAMESPACE_XEPS)

# Фичи, которые объявляет этот клиент. Используются, когда кэш disco пуст.
_CLIENT_FEATURES: Final[tuple[str, ...]] = (
    "http://jabber.org/protocol/disco#info",
    "http://jabber.org/protocol/caps",
    "urn:xmpp:receipts",
    "urn:xmpp:chat-markers:0",
    "http://jabber.org/protocol/chatstates",
    "eu.siacs.conversations.axolotl",
)


def _feature_line(namespace: str) -> str:
    """Пространство имен вместе с названием расширения из справочника."""
    number = _NAMESPACE_XEPS.get(namespace)
    return f"{namespace}  {xep_title(number)}" if number else namespace


@dataclass(frozen=True, slots=True)
class _DiscoProfile:
    """Ответ disco для одного вида сущности."""

    category: str
    type: str
    name: str
    features: tuple[str, ...]
    items: tuple[tuple[str, str], ...]


def _disco_profile(target: str, domain: str) -> _DiscoProfile | None:
    """Профиль ответа disco по цели. ``None`` означает item-not-found."""
    if target == domain:
        return _DiscoProfile(
            category="server",
            type="im",
            name="Example XMPP",
            features=(
                "http://jabber.org/protocol/disco#info",
                "http://jabber.org/protocol/disco#items",
                "urn:xmpp:ping",
                "urn:xmpp:sm:3",
                "urn:xmpp:mam:2",
                "urn:xmpp:carbons:2",
                "urn:xmpp:blocking",
                "urn:xmpp:csi:0",
            ),
            items=(
                (f"conference.{domain}", _("chat rooms")),
                (f"upload.{domain}", _("file upload")),
                (f"proxy.{domain}", _("bytestream proxy")),
            ),
        )
    if target.startswith(("conference.", "muc.")) or target == _MUC_ROOM.split("@", 1)[-1]:
        return _DiscoProfile(
            category="conference",
            type="text",
            name=_("chat room service"),
            features=("http://jabber.org/protocol/muc", "http://jabber.org/protocol/disco#items"),
            items=((_MUC_ROOM, _("on-call")),),
        )
    for contact in _CONTACTS:
        if contact.jid == target.split("/", 1)[0]:
            return _DiscoProfile(
                category="client",
                type="pc",
                name=contact.caps_node,
                features=_CLIENT_FEATURES,
                items=(),
            )
    if target == _MUC_ROOM:
        return _DiscoProfile(
            category="conference",
            type="text",
            name=_("on-call and releases"),
            features=("http://jabber.org/protocol/muc", "urn:xmpp:mam:2"),
            items=(),
        )
    return None


@dataclass(frozen=True, slots=True)
class _Contact:
    """Контакт эмулированного контакт-листа."""

    jid: str
    name: str
    groups: tuple[str, ...]
    show: PresenceShow
    status: str
    resource: str
    caps_node: str
    caps_ver: str
    devices: int


# Семь контактов в трех группах: достаточно, чтобы проверить группировку roster
# и разные состояния присутствия.
_CONTACTS: Final[tuple[_Contact, ...]] = (
    _Contact(
        jid="bob@example.org",
        name="Bob Kern",
        groups=("Work",),
        show=PresenceShow.CHAT,
        status=N_("on a call"),
        resource="Conversations.A1b2",
        caps_node="http://conversations.im",
        caps_ver="QgayPKawpkPSDYmwT/WM94uAlu0=",
        devices=4,
    ),
    _Contact(
        jid="carol@example.org",
        name="Carol Reis",
        groups=("Work", "Ops"),
        show=PresenceShow.AVAILABLE,
        status="",
        resource="gajim.7f3c",
        caps_node="https://gajim.org",
        caps_ver="k3DKwS0f4vB6bKxL9uQ0fNfY2mE=",
        devices=2,
    ),
    _Contact(
        jid="dave@jabber.example.net",
        name="Dave Ulm",
        groups=("Ops",),
        show=PresenceShow.DND,
        status=N_("on duty"),
        resource="profanity",
        caps_node="https://profanity-im.github.io",
        caps_ver="0S0DdJLM0i5EwK/PUv9v0J5fFIM=",
        devices=1,
    ),
    _Contact(
        jid="erin@example.org",
        name="Erin Salo",
        groups=("Ops",),
        show=PresenceShow.AWAY,
        status="",
        resource="dino.2c9a",
        caps_node="https://dino.im",
        caps_ver="y7uAlUsPlHUD8Ai1S8u3Hz0DExg=",
        devices=3,
    ),
    _Contact(
        jid="frank@example.org",
        name="Frank Oja",
        groups=("Work",),
        show=PresenceShow.OFFLINE,
        status="",
        resource="Conversations.C4d5",
        caps_node="http://conversations.im",
        caps_ver="QgayPKawpkPSDYmwT/WM94uAlu0=",
        devices=2,
    ),
    _Contact(
        jid="grace@xmpp.example.com",
        name="Grace Lum",
        groups=("Friends",),
        show=PresenceShow.AVAILABLE,
        status="",
        resource="poezio",
        caps_node="https://poez.io",
        caps_ver="Xw3bMe1pKcT5rV8nZq0LhY6dJ2s=",
        devices=1,
    ),
    _Contact(
        jid="heidi@example.org",
        name="Heidi Ferm",
        groups=("Friends",),
        show=PresenceShow.XA,
        status=N_("on vacation"),
        resource="Conversations.E6f7",
        caps_node="http://conversations.im",
        caps_ver="QgayPKawpkPSDYmwT/WM94uAlu0=",
        devices=2,
    ),
)

# Реплики архива и фонового потока. Текст нейтральный технический: он попадает
# в панель беседы и служит материалом для проверки переноса строк. Реплики и
# статусы контактов помечены N_ и переводятся в момент генерации строфы: выбор
# по seed от языка не зависит, язык меняет только текст.
_ARCHIVE_BODIES: Final[tuple[str, ...]] = (
    N_("worker restarted, queue drained"),
    N_("SRV record updated, TTL 300"),
    N_("log shows a timeout connecting to the broker"),
    N_("set a retry in five minutes"),
    N_("build passed, tests are green"),
    N_("connection pool raised to 40"),
    N_("certificate renewed until March"),
    N_("node in the fourth cluster needs a restart"),
    N_("latency metric is back to normal"),
    N_("data mart export finished"),
    N_("replica disk is 82 percent full"),
    N_("moved the job to the night window"),
)

_INCOMING_BODIES: Final[tuple[str, ...]] = (
    N_("please check the log for the last hour"),
    N_("my resource dropped, reconnecting"),
    N_("server returns service-unavailable on disco"),
    N_("started a separate worker for the archive"),
    N_("found the cause: the token in the config expired"),
)

_MUC_ROOM: Final = "devops@conference.example.org"
_MUC_NICK: Final = "alice"
_MUC_OCCUPANTS: Final[tuple[str, ...]] = ("bob", "carol", "dave")
# Пароль комнаты уходит в поток в открытом виде, как того требует XEP-0045.
# Второй настоящий секрет сценария: правило маскирования заменяет его целиком.
_MUC_PASSWORD: Final = "room-pass-7413"


# SASL: настоящие данные вместо заглушки.

_SASL_MECHANISM: Final = "SCRAM-SHA-256-PLUS"
_SASL_ITERATIONS: Final = 4096
_SASL_SALT: Final = bytes.fromhex("5b6d99a41d3c2e7f08b1c4d6e9f2a3b7")
_SASL_CLIENT_NONCE: Final = "rOprNGfwEbeRlpQ6MyPz"
_SASL_SERVER_NONCE: Final = "3rfcNHYJY1ZVvWVs7j"
_SASL_CB_TYPE: Final = "tls-exporter"
_SASL_CB_DATA: Final = bytes.fromhex(
    "9f2c41b7d0e35a68c174fb0923ad5e6c8b31d7f402a95e1c6d80b4f37e5a2c19"
)

# Пароль лежит в исходнике, чтобы маскирование проверялось на строфе,
# которая действительно несет учетные данные.
# Учетная запись эмулированная, за пределы мока этот пароль не выходит.
_SASL_PASSWORD: Final = "S3cr3t-Passw0rd-2026"


@dataclass(frozen=True, slots=True)
class _SaslPayloads:
    """Готовые base64-нагрузки обмена SASL2."""

    initial_response: str
    challenge: str
    response: str
    additional_data: str


def _b64(data: bytes) -> str:
    """Base64 без переносов строк."""
    return base64.b64encode(data).decode("ascii")


def _build_sasl_payloads(username: str) -> _SaslPayloads:
    """Посчитать обмен SCRAM-SHA-256-PLUS для заданного имени пользователя.

    Доказательство считается по RFC 5802 из настоящего пароля, поэтому строка
    в ``<response/>`` - действующие учетные данные для этой пары nonce, а не
    произвольный набор символов.
    """
    gs2_header = f"p={_SASL_CB_TYPE},,"
    client_first_bare = f"n={username},r={_SASL_CLIENT_NONCE}"
    full_nonce = f"{_SASL_CLIENT_NONCE}{_SASL_SERVER_NONCE}"
    server_first = f"r={full_nonce},s={_b64(_SASL_SALT)},i={_SASL_ITERATIONS}"
    cbind = _b64(gs2_header.encode("utf-8") + _SASL_CB_DATA)
    client_final_bare = f"c={cbind},r={full_nonce}"
    auth_message = f"{client_first_bare},{server_first},{client_final_bare}"

    salted = hashlib.pbkdf2_hmac(
        "sha256", _SASL_PASSWORD.encode("utf-8"), _SASL_SALT, _SASL_ITERATIONS
    )
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    client_signature = hmac.new(stored_key, auth_message.encode("utf-8"), hashlib.sha256).digest()
    proof = bytes(left ^ right for left, right in zip(client_key, client_signature, strict=True))
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    server_signature = hmac.new(server_key, auth_message.encode("utf-8"), hashlib.sha256).digest()

    return _SaslPayloads(
        initial_response=_b64((gs2_header + client_first_bare).encode("utf-8")),
        challenge=_b64(server_first.encode("utf-8")),
        response=_b64(f"{client_final_bare},p={_b64(proof)}".encode()),
        additional_data=_b64(f"v={_b64(server_signature)}".encode()),
    )


# Вспомогательные функции модуля.


def _attr(value: str) -> str:
    """Экранировать значение XML-атрибута. Разметка использует одинарные кавычки."""
    return escape(value, {"'": "&apos;", '"': "&quot;"})


def _compact_fingerprint(value: str) -> str:
    """Отпечаток без пробелов и регистра: форма для сравнения, а не для показа."""
    return "".join(value.split()).lower()


def _clock(ts: float) -> str:
    """Время с миллисекундами для вывода команд отладки."""
    return f"{time.strftime('%H:%M:%S', time.localtime(ts))}.{int(ts % 1 * 1000):03d}"


def _stamp(ts: float) -> str:
    """Метка времени в формате XEP-0082 по UTC."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _read_rss_bytes() -> int | None:
    """Резидентная память процесса в байтах. ``None``, если /proc недоступен.

    Чтение синхронное, но вызывается раз в секунду по таймеру состояния и на
    горячий путь публикации строф не попадает.
    """
    try:
        content = Path("/proc/self/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in content.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


class _SessionStoppedError(Exception):
    """Внутренний сигнал остановки сценария.

    Бросается из паузы, когда вызван ``stop()``. Так сценарий сворачивается из
    любой глубины вложенности без проверки кода возврата на каждом шаге.
    """


class MockSession:
    """Источник эмулированного потока строфов и исполнитель команд.

    Сессия владеет состоянием клиента: ``ClientState``, контакт-лист, список
    бесед и сообщения. Панели ничего не вычисляют, они получают готовые события.
    """

    SCENARIOS: ClassVar[tuple[str, ...]] = (
        "default",
        "handshake",
        "stress",
        "error",
        "muc",
    )
    """Доступные сценарии.

    ``default`` и ``handshake`` - подключение плюс редкий фоновый поток;
    ``stress`` - подключение плюс поток с частотой ``rate``;
    ``error`` - фон с ошибочными строфами и обрывом потока;
    ``muc`` - подключение с входом в комнату и потоком групповой беседы.

    Дубля с одним и тем же поведением под разными именами здесь нет:
    иначе наружу выходит не то имя, которое выбрал пользователь.
    """

    def __init__(
        self,
        bus: EventBus,
        rate: float = 8.0,
        scenario: str = "default",
        *,
        jid: str = "alice@example.org",
        resource: str = "termisations",
        mam_batch: int = 40,
        seed: int = 20260912,
    ) -> None:
        """Собрать сессию.

        :param bus: шина событий и команд;
        :param rate: темп потока. В нагрузочном режиме это строго строф в секунду,
            в остальных - частота фоновых событий, каждое из которых дает от одной
            до трех строф;
        :param scenario: имя сценария из ``SCENARIOS``;
        :param jid: голый JID учетной записи;
        :param resource: ресурс, который запрашивается при bind2;
        :param mam_batch: размер пачки архивных сообщений;
        :param seed: seed генератора случайных элементов.
        """
        if rate <= 0:
            raise ValueError(_("rate must be greater than zero"))
        if scenario not in self.SCENARIOS:
            raise ValueError(
                _("unknown scenario: {scenario}, available: {scenarios}").format(
                    scenario=scenario, scenarios=self.SCENARIOS
                )
            )
        if "@" not in jid or jid.startswith("@") or jid.endswith("@"):
            raise ValueError(_("invalid JID: {jid}").format(jid=jid))
        if mam_batch < 0:
            raise ValueError(_("MAM batch size cannot be negative"))

        self._bus = bus
        self._rate = min(float(rate), _MAX_RATE)
        self._scenario = scenario
        self._jid = jid.split("/", 1)[0]
        self._resource = resource or "termisations"
        self._full_jid = f"{self._jid}/{self._resource}"
        self._username, _sep, self._domain = self._jid.partition("@")
        self._mam_batch = min(mam_batch, _MAX_MAM_BATCH)
        self._rng = Random(seed)
        self._sasl = _build_sasl_payloads(self._username)

        self._state = ClientState(jid=self._full_jid)
        # Последняя фраза каждого собеседника: защита от двух одинаковых строк подряд.
        self._last_body: dict[str, str] = {}
        self._roster: dict[str, RosterItem] = {}
        self._conversations: dict[str, Conversation] = {}
        self._messages: dict[str, Message] = {}
        # Дополнительные идентификаторы сообщений: origin-id и stanza-id.
        self._id_index: dict[str, str] = {}
        # Фичи, полученные через disco. Ключ - адрес сущности.
        self._caps_cache: dict[str, tuple[str, ...]] = {}
        # Заблокированные адреса по XEP-0191.
        self._blocked: set[str] = set()
        # Доверенные отпечатки OMEMO и собственное устройство. Идентификатор
        # устройства один на сессию: он стоит в sid исходящих строф и в rid у
        # входящих, и по нему видно, что ключи адресованы именно этому клиенту.
        self._trusted: set[str] = set()
        self._own_device = self._rng.randrange(10**9, 2 * 10**9)
        self._active: str | None = None

        self._stop = asyncio.Event()
        self._ticker: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._online = False

        self._latency: deque[float] = deque(maxlen=5)
        self._started = time.monotonic()
        self._stanzas_total = 0
        self._bytes_in = 0
        self._bytes_out = 0
        self._window_start = self._started
        self._window_count = 0
        self._stanzas_per_sec = 0.0
        self._reconnects = 0

        self._roster_version = ""
        self._sm_id: str | None = None
        # Накопительные счетчики XEP-0198. По разделу 4 расширения h только растет,
        # поэтому отдельно хранится число отправленных строф и последнее h сервера,
        # а неподтвержденные считаются их разницей.
        self._sm_out = 0
        self._sm_acked = 0
        self._sm_in = 0
        # Фактическая строфа stream:features. /features печатает ее разбор, а не
        # собственный список, который с потоком не сходится.
        self._features = ""
        self._id_counter = 0
        self._filler_index = 0
        # Пинг нагрузочного потока ждет ответа: на него отвечает следующая строфа.
        self._filler_ping = False
        self._unsafe_pending = False
        self._last_slot: tuple[str, str] | None = None
        self._muc_room: str | None = None
        self._muc_nick = _MUC_NICK

        # Сессия - единственный исполнитель команд.
        bus.set_command_handler(self.handle_command)

    # Публичные свойства.

    @property
    def state(self) -> ClientState:
        """Текущее состояние клиента."""
        return self._state

    @property
    def stanzas_total(self) -> int:
        """Число строф, отданных в шину с момента создания сессии."""
        return self._stanzas_total

    @property
    def stanzas_per_sec(self) -> float:
        """Фактическая частота потока за последнее окно измерения."""
        return self._stanzas_per_sec

    # Жизненный цикл.

    async def run(self) -> None:
        """Проиграть сценарий и перейти в режим, заданный его именем."""
        self._stop.clear()
        self._started = time.monotonic()
        self._window_start = self._started
        self._ticker = asyncio.create_task(self._state_loop(), name="mock-state")
        try:
            await self._play_connect()
            if self._scenario == "muc":
                await self._play_muc_join()
            if self._scenario == "stress":
                await self._run_stress()
            else:
                await self._run_background()
        except _SessionStoppedError:
            pass
        finally:
            await self._shutdown()

    def stop(self) -> None:
        """Остановить сценарий. Повторный вызов безопасен."""
        self._stop.set()

    async def _shutdown(self) -> None:
        """Снять фоновые задачи и опубликовать финальное состояние."""
        ticker = self._ticker
        self._ticker = None
        if ticker is not None:
            ticker.cancel()
        for task in tuple(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._online = False
        self._state = replace(self._state, stage=ConnectionStage.OFFLINE)
        self._publish_state()

    async def _state_loop(self) -> None:
        """Публикация ``StateUpdated`` раз в секунду."""
        try:
            while not self._stop.is_set():
                await asyncio.sleep(_STATE_PERIOD)
                self._publish_state()
        except asyncio.CancelledError:
            pass

    def _publish_state(self) -> None:
        """Пересчитать метрики и отдать состояние в шину."""
        now = time.monotonic()
        elapsed = now - self._window_start
        if elapsed >= 0.5:
            self._stanzas_per_sec = self._window_count / elapsed
            self._window_start = now
            self._window_count = 0
        latency = sum(self._latency) / len(self._latency) if self._latency else None
        metrics = Metrics(
            latency_ms=latency,
            rss_bytes=_read_rss_bytes(),
            stanzas_total=self._stanzas_total,
            stanzas_per_sec=self._stanzas_per_sec,
            bytes_in=self._bytes_in,
            bytes_out=self._bytes_out,
            uptime_s=now - self._started,
            reconnects=self._reconnects,
        )
        sm = SmInfo(
            # Закрытый поток не имеет потокового менеджмента, даже если
            # идентификатор сессии сохранен для возобновления.
            enabled=self._online and self._sm_id is not None,
            resumed=self._reconnects > 0 and self._sm_id is not None,
            outbound_unacked=max(self._sm_out - self._sm_acked, 0),
            inbound_handled=self._sm_in,
        )
        self._state = replace(self._state, metrics=metrics, sm=sm)
        self._bus.publish(StateUpdated(self._state))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Запустить фоновую задачу и удержать ссылку на нее.

        Ссылка обязательна: без нее задача может быть собрана сборщиком мусора
        до завершения.
        """
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _wait(self, seconds: float) -> None:
        """Пауза, прерываемая ``stop()``."""
        if self._stop.is_set():
            raise _SessionStoppedError
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except TimeoutError:
            return
        raise _SessionStoppedError

    # Примитивы публикации.

    def _make_id(self, prefix: str | None = None) -> str:
        """Правдоподобный идентификатор строфы."""
        self._id_counter += 1
        token = "".join(self._rng.choices(_ID_ALPHABET, k=_ID_LENGTH))
        return token if prefix is None else f"{prefix}-{token}"

    def _url_token(self, size: int) -> str:
        """Случайный токен в безопасном для URL алфавите."""
        return self._fake_b64(size).replace("+", "-").replace("/", "_").rstrip("=")

    def _fake_b64(self, size: int) -> str:
        """Случайная base64-строка заданной длины в байтах."""
        return _b64(self._rng.randbytes(size))

    def _emit(
        self,
        direction: Direction,
        kind: StanzaKind,
        xml: str,
        *,
        stanza_id: str | None = None,
        peer: str | None = None,
        is_error: bool = False,
    ) -> RawStanza:
        """Отдать строфу в шину. Горячий путь потока."""
        stanza = RawStanza.make(
            direction,
            kind,
            xml,
            stanza_id=stanza_id,
            peer=peer,
            is_error=is_error,
        )
        self._stanzas_total += 1
        self._window_count += 1
        if direction is Direction.IN:
            self._bytes_in += stanza.size_bytes
        elif direction is Direction.OUT:
            self._bytes_out += stanza.size_bytes
            if kind is not StanzaKind.STREAM:
                # По XEP-0198 считаются только строфы: <r/>, <a/> и служебные
                # элементы потока в счетчик не входят.
                self._sm_out += 1
        self._bus.publish(StanzaLogged(stanza))
        return stanza

    def _xep(
        self,
        xep: str,
        action: str,
        *,
        direction: Direction = Direction.LOCAL,
        peer: str | None = None,
        stanza_id: str | None = None,
        detail: Mapping[str, str] | None = None,
        publish: bool = True,
    ) -> XepEvent:
        """Собрать событие расширения и при необходимости отдать его в шину."""
        event = XepEvent(
            xep=xep,
            action=action,
            direction=direction,
            peer=peer,
            stanza_id=stanza_id,
            detail=dict(detail) if detail else {},
        )
        if publish:
            self._bus.publish(XepActivity(event))
        return event

    def _stage_start(self, stage: ConnectionStage, detail: str = "") -> float:
        """Открыть стадию подключения и вернуть точку отсчета."""
        self._state = replace(self._state, stage=stage)
        self._bus.publish(ConnectionStageChanged(stage, detail))
        return time.monotonic()

    def _stage_done(self, stage: ConnectionStage, started: float, detail: str = "") -> None:
        """Закрыть стадию с фактической длительностью.

        У готовой сессии состояние возвращается в READY: иначе стадия, начатая
        после подключения (выборка бандлов, ручной запрос архива), навсегда
        остается текущей в ``ClientState``.
        """
        duration = (time.monotonic() - started) * 1000.0
        if self._online:
            self._state = replace(self._state, stage=ConnectionStage.READY)
        self._bus.publish(ConnectionStageChanged(stage, detail, duration))

    def _notice(self, text: str, level: NoticeLevel = NoticeLevel.INFO) -> None:
        """Служебное сообщение в область беседы."""
        self._bus.publish(Notice(text, level))

    def _feedback(self, text: str, ok: bool = True) -> None:
        """Ответ на слэш-команду."""
        self._bus.publish(CommandFeedback(text, ok))

    def _table(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        """Табличный ответ команды: заголовок и пары имя-значение.

        Единый путь для всех отладочных команд. Ручные пробелы в многострочном
        тексте выравнивание не держат: при переносе значение уходит под имя.
        """
        self._bus.publish(CommandTable(title, tuple(rows)))

    @staticmethod
    def _yes_no(value: bool) -> str:
        """Логическое значение на языке интерфейса. Питоновские True и False наружу не идут."""
        return _("yes") if value else _("no")

    @staticmethod
    def _valid_jid(jid: str) -> bool:
        """Похоже ли значение на голый JID по RFC 7622.

        Проверка одна на все команды с аргументом-адресом и живет в роутере:
        аргументы команд разбирает он, и вторая копия правила разошлась бы.
        """
        return router.is_bare_jid(jid)

    # Операции роутера.

    def feedback(self, text: str, ok: bool = True) -> None:
        """Операция роутера: ответ на команду в область беседы."""
        self._feedback(text, ok)

    def table(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        """Операция роутера: табличный ответ."""
        self._table(title, rows)

    def is_online(self) -> bool:
        """Операция роутера: открыт ли поток."""
        return self._online

    def active_conversation(self) -> str | None:
        """Операция роутера: JID активной беседы."""
        return self._active

    def default_target(self) -> str:
        """Операция роутера: домен учетной записи."""
        return self._domain

    # Хранилище домена.

    def _publish_roster(self) -> None:
        """Отдать контакт-лист целиком."""
        self._bus.publish(RosterUpdated(tuple(self._roster.values())))

    def _publish_conversations(self) -> None:
        """Отдать список бесед."""
        self._bus.publish(ConversationsUpdated(tuple(self._conversations.values())))

    def _ensure_conversation(
        self,
        jid: str,
        *,
        title: str = "",
        is_muc: bool = False,
        encryption: Encryption | None = None,
    ) -> Conversation:
        """Найти или создать беседу и опубликовать обновленный список."""
        existing = self._conversations.get(jid)
        if existing is None:
            item = self._roster.get(jid)
            existing = Conversation(
                jid=jid,
                title=title or (item.display_name if item else jid),
                is_muc=is_muc,
                encryption=encryption or Encryption.PLAIN,
                show=item.show if item else PresenceShow.OFFLINE,
            )
            self._conversations[jid] = existing
            self._publish_conversations()
        elif encryption is not None and existing.encryption is not encryption:
            existing = replace(existing, encryption=encryption)
            self._conversations[jid] = existing
            self._publish_conversations()
        return existing

    def _store_message(self, message: Message) -> None:
        """Положить сообщение в хранилище сессии с ограничением по объему."""
        self._messages[message.message_id] = message
        while len(self._messages) > _MESSAGE_LIMIT:
            oldest = next(iter(self._messages))
            del self._messages[oldest]

    def _add_message(self, message: Message) -> Message:
        """Сохранить и опубликовать новое сообщение.

        Шифрование беседы - это режим, а не свойство последнего сообщения: оно
        меняется командой /omemo и фактом установки сессии. Иначе одно фоновое
        нешифрованное сообщение переводит беседу в plain, и метка мигает.
        Счетчик непрочитанного растет, пока беседа не активна.
        """
        self._store_message(message)
        conversation = self._ensure_conversation(message.conversation)
        if message.direction is Direction.IN and message.conversation != self._active:
            self._conversations[conversation.jid] = replace(
                conversation, unread=conversation.unread + 1
            )
            self._publish_conversations()
        self._bus.publish(MessageAdded(message))
        return message

    def _update_message(self, message: Message) -> Message:
        """Сохранить и опубликовать изменение сообщения."""
        self._store_message(message)
        self._bus.publish(MessageUpdated(message))
        return message

    def _find_message(self, message_id: str) -> Message | None:
        """Сообщение по любому из его идентификаторов.

        Кроме id самой строфы принимаются origin-id и stanza-id: именно они видны
        в панели лога, и именно их пользователь копирует в /trace.
        """
        found = self._messages.get(message_id)
        if found is not None:
            return found
        return self._messages.get(self._id_index.get(message_id, ""))

    def _remember_id(self, alias: str | None, message_id: str) -> None:
        """Запомнить дополнительный идентификатор сообщения."""
        if not alias or alias == message_id:
            return
        self._id_index[alias] = message_id
        while len(self._id_index) > _MESSAGE_LIMIT * 3:
            del self._id_index[next(iter(self._id_index))]

    def _set_active(self, jid: str | None) -> None:
        """Сменить активную беседу и снять с нее счетчик непрочитанного."""
        if self._active == jid:
            return
        self._active = jid
        if jid is not None:
            conversation = self._conversations.get(jid)
            if conversation is not None and conversation.unread:
                self._conversations[jid] = replace(conversation, unread=0)
                self._publish_conversations()
        # Счетчик устройств относится к беседе, а не к клиенту: без пересчета
        # в заголовке новой беседы осталось бы число устройств предыдущей.
        self._refresh_omemo()
        self._bus.publish(ActiveConversationChanged(jid))

    # Сценарий подключения.

    async def _play_connect(self) -> None:
        """Полный сценарий подключения от резолва SRV до готовности."""
        await self._play_srv()
        await self._play_tls()
        await self._play_stream_open()
        await self._play_sasl()
        await self._play_bind()
        await self._play_sm_enable()
        await self._play_roster()
        await self._play_presence()
        await self._play_mam(self._pick_contact(0).jid, self._mam_batch)
        self._online = True
        self._state = replace(self._state, stage=ConnectionStage.READY)
        self._bus.publish(ConnectionStageChanged(ConnectionStage.READY, _("session ready")))
        # Первый пинг сразу после готовности: без него latency остается n/a первые
        # секунды, а рядом с живыми TLS и SM это читается как "сервер не отвечает".
        await self._ping(self._domain)
        self._publish_state()
        await self._play_conversation_demo()

    def _pick_contact(self, index: int) -> _Contact:
        """Контакт по индексу с циклическим переходом."""
        return _CONTACTS[index % len(_CONTACTS)]

    async def _play_srv(self) -> None:
        """Шаг 1: разбор SRV-записей по XEP-0368."""
        started = self._stage_start(ConnectionStage.RESOLVING_SRV, self._domain)
        # Резолв DNS не является XML, в потоке он показывается комментарием:
        # так строка остается синтаксически корректным XML и не ломает подсветку.
        self._emit(
            Direction.LOCAL,
            StanzaKind.OTHER,
            f"<!-- dns: query _xmpps-client._tcp.{self._domain} IN SRV -->",
        )
        await self._wait(_DELAY_DNS)
        self._emit(
            Direction.LOCAL,
            StanzaKind.OTHER,
            f"<!-- dns: _xmpps-client._tcp.{self._domain} 300 IN SRV 5 0 5223 "
            f"xmpp.{self._domain}. -->",
        )
        self._emit(
            Direction.LOCAL,
            StanzaKind.OTHER,
            f"<!-- dns: query _xmpp-client._tcp.{self._domain} IN SRV -->",
        )
        await self._wait(_DELAY_DNS)
        self._emit(
            Direction.LOCAL,
            StanzaKind.OTHER,
            f"<!-- dns: _xmpp-client._tcp.{self._domain} 300 IN SRV 10 0 5222 "
            f"xmpp.{self._domain}. -->",
        )
        self._emit(
            Direction.LOCAL,
            StanzaKind.OTHER,
            f"<!-- dns: xmpp.{self._domain}. 300 IN A 198.51.100.24 -->",
        )
        self._xep(
            "0368",
            "srv-resolved",
            detail={"direct": "5223", "starttls": "5222", "host": f"xmpp.{self._domain}"},
        )
        self._stage_done(ConnectionStage.RESOLVING_SRV, started, _("2 records, 5223 selected"))

    async def _play_tls(self) -> None:
        """Шаг 2: прямой TLS на 5223 и заполнение TlsInfo."""
        started = self._stage_start(ConnectionStage.TLS_HANDSHAKE, "DirectTLS:5223")
        self._emit(
            Direction.LOCAL,
            StanzaKind.TLS,
            "<!-- tls: connect 198.51.100.24:5223 direct TLS, XEP-0368 -->",
        )
        await self._wait(_DELAY_TLS)
        self._emit(
            Direction.LOCAL,
            StanzaKind.TLS,
            "<!-- tls: TLSv1.3 TLS_AES_256_GCM_SHA384 kex=X25519MLKEM768 -->",
        )
        self._emit(
            Direction.LOCAL,
            StanzaKind.TLS,
            f"<!-- tls: cert CN=xmpp.{self._domain} issuer=Example CA R3 "
            "notAfter=2027-03-04T00:00:00Z sha256=3f:a1:9c:22:... -->",
        )
        self._emit(
            Direction.LOCAL,
            StanzaKind.TLS,
            f"<!-- tls: channel binding {_SASL_CB_TYPE} exported -->",
        )
        tls = TlsInfo(
            version="TLS1.3",
            cipher="TLS_AES_256_GCM_SHA384",
            channel_binding=_SASL_CB_TYPE,
            valid=True,
            chain=(
                f"CN=xmpp.{self._domain}",
                "Example CA R3",
                "Example Root X1",
            ),
        )
        self._state = replace(self._state, tls=tls, transport=Transport.DIRECT_TLS)
        # Состояние публикуется сразу: таймер раз в секунду длиннее всего сценария
        # подключения, и статус-бар показывал бы n/a, когда все уже установлено.
        self._publish_state()
        self._xep("0368", "direct-tls", detail={"port": "5223"})
        self._xep("0440", "channel-binding", detail={"type": _SASL_CB_TYPE})
        self._stage_done(ConnectionStage.TLS_HANDSHAKE, started, _("TLS1.3, certificate verified"))

    async def _play_stream_open(self) -> None:
        """Открытие потока и объявленные сервером возможности."""
        self._emit(
            Direction.OUT,
            StanzaKind.STREAM,
            f"<stream:stream to='{_attr(self._domain)}' xmlns='jabber:client' "
            f"xmlns:stream='http://etherx.jabber.org/streams' version='1.0' "
            f"xml:lang='{i18n.get_language()}'>",
        )
        await self._wait(_DELAY_STREAM)
        stream_id = self._make_id()
        self._emit(
            Direction.IN,
            StanzaKind.STREAM,
            f"<stream:stream from='{_attr(self._domain)}' id='{stream_id}' "
            "xmlns='jabber:client' xmlns:stream='http://etherx.jabber.org/streams' "
            f"version='1.0' xml:lang='{i18n.get_language()}'>",
        )
        self._emit(
            Direction.IN,
            StanzaKind.STREAM,
            # Префикс stream в живом потоке связан открывающим тегом. Здесь он
            # объявлен повторно, чтобы каждая строфа лога разбиралась отдельно:
            # панель разворачивает выбранную строфу без остального потока.
            _STREAM_FEATURES.format(binding=_SASL_CB_TYPE),
        )
        self._features = _STREAM_FEATURES.format(binding=_SASL_CB_TYPE)

    async def _play_sasl(self) -> None:
        """Шаг 3: обмен SASL2 с нагрузкой, которая обязана быть замаскирована."""
        started = self._stage_start(ConnectionStage.SASL, _SASL_MECHANISM)
        self._emit(
            Direction.OUT,
            StanzaKind.SASL,
            f"<authenticate xmlns='urn:xmpp:sasl:2' mechanism='{_SASL_MECHANISM}'>"
            f"<initial-response>{self._sasl.initial_response}</initial-response>"
            f"<user-agent id='{_CLIENT_AGENT_ID}'>"
            f"<software>{_CLIENT_NAME}</software><device>workstation</device>"
            "</user-agent>"
            f"<bind xmlns='urn:xmpp:bind:0'><tag>{_attr(self._resource)}</tag></bind>"
            "</authenticate>",
        )
        await self._wait(_DELAY_SASL)
        self._emit(
            Direction.IN,
            StanzaKind.SASL,
            f"<challenge xmlns='urn:xmpp:sasl:2'>{self._sasl.challenge}</challenge>",
        )
        self._xep(
            "0388", "challenge", direction=Direction.IN, detail={"mechanism": _SASL_MECHANISM}
        )
        await self._wait(_DELAY_SASL)
        # Именно эта строфа несет учетные данные: доказательство SCRAM выведено
        # из пароля. В логе она обязана выглядеть как отметка о длине.
        self._emit(
            Direction.OUT,
            StanzaKind.SASL,
            f"<response xmlns='urn:xmpp:sasl:2'>{self._sasl.response}</response>",
        )
        await self._wait(_DELAY_SASL)
        # Ресурс назначает сервер: <tag> из bind2 это метка клиента, к ней сервер
        # дописывает случайную часть. Ресурс ровно из метки невозможен.
        self._full_jid = f"{self._jid}/{self._resource}.{self._make_id()[:6]}"
        self._sm_id = self._fake_b64(24)
        self._emit(
            Direction.IN,
            StanzaKind.SASL,
            "<success xmlns='urn:xmpp:sasl:2'>"
            f"<additional-data>{self._sasl.additional_data}</additional-data>"
            f"<authorization-identifier>{_attr(self._full_jid)}</authorization-identifier>"
            # Результаты inline-запросов приходят внутри success, отдельной
            # IQ-строфы bind в Bind 2 нет вообще.
            "<bound xmlns='urn:xmpp:bind:0'/>"
            f"<enabled xmlns='urn:xmpp:sm:3' id='{self._sm_id}' resume='true' "
            f"location='xmpp.{self._domain}:5223' max='600'/>"
            "</success>",
        )
        self._xep(
            "0388",
            "authenticated",
            direction=Direction.IN,
            detail={"mechanism": _SASL_MECHANISM, "binding": _SASL_CB_TYPE},
        )
        self._stage_done(
            ConnectionStage.SASL,
            started,
            _("{mechanism}, channel bound").format(mechanism=_SASL_MECHANISM),
        )

    async def _play_bind(self) -> None:
        """Шаг 4: разбор inline-результата bind2 из строфы success.

        Отдельной IQ-строфы здесь нет: по XEP-0386 элемент bound
        приходит внутри success, а полный JID несет authorization-identifier.
        """
        started = self._stage_start(ConnectionStage.BINDING, self._resource)
        await self._wait(_DELAY_BIND)
        self._state = replace(self._state, jid=self._full_jid)
        self._xep("0386", "bound", direction=Direction.IN, detail={"jid": self._full_jid})
        self._stage_done(ConnectionStage.BINDING, started, self._full_jid)
        self._publish_state()

    async def _play_sm_enable(self) -> None:
        """Шаг 5: потоковый менеджмент включен inline вместе с bind2."""
        started = self._stage_start(ConnectionStage.SM_ENABLE)
        await self._wait(_DELAY_BIND)
        self._sm_out = 0
        self._sm_acked = 0
        self._sm_in = 0
        self._xep(
            "0198", "enabled", direction=Direction.IN, detail={"resume": "true", "max": "600"}
        )
        self._stage_done(ConnectionStage.SM_ENABLE, started, "resume=true, max=600")
        self._publish_state()

    async def _play_resume(self) -> None:
        """Возобновление потока по XEP-0198 вместо полного подключения."""
        started = self._stage_start(ConnectionStage.SM_RESUME)
        previd = self._sm_id or self._fake_b64(24)
        self._emit(
            Direction.OUT,
            StanzaKind.STREAM,
            f"<resume xmlns='urn:xmpp:sm:3' h='{self._sm_in}' previd='{previd}'/>",
        )
        await self._wait(_DELAY_BIND)
        self._sm_id = previd
        self._emit(
            Direction.IN,
            StanzaKind.STREAM,
            f"<resumed xmlns='urn:xmpp:sm:3' h='{self._sm_out}' previd='{previd}'/>",
        )
        self._sm_acked = self._sm_out
        self._reconnects += 1
        self._online = True
        # Сессия пережила обрыв: присутствие сервер не сбрасывал.
        self._state = replace(
            self._state,
            stage=ConnectionStage.READY,
            presence_show=PresenceShow.AVAILABLE,
        )
        self._xep("0198", "resumed", direction=Direction.IN, detail={"h": str(self._sm_in)})
        self._stage_done(ConnectionStage.SM_RESUME, started, _("stream resumed"))
        self._bus.publish(ConnectionStageChanged(ConnectionStage.READY, _("session ready")))
        # Задержка обнулена вместе с разрывом, и без замера по новому каналу поле
        # оставалось бы n/a до срабатывания таймера пинга.
        await self._ping(self._domain)
        self._publish_state()

    async def _play_roster(self) -> None:
        """Шаг 6: запрос контакт-листа и ответ с группами.

        Версия roster пустая: пустое значение по RFC 6121 и означает, что кэша у
        клиента нет. Запрос с сохраненной версией уместен при переподключении, а
        не на первом соединении сразу после открытия потока.
        """
        started = self._stage_start(ConnectionStage.FETCHING_ROSTER)
        iq_id = self._make_id("roster")
        version = self._roster_version or ""
        self._emit(
            Direction.OUT,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='get' id='{iq_id}'>"
            f"<query xmlns='jabber:iq:roster' ver='{version}'/></iq>",
            stanza_id=iq_id,
        )
        await self._wait(_DELAY_ROSTER)
        items = []
        for contact in _CONTACTS:
            groups = "".join(f"<group>{escape(group)}</group>" for group in contact.groups)
            items.append(
                f"<item jid='{_attr(contact.jid)}' name='{_attr(contact.name)}' "
                f"subscription='both'>{groups}</item>"
            )
            self._roster[contact.jid] = RosterItem(
                jid=contact.jid,
                name=contact.name,
                subscription="both",
                groups=contact.groups,
                show=PresenceShow.OFFLINE,
            )
        self._roster_version = f"ver{self._rng.randrange(10, 99)}"
        self._emit(
            Direction.IN,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='result' id='{iq_id}' "
            f"to='{_attr(self._full_jid)}'>"
            f"<query xmlns='jabber:iq:roster' ver='{self._roster_version}'>"
            f"{''.join(items)}</query></iq>",
            stanza_id=iq_id,
        )
        self._sm_in += 1
        self._publish_roster()
        self._stage_done(
            ConnectionStage.FETCHING_ROSTER,
            started,
            _("contacts: {count}").format(count=len(self._roster)),
        )

    async def _play_presence(self) -> None:
        """Шаг 7: собственное присутствие и presence контактов с capabilities."""
        started = self._stage_start(ConnectionStage.INITIAL_PRESENCE)
        self._emit(
            Direction.OUT,
            StanzaKind.PRESENCE,
            f"<presence xmlns='jabber:client'>"
            "<priority>1</priority>"
            f"<c xmlns='http://jabber.org/protocol/caps' hash='sha-1' "
            f"node='{_attr(_CLIENT_NODE)}' ver='{_CLIENT_CAPS_VER}'/>"
            "</presence>",
        )
        self._state = replace(self._state, presence_show=PresenceShow.AVAILABLE)
        for contact in _CONTACTS:
            if contact.show is PresenceShow.OFFLINE:
                continue
            await self._wait(_DELAY_PRESENCE)
            self._emit_contact_presence(contact, contact.show, _(contact.status))
        online = sum(1 for c in _CONTACTS if c.show is not PresenceShow.OFFLINE)
        self._xep(
            "0115",
            "caps-received",
            direction=Direction.IN,
            detail={"entities": str(online)},
        )
        self._publish_roster()
        self._stage_done(
            ConnectionStage.INITIAL_PRESENCE, started, _("online: {count}").format(count=online)
        )
        self._publish_state()

    def _emit_contact_presence(
        self, contact: _Contact, show: PresenceShow, status: str = ""
    ) -> None:
        """Присутствие контакта вместе с XEP-0115."""
        full = f"{contact.jid}/{contact.resource}"
        if show is PresenceShow.OFFLINE:
            body = "type='unavailable'"
            inner = ""
        else:
            body = ""
            show_element = "" if show is PresenceShow.AVAILABLE else f"<show>{show.value}</show>"
            status_element = f"<status>{escape(status)}</status>" if status else ""
            inner = (
                f"{show_element}{status_element}<priority>8</priority>"
                f"<c xmlns='http://jabber.org/protocol/caps' hash='sha-1' "
                f"node='{_attr(contact.caps_node)}' ver='{contact.caps_ver}'/>"
            )
        self._emit(
            Direction.IN,
            StanzaKind.PRESENCE,
            f"<presence xmlns='jabber:client' from='{_attr(full)}' "
            f"to='{_attr(self._full_jid)}'{' ' + body if body else ''}>{inner}</presence>",
            peer=contact.jid,
        )
        self._sm_in += 1
        item = self._roster.get(contact.jid)
        if item is not None:
            self._roster[contact.jid] = replace(item, show=show, status=status)
        conversation = self._conversations.get(contact.jid)
        if conversation is not None and conversation.show is not show:
            # Полоса бесед показывает тот же маркер присутствия, что и статус-бар.
            self._conversations[contact.jid] = replace(conversation, show=show)
            self._publish_conversations()

    async def _play_mam(self, with_jid: str, limit: int, before: str = "") -> None:
        """Шаг 8: синхронизация архива пачкой. Основная пиковая нагрузка панели."""
        if limit <= 0:
            return
        # Беседа делается активной до начала выгрузки: иначе весь период загрузки
        # заголовок панели сообщает, что беседа не выбрана.
        self._ensure_conversation(with_jid)
        self._set_active(with_jid)
        started = self._stage_start(ConnectionStage.FETCHING_MAM, f"{with_jid} 0/{limit}")
        query_id = self._make_id("mam")
        iq_id = self._make_id("iq")
        self._emit(
            Direction.OUT,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='set' id='{iq_id}'>"
            f"<query xmlns='urn:xmpp:mam:2' queryid='{query_id}'>"
            "<x xmlns='jabber:x:data' type='submit'>"
            "<field var='FORM_TYPE' type='hidden'><value>urn:xmpp:mam:2</value></field>"
            f"<field var='with'><value>{_attr(with_jid)}</value></field>"
            "</x>"
            f"<set xmlns='http://jabber.org/protocol/rsm'><max>{limit}</max>"
            f"<before>{_attr(before)}</before></set>"
            "</query></iq>",
            stanza_id=iq_id,
            peer=with_jid,
        )
        self._xep(
            "0313",
            "fetch-started",
            direction=Direction.OUT,
            peer=with_jid,
            stanza_id=query_id,
            detail={"limit": str(limit)},
        )
        now = time.time()
        first_id = ""
        last_id = ""
        progress_step = max(limit // 8, 1)
        for index in range(limit):
            await self._wait(_DELAY_MAM_PAGE)
            if index and index % progress_step == 0:
                # Прогресс виден по ходу выгрузки, а не только по ее итогу.
                # Событие свое, а не стадия: стадии описывают подключение.
                self._bus.publish(
                    OperationProgress(
                        _("archive {jid}").format(jid=with_jid), index, limit, f"{index}/{limit}"
                    )
                )
            archive_id = self._make_id()
            first_id = first_id or archive_id
            last_id = archive_id
            incoming = index % 3 != 0
            sender = with_jid if incoming else self._jid
            resource = "Conversations.A1b2" if incoming else self._resource
            body = _(_ARCHIVE_BODIES[(index * 5 + 1) % len(_ARCHIVE_BODIES)])
            ts = now - (limit - index) * 47.0
            inner_id = self._make_id("msg")
            self._emit(
                Direction.IN,
                StanzaKind.MESSAGE,
                f"<message xmlns='jabber:client' from='{_attr(self._jid)}' "
                f"to='{_attr(self._full_jid)}' id='{self._make_id()}'>"
                f"<result xmlns='urn:xmpp:mam:2' queryid='{query_id}' id='{archive_id}'>"
                "<forwarded xmlns='urn:xmpp:forward:0'>"
                f"<delay xmlns='urn:xmpp:delay' stamp='{_stamp(ts)}'/>"
                f"<message xmlns='jabber:client' from='{_attr(sender)}/{_attr(resource)}' "
                f"to='{_attr(self._jid if incoming else with_jid)}' type='chat' id='{inner_id}'>"
                f"<body>{escape(body)}</body>"
                f"<stanza-id xmlns='urn:xmpp:sid:0' id='{archive_id}' "
                f"by='{_attr(self._jid)}'/>"
                "</message></forwarded></result></message>",
                stanza_id=archive_id,
                peer=with_jid,
            )
            self._sm_in += 1
            self._remember_id(archive_id, inner_id)
            self._add_message(
                Message(
                    message_id=inner_id,
                    conversation=with_jid,
                    sender=sender,
                    body=body,
                    ts=ts,
                    direction=Direction.IN if incoming else Direction.OUT,
                    state=DeliveryState.RECEIVED if incoming else DeliveryState.ACKED,
                    xeps=(
                        # Метки пачки архива у каждого сообщения одинаковы и
                        # ничего о нем не говорят. Их показывает строка итога
                        # выгрузки, а справочник помечает их непечатаемыми у
                        # сообщения, см. xeps.SILENT_BADGES.
                        self._xep(
                            "0313",
                            "page-received",
                            peer=with_jid,
                            stanza_id=archive_id,
                            publish=False,
                        ),
                        self._xep(
                            "0203",
                            "delayed",
                            peer=with_jid,
                            detail={"stamp": _stamp(ts)},
                            publish=False,
                        ),
                    ),
                )
            )
        self._emit(
            Direction.IN,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='result' id='{iq_id}' "
            f"to='{_attr(self._full_jid)}'>"
            "<fin xmlns='urn:xmpp:mam:2' complete='true'>"
            "<set xmlns='http://jabber.org/protocol/rsm'>"
            f"<first index='0'>{first_id}</first><last>{last_id}</last>"
            f"<count>{limit}</count></set></fin></iq>",
            stanza_id=iq_id,
            peer=with_jid,
        )
        self._sm_in += 1
        self._xep(
            "0313",
            "complete",
            direction=Direction.IN,
            peer=with_jid,
            detail={"messages": str(limit)},
        )
        self._stage_done(
            ConnectionStage.FETCHING_MAM, started, _("messages: {count}").format(count=limit)
        )

    async def _play_conversation_demo(self) -> None:
        """Шаги 9-13: OMEMO, receipt, маркер, корректировка, ошибочная строфа."""
        contact = self._pick_contact(0)
        await self._wait(_DELAY_ACTION)
        await self._play_omemo_bundle(contact)
        await self._wait(_DELAY_ACTION)
        incoming = await self._play_omemo_incoming(contact)
        await self._wait(_DELAY_ACTION)
        await self._play_receipt_out(contact, incoming)
        await self._wait(_DELAY_ACTION)
        outgoing_id = await self._send_text(contact.jid, _("restart the archive worker"))
        await self._wait(_DELAY_ACTION)
        if outgoing_id is not None:
            await self._play_receipt_in(contact, outgoing_id)
            await self._wait(_DELAY_ACTION)
            await self._play_marker_in(contact, outgoing_id)
            await self._wait(_DELAY_ACTION)
            await self._play_correction_out(contact, outgoing_id)
        await self._wait(_DELAY_ACTION)
        await self._play_error_stanza(self._pick_contact(4))

    async def _play_omemo_bundle(self, contact: _Contact) -> None:
        """Шаг 9а: список устройств и бандл ключей OMEMO через PEP.

        Перед первым шифрованным сообщением клиент забирает у пира список устройств
        и бандл предварительных ключей. Это единственное место потока, где
        встречается бандл, поэтому на нем проверяется строка таблицы раздела 5 про
        замену тел ключей их количеством.
        """
        started = self._stage_start(ConnectionStage.OMEMO_SESSIONS, contact.jid)
        # Идентификаторы устройств общие с /omemo fingerprints и с rid в строфах:
        # один и тот же набор устройств не должен выглядеть на экране как три.
        devices = [device for device, _fp in self._peer_devices(contact.jid)]
        listed = "".join(f"<device id='{device}'/>" for device in devices)
        self._emit(
            Direction.IN,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' from='{_attr(contact.jid)}' "
            f"to='{_attr(self._full_jid)}' type='headline' id='{self._make_id('pep')}'>"
            "<event xmlns='http://jabber.org/protocol/pubsub#event'>"
            f"<items node='{_OMEMO_DEVICES_NODE}'><item id='current'>"
            f"<list xmlns='{_OMEMO_NS}'>{listed}</list>"
            "</item></items></event></message>",
            peer=contact.jid,
        )
        self._sm_in += 1
        self._xep(
            "0163",
            "event",
            direction=Direction.IN,
            peer=contact.jid,
            detail={"node": _OMEMO_DEVICES_NODE, "devices": str(len(devices))},
        )

        iq_id = self._make_id("bundle")
        device = devices[0]
        self._emit(
            Direction.OUT,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='get' id='{iq_id}' "
            f"to='{_attr(contact.jid)}'>"
            "<pubsub xmlns='http://jabber.org/protocol/pubsub'>"
            f"<items node='{_OMEMO_BUNDLE_NODE}:{device}' max_items='1'>"
            "</items></pubsub></iq>",
            stanza_id=iq_id,
            peer=contact.jid,
        )
        await self._wait(_DELAY_ROSTER)
        prekeys = "".join(
            f"<preKeyPublic preKeyId='{index + 1}'>{self._fake_b64(33)}</preKeyPublic>"
            for index in range(_OMEMO_PREKEYS)
        )
        self._emit(
            Direction.IN,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='result' id='{iq_id}' "
            f"from='{_attr(contact.jid)}' to='{_attr(self._full_jid)}'>"
            "<pubsub xmlns='http://jabber.org/protocol/pubsub'>"
            f"<items node='{_OMEMO_BUNDLE_NODE}:{device}'><item id='current'>"
            f"<bundle xmlns='{_OMEMO_NS}'>"
            f"<signedPreKeyPublic signedPreKeyId='1'>{self._fake_b64(33)}</signedPreKeyPublic>"
            f"<signedPreKeySignature>{self._fake_b64(64)}</signedPreKeySignature>"
            f"<identityKey>{self._fake_b64(33)}</identityKey>"
            f"<prekeys>{prekeys}</prekeys>"
            "</bundle></item></items></pubsub></iq>",
            stanza_id=iq_id,
            peer=contact.jid,
        )
        self._sm_in += 1
        self._xep(
            "0384",
            "bundle-fetched",
            direction=Direction.IN,
            peer=contact.jid,
            stanza_id=iq_id,
            detail={"device": str(device), "prekeys": str(_OMEMO_PREKEYS)},
        )
        self._stage_done(
            ConnectionStage.OMEMO_SESSIONS,
            started,
            _("{jid}, devices: {count}").format(jid=contact.jid, count=len(devices)),
        )

    async def _play_omemo_incoming(self, contact: _Contact) -> str:
        """Шаг 9: входящее сообщение OMEMO с несколькими key rid."""
        message_id = self._make_id("msg")
        archive_id = self._make_id()
        # Отправляет первое устройство собеседника, ключи адресованы нашему
        # устройству и остальным устройствам собеседника: так выглядит копия
        # сообщения для собственных устройств отправителя.
        devices = self._peer_devices(contact.jid)
        sender_device = devices[0][0]
        recipients = [self._own_device, *(device for device, _fp in devices[1:])]
        keys = "".join(
            f"<key rid='{device}'"
            f"{" prekey='true'" if index == 0 else ''}>{self._fake_b64(48)}</key>"
            for index, device in enumerate(recipients)
        )
        full = f"{contact.jid}/{contact.resource}"
        self._emit(
            Direction.IN,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' from='{_attr(full)}' "
            f"to='{_attr(self._full_jid)}' type='chat' id='{message_id}'>"
            f"<encrypted xmlns='{_OMEMO_NS}'>"
            f"<header sid='{sender_device}'>"
            f"{keys}<iv>{self._fake_b64(12)}</iv></header>"
            f"<payload>{self._fake_b64(96)}</payload>"
            "</encrypted>"
            f"<encryption xmlns='urn:xmpp:eme:0' namespace='{_OMEMO_NS}' name='OMEMO'/>"
            "<store xmlns='urn:xmpp:hints'/>"
            "<request xmlns='urn:xmpp:receipts'/>"
            f"<origin-id xmlns='urn:xmpp:sid:0' id='{message_id}'/>"
            f"<stanza-id xmlns='urn:xmpp:sid:0' id='{archive_id}' by='{_attr(self._jid)}'/>"
            "<markable xmlns='urn:xmpp:chat-markers:0'/>"
            "</message>",
            stanza_id=message_id,
            peer=contact.jid,
        )
        self._sm_in += 1
        self._remember_id(archive_id, message_id)
        body = _("push went through, keys delivered to three devices")
        self._add_message(
            Message(
                message_id=message_id,
                conversation=contact.jid,
                sender=contact.jid,
                body=body,
                direction=Direction.IN,
                encryption=Encryption.OMEMO,
                state=DeliveryState.RECEIVED,
                xeps=(
                    self._xep(
                        "0384",
                        "decrypted",
                        direction=Direction.IN,
                        peer=contact.jid,
                        stanza_id=message_id,
                        detail={"devices": str(contact.devices), "trusted": str(contact.devices)},
                        publish=False,
                    ),
                    self._xep(
                        "0359",
                        "stanza-id",
                        direction=Direction.IN,
                        peer=contact.jid,
                        stanza_id=archive_id,
                        publish=False,
                    ),
                ),
            )
        )
        self._trust_on_first_use(contact.jid)
        self._ensure_conversation(contact.jid, encryption=Encryption.OMEMO)
        self._refresh_omemo(contact.jid)
        return message_id

    def _omemo_payload(self, body: str, peer: str) -> str:
        """Шифрованная нагрузка исходящего сообщения.

        Идентификаторы устройств не случайные: ``sid`` - собственное устройство
        сессии, ``rid`` - устройства собеседника из того же списка, который
        показывают PEP-строфа devicelist и вывод /omemo fingerprints. Случайные
        значения давали три разных набора идентификаторов на один и тот же
        набор устройств.
        """
        keys = "".join(
            f"<key rid='{device}'>{self._fake_b64(48)}</key>"
            for device, _ in self._peer_devices(peer)
        )
        return (
            f"<encrypted xmlns='{_OMEMO_NS}'>"
            f"<header sid='{self._own_device}'>"
            f"{keys}<iv>{self._fake_b64(12)}</iv></header>"
            f"<payload>{self._fake_b64(len(body) + 32)}</payload></encrypted>"
            f"<encryption xmlns='urn:xmpp:eme:0' namespace='{_OMEMO_NS}' name='OMEMO'/>"
        )

    async def _play_receipt_out(self, contact: _Contact, message_id: str) -> None:
        """Шаг 10a: исходящий receipt по XEP-0184."""
        receipt_id = self._make_id("rcp")
        full = f"{contact.jid}/{contact.resource}"
        self._emit(
            Direction.OUT,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' to='{_attr(full)}' "
            f"type='chat' id='{receipt_id}'>"
            f"<received xmlns='urn:xmpp:receipts' id='{message_id}'/>"
            "<store xmlns='urn:xmpp:hints'/>"
            "</message>",
            stanza_id=receipt_id,
            peer=contact.jid,
        )
        self._xep(
            "0184", "receipt-sent", direction=Direction.OUT, peer=contact.jid, stanza_id=message_id
        )

    async def _play_receipt_in(self, contact: _Contact, message_id: str) -> None:
        """Шаг 10b: входящий receipt на отправленное сообщение."""
        full = f"{contact.jid}/{contact.resource}"
        self._emit(
            Direction.IN,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' from='{_attr(full)}' "
            f"to='{_attr(self._full_jid)}' type='chat' id='{self._make_id('rcp')}'>"
            f"<received xmlns='urn:xmpp:receipts' id='{message_id}'/>"
            "</message>",
            stanza_id=message_id,
            peer=contact.jid,
        )
        self._sm_in += 1
        message = self._find_message(message_id)
        if message is None:
            return
        event = self._xep(
            "0184",
            "receipt-received",
            direction=Direction.IN,
            peer=contact.jid,
            stanza_id=message_id,
            publish=False,
        )
        self._update_message(message.with_state(DeliveryState.RECEIVED).with_xep(event))

    async def _play_marker_in(self, contact: _Contact, message_id: str) -> None:
        """Шаг 11: маркер displayed по XEP-0333."""
        full = f"{contact.jid}/{contact.resource}"
        self._emit(
            Direction.IN,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' from='{_attr(full)}' "
            f"to='{_attr(self._full_jid)}' type='chat' id='{self._make_id('mrk')}'>"
            f"<displayed xmlns='urn:xmpp:chat-markers:0' id='{message_id}'/>"
            "<store xmlns='urn:xmpp:hints'/>"
            "</message>",
            stanza_id=message_id,
            peer=contact.jid,
        )
        self._sm_in += 1
        message = self._find_message(message_id)
        if message is None:
            return
        event = self._xep(
            "0333",
            "displayed",
            direction=Direction.IN,
            peer=contact.jid,
            stanza_id=message_id,
            publish=False,
        )
        self._update_message(message.with_state(DeliveryState.DISPLAYED).with_xep(event))

    async def _play_correction_out(self, contact: _Contact, message_id: str) -> None:
        """Шаг 12: корректировка отправленного сообщения по XEP-0308.

        Корректировка шифруется так же, как исходное сообщение: иначе текст,
        который в оригинале был закрыт OMEMO, уходит в поток открытым. Ссылка
        идет на origin-id оригинала, как требует XEP-0308.
        """
        message = self._find_message(message_id)
        if message is None:
            return
        corrected = _("restart the archive worker and clear the disco cache")
        full = f"{contact.jid}/{contact.resource}"
        new_id = self._make_id("msg")
        encrypted = message.encryption is Encryption.OMEMO
        payload = (
            self._omemo_payload(corrected, contact.jid)
            if encrypted
            else f"<body>{escape(corrected)}</body>"
        )
        self._emit(
            Direction.OUT,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' to='{_attr(full)}' "
            f"type='chat' id='{new_id}'>"
            f"{payload}"
            f"<replace xmlns='urn:xmpp:message-correct:0' id='{message_id}'/>"
            f"<origin-id xmlns='urn:xmpp:sid:0' id='{new_id}'/>"
            "</message>",
            stanza_id=new_id,
            peer=contact.jid,
        )
        event = self._xep(
            "0308",
            "corrected",
            direction=Direction.OUT,
            peer=contact.jid,
            stanza_id=message_id,
            publish=False,
        )
        # Корректировка не заводит отдельное сообщение: по XEP-0308 она заменяет
        # исходное. Но в панели лога видна именно ее строфа, и /trace по ее
        # идентификатору должен приводить к тому же сообщению.
        self._remember_id(new_id, message_id)
        self._update_message(message.with_correction(corrected).with_xep(event))

    async def _play_error_stanza(self, contact: _Contact) -> None:
        """Шаг 13: ошибочная строфа type='error'."""
        iq_id = self._make_id("disco")
        full = f"{contact.jid}/{contact.resource}"
        self._emit(
            Direction.OUT,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='get' id='{iq_id}' "
            f"to='{_attr(full)}'>"
            "<query xmlns='http://jabber.org/protocol/disco#info'/></iq>",
            stanza_id=iq_id,
            peer=contact.jid,
        )
        self._xep(
            "0030", "info-requested", direction=Direction.OUT, peer=contact.jid, stanza_id=iq_id
        )
        await self._wait(_DELAY_BIND)
        self._emit(
            Direction.IN,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='error' id='{iq_id}' "
            f"from='{_attr(full)}' to='{_attr(self._full_jid)}'>"
            "<query xmlns='http://jabber.org/protocol/disco#info'/>"
            "<error type='cancel' code='503'>"
            "<service-unavailable xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'/>"
            "<text xmlns='urn:ietf:params:xml:ns:xmpp-stanzas' xml:lang='en'>"
            "Recipient resource is not available</text>"
            "</error></iq>",
            stanza_id=iq_id,
            peer=contact.jid,
            is_error=True,
        )
        self._sm_in += 1
        self._notice(
            _("disco to {jid} failed with service-unavailable").format(jid=contact.jid),
            NoticeLevel.ERROR,
        )

    async def _play_muc_join(
        self, room: str = _MUC_ROOM, nick: str = _MUC_NICK, password: str = _MUC_PASSWORD
    ) -> None:
        """Вход в комнату по XEP-0045.

        Пароль комнаты идет в потоке в открытом виде: это второй настоящий
        секрет сценария, и панель обязана заменить его целиком.
        """
        room_nick = f"{room}/{nick}"
        password_element = f"<password>{escape(password)}</password>" if password else ""
        self._emit(
            Direction.OUT,
            StanzaKind.PRESENCE,
            f"<presence xmlns='jabber:client' "
            f"to='{_attr(room_nick)}'>"
            "<x xmlns='http://jabber.org/protocol/muc'>"
            f"<history maxstanzas='20'/>{password_element}</x>"
            f"<c xmlns='http://jabber.org/protocol/caps' hash='sha-1' "
            f"node='{_attr(_CLIENT_NODE)}' ver='{_CLIENT_CAPS_VER}'/>"
            "</presence>",
            peer=room,
        )
        for occupant in _MUC_OCCUPANTS:
            await self._wait(_DELAY_PRESENCE)
            self._emit(
                Direction.IN,
                StanzaKind.PRESENCE,
                f"<presence xmlns='jabber:client' from='{_attr(room)}/{occupant}' "
                f"to='{_attr(self._full_jid)}'>"
                "<x xmlns='http://jabber.org/protocol/muc#user'>"
                "<item affiliation='member' role='participant'/></x>"
                f"<occupant-id xmlns='urn:xmpp:occupant-id:0' id='{self._fake_b64(16)}'/>"
                "</presence>",
                peer=room,
            )
            self._sm_in += 1
        await self._wait(_DELAY_PRESENCE)
        self._emit(
            Direction.IN,
            StanzaKind.PRESENCE,
            f"<presence xmlns='jabber:client' from='{_attr(room_nick)}' "
            f"to='{_attr(self._full_jid)}'>"
            "<x xmlns='http://jabber.org/protocol/muc#user'>"
            f"<item affiliation='member' role='participant' jid='{_attr(self._full_jid)}'/>"
            # 110 - собственное присутствие, 100 - комната не анонимна,
            # 170 - включено журналирование. Кода 210 здесь быть не может:
            # он означает, что служба назначила другой ник.
            "<status code='110'/><status code='100'/><status code='170'/></x></presence>",
            peer=room,
        )
        subject = _("on-call and releases")
        self._emit(
            Direction.IN,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' from='{_attr(room)}' "
            f"to='{_attr(self._full_jid)}' type='groupchat' id='{self._make_id('sub')}'>"
            f"<subject>{escape(subject)}</subject></message>",
            peer=room,
        )
        self._muc_room = room
        self._muc_nick = nick
        conversation = self._ensure_conversation(room, title=room.split("@", 1)[0], is_muc=True)
        self._conversations[room] = replace(
            conversation, topic=subject, show=PresenceShow.AVAILABLE
        )
        self._publish_conversations()
        # Состав комнаты нужен автодополнению ника: список участников знает
        # только сессия.
        self._bus.publish(OccupantsUpdated(room, (*_MUC_OCCUPANTS, nick)))
        self._xep(
            "0045",
            "joined",
            direction=Direction.IN,
            peer=room,
            detail={"nick": nick, "occupants": str(len(_MUC_OCCUPANTS) + 1)},
        )
        self._xep("0045", "subject", direction=Direction.IN, peer=room, detail={"subject": subject})
        self._xep("0421", "occupant-id", direction=Direction.IN, peer=room)
        self._set_active(room)

    # Фоновый и нагрузочный режимы.

    async def _run_background(self) -> None:
        """Фоновый режим: редкие сообщения, presence, chat state, периодический ping."""
        period = 1.0 / self._rate
        next_ping = time.monotonic() + _PING_PERIOD
        while not self._stop.is_set():
            await self._wait(period)
            if not self._online:
                continue
            now = time.monotonic()
            if now >= next_ping:
                next_ping = now + _PING_PERIOD
                await self._ping(self._domain)
                continue
            await self._background_event()

    async def _background_event(self) -> None:
        """Одно событие фонового потока, выбранное по весам."""
        roll = self._rng.random()
        contact = self._rng.choice([c for c in _CONTACTS if c.show is not PresenceShow.OFFLINE])
        if self._scenario == "error" and roll < 0.02:
            await self._stream_failure()
            return
        if self._scenario == "error" and roll < 0.14:
            await self._play_error_stanza(contact)
            return
        # Веса выставлены по живому клиенту: содержательные события преобладают.
        # Состояние набора идет вдвое реже сообщений, а не втрое чаще, и пинга
        # здесь нет вовсе - он живет на своем таймере с периодом _PING_PERIOD.
        if roll < 0.18:
            self._emit_chat_state(contact, self._rng.choice(("composing", "paused", "active")))
        elif roll < 0.30:
            show = self._rng.choice(
                (PresenceShow.AVAILABLE, PresenceShow.AWAY, PresenceShow.CHAT, PresenceShow.DND)
            )
            self._emit_contact_presence(contact, show, _(contact.status))
            self._publish_roster()
        elif roll < 0.38:
            self._emit_sm_ack()
        elif roll < 0.78 or self._muc_room is None:
            await self._incoming_message(contact)
        else:
            self._emit_muc_message()

    def _emit_chat_state(self, contact: _Contact, state: str) -> None:
        """Уведомление о состоянии набора по XEP-0085."""
        full = f"{contact.jid}/{contact.resource}"
        self._emit(
            Direction.IN,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' from='{_attr(full)}' "
            f"to='{_attr(self._full_jid)}' type='chat' id='{self._make_id('cs')}'>"
            f"<{state} xmlns='http://jabber.org/protocol/chatstates'/>"
            "<no-store xmlns='urn:xmpp:hints'/></message>",
            peer=contact.jid,
        )
        self._sm_in += 1
        self._xep("0085", state, direction=Direction.IN, peer=contact.jid)

    def _emit_sm_ack(self) -> None:
        """Запрос и подтверждение потокового менеджмента.

        Счетчик h накопительный: по разделу 4 XEP-0198 это число обработанных
        сервером строф, оно растет по модулю 2^32 и не сбрасывается. Величина
        неподтвержденного считается разницей между отправленным и последним h.
        """
        self._emit(Direction.OUT, StanzaKind.STREAM, "<r xmlns='urn:xmpp:sm:3'/>")
        handled = self._sm_out
        self._emit(
            Direction.IN,
            StanzaKind.STREAM,
            f"<a xmlns='urn:xmpp:sm:3' h='{handled}'/>",
        )
        self._sm_acked = handled
        self._xep("0198", "acked", direction=Direction.IN, detail={"h": str(handled)})

    def _emit_muc_message(self) -> None:
        """Сообщение в комнате по XEP-0045."""
        room = self._muc_room
        if room is None:
            return
        nick = self._rng.choice(_MUC_OCCUPANTS)
        body = _(self._rng.choice(_ARCHIVE_BODIES))
        message_id = self._make_id("muc")
        self._emit(
            Direction.IN,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' from='{_attr(room)}/{nick}' "
            f"to='{_attr(self._full_jid)}' type='groupchat' id='{message_id}'>"
            f"<body>{escape(body)}</body>"
            f"<occupant-id xmlns='urn:xmpp:occupant-id:0' id='{self._fake_b64(16)}'/>"
            f"<stanza-id xmlns='urn:xmpp:sid:0' id='{self._make_id()}' "
            f"by='{_attr(room)}'/></message>",
            stanza_id=message_id,
            peer=room,
        )
        self._sm_in += 1
        self._add_message(
            Message(
                message_id=message_id,
                conversation=room,
                sender=nick,
                body=body,
                direction=Direction.IN,
                state=DeliveryState.RECEIVED,
            )
        )

    def _pick_body(self, jid: str) -> str:
        """Текст входящего сообщения, не повторяющий предыдущий у того же контакта.

        Случайный выбор из короткого набора дает подряд идущие одинаковые строки,
        и лента выглядит как сбой, а не как переписка. Запоминается последняя
        фраза каждого собеседника, повтор берет следующую по кругу.
        """
        body = self._rng.choice(_INCOMING_BODIES)
        if body == self._last_body.get(jid):
            index = (_INCOMING_BODIES.index(body) + 1) % len(_INCOMING_BODIES)
            body = _INCOMING_BODIES[index]
        self._last_body[jid] = body
        return _(body)

    async def _incoming_message(self, contact: _Contact) -> None:
        """Входящее сообщение с запросом receipt и ответом на него."""
        message_id = self._make_id("msg")
        full = f"{contact.jid}/{contact.resource}"
        body = self._pick_body(contact.jid)
        archive_id = self._make_id()
        self._emit(
            Direction.IN,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' from='{_attr(full)}' "
            f"to='{_attr(self._full_jid)}' type='chat' id='{message_id}'>"
            f"<body>{escape(body)}</body>"
            "<request xmlns='urn:xmpp:receipts'/>"
            "<markable xmlns='urn:xmpp:chat-markers:0'/>"
            f"<stanza-id xmlns='urn:xmpp:sid:0' id='{archive_id}' by='{_attr(self._jid)}'/>"
            "</message>",
            stanza_id=message_id,
            peer=contact.jid,
        )
        self._sm_in += 1
        self._remember_id(archive_id, message_id)
        self._add_message(
            Message(
                message_id=message_id,
                conversation=contact.jid,
                sender=contact.jid,
                body=body,
                direction=Direction.IN,
                state=DeliveryState.RECEIVED,
                xeps=(
                    self._xep(
                        "0359",
                        "stanza-id",
                        direction=Direction.IN,
                        peer=contact.jid,
                        stanza_id=archive_id,
                        publish=False,
                    ),
                ),
            )
        )
        await self._wait(0.05)
        await self._play_receipt_out(contact, message_id)

    async def _run_stress(self) -> None:
        """Нагрузочный режим: поток строф строго по расписанию.

        При частоте выше сотни строф в секунду отдельный ``asyncio.sleep`` на
        строфу стоит дороже самой строфы, поэтому расписание держится по тикам
        не короче ``_MIN_TICK``, а внутри тика отдается пачка. Средняя частота
        при этом равна заданной.
        """
        period = 1.0 / self._rate
        batch = 1
        if period < _MIN_TICK:
            batch = max(1, math.ceil(_MIN_TICK * self._rate))
            period = batch / self._rate
        next_at = time.monotonic()
        while not self._stop.is_set():
            for _step in range(batch):
                self._emit_filler()
            next_at += period
            delay = next_at - time.monotonic()
            if delay > 0:
                await self._wait(delay)
            elif delay < -period:
                # Расписание не догнать: долг не копится, отсчет начинается заново.
                next_at = time.monotonic()
                await self._wait(0)

    def _emit_filler(self) -> None:
        """Одна строфа нагрузочного потока.

        Строфы нагрузки не попадают в хранилище сообщений: проверяется пропускная
        способность панели лога, а не объем беседы. Раз в сто строф добавляется
        настоящее сообщение, чтобы панель беседы тоже обновлялась.
        """
        index = self._filler_index
        self._filler_index += 1
        contact = _CONTACTS[index % len(_CONTACTS)]
        full = f"{contact.jid}/{contact.resource}"
        variant = index % 5
        if variant == 0:
            archive_id = self._make_id()
            body = _(_ARCHIVE_BODIES[index % len(_ARCHIVE_BODIES)])
            self._emit(
                Direction.IN,
                StanzaKind.MESSAGE,
                f"<message xmlns='jabber:client' from='{_attr(self._jid)}' "
                f"to='{_attr(self._full_jid)}' id='{self._make_id()}'>"
                f"<result xmlns='urn:xmpp:mam:2' queryid='stress' id='{archive_id}'>"
                "<forwarded xmlns='urn:xmpp:forward:0'>"
                f"<delay xmlns='urn:xmpp:delay' stamp='{_stamp(time.time())}'/>"
                f"<message xmlns='jabber:client' from='{_attr(full)}' "
                f"to='{_attr(self._jid)}' type='chat' id='{self._make_id('msg')}'>"
                f"<body>{escape(body)}</body></message>"
                "</forwarded></result></message>",
                stanza_id=archive_id,
                peer=contact.jid,
            )
        elif variant == 1:
            self._emit(
                Direction.IN,
                StanzaKind.PRESENCE,
                f"<presence xmlns='jabber:client' from='{_attr(full)}' "
                f"to='{_attr(self._full_jid)}'><priority>8</priority>"
                f"<c xmlns='http://jabber.org/protocol/caps' hash='sha-1' "
                f"node='{_attr(contact.caps_node)}' ver='{contact.caps_ver}'/></presence>",
                peer=contact.jid,
            )
        elif variant == 2:
            self._emit(
                Direction.IN,
                StanzaKind.MESSAGE,
                f"<message xmlns='jabber:client' from='{_attr(full)}' "
                f"to='{_attr(self._full_jid)}' type='chat' id='{self._make_id('cs')}'>"
                "<composing xmlns='http://jabber.org/protocol/chatstates'/>"
                "<no-store xmlns='urn:xmpp:hints'/></message>",
                peer=contact.jid,
            )
        elif variant == 3:
            iq_id = self._make_id("ping")
            self._filler_ping = True
            self._emit(
                Direction.OUT,
                StanzaKind.IQ,
                f"<iq xmlns='jabber:client' type='get' id='{iq_id}' "
                f"to='{_attr(self._domain)}'>"
                "<ping xmlns='urn:xmpp:ping'/></iq>",
                stanza_id=iq_id,
            )
        else:
            iq_id = self._make_id("ping")
            self._emit(
                Direction.IN,
                StanzaKind.IQ,
                f"<iq xmlns='jabber:client' type='result' id='{iq_id}' "
                f"from='{_attr(self._domain)}' to='{_attr(self._full_jid)}'/>",
                stanza_id=iq_id,
            )
            self._sm_in += 1
            if self._filler_ping:
                # Пинг нагрузочного потока тоже дает замер, иначе на экране видны
                # строфы пинга и latency n/a одновременно. Берется эмулированная
                # задержка сети: строфы нагрузки идут пачкой в одном тике, и
                # измеренное по часам время к сети отношения не имеет.
                self._latency.append(self._rng.uniform(_RTT_MIN, _RTT_MAX) * 1000.0)
                self._filler_ping = False
        if index % _STRESS_ACK_EVERY == 0:
            # Живой сервер подтверждает поток, иначе очередь неподтвержденных
            # растет линейно до сотен, чего на настоящем соединении не бывает.
            self._emit_sm_ack()
        if index % 100 == 0:
            body = _(_ARCHIVE_BODIES[(index // 100) % len(_ARCHIVE_BODIES)])
            self._add_message(
                Message(
                    message_id=self._make_id("msg"),
                    conversation=contact.jid,
                    sender=contact.jid,
                    body=body,
                    direction=Direction.IN,
                    state=DeliveryState.RECEIVED,
                )
            )

    # Действия, вызываемые командами.

    async def _ping(self, target: str) -> float:
        """Цикл XEP-0199 с измерением RTT. Возвращает задержку в миллисекундах."""
        iq_id = self._make_id("ping")
        self._emit(
            Direction.OUT,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='get' id='{iq_id}' "
            f"to='{_attr(target)}'>"
            "<ping xmlns='urn:xmpp:ping'/></iq>",
            stanza_id=iq_id,
            peer=target,
        )
        self._xep("0199", "ping-sent", direction=Direction.OUT, peer=target, stanza_id=iq_id)
        started = time.monotonic()
        await self._wait(self._rng.uniform(_RTT_MIN, _RTT_MAX))
        rtt = (time.monotonic() - started) * 1000.0
        self._emit(
            Direction.IN,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='result' id='{iq_id}' "
            f"from='{_attr(target)}' to='{_attr(self._full_jid)}'/>",
            stanza_id=iq_id,
            peer=target,
        )
        self._sm_in += 1
        self._latency.append(rtt)
        self._xep(
            "0199",
            "pong",
            direction=Direction.IN,
            peer=target,
            stanza_id=iq_id,
            detail={"rtt": f"{rtt:.0f}ms"},
        )
        return rtt

    async def _send_text(self, conversation: str | None, text: str) -> str | None:
        """Отправить текст в беседу и через задержку получить receipt."""
        body = text.strip()
        if not body:
            self._feedback(_("empty message not sent"), ok=False)
            return None
        target = conversation or self._active
        if target is None:
            self._feedback(_("no active conversation, select one with /chat <jid>"), ok=False)
            return None
        conversation_item = self._ensure_conversation(target)
        # Идентификатор строфы и origin-id совпадают: так делают Conversations и
        # Dino, и тогда ссылка из корректировки однозначна.
        message_id = self._make_id("msg")
        origin_id = message_id
        if conversation_item.is_muc:
            stanza_type = "groupchat"
            recipient = target
        else:
            stanza_type = "chat"
            contact = self._contact_by_jid(target)
            recipient = f"{target}/{contact.resource}" if contact else target
        encrypted = conversation_item.encryption is Encryption.OMEMO
        payload = self._omemo_payload(body, target) if encrypted else f"<body>{escape(body)}</body>"
        self._emit(
            Direction.OUT,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' "
            f"to='{_attr(recipient)}' type='{stanza_type}' id='{message_id}'>"
            f"{payload}"
            "<request xmlns='urn:xmpp:receipts'/>"
            "<markable xmlns='urn:xmpp:chat-markers:0'/>"
            f"<origin-id xmlns='urn:xmpp:sid:0' id='{origin_id}'/>"
            "</message>",
            stanza_id=message_id,
            peer=target,
        )
        self._remember_id(origin_id, message_id)
        message = self._add_message(
            Message(
                message_id=message_id,
                conversation=target,
                sender=self._full_jid,
                body=body,
                direction=Direction.OUT,
                encryption=Encryption.OMEMO if encrypted else Encryption.PLAIN,
                state=DeliveryState.SENT,
                xeps=(
                    self._xep(
                        "0359",
                        "origin-id",
                        direction=Direction.OUT,
                        peer=target,
                        stanza_id=origin_id,
                        publish=False,
                    ),
                    self._xep(
                        "0184",
                        "receipt-requested",
                        direction=Direction.OUT,
                        peer=target,
                        stanza_id=message_id,
                        publish=False,
                    ),
                ),
            )
        )
        self._spawn(self._confirm_outgoing(message, target))
        return message_id

    async def _confirm_outgoing(self, message: Message, target: str) -> None:
        """Подтверждение отправленного сообщения: ack потока и receipt."""
        try:
            await self._wait(self._rng.uniform(0.15, 0.4))
            self._emit_sm_ack()
            current = self._find_message(message.message_id)
            if current is None:
                return
            event = self._xep(
                "0198",
                "acked",
                direction=Direction.IN,
                peer=target,
                stanza_id=message.message_id,
                publish=False,
            )
            current = self._update_message(current.with_state(DeliveryState.ACKED).with_xep(event))
            contact = self._contact_by_jid(target)
            if contact is None:
                return
            await self._wait(self._rng.uniform(0.2, 0.6))
            await self._play_receipt_in(contact, message.message_id)
        except _SessionStoppedError:
            return

    def _contact_by_jid(self, jid: str) -> _Contact | None:
        """Контакт по голому JID."""
        bare = jid.split("/", 1)[0]
        for contact in _CONTACTS:
            if contact.jid == bare:
                return contact
        return None

    def _fingerprint(self, jid: str) -> str:
        """Устойчивый отпечаток OMEMO для показа в командах."""
        digest = hashlib.sha256(jid.encode("utf-8")).hexdigest()[:32]
        return " ".join(digest[index : index + 8] for index in range(0, 32, 8))

    async def _reconnect(self, *, resume: bool) -> None:
        """Переподключение: возобновление потока или полный сценарий."""
        try:
            self._online = False
            if resume and self._sm_id is not None:
                # Возобновление идет по новому каналу: сначала TLS и открытие
                # потока, только потом <resume/>. Без этого после обрыва в
                # статус-баре оставались TLS n/a и транспорт n/a при живом потоке.
                await self._play_tls()
                await self._play_stream_open()
                await self._play_resume()
            else:
                self._reconnects += 1
                await self._play_connect()
        except _SessionStoppedError:
            return

    async def _stream_failure(self) -> None:
        """Обрыв потока с ошибкой и восстановление по XEP-0198.

        Нужен, чтобы красный индикатор ошибки и стадия SM resume вообще были
        достижимы: без обрыва ветка ERROR не срабатывает ни в одном сценарии.
        """
        self._emit(
            Direction.IN,
            StanzaKind.STREAM,
            "<stream:error xmlns:stream='http://etherx.jabber.org/streams'>"
            "<connection-timeout xmlns='urn:ietf:params:xml:ns:xmpp-streams'/>"
            "</stream:error>",
            is_error=True,
        )
        self._disconnect(_("stream closed by the server"), failed=True)
        await self._wait(_DELAY_ACTION)
        await self._reconnect(resume=True)

    def _disconnect(self, reason: str, *, failed: bool = False) -> bool:
        """Закрыть поток и перевести состояние в offline.

        Возвращает признак того, что поток действительно был закрыт: повторная
        команда не должна отвечать ложным подтверждением.
        """
        if not self._online:
            return False
        self._emit(
            Direction.OUT,
            StanzaKind.PRESENCE,
            "<presence xmlns='jabber:client' type='unavailable'/>",
        )
        self._emit(Direction.OUT, StanzaKind.STREAM, "</stream:stream>")
        self._online = False
        # Метрики закрытого потока обнуляются: latency от прошлого соединения
        # показывалась бы как текущая. Недоступная метрика выглядит как n/a,
        # а не как последнее известное значение.
        #
        # Счетчики XEP-0198 при этом сохраняются: закрытый поток и так отдает
        # SM n/a, потому что SmInfo.enabled считается по признаку online, а
        # возобновление обязано сообщить серверу фактическое число обработанных
        # строф в <resume h='...'/>. Полное подключение обнуляет их само, в
        # _play_sm_enable: там начинается новый поток.
        self._latency.clear()
        stage = ConnectionStage.ERROR if failed else ConnectionStage.OFFLINE
        self._state = replace(
            self._state,
            stage=stage,
            presence_show=PresenceShow.OFFLINE,
            transport=Transport.NONE,
            tls=TlsInfo(),
            sm=SmInfo(),
            metrics=replace(self._state.metrics, latency_ms=None),
        )
        self._bus.publish(ConnectionStageChanged(stage, reason))
        self._publish_state()
        return True

    # Обработчик команд.

    async def handle_command(self, command: Command) -> None:
        """Исполнить команду шины. Назначается через ``bus.set_command_handler``."""
        try:
            await self._dispatch(command)
        except _SessionStoppedError:
            return

    async def _dispatch(self, command: Command) -> None:
        """Разбор команды по типу."""
        match command:
            case RunCommandLine(line=line):
                await self._run_command_line(line)
            case SendText(conversation=conversation, text=text):
                await self._send_text(conversation or self._active, text)
            case PingServer(target=target):
                rtt = await self._ping(target or self._domain)
                self._feedback(
                    _("pong from {target}: {rtt:.0f}ms").format(
                        target=target or self._domain, rtt=rtt
                    )
                )
            case RequestMam(jid=jid, limit=limit, before=before):
                self._spawn(self._mam_task(jid, limit, before))
            case RequestDisco(jid=jid, node=node):
                await self._disco(jid, node)
            case SetPresence(show=show, status=status):
                self._set_presence(show, status)
            case SetChatState(conversation=conversation, state=state):
                # Эмулятор состояние набора никуда не шлет, но показывает его
                # событием расширения: метка беседы в обоих режимах одна.
                self._xep("0085", state, direction=Direction.OUT, peer=conversation)
            case Connect():
                if self._online:
                    self._feedback(_("session already connected"), ok=False)
                else:
                    self._feedback(_("connection started"))
                    self._spawn(self._reconnect(resume=False))
            case Reconnect():
                self._feedback(_("reconnecting with an attempt to resume the stream"))
                self._disconnect(_("reconnecting"))
                self._spawn(self._reconnect(resume=True))
            case Disconnect():
                if self._disconnect(_("by command")):
                    self._feedback(_("session disconnected"))
                else:
                    self._feedback(_("no connection"), ok=False)
            case OpenConversation(jid=jid):
                self._open_conversation(jid)
            case CloseConversation(jid=jid):
                self._close_conversation(jid)
            case SetOmemoEnabled(conversation=conversation, enabled=enabled):
                self._set_omemo(conversation, enabled=enabled)
            case SetXmlMode(mode=mode):
                self._bus.publish(XmlLogModeChanged(mode))
                self._feedback(_("log panel mode: {mode}").format(mode=mode.value))
            case SetXmlFilter():
                # Ответ печатает панель лога через слой интерфейса: она знает,
                # сколько строф подошло под фильтр. Второй текст здесь дал бы
                # две строки на одно действие.
                pass
            case SetUnsafeXml(enabled=enabled):
                self._set_unsafe(enabled=enabled)
            case ClearXmlLog():
                self._feedback(_("log panel cleared"))
            case SendRawXml(xml=xml):
                self._send_raw(xml)
            case Quit():
                self._feedback(_("shutting down"))
                self.stop()
            case _:
                self._feedback(
                    _("command {name} is not supported by the mock").format(
                        name=type(command).__name__
                    ),
                    ok=False,
                )

    async def _mam_task(self, jid: str, limit: int, before: str = "") -> None:
        """Запрос архива отдельной задачей, чтобы не держать очередь команд."""
        try:
            await self._play_mam(jid, max(0, min(limit, _MAX_MAM_BATCH)), before)
            self._state = replace(self._state, stage=ConnectionStage.READY)
        except _SessionStoppedError:
            return

    def _set_presence(self, show: PresenceShow, status: str) -> None:
        """Сменить собственное присутствие."""
        if show is PresenceShow.OFFLINE:
            # presence offline закрывает поток. Молча это делать нельзя: команда
            # выглядит как смена значения show, а разрывает соединение.
            closed = self._disconnect("presence offline")
            self._feedback(
                _("presence offline: stream closed, connection dropped")
                if closed
                else _("no connection"),
                ok=closed,
            )
            return
        show_element = "" if show is PresenceShow.AVAILABLE else f"<show>{show.value}</show>"
        status_element = f"<status>{escape(status)}</status>" if status else ""
        self._emit(
            Direction.OUT,
            StanzaKind.PRESENCE,
            f"<presence xmlns='jabber:client'>"
            f"{show_element}{status_element}<priority>1</priority>"
            f"<c xmlns='http://jabber.org/protocol/caps' hash='sha-1' "
            f"node='{_attr(_CLIENT_NODE)}' ver='{_CLIENT_CAPS_VER}'/></presence>",
        )
        self._state = replace(self._state, presence_show=show)
        suffix = f" {status}" if status else ""
        self._feedback(_("presence: {show}").format(show=show.value) + suffix)

    def _open_conversation(self, jid: str) -> None:
        """Открыть беседу и сделать ее активной."""
        bare = jid.split("/", 1)[0]
        if "@" not in bare:
            self._feedback(_("invalid JID: {jid}").format(jid=jid), ok=False)
            return
        self._ensure_conversation(bare)
        self._set_active(bare)
        self._feedback(_("conversation {jid} opened").format(jid=bare))

    def _close_conversation(self, jid: str | None) -> None:
        """Закрыть беседу. ``None`` закрывает активную."""
        target = jid or self._active
        if target is None or target not in self._conversations:
            self._feedback(_("nothing to close"), ok=False)
            return
        del self._conversations[target]
        self._publish_conversations()
        if self._active == target:
            self._set_active(next(iter(self._conversations), None))
        self._feedback(_("conversation {jid} closed").format(jid=target))

    def _set_omemo(self, conversation: str | None, *, enabled: bool) -> None:
        """Включить или выключить OMEMO для беседы."""
        target = conversation or self._active
        if target is None:
            self._feedback(_("no active conversation"), ok=False)
            return
        devices = len(self._peer_devices(target))
        if enabled:
            self._trust_on_first_use(target)
        self._ensure_conversation(
            target, encryption=Encryption.OMEMO if enabled else Encryption.PLAIN
        )
        self._refresh_omemo(target)
        self._xep(
            "0384",
            "session-built" if enabled else "device-untrusted",
            peer=target,
            detail={"devices": str(devices)},
        )
        state = _("OMEMO for {jid}: enabled") if enabled else _("OMEMO for {jid}: disabled")
        self._feedback(state.format(jid=target))

    def _set_unsafe(self, *, enabled: bool) -> None:
        """Переключить показ сырого XML без маскирования.

        Предупреждение и ответ печатает слой интерфейса: он ведет подтверждение
        вводом. Сессия только хранит флаг и публикует событие.
        """
        self._state = replace(self._state, unsafe_xml=enabled)
        self._bus.publish(UnsafeModeChanged(enabled))

    def _send_raw(self, xml: str) -> None:
        """Отправить произвольную строфу как есть."""
        payload = xml.strip()
        problem = router.validate_stanza(payload)
        if problem is not None:
            self._feedback(f"{problem}: /send <raw-xml>", ok=False)
            return
        kind = StanzaKind.OTHER
        for candidate in (StanzaKind.MESSAGE, StanzaKind.PRESENCE, StanzaKind.IQ):
            if payload.startswith(f"<{candidate.value}"):
                kind = candidate
                break
        self._emit(Direction.OUT, kind, payload)
        self._feedback(_("stanza sent, {size} bytes").format(size=len(payload.encode("utf-8"))))

    # Разбор слэш-команд.

    async def _run_command_line(self, line: str) -> None:
        """Разобрать строку из поля ввода и выполнить команду.

        Разбор лежит на реестре ``core.commands``, исполнение - на общем роутере
        ``core.router``. Второй экземпляр роутера здесь означал бы, что новая
        команда пишется дважды и расходится по формулировкам уже на второй.
        """
        raw = line.strip()
        if not raw:
            return
        parsed = commands.parse(raw)
        if parsed is None:
            # Не команда: двойной слэш экранирует текст, один слэш снимается.
            await self._send_text(self._active, raw[1:] if raw.startswith("//") else raw)
            return
        if parsed.error is not None:
            self._feedback(parsed.error, ok=False)
            return
        await router.route(self, parsed)

    async def connect(self) -> None:
        """Операция роутера: /connect."""
        await self._dispatch(Connect())

    async def disconnect(self) -> None:
        """Операция роутера: /disconnect."""
        await self._dispatch(Disconnect())

    async def reconnect(self) -> None:
        """Операция роутера: /reconnect."""
        await self._dispatch(Reconnect())

    def show_account(self) -> None:
        """Операция роутера: /account."""
        tls = self._state.tls
        self._feedback(
            _(
                "{jid}\nstage: {stage}\ntransport: {transport}\n"
                "channel: {version} {cipher} {binding}"
            ).format(
                jid=self._full_jid,
                stage=stage_label(self._state.stage),
                transport=transport_label(self._state.transport),
                version=tls.version or "n/a",
                cipher=tls.cipher or "",
                binding=tls.channel_binding or "",
            )
        )

    def set_presence(self, show: PresenceShow, status: str) -> None:
        """Операция роутера: /presence."""
        self._set_presence(show, status)

    def stop_session(self) -> None:
        """Операция роутера: /quit."""
        self.stop()

    def open_conversation(self, jid: str) -> None:
        """Операция роутера: /chat."""
        self._open_conversation(jid)

    def close_conversation(self, jid: str | None) -> None:
        """Операция роутера: /close."""
        self._close_conversation(jid)

    async def join_room(self, room: str, nick: str) -> None:
        """Операция роутера: /join."""
        await self._join_room(room, nick)

    def leave_room(self) -> None:
        """Операция роутера: /leave."""
        self._leave_room()

    def set_topic(self, subject: str) -> None:
        """Операция роутера: /topic."""
        self._topic(subject)

    def set_nick(self, nick: str) -> None:
        """Операция роутера: /nick."""
        self._change_nick(nick or self._muc_nick)

    async def reply_to(self, message_id: str, text: str) -> None:
        """Операция роутера: /reply."""
        target = self._find_message(message_id)
        if target is None:
            self._feedback(
                _("message {message_id} is not in the conversation feed").format(
                    message_id=message_id
                ),
                ok=False,
            )
            return
        if not text:
            self._feedback(_("reply is empty: /reply <id> <text>"), ok=False)
            return
        sent = await self._send_text(target.conversation, text)
        if sent is None:
            return
        reply = self._find_message(sent)
        if reply is not None:
            self._update_message(
                reply.with_xep(
                    self._xep(
                        "0461",
                        "reply",
                        direction=Direction.OUT,
                        peer=target.conversation,
                        stanza_id=target.message_id,
                        publish=False,
                    )
                )
            )

    async def react_to(self, message_id: str, emoji: Sequence[str]) -> None:
        """Операция роутера: /react.

        Набор реакций передается целиком и заменяет прежний, пустой снимает их.
        Строфа уходит без тела - так и выглядит XEP-0444 в потоке.
        """
        target = self._find_message(message_id)
        if target is None:
            self._feedback(
                _("message {message_id} is not in the conversation feed").format(
                    message_id=message_id
                ),
                ok=False,
            )
            return
        marks = " ".join(sorted(emoji))
        items = "".join(f"<reaction>{escape(value)}</reaction>" for value in sorted(emoji))
        stanza_id = self._make_id("react")
        self._emit(
            Direction.OUT,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' to='{_attr(target.conversation)}' "
            f"type='chat' id='{stanza_id}'>"
            f"<reactions xmlns='urn:xmpp:reactions:0' id='{_attr(target.message_id)}'>"
            f"{items}</reactions></message>",
            stanza_id=stanza_id,
            peer=target.conversation,
        )
        event = self._xep(
            "0444",
            "reaction",
            direction=Direction.OUT,
            peer=self._full_jid,
            stanza_id=target.message_id,
            detail={"emoji": marks},
        )
        self._update_message(router.replace_mark(target, event, drop=not marks))
        self._feedback(
            _("reactions updated: {marks}").format(marks=marks)
            if marks
            else _("reactions updated: removed")
        )

    async def retract(self, message_id: str) -> None:
        """Операция роутера: /retract."""
        target = self._find_message(message_id)
        if target is None:
            self._feedback(
                _("message {message_id} is not in the conversation feed").format(
                    message_id=message_id
                ),
                ok=False,
            )
            return
        if target.direction is not Direction.OUT:
            self._feedback(_("only your own message can be retracted"), ok=False)
            return
        stanza_id = self._make_id("retract")
        self._emit(
            Direction.OUT,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' to='{_attr(target.conversation)}' "
            f"type='chat' id='{stanza_id}'>"
            f"<retract xmlns='urn:xmpp:message-retract:1' id='{_attr(target.message_id)}'/>"
            "</message>",
            stanza_id=stanza_id,
            peer=target.conversation,
        )
        event = self._xep(
            "0424",
            "retracted",
            direction=Direction.OUT,
            peer=self._full_jid,
            stanza_id=target.message_id,
        )
        self._update_message(replace(target, body=_(router.RETRACTED_BODY)).with_xep(event))
        self._feedback(_("message {message_id} retracted").format(message_id=target.message_id))

    async def _join_room(self, room: str, nick: str) -> None:
        """Вход в комнату по команде.

        Пароль комнаты фиксированный: сценарий показывает, как он выглядит
        в потоке и как его маскирует панель.
        """
        if "@" not in room:
            self._feedback(_("invalid room JID: {room}").format(room=room), ok=False)
            return
        await self._play_muc_join(room, nick, _MUC_PASSWORD)
        self._feedback(_("joined {room} as {nick}").format(room=room, nick=nick))

    def _leave_room(self) -> None:
        """Выход из активной комнаты."""
        target = self._active
        if target is None or not self._conversations.get(target, Conversation(jid="")).is_muc:
            self._feedback(_("the active conversation is not a room"), ok=False)
            return
        self._emit(
            Direction.OUT,
            StanzaKind.PRESENCE,
            f"<presence xmlns='jabber:client' "
            f"to='{_attr(target)}/{_attr(self._muc_nick)}' type='unavailable'/>",
            peer=target,
        )
        self._muc_room = None
        self._xep("0045", "left", direction=Direction.OUT, peer=target)
        self._close_conversation(target)

    def _topic(self, subject: str) -> None:
        """Показать или сменить тему комнаты.

        Без аргумента команда печатает текущую тему и ничего не отправляет: по
        реестру она умеет и то, и другое, а пустая строфа subject стирала тему.
        """
        target = self._active
        conversation = self._conversations.get(target or "")
        if target is None or conversation is None or not conversation.is_muc:
            self._feedback(_("the topic can only be changed in a room"), ok=False)
            return
        if not subject:
            self._feedback(
                _("room topic: {topic}").format(topic=conversation.topic)
                if conversation.topic
                else _("room topic: not set")
            )
            return
        self._emit(
            Direction.OUT,
            StanzaKind.MESSAGE,
            f"<message xmlns='jabber:client' "
            f"to='{_attr(target)}' type='groupchat' id='{self._make_id('sub')}'>"
            f"<subject>{escape(subject)}</subject></message>",
            peer=target,
        )
        self._xep(
            "0045", "subject", direction=Direction.OUT, peer=target, detail={"subject": subject}
        )
        self._conversations[target] = replace(conversation, topic=subject)
        self._publish_conversations()
        self._feedback(_("room topic: {topic}").format(topic=subject))

    def _change_nick(self, nick: str) -> None:
        """Смена ника в комнате."""
        target = self._active
        if target is None or not self._conversations.get(target, Conversation(jid="")).is_muc:
            self._feedback(_("the nick can only be changed in a room"), ok=False)
            return
        self._emit(
            Direction.OUT,
            StanzaKind.PRESENCE,
            f"<presence xmlns='jabber:client' to='{_attr(target)}/{_attr(nick)}'/>",
            peer=target,
        )
        self._muc_nick = nick
        self._xep(
            "0045", "nick-changed", direction=Direction.OUT, peer=target, detail={"nick": nick}
        )
        self._feedback(_("room nick: {nick}").format(nick=nick))

    def show_roster(self, *, raw: bool, groups: bool) -> None:
        """Операция роутера: /roster."""
        self._feedback(self._format_roster(raw=raw, groups=groups))

    async def roster_add(self, jid: str) -> None:
        """Операция роутера: /add."""
        self._roster_write(jid, subscription="none")

    async def roster_remove(self, jid: str) -> None:
        """Операция роутера: /remove."""
        if jid not in self._roster:
            self._feedback(_("contact {jid} is not in the roster").format(jid=jid), ok=False)
            return
        self._roster_write(jid, subscription="remove")

    def _roster_write(self, jid: str, *, subscription: str) -> None:
        """Строфа правки контакт-листа вместе с локальным изменением."""
        iq_id = self._make_id("set")
        self._emit(
            Direction.OUT,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='set' id='{iq_id}'>"
            f"<query xmlns='jabber:iq:roster'>"
            f"<item jid='{_attr(jid)}' subscription='{subscription}'/>"
            "</query></iq>",
            stanza_id=iq_id,
            peer=jid,
        )
        if subscription == "remove":
            self._roster.pop(jid, None)
        else:
            self._roster[jid] = RosterItem(jid=jid, subscription="none")
        self._publish_roster()
        done = (
            _("contact {jid}: removed") if subscription == "remove" else _("contact {jid}: added")
        )
        self._feedback(done.format(jid=jid))

    async def subscribe(self, jid: str) -> None:
        """Операция роутера: /sub."""
        self._send_subscription(jid, "subscribe")

    async def unsubscribe(self, jid: str) -> None:
        """Операция роутера: /unsub."""
        self._send_subscription(jid, "unsubscribe")

    def _send_subscription(self, jid: str, stanza_type: str) -> None:
        """Строфа присутствия для подписки и отписки."""
        self._emit(
            Direction.OUT,
            StanzaKind.PRESENCE,
            f"<presence xmlns='jabber:client' to='{_attr(jid)}' type='{stanza_type}'/>",
            peer=jid,
        )
        self._feedback(_("presence {type} sent to {jid}").format(type=stanza_type, jid=jid))

    async def set_blocked(self, jid: str, *, blocked: bool) -> None:
        """Операция роутера: /block и /unblock."""
        if not blocked and jid not in self._blocked:
            self._feedback(_("{jid} is not blocked").format(jid=jid), ok=False)
            return
        iq_id = self._make_id("set")
        element = "block" if blocked else "unblock"
        self._emit(
            Direction.OUT,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='set' id='{iq_id}'>"
            f"<{element} xmlns='urn:xmpp:blocking'>"
            f"<item jid='{_attr(jid)}'/></{element}></iq>",
            stanza_id=iq_id,
            peer=jid,
        )
        self._xep("0191", "blocked" if blocked else "unblocked", direction=Direction.OUT, peer=jid)
        if blocked:
            self._blocked.add(jid)
        else:
            self._blocked.discard(jid)
        done = _("{jid}: blocked") if blocked else _("{jid}: unblocked")
        self._feedback(done.format(jid=jid))

    def _format_roster(self, *, raw: bool, groups: bool) -> str:
        """Текстовое представление контакт-листа."""
        if not self._roster:
            self._request_roster_again()
            return _("roster is empty, requested again")
        if raw:
            items = "".join(
                f"<item jid='{_attr(item.jid)}' name='{_attr(item.name)}' "
                f"subscription='{item.subscription}'/>"
                for item in self._roster.values()
            )
            return f"<query xmlns='jabber:iq:roster'>{items}</query>"
        if groups:
            buckets: dict[str, list[RosterItem]] = {}
            for item in self._roster.values():
                for group in item.groups or (_("no group"),):
                    buckets.setdefault(group, []).append(item)
            lines = []
            for group in sorted(buckets):
                lines.append(f"{group}:")
                lines.extend(
                    f"  {'+' if entry.online else '-'} {entry.display_name} <{entry.jid}>"
                    for entry in buckets[group]
                )
            return "\n".join(lines)
        return "\n".join(
            f"{'+' if item.online else '-'} {item.display_name} <{item.jid}> "
            f"{item.show.value}{' ' + item.status if item.status else ''}"
            for item in self._roster.values()
        )

    def _request_roster_again(self) -> None:
        """Повторный запрос контакт-листа без ожидания ответа."""
        iq_id = self._make_id("roster")
        self._emit(
            Direction.OUT,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='get' id='{iq_id}'>"
            "<query xmlns='jabber:iq:roster'/></iq>",
            stanza_id=iq_id,
        )

    async def ping(self, target: str) -> float | None:
        """Операция роутера: /ping."""
        return await self._ping(target)

    async def disco(self, jid: str, node: str) -> None:
        """Операция роутера: /disco."""
        await self._disco(jid, node or None)

    async def show_caps(self, jid: str) -> None:
        """Операция роутера: /caps."""
        self._show_caps(jid)

    def show_features(self) -> None:
        """Операция роутера: /features."""
        self._show_features()

    def show_sm(self) -> None:
        """Операция роутера: /sm."""
        self._show_sm()

    def show_tls(self) -> None:
        """Операция роутера: /tls."""
        self._show_tls()

    def show_trace(self, stanza_id: str) -> None:
        """Операция роутера: /trace."""
        self._trace(stanza_id)

    def _show_tls(self) -> None:
        """Параметры защиты канала вместе с цепочкой сертификатов."""
        tls = self._state.tls
        chain = " <- ".join(tls.chain) if tls.chain else NOT_AVAILABLE
        self._table(
            "TLS",
            (
                (_("transport"), transport_label(self._state.transport)),
                (_("version"), tls.version or NOT_AVAILABLE),
                (_("cipher"), tls.cipher or NOT_AVAILABLE),
                ("channel binding", tls.channel_binding or NOT_AVAILABLE),
                (_("certificate verified"), self._yes_no(tls.valid)),
                (_("chain"), chain),
            ),
        )

    def _show_sm(self) -> None:
        """Состояние потокового менеджмента: счетчики и размер очереди."""
        sm = self._state.sm
        self._table(
            xep_title("0198"),
            (
                (_("enabled"), self._yes_no(sm.enabled)),
                (_("resumed"), self._yes_no(sm.resumed)),
                (_("identifier"), self._sm_id or NOT_AVAILABLE),
                (_("stanzas sent"), str(self._sm_out)),
                (_("acked by server"), str(self._sm_acked)),
                (_("unacked queue"), str(sm.outbound_unacked)),
                (_("inbound handled"), str(sm.inbound_handled)),
            ),
        )

    async def fetch_mam(self, jid: str, limit: int, before: str) -> None:
        """Операция роутера: /mam. Границы значения проверяет разбор команды."""
        await self._dispatch(RequestMam(jid, limit, before))
        if before:
            text = _("archive {jid} requested, message limit {limit}, before stanza {before}")
        else:
            text = _("archive {jid} requested, message limit {limit}")
        self._feedback(text.format(jid=jid, limit=limit, before=before))

    async def send_iq(self, target: str, namespace: str, kind: str) -> None:
        """Операция роутера: /iq. Собрать строфу, отправить и дождаться ответа.

        Ответ показывается так же, как у /disco и /upload: известное пространство
        имен дает result, неизвестное - error item-not-found. Команда, которая
        только отправляет строфу и молчит, для отладки бесполезна.
        """
        iq_type = kind
        iq_id = self._make_id("iq")
        self._emit(
            Direction.OUT,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='{iq_type}' id='{iq_id}' to='{_attr(target)}'>"
            f"<query xmlns='{_attr(namespace)}'/></iq>",
            stanza_id=iq_id,
            peer=target,
        )
        started = time.monotonic()
        await self._wait(self._rng.uniform(_RTT_MIN, _RTT_MAX))
        known = namespace in _KNOWN_NAMESPACES
        if known:
            self._emit(
                Direction.IN,
                StanzaKind.IQ,
                f"<iq xmlns='jabber:client' type='result' id='{iq_id}' "
                f"from='{_attr(target)}' to='{_attr(self._full_jid)}'>"
                f"<query xmlns='{_attr(namespace)}'/></iq>",
                stanza_id=iq_id,
                peer=target,
            )
        else:
            self._emit(
                Direction.IN,
                StanzaKind.IQ,
                f"<iq xmlns='jabber:client' type='error' id='{iq_id}' "
                f"from='{_attr(target)}' to='{_attr(self._full_jid)}'>"
                f"<query xmlns='{_attr(namespace)}'/>"
                "<error type='cancel'>"
                "<service-unavailable xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'/>"
                "</error></iq>",
                stanza_id=iq_id,
                peer=target,
                is_error=True,
            )
        self._sm_in += 1
        rtt = (time.monotonic() - started) * 1000.0
        self._table(
            f"IQ {iq_type} {target}",
            (
                ("namespace", namespace),
                ("id", iq_id),
                (_("response"), "result" if known else "error service-unavailable"),
                ("RTT", f"{rtt:.0f}ms"),
            ),
        )

    async def _disco(self, target: str, node: str | None) -> None:
        """Запрос disco info и items по XEP-0030.

        Отправляются обе строфы, как обещает справка команды. Ответ собирается по
        виду цели: сервер, служба комнат и клиент отвечают по-разному, а
        неизвестная цель отвечает ошибкой item-not-found.
        """
        profile = _disco_profile(target, self._domain)
        if profile is None:
            await self._disco_error(target)
            return
        node_attr = f" node='{_attr(node)}'" if node else ""
        info_id = self._make_id("disco")
        self._emit(
            Direction.OUT,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='get' id='{info_id}' to='{_attr(target)}'>"
            f"<query xmlns='http://jabber.org/protocol/disco#info'{node_attr}/></iq>",
            stanza_id=info_id,
            peer=target,
        )
        self._xep("0030", "info-requested", direction=Direction.OUT, peer=target, stanza_id=info_id)
        await self._wait(self._rng.uniform(_RTT_MIN, _RTT_MAX))
        features = "".join(f"<feature var='{item}'/>" for item in profile.features)
        self._emit(
            Direction.IN,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='result' id='{info_id}' "
            f"from='{_attr(target)}' to='{_attr(self._full_jid)}'>"
            f"<query xmlns='http://jabber.org/protocol/disco#info'{node_attr}>"
            f"<identity category='{profile.category}' type='{profile.type}' "
            f"name='{_attr(profile.name)}'/>"
            f"{features}</query></iq>",
            stanza_id=info_id,
            peer=target,
        )
        self._sm_in += 1
        self._xep(
            "0030",
            "info-received",
            direction=Direction.IN,
            peer=target,
            detail={"features": str(len(profile.features))},
        )

        items_id = self._make_id("disco")
        self._emit(
            Direction.OUT,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='get' id='{items_id}' to='{_attr(target)}'>"
            "<query xmlns='http://jabber.org/protocol/disco#items'/></iq>",
            stanza_id=items_id,
            peer=target,
        )
        await self._wait(self._rng.uniform(_RTT_MIN, _RTT_MAX))
        items = "".join(
            f"<item jid='{_attr(jid)}' name='{_attr(name)}'/>" for jid, name in profile.items
        )
        self._emit(
            Direction.IN,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='result' id='{items_id}' "
            f"from='{_attr(target)}' to='{_attr(self._full_jid)}'>"
            f"<query xmlns='http://jabber.org/protocol/disco#items'>{items}</query></iq>",
            stanza_id=items_id,
            peer=target,
        )
        self._sm_in += 1
        self._xep(
            "0030",
            "items-received",
            direction=Direction.IN,
            peer=target,
            detail={"items": str(len(profile.items))},
        )
        self._caps_cache[target] = profile.features
        rows: list[tuple[str, str]] = [
            ("identity", f"{profile.category}/{profile.type}  {profile.name}"),
        ]
        if node:
            rows.append(("node", node))
        rows.extend(
            (_("feature {number}").format(number=index + 1), _feature_line(item))
            for index, item in enumerate(profile.features)
        )
        rows.extend(
            (f"item {index + 1}", f"{jid}  {name}")
            for index, (jid, name) in enumerate(profile.items)
        )
        self._table(f"disco {target}", rows)

    async def _disco_error(self, target: str) -> None:
        """Ответ item-not-found для неизвестной цели."""
        iq_id = self._make_id("disco")
        self._emit(
            Direction.OUT,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='get' id='{iq_id}' to='{_attr(target)}'>"
            "<query xmlns='http://jabber.org/protocol/disco#info'/></iq>",
            stanza_id=iq_id,
            peer=target,
        )
        self._xep("0030", "info-requested", direction=Direction.OUT, peer=target, stanza_id=iq_id)
        await self._wait(self._rng.uniform(_RTT_MIN, _RTT_MAX))
        self._emit(
            Direction.IN,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='error' id='{iq_id}' "
            f"from='{_attr(target)}' to='{_attr(self._full_jid)}'>"
            "<query xmlns='http://jabber.org/protocol/disco#info'/>"
            "<error type='cancel'>"
            "<item-not-found xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'/>"
            "</error></iq>",
            stanza_id=iq_id,
            peer=target,
            is_error=True,
        )
        self._sm_in += 1
        self._feedback(_("disco to {jid}: item-not-found").format(jid=target), ok=False)

    def _show_caps(self, target: str) -> None:
        """Показать capabilities контакта по XEP-0115 вместе с расшифровкой фич."""
        contact = self._contact_by_jid(target)
        if contact is None:
            self._feedback(
                _("contact {jid} not found in the caps cache").format(jid=target), ok=False
            )
            return
        self._xep("0115", "caps-cached", peer=contact.jid, detail={"ver": contact.caps_ver})
        cached = self._caps_cache.get(contact.jid)
        rows: list[tuple[str, str]] = [
            ("node", contact.caps_node),
            ("hash", "sha-1"),
            ("ver", contact.caps_ver),
            (_("source"), _("disco cache") if cached else _("response not requested")),
        ]
        features = cached or _CLIENT_FEATURES
        rows.extend(
            (_("feature {number}").format(number=index + 1), _feature_line(item))
            for index, item in enumerate(features)
        )
        self._table(f"caps {contact.jid}", rows)

    def _show_features(self) -> None:
        """Разобрать фактическую строфу stream:features.

        Печатается именно то, что пришло в потоке: механизмы SASL, типы привязки
        канала, содержимое inline и прочие объявленные элементы вместе с их
        пространствами имен. Захардкоженный список расходился с потоком и называл
        MAM и Carbons, которых в возможностях потока нет: они приходят из disco.
        """
        features = self._features
        if not features:
            self._feedback(_("stream features not received yet"), ok=False)
            return
        rows: list[tuple[str, str]] = []
        mechanisms = _MECHANISM_RE.findall(features)
        if mechanisms:
            rows.append((_("SASL mechanisms"), ", ".join(mechanisms)))
        bindings = _BINDING_RE.findall(features)
        if bindings:
            rows.append((_("channel binding"), ", ".join(bindings)))
        inline = _INLINE_RE.search(features)
        inline_body = inline.group(1) if inline is not None else ""
        if inline_body:
            rows.append(
                (
                    "inline",
                    ", ".join(f"{name} ({ns})" for name, ns in _ELEMENT_RE.findall(inline_body)),
                )
            )
        for name, namespace in _ELEMENT_RE.findall(_INLINE_RE.sub("", features)):
            rows.append((name, namespace))
        self._table(_("server announced in the stream"), rows)

    def _trace(self, stanza_id: str) -> None:
        """Полный путь строфы по накопленным событиям расширений.

        Строка на этап: время с миллисекундами, дельта от предыдущего этапа,
        направление, метка расширения, идентификатор строфы этапа и детали.
        Поиск идет по всем трем видам идентификатора: по id строфы, по origin-id
        и по stanza-id. Пользователь копирует в команду то, что видит в панели
        лога, и это далеко не всегда id самого сообщения.
        """
        message = self._find_message(stanza_id)
        if message is None:
            self._feedback(
                _(
                    "stanza {stanza_id} not found: stanza id, origin-id and stanza-id are accepted"
                ).format(stanza_id=stanza_id),
                ok=False,
            )
            return
        # Отсчет идет от самого раннего события пути. Локальная метка строфы
        # (origin-id) появляется на доли миллисекунды раньше самого сообщения, и
        # от времени сообщения дельта первого этапа выходила бы отрицательной.
        base = min([message.ts, *(event.ts for event in message.xeps)])
        rows: list[tuple[str, str]] = [
            (_("conversation"), message.conversation),
            (_("delivery state"), message.state.value),
            (_("encryption"), message.encryption.value),
            (_("sent"), _clock(message.ts)),
        ]
        previous = base
        for event in message.xeps:
            delta = (event.ts - previous) * 1000.0
            previous = event.ts
            parts = [
                _clock(event.ts),
                _("+{delta:.0f} ms").format(delta=delta),
                event.direction.value.upper(),
            ]
            if event.stanza_id:
                parts.append(event.stanza_id)
            detail = format_xep_detail(event)
            if detail:
                parts.append(detail)
            rows.append((format_xep_badge(event), " ".join(parts)))
        total = (previous - base) * 1000.0
        rows.append((_("full path"), _("{duration:.0f} ms").format(duration=total)))
        self._table(_("stanza path {stanza_id}").format(stanza_id=stanza_id), rows)

    def show_omemo(self, jid: str | None) -> None:
        """Операция роутера: /omemo status."""
        omemo = self._omemo_info(jid)
        self._table(
            f"OMEMO {jid or _('no conversation')}",
            (
                (_("enabled"), self._yes_no(omemo.enabled)),
                (
                    _("trusted devices"),
                    _("{trusted} of {total}").format(
                        trusted=omemo.trusted_devices, total=omemo.total_devices
                    ),
                ),
                (_("own fingerprint"), omemo.own_fingerprint or NOT_AVAILABLE),
            ),
        )

    def set_omemo(self, jid: str | None, *, enabled: bool) -> None:
        """Операция роутера: /omemo enable и /omemo disable."""
        self._set_omemo(jid, enabled=enabled)

    def show_fingerprints(self, jid: str | None) -> None:
        """Операция роутера: /omemo fingerprints."""
        self._show_fingerprints(jid)

    def omemo_purge(self, jid: str | None) -> None:
        """Операция роутера: /omemo purge."""
        self._omemo_purge(jid)

    def omemo_rotate(self, jid: str | None) -> None:
        """Операция роутера: /omemo rotate."""
        self._omemo_rotate(jid)

    def show_ox(self, action: str) -> None:
        """Операция роутера: /ox."""
        self._show_ox(action)

    def _show_ox(self, action: str) -> None:
        """Состояние OpenPGP for XMPP.

        Реализации нет: python-omemo закрывает XEP-0384, а для XEP-0373 в
        экосистеме Python нет поддерживаемой библиотеки. Собственную криптографию
        проект не пишет, поэтому команда отвечает, чего нет.
        """
        self._table(
            xep_title("0373"),
            (
                (_("requested"), action),
                (_("state"), _("not implemented")),
                (_("key"), NOT_AVAILABLE),
                (_("reason"), _("no supported XEP-0373 implementation for Python")),
            ),
        )

    def _show_fingerprints(self, target: str | None) -> None:
        """Отпечатки устройств беседы вместе с идентификатором и доверием."""
        if target is None:
            self._feedback(_("no active conversation"), ok=False)
            return
        rows: list[tuple[str, str]] = [(_("own"), self._fingerprint(self._jid))]
        for device_id, fingerprint in self._peer_devices(target):
            state = _("trusted") if fingerprint in self._trusted else _("untrusted")
            rows.append(
                (_("device {device_id}").format(device_id=device_id), f"{fingerprint}  {state}")
            )
        self._table(_("fingerprints {jid}").format(jid=target), rows)

    def set_trust(self, jid: str | None, fingerprint: str, *, trusted: bool) -> None:
        """Операция роутера: /omemo trust и /omemo distrust.

        Принимается только отпечаток из списка /omemo fingerprints: произвольная
        строка давала подтверждение доверия неизвестно чему. Пробелы в отпечатке
        не значимы: список печатает его группами по восемь знаков, и строку из
        него пользователь копирует как есть.
        """
        target = jid
        typed = fingerprint
        wanted = _compact_fingerprint(typed)
        known = {_compact_fingerprint(item): item for item in self._known_fingerprints(target)}
        found = known.get(wanted)
        if found is None:
            self._feedback(
                _("fingerprint {fingerprint} not found for the peer").format(fingerprint=typed),
                ok=False,
            )
            self._show_fingerprints(target)
            return
        if trusted:
            self._trusted.add(found)
        else:
            self._trusted.discard(found)
        self._refresh_omemo()
        self._xep(
            "0384",
            "device-trusted" if trusted else "device-untrusted",
            peer=target,
            detail={"fingerprint": found},
        )
        done = (
            _("fingerprint {fingerprint}: trusted")
            if trusted
            else _("fingerprint {fingerprint}: untrusted")
        )
        self._feedback(done.format(fingerprint=found))

    def _known_fingerprints(self, target: str | None) -> set[str]:
        """Отпечатки устройств беседы вместе с собственным."""
        known = {self._fingerprint(self._jid)}
        if target is not None:
            known.update(fingerprint for _, fingerprint in self._peer_devices(target))
        return known

    def _peer_devices(self, jid: str) -> tuple[tuple[int, str], ...]:
        """Устройства собеседника: идентификатор и отпечаток каждого.

        Список никогда не пуст: у неизвестного адреса одно устройство. Это
        единственный источник идентификаторов устройств пира, им пользуются и
        строфы потока, и вывод /omemo fingerprints.
        """
        contact = self._contact_by_jid(jid)
        devices = contact.devices if contact else 1
        return tuple(
            (self._device_id(jid, index), self._fingerprint(f"{jid}/{index}"))
            for index in range(devices)
        )

    def _trust_on_first_use(self, jid: str) -> None:
        """Доверить устройства собеседника при первой установке сессии.

        Так работают клиенты с слепым доверием (Conversations, Dino): все
        устройства принимаются до появления нового, и только оно требует решения
        пользователя. Отдельный список доверенных нужен, чтобы счетчик в
        статус-баре, заголовок беседы и вывод /omemo fingerprints показывали одну
        и ту же величину.
        """
        self._trusted.update(fingerprint for _, fingerprint in self._peer_devices(jid))

    def _omemo_info(self, jid: str | None) -> OmemoInfo:
        """Состояние шифрования беседы, собранное из списка доверенных отпечатков."""
        if jid is None:
            return OmemoInfo(own_fingerprint=self._fingerprint(self._jid))
        conversation = self._conversations.get(jid)
        devices = self._peer_devices(jid)
        return OmemoInfo(
            enabled=conversation is not None and conversation.encryption is Encryption.OMEMO,
            trusted_devices=sum(1 for _, item in devices if item in self._trusted),
            total_devices=len(devices),
            own_fingerprint=self._fingerprint(self._jid),
        )

    def _refresh_omemo(self, jid: str | None = None) -> None:
        """Пересчитать состояние шифрования беседы и отдать его в шину.

        Счетчик доверенных устройств - свойство беседы, а не клиента. Без адреса
        пересчитываются все открытые беседы: это нужно при подключении. В потоке
        сообщений адрес известен, и пересчет одной беседы там обязателен - обход
        всех бесед на каждую строфу заметен уже на сотне строф в секунду.
        """
        targets = [jid] if jid is not None else list(self._conversations)
        changed = False
        for target in targets:
            conversation = self._conversations.get(target)
            if conversation is None:
                continue
            omemo = self._omemo_info(target)
            if omemo != conversation.omemo:
                self._conversations[target] = replace(conversation, omemo=omemo)
                changed = True
        if changed:
            self._publish_conversations()

    def _device_id(self, jid: str, index: int) -> int:
        """Устойчивый идентификатор устройства собеседника."""
        digest = hashlib.sha256(f"{jid}/{index}".encode()).digest()
        return int.from_bytes(digest[:4], "big") % (2**31)

    def _omemo_purge(self, target: str | None) -> None:
        """Удалить сессии беседы. Ключи устройств и решения о доверии сохраняются."""
        devices = len(self._peer_devices(target)) if target else 1
        self._xep("0384", "session-built", peer=target, detail={"action": "purge"})
        self._table(
            f"OMEMO purge {target or _('all conversations')}",
            (
                (_("sessions deleted"), str(devices)),
                (_("devices remaining"), str(devices)),
                (_("next message"), _("will rebuild sessions from bundles")),
            ),
        )

    def _omemo_rotate(self, target: str | None) -> None:
        """Сменить собственное устройство: новый идентификатор и публикация бандла."""
        self._own_device = self._rng.randrange(10**9, 2 * 10**9)
        self._xep(
            "0384",
            "session-built",
            peer=target,
            detail={"action": "rotate", "device": str(self._own_device)},
        )
        self._table(
            "OMEMO rotate",
            (
                (_("new device"), str(self._own_device)),
                (_("bundle published"), self._yes_no(True)),
                (_("one-time prekeys"), str(_OMEMO_PREKEYS)),
            ),
        )

    def show_slot(self) -> None:
        """Операция роутера: /slot."""
        if self._last_slot is None:
            self._feedback(_("no slot has been requested"), ok=False)
            return
        put, get = self._last_slot
        # Ссылка put несет подпись в query string, тот же токен идет в заголовок
        # Authorization. Панель беседы маскирование не применяет, поэтому оно
        # делается здесь, теми же правилами, что и для панели лога. Полный вид
        # доступен только в режиме unsafe, как и сырой поток.
        unsafe = self._state.unsafe_xml
        self._feedback(f"put: {redact(put, unsafe)}\nget: {redact(get, unsafe)}")

    async def upload(self, source: str) -> None:
        """Операция роутера: /upload. Запрос слота по XEP-0363."""
        path = Path(source).expanduser()
        size = 0
        try:
            size = path.stat().st_size
        except OSError:
            self._feedback(_("file not accessible: {path}").format(path=path), ok=False)
            return
        iq_id = self._make_id("slot")
        name = path.name
        self._emit(
            Direction.OUT,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='get' id='{iq_id}' "
            f"to='upload.{_attr(self._domain)}'>"
            f"<request xmlns='urn:xmpp:http:upload:0' filename='{_attr(name)}' "
            f"size='{size}' content-type='application/octet-stream'/></iq>",
            stanza_id=iq_id,
        )
        self._xep(
            "0363",
            "slot-requested",
            direction=Direction.OUT,
            detail={"file": name, "size": str(size)},
        )
        token = self._url_token(24)
        base = f"https://upload.{self._domain}/{self._url_token(9)}/{name}"
        put = f"{base}?v=1&signature={token}"
        get = base
        self._last_slot = (put, get)
        # Подпись в query string маскируется панелью лога: в сыром потоке она есть,
        # на экран попадает уже срезанной.
        self._emit(
            Direction.IN,
            StanzaKind.IQ,
            f"<iq xmlns='jabber:client' type='result' id='{iq_id}' "
            f"from='upload.{_attr(self._domain)}' to='{_attr(self._full_jid)}'>"
            "<slot xmlns='urn:xmpp:http:upload:0'>"
            f"<put url='{_attr(put)}'>"
            f"<header name='Authorization'>Bearer {token}</header></put>"
            f"<get url='{_attr(get)}'/></slot></iq>",
            stanza_id=iq_id,
        )
        self._sm_in += 1
        self._xep("0363", "slot-received", direction=Direction.IN, detail={"file": name})
        self._feedback(
            _("slot received for {name}, {size}").format(name=name, size=humanize_bytes(size))
        )

    def describe(self) -> Sequence[str]:
        """Короткая сводка о сессии для команд и тестов."""
        return (
            _("scenario: {name}").format(name=self._scenario),
            _("rate: {rate:.0f} stanzas/s").format(rate=self._rate),
            _("MAM batch: {count}").format(count=self._mam_batch),
            _("account: {jid}").format(jid=self._full_jid),
        )
