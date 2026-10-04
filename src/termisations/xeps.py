"""Справочник протокольных расширений: номер, название, метка, цвет.

Единственное место в проекте, где живут тексты подписей расширений. UI получает
структурированное событие ``XepEvent`` и рендерит его через функции этого модуля.
Добавление нового расширения в вывод не требует правок в пакете ``ui``.

Цвета взяты из базовой палитры терминала (red, green, yellow, blue, magenta, cyan,
white). Это пересечение имен, которые понимают и Rich, и TCSS, поэтому одно и то же
значение годится и для ``rich.text.Text``, и для стилей Textual.
"""

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from termisations.core.i18n import N_, _
from termisations.core.models import XepEvent

__all__ = [
    "ACTIONS",
    "ACTION_SCOPES",
    "BADGE_DETAILS",
    "COMPACT_BADGES",
    "COMPACT_LEGEND",
    "NAMESPACE_XEPS",
    "PRESENCE_TEXTS",
    "SCOPES",
    "SILENT_BADGES",
    "UNKNOWN_XEP",
    "XEPS",
    "XepInfo",
    "XepScope",
    "badge_text",
    "badge_visible",
    "compact_badge",
    "compact_legend",
    "format_action",
    "format_xep_badge",
    "format_xep_detail",
    "normalize_number",
    "presence_text",
    "xep_color",
    "xep_info",
    "xep_label",
    "xep_scope",
    "xep_title",
]

# Префикс вынесен в константу, чтобы формат метки правился в одном месте.
_PREFIX: Final = "XEP-"

# Цветовые группы: соединение и поток, доставка, шифрование, обнаружение,
# архив и история, ошибки и блокировки, прочее.
_CONNECTION: Final = "cyan"
_DELIVERY: Final = "green"
_CRYPTO: Final = "magenta"
_DISCOVERY: Final = "blue"
_ARCHIVE: Final = "yellow"
_CONTROL: Final = "red"
_MISC: Final = "white"


@dataclass(frozen=True, slots=True)
class XepInfo:
    """Запись справочника расширений."""

    number: str
    """Номер из четырех цифр без префикса, например "0184"."""

    title: str
    """Официальное английское название расширения."""

    label: str
    """Короткая метка для строки статуса сообщения, например "Receipt"."""

    color: str
    """Имя цвета для Rich и TCSS."""


UNKNOWN_XEP: Final = XepInfo(number="0000", title="Unknown Extension", label="XEP", color=_MISC)
"""Безопасная заглушка для номера, которого нет в справочнике."""


XEPS: Final[Mapping[str, XepInfo]] = {
    # Основные: соединение и доставка.
    "0030": XepInfo("0030", "Service Discovery", "Disco", _DISCOVERY),
    "0115": XepInfo("0115", "Entity Capabilities", "Caps", _DISCOVERY),
    "0198": XepInfo("0198", "Stream Management", "SM", _CONNECTION),
    "0199": XepInfo("0199", "XMPP Ping", "Ping", _CONNECTION),
    "0280": XepInfo("0280", "Message Carbons", "Carbon", _DELIVERY),
    "0313": XepInfo("0313", "Message Archive Management", "MAM", _ARCHIVE),
    "0359": XepInfo("0359", "Unique and Stable Stanza IDs", "Stanza ID", _DELIVERY),
    "0368": XepInfo("0368", "SRV Records for XMPP over TLS", "Direct TLS", _CONNECTION),
    "0386": XepInfo("0386", "Bind 2", "Bind 2", _CONNECTION),
    "0388": XepInfo("0388", "Extensible SASL Profile", "SASL2", _CONNECTION),
    "0440": XepInfo("0440", "SASL Channel-Binding Type Capability", "Binding", _CONNECTION),
    # Основные: сообщения.
    "0085": XepInfo("0085", "Chat State Notifications", "State", _MISC),
    "0184": XepInfo("0184", "Message Delivery Receipts", "Receipt", _DELIVERY),
    "0203": XepInfo("0203", "Delayed Delivery", "Delayed", _ARCHIVE),
    "0245": XepInfo("0245", "The /me Command", "Action", _MISC),
    "0308": XepInfo("0308", "Last Message Correction", "Corrected", _DELIVERY),
    "0333": XepInfo("0333", "Chat Markers", "Marker", _DELIVERY),
    "0384": XepInfo("0384", "OMEMO Encryption", "OMEMO", _CRYPTO),
    # Дополнительные расширения.
    "0045": XepInfo("0045", "Multi-User Chat", "MUC", _MISC),
    "0084": XepInfo("0084", "User Avatar", "Avatar", _MISC),
    "0191": XepInfo("0191", "Blocking Command", "Blocking", _CONTROL),
    "0352": XepInfo("0352", "Client State Indication", "CSI", _CONNECTION),
    "0363": XepInfo("0363", "HTTP File Upload", "Upload", _MISC),
    "0392": XepInfo("0392", "Consistent Color Generation", "Color", _MISC),
    "0402": XepInfo("0402", "PEP Native Bookmarks", "Bookmarks", _DISCOVERY),
    "0421": XepInfo("0421", "Occupant Identifiers for Semi-Anonymous MUCs", "Occupant", _MISC),
    "0424": XepInfo("0424", "Message Retraction", "Retracted", _CONTROL),
    "0444": XepInfo("0444", "Message Reactions", "Reaction", _DELIVERY),
    "0446": XepInfo("0446", "File Metadata Element", "File Meta", _MISC),
    "0447": XepInfo("0447", "Stateless File Sharing", "File", _MISC),
    "0461": XepInfo("0461", "Message Replies", "Reply", _DELIVERY),
    # Инфраструктура PEP, нужна для бандлов OMEMO и аватаров.
    "0060": XepInfo("0060", "Publish-Subscribe", "PubSub", _DISCOVERY),
    "0163": XepInfo("0163", "Personal Eventing Protocol", "PEP", _DISCOVERY),
    "0004": XepInfo("0004", "Data Forms", "Form", _MISC),
    # Расширения без реализации в клиенте.
    "0027": XepInfo("0027", "Current Jabber OpenPGP Usage", "PGP", _CRYPTO),
    "0369": XepInfo("0369", "Mediated Information eXchange (MIX)", "MIX", _MISC),
    "0373": XepInfo("0373", "OpenPGP for XMPP", "OX", _CRYPTO),
    "0405": XepInfo("0405", "MIX-PAM: MIX Participant Server Requirements", "MIX-PAM", _MISC),
}
"""Справочник расширений. Ключ - нормализованный номер из четырех цифр."""


ACTIONS: Final[Mapping[tuple[str, str], str]] = {
    # соединение и поток
    ("0368", "srv-resolved"): "SRV Resolved",
    ("0368", "direct-tls"): "Direct TLS",
    ("0368", "fallback-starttls"): "STARTTLS Fallback",
    ("0388", "challenge"): "SASL2 Challenge",
    ("0388", "authenticated"): "SASL2 Authenticated",
    ("0388", "failure"): "SASL2 Failure",
    ("0386", "bound"): "Resource Bound",
    ("0440", "channel-binding"): "Channel Binding",
    ("0198", "enabled"): "SM Enabled",
    ("0198", "resumed"): "SM Resumed",
    ("0198", "acked"): "Acked",
    ("0198", "failed"): "SM Failed",
    ("0199", "ping-sent"): "Ping Sent",
    ("0199", "pong"): "Pong",
    ("0352", "active"): "Client Active",
    ("0352", "inactive"): "Client Inactive",
    # обнаружение
    ("0030", "info-requested"): "Disco Info Requested",
    ("0030", "info-received"): "Disco Info",
    ("0030", "items-received"): "Disco Items",
    ("0115", "caps-received"): "Caps",
    ("0115", "caps-cached"): "Caps Cached",
    ("0060", "item-published"): "Item Published",
    ("0163", "event"): "PEP Event",
    ("0402", "bookmarks-fetched"): "Bookmarks Fetched",
    # доставка
    ("0184", "receipt-requested"): "Receipt Requested",
    ("0184", "receipt-sent"): "Receipt Sent",
    ("0184", "receipt-received"): "Receipt Received",
    ("0333", "received"): "Received",
    ("0333", "displayed"): "Displayed",
    ("0333", "acknowledged"): "Acknowledged",
    ("0359", "stanza-id"): "Stanza ID",
    ("0359", "origin-id"): "Origin ID",
    ("0280", "enabled"): "Carbons Enabled",
    ("0280", "carbon-received"): "Carbon Received",
    ("0280", "carbon-sent"): "Carbon Sent",
    ("0308", "corrected"): "Corrected",
    ("0424", "retracted"): "Retracted",
    ("0444", "reaction"): "Reaction",
    ("0461", "reply"): "Reply",
    ("0085", "active"): "Active",
    ("0085", "composing"): "Composing",
    ("0085", "paused"): "Paused",
    ("0085", "gone"): "Gone",
    ("0245", "me"): "Action",
    ("0203", "delayed"): "Delayed",
    # архив
    ("0313", "fetch-started"): "MAM Fetch",
    ("0313", "page-received"): "MAM Page",
    ("0313", "complete"): "MAM Complete",
    # шифрование
    ("0384", "encrypted"): "Encrypted",
    ("0384", "decrypted"): "Decrypted",
    ("0384", "session-built"): "Session Built",
    ("0384", "bundle-fetched"): "Bundle Fetched",
    ("0384", "device-untrusted"): "Untrusted Device",
    ("0384", "device-trusted"): "Trusted Device",
    ("0373", "encrypted"): "OX Encrypted",
    ("0373", "decrypted"): "OX Decrypted",
    # комнаты, файлы, контроль
    ("0045", "joined"): "Joined",
    ("0045", "left"): "Left",
    ("0045", "nick-changed"): "Nick Changed",
    ("0045", "subject"): "Subject",
    ("0421", "occupant-id"): "Occupant ID",
    ("0363", "slot-requested"): "Slot Requested",
    ("0363", "slot-received"): "Slot Received",
    ("0363", "uploaded"): "Uploaded",
    ("0446", "metadata"): "File Metadata",
    ("0447", "file-shared"): "File Shared",
    ("0191", "blocked"): "Blocked",
    ("0191", "unblocked"): "Unblocked",
    ("0084", "avatar-updated"): "Avatar Updated",
    ("0392", "color"): "Color",
}
"""Подписи действий. Ключ - пара (номер расширения, действие в kebab-case)."""


class XepScope(enum.StrEnum):
    """Область действия расширения: куда его событие попадает на экране.

    Классификация нужна панели беседы, чтобы лента оставалась читаемой. Это данные,
    а не условия в UI: панель спрашивает область у справочника и решает по ней, а не
    по номеру расширения.
    """

    TRANSPORT = "transport"
    """Поток и соединение: пинги, подтверждения потока, TLS, SASL, disco.

    В ленту беседы не попадает вовсе. Полный поток виден в панели RAW XML,
    сводные значения - в статус-баре и в выводе команд /sm, /ping, /stats.
    """

    CONVERSATION = "conversation"
    """Относится к переписке: квитанции, маркеры, архив, шифрование, комнаты."""

    PRESENCE = "presence"
    """Текущее состояние собеседника: набор текста, присутствие.

    Это не событие истории, а значение, которое устаревает. Показывается строкой
    состояния в заголовке беседы и гаснет по таймауту.
    """


# Область действия по номеру расширения. Умолчание для номера, которого в таблице
# нет, - CONVERSATION: неизвестное расширение лучше показать, чем потерять.
SCOPES: Final[Mapping[str, XepScope]] = {
    # Поток и соединение.
    "0198": XepScope.TRANSPORT,
    "0199": XepScope.TRANSPORT,
    "0352": XepScope.TRANSPORT,
    "0368": XepScope.TRANSPORT,
    "0386": XepScope.TRANSPORT,
    "0388": XepScope.TRANSPORT,
    "0440": XepScope.TRANSPORT,
    "0030": XepScope.TRANSPORT,
    "0115": XepScope.TRANSPORT,
    "0060": XepScope.TRANSPORT,
    "0163": XepScope.TRANSPORT,
    "0402": XepScope.TRANSPORT,
    "0191": XepScope.TRANSPORT,
    "0392": XepScope.TRANSPORT,
    # Состояние собеседника.
    "0085": XepScope.PRESENCE,
    "0084": XepScope.PRESENCE,
    # Переписка.
    "0045": XepScope.CONVERSATION,
    "0184": XepScope.CONVERSATION,
    "0203": XepScope.CONVERSATION,
    "0245": XepScope.CONVERSATION,
    "0280": XepScope.CONVERSATION,
    "0308": XepScope.CONVERSATION,
    "0313": XepScope.CONVERSATION,
    "0333": XepScope.CONVERSATION,
    "0359": XepScope.CONVERSATION,
    "0363": XepScope.CONVERSATION,
    "0373": XepScope.CONVERSATION,
    "0384": XepScope.CONVERSATION,
    "0421": XepScope.CONVERSATION,
    "0424": XepScope.CONVERSATION,
    "0444": XepScope.CONVERSATION,
    "0446": XepScope.CONVERSATION,
    "0447": XepScope.CONVERSATION,
    "0461": XepScope.CONVERSATION,
}
"""Область действия расширения. Ключ - нормализованный номер."""


# Уточнение области по конкретному действию. Нужно там, где одно расширение дает
# и протокольные события, и события переписки.
ACTION_SCOPES: Final[Mapping[tuple[str, str], XepScope]] = {
    # Выборка бандла и построение сессии - подготовка канала, а не переписка.
    ("0384", "bundle-fetched"): XepScope.TRANSPORT,
    ("0384", "session-built"): XepScope.TRANSPORT,
    # Стабильный идентификатор строфы сам по себе истории не несет: он значим
    # только приклеенным к сообщению.
    ("0359", "stanza-id"): XepScope.TRANSPORT,
    ("0359", "origin-id"): XepScope.TRANSPORT,
}
"""Область действия для пары (номер, действие). Приоритетнее таблицы SCOPES."""


# Метки, которые к отдельному сообщению ничего не добавляют: они одинаковы для всей
# пачки и уже показаны строкой итога. У сообщения не рисуются.
SILENT_BADGES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("0313", "page-received"),
        ("0203", "delayed"),
    }
)
"""Пары (номер, действие), которые не выводятся меткой у сообщения."""


# Расширения, которые срабатывают на каждом сообщении подряд. Полная метка у них
# одинакова у всей ленты и информации не несет, но и скрывать их нельзя: видимость
# работы протокола - смысл клиента. Такие события показываются знаком в строке
# сообщения, а полный вид остается в панели сырого потока и в выводе /trace.
COMPACT_BADGES: Final[Mapping[tuple[str, str], str]] = {
    ("0359", "stanza-id"): "#",
    ("0359", "origin-id"): "#",
    ("0184", "receipt-sent"): "✓",
    ("0184", "receipt-requested"): "⇢",
    ("0184", "receipt-received"): "✓",
    ("0198", "acked"): "↑",
}
"""Пары (номер, действие), которые показываются знаком, а не полной меткой."""


COMPACT_LEGEND: Final[tuple[tuple[str, str], ...]] = (
    ("#", N_("XEP-0359: the stanza has a stable identifier")),
    ("✓", N_("XEP-0184: delivery receipt sent or received")),
    ("⇢", N_("XEP-0184: receipt requested from the peer")),
    ("↑", N_("XEP-0198: stanza acknowledged by the server")),
)
"""Расшифровка знаков компактных меток для справки. Текст переводит ``compact_legend``."""


# Детали, значимые в метке у сообщения. Все остальные детали видны в отдельной
# строке события и в панели RAW XML, дублировать их у сообщения незачем.
BADGE_DETAILS: Final[Mapping[tuple[str, str], tuple[str, ...]]] = {
    ("0384", "decrypted"): ("devices", "trusted"),
    ("0384", "encrypted"): ("devices",),
    ("0333", "displayed"): ("nick",),
    ("0333", "received"): ("nick",),
    ("0444", "reaction"): ("emoji", "nick"),
    ("0198", "acked"): (),
}
"""Ключи деталей, которые попадают в метку сообщения."""


# Тексты состояния собеседника. Пустая строка означает, что состояние показывать
# не нужно: оно означает возврат к обычному виду.
PRESENCE_TEXTS: Final[Mapping[tuple[str, str], str]] = {
    ("0085", "composing"): N_("typing"),
    ("0085", "paused"): N_("stopped typing"),
    ("0085", "active"): "",
    ("0085", "gone"): N_("left the conversation"),
    ("0084", "avatar-updated"): N_("changed avatar"),
}
"""Текст строки состояния. Ключ - пара (номер расширения, действие).

Перевод делает ``presence_text``.
"""


# Сокращения, которые в подписи должны остаться заглавными, когда действия
# нет в таблице ACTIONS и подпись собирается автоматически.
_ACRONYMS: Final[frozenset[str]] = frozenset(
    {
        "sm",
        "mam",
        "tls",
        "sasl",
        "iq",
        "id",
        "ids",
        "omemo",
        "ox",
        "muc",
        "srv",
        "pep",
        "xml",
        "rtt",
        "csi",
        "jid",
        "url",
        "pgp",
        "mix",
        "sims",
    }
)


NAMESPACE_XEPS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "http://jabber.org/protocol/disco#info": "0030",
        "http://jabber.org/protocol/disco#items": "0030",
        "http://jabber.org/protocol/caps": "0115",
        "http://jabber.org/protocol/muc": "0045",
        "urn:xmpp:ping": "0199",
        "urn:xmpp:sm:3": "0198",
        "urn:xmpp:mam:2": "0313",
        "urn:xmpp:carbons:2": "0280",
        "urn:xmpp:blocking": "0191",
        "urn:xmpp:http:upload:0": "0363",
        "urn:xmpp:sid:0": "0359",
        "urn:xmpp:receipts": "0184",
        "urn:xmpp:chat-markers:0": "0333",
        "urn:xmpp:message-correct:0": "0308",
        "http://jabber.org/protocol/chatstates": "0085",
        "urn:xmpp:csi:0": "0352",
        "urn:xmpp:sasl:2": "0388",
        "urn:xmpp:bind:0": "0386",
        "eu.siacs.conversations.axolotl": "0384",
    }
)
"""Пространство имен протокола и номер расширения, которое его вводит.

Таблица живет здесь, а не в эмуляторе: по ней подписываются строки ответа
disco и в моке, и на настоящем соединении, а две копии разошлись бы.
"""


def normalize_number(value: str) -> str:
    """Привести номер расширения к виду из четырех цифр.

    Принимает "184", "0184", "XEP-0184", "xep0184". Мусор на входе не приводит
    к исключению: вернется "0000".
    """
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        return UNKNOWN_XEP.number
    return digits[-4:].zfill(4)


def xep_info(number: str) -> XepInfo:
    """Запись справочника. Неизвестный номер отдает заглушку с этим же номером."""
    normalized = normalize_number(number)
    found = XEPS.get(normalized)
    if found is not None:
        return found
    return XepInfo(
        number=normalized,
        title=UNKNOWN_XEP.title,
        label=UNKNOWN_XEP.label,
        color=UNKNOWN_XEP.color,
    )


def xep_title(number: str) -> str:
    """Официальное название расширения."""
    return xep_info(number).title


def xep_label(number: str) -> str:
    """Короткая метка расширения."""
    return xep_info(number).label


def xep_color(number: str) -> str:
    """Цвет расширения для Rich и TCSS. Неизвестный номер отдает нейтральный цвет."""
    return xep_info(number).color


def format_action(xep: str, action: str) -> str:
    """Подпись действия.

    Если пары нет в таблице, подпись собирается из kebab-case автоматически:
    "receipt-sent" превращается в "Receipt Sent", известные сокращения остаются
    заглавными.
    """
    normalized = normalize_number(xep)
    found = ACTIONS.get((normalized, action))
    if found is not None:
        return found
    if not action:
        return xep_label(normalized)
    words = [word for word in action.replace("_", "-").split("-") if word]
    return " ".join(
        word.upper() if word.lower() in _ACRONYMS else word.capitalize() for word in words
    )


def format_xep_badge(event: XepEvent) -> str:
    """Текст метки вида "XEP-0184: Receipt Sent".

    Скобки вокруг метки рисует UI, здесь их нет: разные панели
    оформляют метку по-разному.
    """
    number = normalize_number(event.xep)
    return f"{_PREFIX}{number}: {format_action(number, event.action)}"


def format_xep_detail(event: XepEvent, separator: str = " ") -> str:
    """Детали события одной строкой вида "devices=4 trusted=4".

    Пустые детали дают пустую строку, чтобы вызывающему коду не приходилось
    проверять наличие данных.
    """
    if not event.detail:
        return ""
    return separator.join(f"{key}={value}" for key, value in event.detail.items())


def xep_scope(event: XepEvent) -> XepScope:
    """Область действия события: куда его показывать.

    Сначала проверяется уточнение по паре (номер, действие), затем таблица по
    номеру. Неизвестное расширение считается относящимся к переписке: потерять
    событие хуже, чем показать лишнее.
    """
    number = normalize_number(event.xep)
    found = ACTION_SCOPES.get((number, event.action))
    if found is not None:
        return found
    return SCOPES.get(number, XepScope.CONVERSATION)


def badge_visible(event: XepEvent) -> bool:
    """Показывать ли событие меткой у сообщения.

    Скрываются метки, одинаковые для целой пачки сообщений: они не описывают
    конкретное сообщение и удваивают объем истории на экране.
    """
    return (normalize_number(event.xep), event.action) not in SILENT_BADGES


def compact_badge(event: XepEvent) -> str | None:
    """Знак вместо полной метки или ``None``, если событие показывается меткой.

    Знак нужен там, где расширение срабатывает на каждом сообщении: полная метка
    в этом случае одинакова у всей ленты и только удваивает ее высоту.
    """
    return COMPACT_BADGES.get((normalize_number(event.xep), event.action))


def badge_text(event: XepEvent) -> str:
    """Текст метки у сообщения вместе со значимыми деталями.

    Полный набор деталей показывается в отдельной строке события и в панели
    сырого потока. У сообщения остаются только те ключи, которые названы в
    ``BADGE_DETAILS``: кто прочитал, сколько устройств.
    """
    number = normalize_number(event.xep)
    badge = format_xep_badge(event)
    keys = BADGE_DETAILS.get((number, event.action))
    if keys is None:
        keys = tuple(event.detail)
    parts = [f"{key}={event.detail[key]}" for key in keys if key in event.detail]
    return f"{badge} {' '.join(parts)}" if parts else badge


def presence_text(event: XepEvent) -> str:
    """Текст строки состояния собеседника.

    Пустая строка означает, что состояние снимается: собеседник вернулся к
    обычному виду и писать об этом нечего.
    """
    number = normalize_number(event.xep)
    found = PRESENCE_TEXTS.get((number, event.action))
    if found is not None:
        return _(found) if found else found
    return format_action(number, event.action).lower()


def compact_legend() -> tuple[tuple[str, str], ...]:
    """Расшифровка знаков компактных меток на текущем языке."""
    return tuple((mark, _(text)) for mark, text in COMPACT_LEGEND)
