"""Доменные модели клиента.

Все структуры неизменяемые (``frozen=True, slots=True``). Причина: модели ходят
через шину событий между слоями и попадают в буферы UI. Неизменяемость исключает
ситуацию, когда панель рисует объект, который в этот момент правит протокольный слой.
Обновление состояния выполняется методами ``with_*``, возвращающими новый экземпляр.
"""

import enum
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Final, Self

from termisations.core.i18n import N_

__all__ = [
    "DELIVERY_MARKS",
    "EMPTY_DETAIL",
    "MUC_MARK",
    "NOT_AVAILABLE",
    "PRESENCE_LABELS",
    "PRESENCE_MARKS",
    "STAGE_LABELS",
    "TRANSPORT_LABELS",
    "ClientState",
    "ConnectionStage",
    "Conversation",
    "DeliveryState",
    "Direction",
    "Encryption",
    "Message",
    "Metrics",
    "OmemoInfo",
    "PresenceShow",
    "RawStanza",
    "RosterItem",
    "SmInfo",
    "StanzaKind",
    "TlsInfo",
    "Transport",
    "XepEvent",
    "format_latency",
    "humanize_bytes",
    "humanize_duration",
    "stage_label",
    "transport_label",
]

# Общий пустой словарь деталей. Используется как значение по умолчанию, чтобы
# не создавать новый dict на каждое событие: на горячем пути их до 500 в секунду.
EMPTY_DETAIL: Final[Mapping[str, str]] = MappingProxyType({})

# Единая подпись недоступной метрики. Недоступное значение показывается как
# "n/a", а не как ноль.
NOT_AVAILABLE: Final = "n/a"


class Direction(enum.StrEnum):
    """Направление строфы или события."""

    IN = "in"
    OUT = "out"
    LOCAL = "local"


class StanzaKind(enum.StrEnum):
    """Тип строфы в сыром потоке."""

    MESSAGE = "message"
    PRESENCE = "presence"
    IQ = "iq"
    STREAM = "stream"
    SASL = "sasl"
    TLS = "tls"
    OTHER = "other"


class Encryption(enum.StrEnum):
    """Тип шифрования беседы или отдельного сообщения."""

    PLAIN = "plain"
    OMEMO = "omemo"
    OX = "ox"
    PGP = "pgp"


class DeliveryState(enum.StrEnum):
    """Состояние доставки сообщения.

    PENDING - поставлено в очередь отправки, SENT - ушло в поток,
    ACKED - подтверждено потоковым менеджментом, RECEIVED - подтверждено получателем,
    DISPLAYED - прочитано, FAILED - отправка завершилась ошибкой.
    """

    PENDING = "pending"
    SENT = "sent"
    ACKED = "acked"
    RECEIVED = "received"
    DISPLAYED = "displayed"
    FAILED = "failed"


class Transport(enum.StrEnum):
    """Способ подключения к серверу."""

    DIRECT_TLS = "direct-tls"
    STARTTLS = "starttls"
    WEBSOCKET = "websocket"
    NONE = "none"


class PresenceShow(enum.StrEnum):
    """Значение элемента show в присутствии.

    AVAILABLE соответствует присутствию без элемента show, OFFLINE - отсутствию
    присутствия вообще.
    """

    AVAILABLE = "available"
    CHAT = "chat"
    AWAY = "away"
    XA = "xa"
    DND = "dnd"
    OFFLINE = "offline"


class ConnectionStage(enum.StrEnum):
    """Стадия установки соединения.

    Каждая стадия показывается рядом со спиннером и завершается строкой
    с фактическим результатом и длительностью.
    """

    OFFLINE = "offline"
    RESOLVING_SRV = "resolving-srv"
    TLS_HANDSHAKE = "tls-handshake"
    SASL = "sasl"
    BINDING = "binding"
    SM_ENABLE = "sm-enable"
    SM_RESUME = "sm-resume"
    FETCHING_ROSTER = "fetching-roster"
    INITIAL_PRESENCE = "initial-presence"
    FETCHING_MAM = "fetching-mam"
    OMEMO_SESSIONS = "omemo-sessions"
    READY = "ready"
    ERROR = "error"


# Человекочитаемые подписи стадий на английском: это термины протокола, они
# совпадают с тем, что пользователь увидит в RFC и в логе сервера.
# Поэтому в каталог перевода они не входят: подпись одна на обоих языках.
STAGE_LABELS: Final[Mapping[ConnectionStage, str]] = MappingProxyType(
    {
        ConnectionStage.OFFLINE: "offline",
        ConnectionStage.RESOLVING_SRV: "resolving SRV",
        ConnectionStage.TLS_HANDSHAKE: "TLS handshake",
        ConnectionStage.SASL: "SASL2",
        ConnectionStage.BINDING: "binding resource",
        ConnectionStage.SM_ENABLE: "enabling stream management",
        ConnectionStage.SM_RESUME: "SM resume",
        ConnectionStage.FETCHING_ROSTER: "fetching roster",
        ConnectionStage.INITIAL_PRESENCE: "initial presence",
        ConnectionStage.FETCHING_MAM: "fetching MAM",
        ConnectionStage.OMEMO_SESSIONS: "building OMEMO sessions",
        ConnectionStage.READY: "ready",
        ConnectionStage.ERROR: "error",
    }
)

# Подписи транспорта вместе со стандартным портом: именно в таком виде их ждет
# статус-бар.
TRANSPORT_LABELS: Final[Mapping[Transport, str]] = MappingProxyType(
    {
        Transport.DIRECT_TLS: "DirectTLS:5223",
        Transport.STARTTLS: "STARTTLS:5222",
        Transport.WEBSOCKET: "WebSocket",
        Transport.NONE: NOT_AVAILABLE,
    }
)

# Маркер присутствия и его цвет. Таблица общая для статус-бара и полосы бесед:
# один и тот же признак не должен выглядеть на экране по-разному.
PRESENCE_MARKS: Final[Mapping[PresenceShow, tuple[str, str]]] = MappingProxyType(
    {
        PresenceShow.AVAILABLE: ("●", "green"),
        PresenceShow.CHAT: ("●", "green"),
        PresenceShow.AWAY: ("●", "yellow"),
        PresenceShow.XA: ("●", "yellow"),
        PresenceShow.DND: ("●", "red"),
        PresenceShow.OFFLINE: ("○", "dim"),
    }
)


MUC_MARK: Final = "#"
"""Знак комнаты. Общий для полосы бесед, заголовка беседы и палитры контактов."""


# Подписи присутствия для списков, где знака недостаточно: в палитре контактов
# знак стоит слева, а подсказка справа должна читаться словами. Значения только
# помечены для каталога: переводит их через _() тот, кто выводит подпись.
PRESENCE_LABELS: Final[Mapping[PresenceShow, str]] = MappingProxyType(
    {
        PresenceShow.AVAILABLE: N_("online"),
        PresenceShow.CHAT: N_("ready to chat"),
        PresenceShow.AWAY: N_("away"),
        PresenceShow.XA: N_("extended away"),
        PresenceShow.DND: N_("busy"),
        PresenceShow.OFFLINE: N_("offline"),
    }
)


# Единые пометки состояния доставки, чтобы панели не расходились в символах.
# Доставлено и прочитано различаются формой, а не только цветом: в монохромном
# терминале и при нарушении цветовосприятия цвет не читается.
DELIVERY_MARKS: Final[Mapping[DeliveryState, str]] = MappingProxyType(
    {
        DeliveryState.PENDING: "…",
        DeliveryState.SENT: "→",
        DeliveryState.ACKED: "✓",
        DeliveryState.RECEIVED: "✓✓",
        DeliveryState.DISPLAYED: "✓✓●",
        DeliveryState.FAILED: "✗",
    }
)


def stage_label(stage: ConnectionStage) -> str:
    """Подпись стадии соединения. Неизвестная стадия отдает свое значение."""
    return STAGE_LABELS.get(stage, str(stage))


def transport_label(transport: Transport) -> str:
    """Подпись транспорта для статус-бара."""
    return TRANSPORT_LABELS.get(transport, NOT_AVAILABLE)


@dataclass(frozen=True, slots=True)
class XepEvent:
    """Факт срабатывания протокольного расширения.

    Первичная сущность отображения: UI не знает названий расширений, он получает
    номер и действие, а подпись берет из ``termisations.xeps``.
    """

    xep: str
    """Номер расширения из четырех цифр без префикса, например "0184"."""

    action: str
    """Действие в kebab-case, например "receipt-sent"."""

    direction: Direction = Direction.LOCAL
    peer: str | None = None
    stanza_id: str | None = None
    detail: Mapping[str, str] = EMPTY_DETAIL
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class RawStanza:
    """Строфа сырого потока, уже приведенная к виду для показа.

    Поле ``xml`` содержит текст строфы без маскирования. Маскирование выполняет
    панель лога при рендере, см. ``termisations.core.redact``.
    """

    ts: float
    direction: Direction
    kind: StanzaKind
    xml: str
    stanza_id: str | None = None
    peer: str | None = None
    is_error: bool = False
    size_bytes: int = 0

    @classmethod
    def make(
        cls,
        direction: Direction,
        kind: StanzaKind,
        xml: str,
        *,
        stanza_id: str | None = None,
        peer: str | None = None,
        is_error: bool = False,
        ts: float | None = None,
    ) -> Self:
        """Собрать строфу, посчитав размер и время автоматически."""
        return cls(
            ts=time.time() if ts is None else ts,
            direction=direction,
            kind=kind,
            xml=xml,
            stanza_id=stanza_id,
            peer=peer,
            is_error=is_error,
            size_bytes=len(xml.encode("utf-8", "replace")),
        )


@dataclass(frozen=True, slots=True)
class Message:
    """Сообщение беседы вместе с накопленной протокольной историей."""

    message_id: str
    conversation: str
    sender: str
    body: str
    ts: float = field(default_factory=time.time)
    direction: Direction = Direction.IN
    encryption: Encryption = Encryption.PLAIN
    state: DeliveryState = DeliveryState.RECEIVED
    corrected: bool = False
    xeps: tuple[XepEvent, ...] = ()

    def with_state(self, state: DeliveryState) -> Self:
        """Новый экземпляр с другим состоянием доставки."""
        return replace(self, state=state)

    def with_xep(self, event: XepEvent) -> Self:
        """Новый экземпляр с добавленным протокольным событием.

        Дубликаты по тройке (номер, действие, участник) не добавляются: повторный
        маркер прочтения от того же участника не должен плодить одинаковые метки.
        Участник входит в ключ: в комнате маркеры прочтения приходят от
        разных людей, и схлопывание их в одну метку теряет число прочитавших.
        """
        if any(
            item.xep == event.xep and item.action == event.action and item.peer == event.peer
            for item in self.xeps
        ):
            return self
        return replace(self, xeps=(*self.xeps, event))

    def with_correction(self, body: str) -> Self:
        """Новый экземпляр с исправленным текстом и признаком корректировки."""
        return replace(self, body=body, corrected=True)


@dataclass(frozen=True, slots=True)
class RosterItem:
    """Запись контакт-листа."""

    jid: str
    name: str = ""
    subscription: str = "none"
    groups: tuple[str, ...] = ()
    show: PresenceShow = PresenceShow.OFFLINE
    status: str = ""

    @property
    def display_name(self) -> str:
        """Имя для показа: заданное имя или сам JID."""
        return self.name or self.jid

    @property
    def online(self) -> bool:
        """Контакт доступен, если его присутствие не OFFLINE."""
        return self.show is not PresenceShow.OFFLINE


@dataclass(frozen=True, slots=True)
class OmemoInfo:
    """Состояние сквозного шифрования беседы.

    Живет полем ``Conversation``, а не полем клиента: тип шифрования и число
    доверенных устройств относятся к конкретному собеседнику. Пока эти величины
    брались из двух источников, счетчик в статус-баре показывал последнюю
    беседу, где выполнялась команда, а не ту, что открыта.
    """

    enabled: bool = False
    trusted_devices: int = 0
    total_devices: int = 0
    own_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class Conversation:
    """Открытая беседа: приватная или комната."""

    jid: str
    title: str = ""
    is_muc: bool = False
    unread: int = 0
    encryption: Encryption = Encryption.PLAIN
    omemo: OmemoInfo = field(default_factory=OmemoInfo)
    """Состояние OMEMO этой беседы: число доверенных устройств и свой отпечаток."""

    show: PresenceShow = PresenceShow.OFFLINE
    """Присутствие собеседника. Для комнаты означает, что вход выполнен."""

    topic: str = ""
    """Тема комнаты. Для личной беседы пуста."""

    @property
    def display_title(self) -> str:
        """Заголовок для показа: заданный заголовок или сам JID."""
        return self.title or self.jid


@dataclass(frozen=True, slots=True)
class TlsInfo:
    """Параметры защиты канала. None означает, что параметр недоступен."""

    version: str | None = None
    cipher: str | None = None
    channel_binding: str | None = None
    valid: bool = False
    chain: tuple[str, ...] = ()
    """Цепочка сертификатов от листа к корню. Пустая, пока канала нет."""


@dataclass(frozen=True, slots=True)
class SmInfo:
    """Состояние потокового менеджмента."""

    enabled: bool = False
    resumed: bool = False
    outbound_unacked: int = 0
    inbound_handled: int = 0


@dataclass(frozen=True, slots=True)
class Metrics:
    """Метрики процесса и соединения.

    Недоступная метрика равна None и показывается как "n/a". Ноль означает
    измеренный ноль, а не отсутствие данных.
    """

    latency_ms: float | None = None
    rss_bytes: int | None = None
    stanzas_total: int = 0
    stanzas_per_sec: float = 0.0
    bytes_in: int = 0
    bytes_out: int = 0
    uptime_s: float = 0.0
    reconnects: int = 0


@dataclass(frozen=True, slots=True)
class ClientState:
    """Полное состояние клиента для статус-бара и команд вида /sm, /tls, /stats.

    Единственный источник правды хранится в store приложения, панели получают
    копию через событие StateUpdated и ничего не вычисляют сами.
    """

    jid: str | None = None
    presence_show: PresenceShow = PresenceShow.OFFLINE
    stage: ConnectionStage = ConnectionStage.OFFLINE
    transport: Transport = Transport.NONE
    tls: TlsInfo = field(default_factory=TlsInfo)
    sm: SmInfo = field(default_factory=SmInfo)
    metrics: Metrics = field(default_factory=Metrics)
    unsafe_xml: bool = False

    @property
    def connected(self) -> bool:
        """Соединение считается рабочим только на стадии READY."""
        return self.stage is ConnectionStage.READY


def humanize_bytes(value: int | None) -> str:
    """Размер в компактном виде: 61M, 512K, 940B. None отдает "n/a"."""
    if value is None:
        return NOT_AVAILABLE
    size = float(value)
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.0f}{unit}" if size >= 10 else f"{size:.1f}{unit}"
        size /= 1024
    return NOT_AVAILABLE


def humanize_duration(seconds: float | None) -> str:
    """Длительность в компактном виде: 42s, 5m03s, 2h05m. None отдает "n/a"."""
    if seconds is None or seconds < 0:
        return NOT_AVAILABLE
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def format_latency(latency_ms: float | None) -> str:
    """Задержка в миллисекундах. None отдает "n/a"."""
    if latency_ms is None:
        return NOT_AVAILABLE
    return f"{latency_ms:.0f}ms"
