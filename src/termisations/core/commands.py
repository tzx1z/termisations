"""Реестр слэш-команд: данные, разбор строки, автодополнение и справка.

Реестр - это данные, а не цепочка условий. Каждая команда описана структурой
``CommandSpec``: имя, алиасы, сигнатура, справка, группа, ключ обработчика,
допустимые флаги, подкоманды и вид позиционных аргументов. Справка ``/help`` и
контекстное автодополнение строятся из реестра автоматически, поэтому новая
команда добавляется одной записью в ``REGISTRY`` и нигде больше.

Модуль чистый: он не импортирует ``textual``, не обращается к сети и к файловой
системе, поэтому проверяется тестами без терминала.

Решения по разбору, принятые здесь:

1. Слэш распознается только в самом начале строки. Двойной слэш экранирует:
   ``//help`` - это обычный текст сообщения, а не команда.
2. Разбор аргументов идет через ``shlex``, поэтому кавычки работают как в
   оболочке. Исключение - команды со свободным хвостом (``/send``, ``/topic``,
   ``/presence``, ``/xml filter``): у них остаток строки берется как есть, иначе
   сырой XML и текст с апострофами разбивались бы на токены.
3. Любая ошибка разбора возвращает ожидаемую сигнатуру из ``usage``, а не общий
   текст. Неизвестная команда дополнительно получает подсказку ближайшего
   совпадения через ``difflib``.
"""

import difflib
import enum
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from termisations.core.events import XmlMode
from termisations.core.i18n import LANGUAGES, N_, _, ngettext
from termisations.core.models import EMPTY_DETAIL, PresenceShow
from termisations.core.redact import redaction_summary
from termisations.xeps import normalize_number, xep_title

__all__ = [
    "DEFAULT_THEMES",
    "GROUP_TITLES",
    "NAMESPACE_HINTS",
    "REGISTRY",
    "XML_FILTER_FIELDS",
    "ArgKind",
    "CommandSpec",
    "CompletionContext",
    "FlagSpec",
    "ParsedCommand",
    "SubcommandSpec",
    "complete",
    "find",
    "help_text",
    "parse",
]


class ArgKind(enum.StrEnum):
    """Вид позиционного аргумента. Определяет, чем его дополняет ``complete``."""

    TEXT = "text"
    """Свободный текст, автодополнения нет."""

    JID = "jid"
    """Адрес контакта: дополняется из roster и открытых бесед."""

    MUC = "muc"
    """Адрес комнаты: дополняется так же, как JID."""

    NICK = "nick"
    """Ник участника комнаты."""

    XMLNS = "xmlns"
    """Пространство имен, дополняется списком известных."""

    STANZA_ID = "stanza-id"
    """Идентификатор строфы по XEP-0359, автодополнения нет."""

    PATH = "path"
    """Путь в файловой системе. Модуль чистый, к диску не обращается."""

    THEME = "theme"
    """Имя темы оформления."""

    COMMAND = "command"
    """Имя другой команды, нужно для /help."""

    RAW = "raw"
    """Сырой XML, берется хвостом строки. Автодополнения нет."""

    FILTER = "filter"
    """Выражение фильтра панели лога: дополняется именами полей из XML_FILTER_FIELDS."""


GROUP_TITLES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "connection": N_("Connection and account"),
        "conversation": N_("Conversations"),
        "roster": N_("Roster and subscriptions"),
        "debug": N_("Protocol debugging"),
        "crypto": N_("Encryption"),
        "files": N_("Files"),
        "system": N_("System"),
    }
)
"""Названия групп для /help. Порядок ключей задает порядок разделов справки."""


NAMESPACE_HINTS: Final[tuple[str, ...]] = (
    "http://jabber.org/protocol/disco#info",
    "http://jabber.org/protocol/disco#items",
    "jabber:iq:roster",
    "jabber:iq:version",
    "urn:xmpp:ping",
    "urn:xmpp:mam:2",
    "urn:xmpp:carbons:2",
    "urn:xmpp:blocking",
    "urn:xmpp:http:upload:0",
    "urn:xmpp:sm:3",
)
"""Пространства имен для автодополнения аргумента /iq. Список подсказок, не ограничение."""


XML_FILTER_FIELDS: Final[tuple[str, ...]] = ("kind", "jid", "ns", "err")
"""Поля выражения фильтра панели лога.

Единственный источник набора: отсюда его берут и автодополнение, и разбор
выражения в панели. Иначе справка, подсказка Tab и фактический разбор расходятся,
а пользователь получает молча пустую панель.
"""


DEFAULT_THEMES: Final[tuple[str, ...]] = (
    "textual-dark",
    "textual-light",
    "nord",
    "gruvbox",
    "dracula",
    "tokyo-night",
    "monokai",
    "solarized-dark",
    "solarized-light",
)
"""Запасной список тем для /theme. Приложение передает актуальный через CompletionContext."""


# Значения берутся из перечислений, чтобы список допустимых аргументов не
# расходился с моделью при ее изменении.
_PRESENCE_CHOICES: Final[tuple[str, ...]] = tuple(show.value for show in PresenceShow)
_XML_MODE_CHOICES: Final[tuple[str, ...]] = tuple(mode.value for mode in XmlMode)
_IQ_TYPE_CHOICES: Final[tuple[str, ...]] = ("get", "set", "result", "error")


@dataclass(frozen=True, slots=True)
class FlagSpec:
    """Описание флага команды."""

    name: str
    """Имя без двух минусов, например "limit"."""

    summary: str
    """Одна строка справки: английский ключ, перевод через ``_`` при выводе."""

    takes_value: bool = False
    """Забирает ли флаг следующий токен как значение."""

    placeholder: str = ""
    """Обозначение значения в сигнатуре, например "N". Текст помечается ``N_``."""

    numeric: bool = False
    """Требовать ли целое число в значении."""

    minimum: int | None = None
    """Нижняя граница числового значения включительно."""

    maximum: int | None = None
    """Верхняя граница числового значения включительно."""

    choices: tuple[str, ...] = ()
    """Закрытый набор допустимых значений. Пустой кортеж означает произвольное."""

    value_kind: "ArgKind | None" = None
    """Вид значения. Нужен дополнению, когда закрытого набора значений нет."""

    @property
    def token(self) -> str:
        """Флаг так, как его набирают: "--limit"."""
        return f"--{self.name}"

    @property
    def usage(self) -> str:
        """Флаг в сигнатуре: "--limit N" или "--raw". Обозначение значения переведено."""
        if not self.takes_value:
            return self.token
        placeholder = _(self.placeholder) if self.placeholder else _("value")
        return f"{self.token} {placeholder}"


@dataclass(frozen=True, slots=True)
class SubcommandSpec:
    """Описание подкоманды: у /xml, /omemo, /ox и /log второй токен меняет смысл."""

    name: str
    """Имя подкоманды, например "filter"."""

    usage: str
    """Полная сигнатура вместе с командой: "/xml filter <выражение>". Английский ключ."""

    summary: str
    """Одна строка справки: английский ключ, перевод через ``_`` при выводе."""

    handler_key: str
    """Ключ обработчика для роутера, например "debug.xml.filter"."""

    min_args: int = 0
    """Минимум аргументов после имени подкоманды."""

    max_args: int | None = 0
    """Максимум аргументов после имени подкоманды. None снимает верхнюю границу."""

    arg_kinds: tuple[ArgKind, ...] = ()
    """Вид аргументов по позициям, для автодополнения."""

    arg_choices: tuple[tuple[str, ...], ...] = ()
    """Закрытые наборы значений по позициям. Пустой кортеж на позиции снимает проверку."""

    flags: tuple[FlagSpec, ...] = ()
    """Флаги, допустимые только у этой подкоманды."""

    raw_tail: int | None = None
    """С какой позиции остаток строки берется как один аргумент без разбора кавычек."""


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Описание слэш-команды. Единственный источник правды для /help и автодополнения."""

    name: str
    """Имя без слэша, например "chat"."""

    usage: str
    """Сигнатура целиком: "/chat <jid>". Она же попадает в текст ошибки разбора.

    Сигнатура с текстом для человека помечена ``N_`` и выводится через ``_``.
    """

    summary: str
    """Одна строка справки: английский ключ, перевод через ``_`` при выводе."""

    handler_key: str
    """Ключ обработчика для роутера, например "chat.open"."""

    group: str
    """Группа справки: ключ из GROUP_TITLES."""

    aliases: tuple[str, ...] = ()
    """Синонимы имени, например ("msg",) для /chat."""

    xeps: tuple[str, ...] = ()
    """Номера задействованных расширений в формате "0199"."""

    flags: tuple[FlagSpec, ...] = ()
    """Допустимые флаги. Флаг вне этого набора считается ошибкой разбора."""

    sub_specs: tuple[SubcommandSpec, ...] = ()
    """Подкоманды. Первый позиционный токен сверяется с этим набором."""

    min_args: int = 0
    """Минимум позиционных аргументов."""

    max_args: int | None = None
    """Максимум позиционных аргументов. None снимает верхнюю границу."""

    arg_kinds: tuple[ArgKind, ...] = ()
    """Вид аргументов по позициям, для автодополнения."""

    arg_choices: tuple[tuple[str, ...], ...] = ()
    """Закрытые наборы значений по позициям. Пустой кортеж на позиции снимает проверку."""

    raw_tail: int | None = None
    """С какой позиции остаток строки берется как один аргумент без разбора кавычек."""

    @property
    def value_flags(self) -> tuple[str, ...]:
        """Имена флагов, забирающих следующий токен. Вычисляется из flags."""
        return tuple(flag.name for flag in self.flags if flag.takes_value)

    @property
    def subcommands(self) -> tuple[str, ...]:
        """Имена подкоманд. Вычисляется из sub_specs."""
        return tuple(sub.name for sub in self.sub_specs)

    @property
    def names(self) -> tuple[str, ...]:
        """Имя вместе с алиасами."""
        return (self.name, *self.aliases)


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """Результат разбора строки ввода.

    Подкоманда остается первым элементом ``args``: роутер, который смотрит только
    на ``spec.handler_key``, продолжает работать по ``args[0]``. Разобранное
    описание подкоманды лежит отдельно в ``subcommand``, а ``handler_key`` уже
    указывает на нее.
    """

    spec: CommandSpec | None
    """Найденное описание команды. None означает, что команда неизвестна."""

    args: tuple[str, ...] = ()
    """Позиционные аргументы, включая имя подкоманды на нулевой позиции."""

    flags: Mapping[str, str | bool] = EMPTY_DETAIL
    """Флаги: True для флага без значения, строка для флага со значением."""

    error: str | None = None
    """Текст ошибки с ожидаемой сигнатурой. None означает успешный разбор."""

    subcommand: SubcommandSpec | None = None
    """Описание подкоманды, если она распознана."""

    @property
    def ok(self) -> bool:
        """Успешен ли разбор."""
        return self.spec is not None and self.error is None

    @property
    def name(self) -> str:
        """Каноническое имя команды без слэша. Пустая строка для неизвестной команды."""
        return self.spec.name if self.spec is not None else ""

    @property
    def handler_key(self) -> str:
        """Ключ обработчика с учетом подкоманды."""
        if self.subcommand is not None:
            return self.subcommand.handler_key
        return self.spec.handler_key if self.spec is not None else ""

    @property
    def usage(self) -> str:
        """Ожидаемая сигнатура с учетом подкоманды, уже переведенная."""
        if self.subcommand is not None:
            return _(self.subcommand.usage)
        return _(self.spec.usage) if self.spec is not None else ""

    @property
    def sub_args(self) -> tuple[str, ...]:
        """Аргументы без имени подкоманды."""
        return self.args[1:] if self.subcommand is not None else self.args

    def flag_bool(self, name: str) -> bool:
        """Задан ли флаг. Флаг со значением считается заданным."""
        return self.flags.get(name, False) is not False

    def flag_str(self, name: str, default: str = "") -> str:
        """Значение флага строкой. Флаг без значения отдает default."""
        value = self.flags.get(name)
        return value if isinstance(value, str) else default

    def flag_int(self, name: str, default: int) -> int:
        """Значение флага числом. Разбор уже проверен, но default страхует вызывающего."""
        value = self.flags.get(name)
        if isinstance(value, str) and _is_int(value):
            return int(value)
        return default

    def arg(self, index: int, default: str | None = None) -> str | None:
        """Позиционный аргумент по индексу или default, если его нет."""
        if 0 <= index < len(self.args):
            return self.args[index]
        return default


@dataclass(frozen=True, slots=True)
class CompletionContext:
    """Снимок состояния, по которому строится контекстное автодополнение."""

    conversation: str | None = None
    """Активная беседа. None означает, что беседа не выбрана."""

    roster: tuple[str, ...] = ()
    """Адреса контактов из roster."""

    conversations: tuple[str, ...] = ()
    """Адреса открытых бесед."""

    nicks: tuple[str, ...] = ()
    """Ники участников активной комнаты."""

    online: frozenset[str] = frozenset()
    """Подмножество roster, которое сейчас в сети. Эти адреса идут первыми."""

    themes: tuple[str, ...] = ()
    """Известные темы оформления. Пустой кортеж включает DEFAULT_THEMES."""


# Реестр.

# Отпечаток OMEMO печатается группами по восемь знаков: восемь групп плюс
# необязательный адрес беседы. Предел рассчитан на отпечаток в том виде, в котором
# его показывает /omemo fingerprints, а копируют его именно оттуда.
_FINGERPRINT_ARGS: Final = 9

_FLAG_UNSAFE: Final = FlagSpec("unsafe", N_("disable redaction, requires typed confirmation"))
_FLAG_RAW: Final = FlagSpec("raw", N_("show the raw server response"))
_FLAG_GROUPS: Final = FlagSpec("groups", N_("group the output by roster groups"))
_FLAG_NICK: Final = FlagSpec(
    "nick",
    N_("nickname in the room"),
    takes_value=True,
    placeholder=N_("<nick>"),
    value_kind=ArgKind.NICK,
)
_FLAG_LIMIT: Final = FlagSpec(
    "limit",
    N_("how many messages to request"),
    takes_value=True,
    placeholder="N",
    numeric=True,
    minimum=1,
    maximum=2000,
)
_FLAG_BEFORE: Final = FlagSpec(
    "before", N_("page before the given stanza-id"), takes_value=True, placeholder="id"
)

_OMEMO_SUBS: Final[tuple[SubcommandSpec, ...]] = (
    SubcommandSpec(
        "status",
        "/omemo status [jid]",
        N_("conversation encryption state and number of trusted devices"),
        "crypto.omemo.status",
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    SubcommandSpec(
        "enable",
        "/omemo enable [jid]",
        N_("enable encryption in the conversation"),
        "crypto.omemo.enable",
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    SubcommandSpec(
        "disable",
        "/omemo disable [jid]",
        N_("disable encryption in the conversation"),
        "crypto.omemo.disable",
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    SubcommandSpec(
        "fingerprints",
        "/omemo fingerprints [jid]",
        N_("device fingerprints of the peer and your own"),
        "crypto.omemo.fingerprints",
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    # Отпечаток печатается группами по восемь знаков, и пользователь копирует его
    # из /omemo fingerprints как есть. Поэтому принимается и слитная запись, и
    # четыре группы через пробел, к ним необязательный адрес беседы.
    SubcommandSpec(
        "trust",
        "/omemo trust <fp> [jid]",
        N_("mark a fingerprint as trusted"),
        "crypto.omemo.trust",
        min_args=1,
        max_args=_FINGERPRINT_ARGS,
    ),
    SubcommandSpec(
        "distrust",
        "/omemo distrust <fp> [jid]",
        N_("remove trust from a fingerprint"),
        "crypto.omemo.distrust",
        min_args=1,
        max_args=_FINGERPRINT_ARGS,
    ),
    SubcommandSpec(
        "purge",
        "/omemo purge [jid]",
        N_("delete the peer's sessions and devices"),
        "crypto.omemo.purge",
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    SubcommandSpec(
        "rotate",
        "/omemo rotate",
        N_("generate a new device and publish the bundle"),
        "crypto.omemo.rotate",
    ),
)

_OX_SUBS: Final[tuple[SubcommandSpec, ...]] = (
    SubcommandSpec(
        "status", "/ox status", N_("OpenPGP state for the conversation"), "crypto.ox.status"
    ),
    SubcommandSpec(
        "enable", "/ox enable", N_("enable OpenPGP in the conversation"), "crypto.ox.enable"
    ),
    SubcommandSpec(
        "disable", "/ox disable", N_("disable OpenPGP in the conversation"), "crypto.ox.disable"
    ),
)

_XML_SUBS: Final[tuple[SubcommandSpec, ...]] = (
    SubcommandSpec(
        "filter",
        N_("/xml filter kind:<type> jid:<address> ns:<namespace> err:<yes|no> <text>"),
        N_("log panel filter, conditions joined by AND, empty expression clears the filter"),
        "debug.xml.filter",
        max_args=1,
        arg_kinds=(ArgKind.FILTER,),
        raw_tail=0,
    ),
)

_LOG_SUBS: Final[tuple[SubcommandSpec, ...]] = (
    SubcommandSpec(
        "save",
        N_("/log save <file>"),
        N_("write the log buffer to a file with mode 0600 and redaction"),
        "sys.log.save",
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.PATH,),
    ),
)


REGISTRY: Final[tuple[CommandSpec, ...]] = (
    # Соединение и аккаунт.
    CommandSpec(
        "connect",
        "/connect",
        N_("connect to the account server"),
        "conn.connect",
        "connection",
        xeps=("0368", "0388", "0386"),
        max_args=0,
    ),
    CommandSpec(
        "disconnect",
        "/disconnect",
        N_("disconnect without attempting resumption"),
        "conn.disconnect",
        "connection",
        max_args=0,
    ),
    CommandSpec(
        "reconnect",
        "/reconnect",
        N_("reconnect and try to resume the stream"),
        "conn.reconnect",
        "connection",
        xeps=("0198",),
        max_args=0,
    ),
    CommandSpec(
        "account",
        "/account",
        N_("show account settings and the current resource"),
        "conn.account",
        "connection",
        max_args=0,
    ),
    CommandSpec(
        "presence",
        N_("/presence <show> [text]"),
        N_("change presence and status text"),
        "conn.presence",
        "connection",
        min_args=1,
        arg_kinds=(ArgKind.TEXT, ArgKind.TEXT),
        arg_choices=(_PRESENCE_CHOICES,),
        raw_tail=1,
    ),
    # Беседы.
    CommandSpec(
        "chat",
        "/chat <jid>",
        N_("open a conversation with a contact"),
        "chat.open",
        "conversation",
        aliases=("msg",),
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    CommandSpec(
        "join",
        N_("/join <muc> [--nick <nick>]"),
        N_("join a room"),
        "chat.join",
        "conversation",
        aliases=("j",),
        xeps=("0045", "0421"),
        flags=(_FLAG_NICK,),
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.MUC,),
    ),
    CommandSpec(
        "leave",
        "/leave",
        N_("leave the active room"),
        "chat.leave",
        "conversation",
        aliases=("part",),
        xeps=("0045",),
        max_args=0,
    ),
    CommandSpec(
        "close",
        "/close",
        N_("close the active conversation"),
        "chat.close",
        "conversation",
        max_args=0,
    ),
    CommandSpec(
        "topic",
        N_("/topic [text]"),
        N_("show or change the room topic"),
        "chat.topic",
        "conversation",
        xeps=("0045",),
        max_args=1,
        arg_kinds=(ArgKind.TEXT,),
        raw_tail=0,
    ),
    CommandSpec(
        "nick",
        N_("/nick <nick>"),
        N_("change your nickname in the room"),
        "chat.nick",
        "conversation",
        xeps=("0045",),
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.NICK,),
    ),
    CommandSpec(
        "reply",
        N_("/reply <id> <text>"),
        N_("reply to a message with a reference to the original"),
        "chat.reply",
        "conversation",
        xeps=("0461",),
        min_args=2,
        max_args=2,
        arg_kinds=(ArgKind.STANZA_ID, ArgKind.TEXT),
        raw_tail=1,
    ),
    CommandSpec(
        "react",
        N_("/react <id> [emoji...]"),
        N_("set reactions on a message, without emoji - remove them"),
        "chat.react",
        "conversation",
        xeps=("0444",),
        min_args=1,
        max_args=None,
        arg_kinds=(ArgKind.STANZA_ID, ArgKind.TEXT),
    ),
    CommandSpec(
        "retract",
        "/retract <id>",
        N_("retract your message"),
        "chat.retract",
        "conversation",
        xeps=("0424",),
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.STANZA_ID,),
    ),
    CommandSpec(
        "clear",
        "/clear",
        N_("clear the conversation area"),
        "chat.clear",
        "conversation",
        max_args=0,
    ),
    # Roster и подписки.
    CommandSpec(
        "roster",
        "/roster [--raw] [--groups]",
        N_("show the contact list"),
        "roster.show",
        "roster",
        flags=(_FLAG_RAW, _FLAG_GROUPS),
        max_args=0,
    ),
    CommandSpec(
        "add",
        "/add <jid>",
        N_("add a contact to the roster"),
        "roster.add",
        "roster",
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    CommandSpec(
        "remove",
        "/remove <jid>",
        N_("remove a contact from the roster"),
        "roster.remove",
        "roster",
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    CommandSpec(
        "sub",
        "/sub <jid>",
        N_("request a presence subscription"),
        "roster.subscribe",
        "roster",
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    CommandSpec(
        "unsub",
        "/unsub <jid>",
        N_("cancel a presence subscription"),
        "roster.unsubscribe",
        "roster",
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    CommandSpec(
        "block",
        "/block <jid>",
        N_("block an address"),
        "roster.block",
        "roster",
        xeps=("0191",),
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    CommandSpec(
        "unblock",
        "/unblock <jid>",
        N_("unblock an address"),
        "roster.unblock",
        "roster",
        xeps=("0191",),
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    # Отладка протокола.
    CommandSpec(
        "xml",
        "/xml [in|out|both|off] [--unsafe]",
        N_("raw stream panel mode, filter and redaction removal"),
        "debug.xml",
        "debug",
        flags=(_FLAG_UNSAFE,),
        sub_specs=_XML_SUBS,
        max_args=1,
        arg_kinds=(ArgKind.TEXT,),
        arg_choices=(_XML_MODE_CHOICES,),
    ),
    CommandSpec(
        "debug",
        "/debug",
        N_("expand the log panel to full screen"),
        "debug.panel",
        "debug",
        max_args=0,
    ),
    CommandSpec(
        "send",
        "/send <raw-xml>",
        N_("send an arbitrary stanza, with confirmation"),
        "debug.send",
        "debug",
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.RAW,),
        raw_tail=0,
    ),
    CommandSpec(
        "iq",
        "/iq <to> <xmlns> [type]",
        N_("build and send an IQ of the given type"),
        "debug.iq",
        "debug",
        xeps=("0030",),
        min_args=2,
        max_args=3,
        arg_kinds=(ArgKind.JID, ArgKind.XMLNS, ArgKind.TEXT),
        arg_choices=((), (), _IQ_TYPE_CHOICES),
    ),
    CommandSpec(
        "ping",
        "/ping [jid]",
        N_("ping with RTT output, pings the server without an argument"),
        "debug.ping",
        "debug",
        xeps=("0199",),
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    CommandSpec(
        "disco",
        "/disco <jid> [node]",
        N_("query info and items from an entity"),
        "debug.disco",
        "debug",
        xeps=("0030",),
        min_args=1,
        max_args=2,
        arg_kinds=(ArgKind.JID, ArgKind.TEXT),
    ),
    CommandSpec(
        "caps",
        "/caps <jid>",
        N_("capabilities hash and decoded features"),
        "debug.caps",
        "debug",
        xeps=("0115",),
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    CommandSpec(
        "features",
        "/features",
        N_("what the server announced in the stream"),
        "debug.features",
        "debug",
        xeps=("0388", "0440"),
        max_args=0,
    ),
    CommandSpec(
        "sm",
        "/sm",
        N_("stream management state: counters, resumption, queue"),
        "debug.sm",
        "debug",
        xeps=("0198",),
        max_args=0,
    ),
    CommandSpec(
        "mam",
        "/mam <jid> [--limit N] [--before id]",
        N_("manual query of the conversation archive"),
        "debug.mam",
        "debug",
        xeps=("0313", "0359"),
        flags=(_FLAG_LIMIT, _FLAG_BEFORE),
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.JID,),
    ),
    CommandSpec(
        "trace",
        "/trace <stanza-id>",
        N_("stanza path: sending, acknowledgement, receipt, marker"),
        "debug.trace",
        "debug",
        xeps=("0359", "0184", "0333", "0198"),
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.STANZA_ID,),
    ),
    CommandSpec(
        "tls",
        "/tls",
        N_("version, cipher suite, certificate chain, channel binding"),
        "debug.tls",
        "debug",
        xeps=("0368", "0440"),
        max_args=0,
    ),
    CommandSpec(
        "stats",
        "/stats",
        N_("stanzas per second, traffic, uptime, reconnect count"),
        "debug.stats",
        "debug",
        max_args=0,
    ),
    # Шифрование.
    CommandSpec(
        "omemo",
        "/omemo status|enable|disable|fingerprints|trust <fp>|distrust <fp>|purge|rotate",
        N_("manage end-to-end encryption and device trust"),
        "crypto.omemo",
        "crypto",
        xeps=("0384",),
        sub_specs=_OMEMO_SUBS,
        min_args=1,
        max_args=2,
    ),
    CommandSpec(
        "ox",
        "/ox status|enable|disable",
        N_("manage OpenPGP encryption"),
        "crypto.ox",
        "crypto",
        xeps=("0373", "0027"),
        sub_specs=_OX_SUBS,
        min_args=1,
        max_args=1,
    ),
    # Файлы.
    CommandSpec(
        "upload",
        N_("/upload <path>"),
        N_("upload a file to the server and send the link"),
        "files.upload",
        "files",
        xeps=("0363", "0446", "0447"),
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.PATH,),
        raw_tail=0,
    ),
    CommandSpec(
        "slot",
        "/slot",
        N_("show the last received upload slot"),
        "files.slot",
        "files",
        xeps=("0363",),
        max_args=0,
    ),
    # Система.
    CommandSpec(
        "help",
        N_("/help [command]"),
        N_("command help"),
        "sys.help",
        "system",
        aliases=("?",),
        max_args=1,
        arg_kinds=(ArgKind.COMMAND,),
    ),
    CommandSpec(
        "keys",
        "/keys",
        N_("list of keyboard shortcuts"),
        "sys.keys",
        "system",
        max_args=0,
    ),
    CommandSpec(
        "theme",
        N_("/theme <name>"),
        N_("change the color theme"),
        "sys.theme",
        "system",
        min_args=1,
        max_args=1,
        arg_kinds=(ArgKind.THEME,),
    ),
    # Набор языков закрыт и не зависит от состояния приложения, поэтому он задан
    # как arg_choices: разбор сразу отклоняет неизвестный код и приводит регистр,
    # а дополнение берет тот же набор. Сигнатура собирается из LANGUAGES, чтобы
    # новый язык не требовал правки справки.
    CommandSpec(
        "lang",
        f"/lang [{'|'.join(LANGUAGES)}]",
        N_("show or change the interface language"),
        "sys.lang",
        "system",
        max_args=1,
        arg_kinds=(ArgKind.TEXT,),
        arg_choices=(LANGUAGES,),
    ),
    CommandSpec(
        "log",
        N_("/log save <file>"),
        N_("write the log to a file"),
        "sys.log",
        "system",
        sub_specs=_LOG_SUBS,
        min_args=1,
        max_args=2,
    ),
    CommandSpec(
        "quit",
        "/quit",
        N_("quit the client"),
        "sys.quit",
        "system",
        aliases=("q",),
        max_args=0,
    ),
)
"""Полный реестр команд. Порядок внутри групп определяет порядок в /help."""


def _build_index() -> Mapping[str, CommandSpec]:
    """Собрать индекс по именам и алиасам. Дубликат имени - ошибка сборки реестра."""
    index: dict[str, CommandSpec] = {}
    for spec in REGISTRY:
        for name in spec.names:
            if name in index:
                message = _("duplicate command name in registry: /{name}").format(name=name)
                raise ValueError(message)
            index[name] = spec
    return MappingProxyType(index)


_INDEX: Final[Mapping[str, CommandSpec]] = _build_index()


def find(name: str) -> CommandSpec | None:
    """Найти команду по имени или алиасу. Ведущий слэш и регистр не важны."""
    return _INDEX.get(name.lstrip("/").lower())


# Разбор.


def _is_int(value: str) -> bool:
    """Целое ли число в строке. Знак допускается, дробная часть нет."""
    candidate = value[1:] if value[:1] in {"+", "-"} else value
    return candidate.isdigit()


def _unquote(value: str) -> str:
    """Снять парные кавычки, если в них обернут весь хвост строки."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _unknown_command_error(raw_name: str) -> str:
    """Текст ошибки для неизвестной команды с подсказкой ближайшего совпадения."""
    matches = difflib.get_close_matches(
        raw_name.lower(), [spec.name for spec in REGISTRY], n=3, cutoff=0.6
    )
    if not matches:
        return _("unknown command: /{name}").format(name=raw_name)
    suggestions = ", ".join(f"/{match}" for match in matches)
    return _("unknown command: /{name}, maybe: {suggestions}").format(
        name=raw_name, suggestions=suggestions
    )


def _find_sub(spec: CommandSpec, token: str) -> SubcommandSpec | None:
    """Подкоманда по первому позиционному токену."""
    lowered = token.lower()
    for sub in spec.sub_specs:
        if sub.name == lowered:
            return sub
    return None


def _allowed_flags(spec: CommandSpec, sub: SubcommandSpec | None) -> Mapping[str, FlagSpec]:
    """Флаги команды вместе с флагами распознанной подкоманды."""
    allowed = {flag.name: flag for flag in spec.flags}
    if sub is not None:
        allowed.update({flag.name: flag for flag in sub.flags})
    return allowed


def _split_raw_tail(text: str, head_count: int) -> tuple[str, ...]:
    """Разобрать строку, у которой хвост берется как один аргумент.

    ``head_count`` - сколько токенов перед хвостом разбирается по пробелам.
    Для ``/send`` это ноль, для ``/presence`` - один: показатель присутствия
    отделяется, а текст статуса остается целым.
    """
    stripped = text.strip()
    if not stripped:
        return ()
    if head_count <= 0:
        return (_unquote(stripped),)
    pieces = stripped.split(maxsplit=head_count)
    if len(pieces) > head_count:
        return (*pieces[:head_count], _unquote(pieces[head_count]))
    return tuple(pieces)


def _split_flags(
    tokens: Sequence[str],
    allowed: Mapping[str, FlagSpec],
    usage: str,
) -> tuple[tuple[str, ...], Mapping[str, str | bool], str | None]:
    """Разделить токены на позиционные аргументы и флаги.

    Возвращает тройку: аргументы, флаги, текст ошибки. Ошибка всегда содержит
    ожидаемую сигнатуру.
    """
    args: list[str] = []
    flags: dict[str, str | bool] = {}
    index = 0
    positional_only = False
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if positional_only or not token.startswith("--"):
            args.append(token)
            continue
        if token == "--":
            # Двойной минус отдельным токеном: дальше только позиционные аргументы.
            positional_only = True
            continue
        raw_name, separator, inline = token[2:].partition("=")
        name = raw_name.lower()
        flag = allowed.get(name)
        if flag is None:
            error = _("unknown flag --{name}, expected: {usage}").format(name=name, usage=usage)
            return (), EMPTY_DETAIL, error
        if separator:
            value = inline
        elif flag.takes_value:
            if index >= len(tokens):
                error = _("flag --{name} needs a value, expected: {usage}")
                return (), EMPTY_DETAIL, error.format(name=name, usage=usage)
            value = tokens[index]
            index += 1
        else:
            flags[name] = True
            continue
        if flag.numeric:
            if not _is_int(value):
                error = _("flag --{name} needs an integer, expected: {usage}")
                return (), EMPTY_DETAIL, error.format(name=name, usage=usage)
            number = int(value)
            low, high = flag.minimum, flag.maximum
            if (low is not None and number < low) or (high is not None and number > high):
                low_text = str(low) if low is not None else "-"
                high_text = str(high) if high is not None else "-"
                error = _("value of --{name} out of range ({low} to {high}), expected: {usage}")
                return (
                    (),
                    EMPTY_DETAIL,
                    error.format(name=name, low=low_text, high=high_text, usage=usage),
                )
        if flag.choices and value not in flag.choices:
            error = _("invalid value for flag --{name}: {value}, expected: {usage}")
            return (), EMPTY_DETAIL, error.format(name=name, value=value, usage=usage)
        flags[name] = value
    return tuple(args), MappingProxyType(flags), None


def _apply_choices(
    args: tuple[str, ...], choices: Sequence[tuple[str, ...]], usage: str
) -> tuple[tuple[str, ...], str | None]:
    """Проверить позиционные аргументы по закрытым наборам значений.

    Сверка идет без учета регистра, а значение приводится к виду из набора:
    ``/presence AWAY`` дает ``away``, и обработчику не нужно нормализовать его
    повторно. Возвращает исправленные аргументы и текст ошибки.
    """
    result = list(args)
    for position, allowed in enumerate(choices):
        if not allowed or position >= len(result):
            continue
        lowered = result[position].lower()
        if lowered not in allowed:
            error = _("invalid value: {value}, expected: {usage}")
            return args, error.format(value=result[position], usage=usage)
        result[position] = lowered
    return tuple(result), None


def parse(line: str) -> ParsedCommand | None:
    """Разобрать строку ввода.

    Возвращает None, если строка не является командой: она пуста, не начинается
    со слэша или начинается с двойного слэша (экранирование). Во всех остальных
    случаях возвращается ``ParsedCommand``, и при ошибке в нем заполнено поле
    ``error`` с ожидаемой сигнатурой.
    """
    if not line.startswith("/") or line.startswith("//"):
        return None
    body = line[1:]
    # Пробел сразу после слэша - это обычный текст, а не команда.
    if not body.strip() or body[:1].isspace():
        return None

    parts = body.split(maxsplit=1)
    raw_name = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    spec = find(raw_name)
    if spec is None:
        return ParsedCommand(spec=None, error=_unknown_command_error(raw_name))

    sub = _find_sub(spec, rest.split(maxsplit=1)[0]) if rest.strip() else None
    tail = rest
    if sub is not None:
        pieces = rest.split(maxsplit=1)
        tail = pieces[1] if len(pieces) > 1 else ""

    usage = _(sub.usage if sub is not None else spec.usage)
    raw_tail = sub.raw_tail if sub is not None else spec.raw_tail
    allowed = _allowed_flags(spec, sub)

    flags: Mapping[str, str | bool] = EMPTY_DETAIL
    if raw_tail is None:
        try:
            tokens = shlex.split(tail)
        except ValueError:
            message = _("unclosed quote, expected: {usage}").format(usage=usage)
            return ParsedCommand(spec=spec, subcommand=sub, error=message)
        args, flags, error = _split_flags(tokens, allowed, usage)
        if error is not None:
            return ParsedCommand(spec=spec, subcommand=sub, error=error)
    else:
        # У команд со свободным хвостом флаги не разбираются: любой минус в
        # сыром XML или в тексте статуса должен дойти до обработчика как есть.
        args = _split_raw_tail(tail, raw_tail)

    # Первый токен команды с подкомандами должен быть подкомандой или значением
    # из закрытого набора: так /omemo fingerprint сразу получает сигнатуру.
    if sub is None and spec.sub_specs:
        head_choices = spec.arg_choices[0] if spec.arg_choices else ()
        if not args:
            if spec.min_args > 0:
                return ParsedCommand(spec=spec, error=_("expected: {usage}").format(usage=usage))
        elif args[0].lower() not in head_choices:
            message = _("invalid value: {value}, expected: {usage}")
            return ParsedCommand(spec=spec, error=message.format(value=args[0], usage=usage))

    min_args = sub.min_args if sub is not None else spec.min_args
    max_args = sub.max_args if sub is not None else spec.max_args
    if len(args) < min_args or (max_args is not None and len(args) > max_args):
        message = _("expected: {usage}").format(usage=usage)
        return ParsedCommand(spec=spec, subcommand=sub, error=message)

    choices = sub.arg_choices if sub is not None else spec.arg_choices
    args, error = _apply_choices(args, choices, usage)
    if error is not None:
        return ParsedCommand(spec=spec, subcommand=sub, error=error)

    if sub is not None:
        # Имя подкоманды возвращается нулевым аргументом: роутеру, который
        # смотрит только на spec.handler_key, этого достаточно.
        args = (sub.name, *args)
    return ParsedCommand(spec=spec, args=args, flags=flags, subcommand=sub)


# Автодополнение.


def _split_current(text: str) -> tuple[str, str]:
    """Разделить строку на неизменяемое начало и текущий, еще не дописанный токен."""
    index = max(text.rfind(" "), text.rfind("\t"))
    return text[: index + 1], text[index + 1 :]


def _quote(value: str) -> str:
    """Обернуть подстановку в кавычки, если в ней есть пробелы."""
    return shlex.quote(value) if any(char.isspace() for char in value) else value


def _jid_candidates(context: CompletionContext) -> list[str]:
    """Адреса для дополнения: открытые беседы и roster, контакты в сети первыми."""
    unique: dict[str, None] = {}
    for jid in (*context.conversations, *context.roster):
        unique.setdefault(jid, None)
    items = list(unique)
    items.sort(key=lambda jid: (jid not in context.online, jid))
    return items


def _candidates_for(kind: ArgKind, context: CompletionContext) -> list[str]:
    """Список подстановок для аргумента заданного вида."""
    if kind in {ArgKind.JID, ArgKind.MUC}:
        return _jid_candidates(context)
    if kind is ArgKind.NICK:
        return sorted(context.nicks)
    if kind is ArgKind.THEME:
        return sorted(context.themes) if context.themes else list(DEFAULT_THEMES)
    if kind is ArgKind.COMMAND:
        return [spec.name for spec in REGISTRY]
    if kind is ArgKind.XMLNS:
        return list(NAMESPACE_HINTS)
    if kind is ArgKind.FILTER:
        # Двоеточие входит в подстановку: после него сразу набирается значение.
        return [f"{field}:" for field in XML_FILTER_FIELDS]
    # PATH не дополняется: модуль не обращается к файловой системе.
    return []


def _positional_index(tokens: Sequence[str], allowed: Mapping[str, FlagSpec]) -> int:
    """Сколько позиционных аргументов уже набрано. Флаги и их значения пропускаются."""
    count = 0
    skip_value = False
    for token in tokens:
        if skip_value:
            skip_value = False
            continue
        if token.startswith("--"):
            raw_name, separator, _value = token[2:].partition("=")
            flag = allowed.get(raw_name.lower())
            skip_value = flag is not None and flag.takes_value and not separator
            continue
        count += 1
    return count


def _complete_nick(prefix: str, context: CompletionContext) -> list[str]:
    """Дополнить ник участника комнаты в обычном тексте сообщения."""
    if not context.nicks:
        return []
    head, current = _split_current(prefix)
    if not current:
        return []
    lowered = current.lower()
    return [head + nick for nick in sorted(context.nicks) if nick.lower().startswith(lowered)]


def complete(prefix: str, context: CompletionContext) -> list[str]:
    """Контекстное автодополнение строки ввода.

    Возвращает готовые подстановки целиком, а не суффиксы: виджет ввода заменяет
    текст строки на выбранный вариант. Правила выбора:

    * в начале строки дополняются имена команд;
    * после имени команды - ее подкоманды, закрытые наборы значений и флаги;
    * аргумент-адрес дополняется из roster и открытых бесед, контакты в сети идут
      первыми;
    * в обычном тексте дополняется ник участника комнаты.
    """
    if not prefix.startswith("/") or prefix.startswith("//"):
        return _complete_nick(prefix, context)

    body = prefix[1:]
    if body[:1].isspace():
        return []
    if " " not in body and "\t" not in body:
        lowered = body.lower()
        return [f"/{spec.name}" for spec in REGISTRY if spec.name.startswith(lowered)]

    head, current = _split_current(prefix)
    typed = head[1:].split()
    if not typed:
        return []
    spec = find(typed[0])
    if spec is None:
        return []

    rest_tokens = typed[1:]
    sub = _find_sub(spec, rest_tokens[0]) if rest_tokens else None
    allowed = _allowed_flags(spec, sub)
    lowered = current.lower()

    if current.startswith("--"):
        names = sorted(flag.token for flag in allowed.values())
        return [head + name for name in names if name.startswith(lowered)]

    if rest_tokens:
        last = rest_tokens[-1]
        if last.startswith("--") and "=" not in last:
            flag = allowed.get(last[2:].lower())
            if flag is not None and flag.takes_value:
                values = list(flag.choices)
                if not values and flag.value_kind is not None:
                    values = _candidates_for(flag.value_kind, context)
                return [head + value for value in values if value.startswith(current)]

    position = _positional_index(rest_tokens, allowed)
    items: list[str] = []
    if sub is None and spec.sub_specs and position == 0:
        items = [item.name for item in spec.sub_specs]
        if spec.arg_choices:
            items.extend(spec.arg_choices[0])
    else:
        if sub is not None:
            position = max(position - 1, 0)
            kinds, choices = sub.arg_kinds, sub.arg_choices
            raw_tail = sub.raw_tail
        else:
            kinds, choices = spec.arg_kinds, spec.arg_choices
            raw_tail = spec.raw_tail
        if raw_tail is not None and kinds and position >= len(kinds):
            # Хвост строки - один аргумент, сколько бы токенов в нем ни было.
            # Второе и третье условие фильтра дополняются так же, как первое.
            position = len(kinds) - 1
        if position < len(choices) and choices[position]:
            items = list(choices[position])
        elif position < len(kinds):
            items = _candidates_for(kinds[position], context)

    return [head + _quote(item) for item in items if item.lower().startswith(lowered)]


# Справка.


# Тема справки, которая не является командой: правила маскирования.
_REDACT_TOPIC: Final = "redact"


def _format_xeps(numbers: Sequence[str]) -> list[str]:
    """Строки списка расширений. Названия берутся из справочника, своих текстов нет."""
    lines: list[str] = []
    for number in numbers:
        normalized = normalize_number(number)
        lines.append(f"  XEP-{normalized}  {xep_title(normalized)}")
    return lines


def _names(spec: CommandSpec) -> str:
    """Имя команды вместе с алиасами: "/chat|msg"."""
    return f"/{spec.name}" + "".join(f"|{alias}" for alias in spec.aliases)


def _help_overview() -> str:
    """Сводка по группам.

    Сводка обязана помещаться на один экран: команда без прокрутки и счетчика
    страниц, из которой видно только хвост, бесполезна. Подробности по команде и
    раскрытие группы вынесены в аргумент, правила маскирования - в /help redact.
    """
    lines = [
        _("Slash commands: {count}. Command details: /help <command>").format(count=len(REGISTRY)),
        _("Whole group: /help <group>, redaction rules: /help {topic}").format(topic=_REDACT_TOPIC),
        _("Groups: {groups}").format(groups=" ".join(GROUP_TITLES)),
        "",
    ]
    for group, title in GROUP_TITLES.items():
        specs = [spec for spec in REGISTRY if spec.group == group]
        if not specs:
            continue
        lines.append(f"{group}  {_(title)}")
        lines.append("  " + " ".join(_names(spec) for spec in specs))
    return "\n".join(lines)


def _help_group(group: str) -> str:
    """Развернутая справка по одной группе команд."""
    specs = [spec for spec in REGISTRY if spec.group == group]
    title = _(GROUP_TITLES.get(group, group))
    count = len(specs)
    heading = ngettext("{title}: {count} command", "{title}: {count} commands", count)
    lines = [heading.format(title=title, count=count), ""]
    lines.extend(f"  {_names(spec):<18} {_(spec.summary)}" for spec in specs)
    return "\n".join(lines)


def _help_redact() -> str:
    """Действующие правила маскирования сырого потока."""
    lines = [_("Raw stream redaction is on by default:")]
    lines.extend(f"  - {rule}" for rule in redaction_summary())
    lines.append("")
    lines.append(_("Full output: /xml --unsafe, requires confirmation by typing yes."))
    return "\n".join(lines)


def _help_command(spec: CommandSpec) -> str:
    """Подробная справка по одной команде."""
    lines = [_(spec.usage), f"  {_(spec.summary)}", ""]
    lines.append(_("Group: {title}").format(title=_(GROUP_TITLES.get(spec.group, spec.group))))
    if spec.aliases:
        aliases = ", ".join(f"/{alias}" for alias in spec.aliases)
        lines.append(_("Aliases: {aliases}").format(aliases=aliases))
    if spec.sub_specs:
        lines.append(_("Subcommands:"))
        lines.extend(f"  {_(sub.usage):<32} {_(sub.summary)}" for sub in spec.sub_specs)
    flags = list(spec.flags) + [flag for sub in spec.sub_specs for flag in sub.flags]
    if flags:
        lines.append(_("Flags:"))
        lines.extend(f"  {flag.usage:<16} {_(flag.summary)}" for flag in flags)
    if spec.xeps:
        lines.append(_("Extensions:"))
        lines.extend(_format_xeps(spec.xeps))
    return "\n".join(lines)


def help_text(name: str | None = None) -> str:
    """Текст справки.

    Без аргумента отдает сводку по группам и правила маскирования, с аргументом -
    подробности по команде вместе со списком задействованных расширений.
    """
    if name is None or not name.strip():
        return _help_overview()
    topic = name.strip()
    lowered = topic.lower()
    if lowered == _REDACT_TOPIC:
        return _help_redact()
    spec = find(topic)
    if spec is None:
        if lowered in GROUP_TITLES:
            return _help_group(lowered)
        return _unknown_command_error(topic.lstrip("/"))
    # Имя roster занято и командой, и группой. Команда важнее: ее спрашивают
    # чаще. Группа дописывается следом, чтобы ответ не терял половину смысла.
    if lowered in GROUP_TITLES:
        return f"{_help_command(spec)}\n\n{_help_group(lowered)}"
    return _help_command(spec)
