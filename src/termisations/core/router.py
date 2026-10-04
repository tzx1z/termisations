"""Единый роутер слэш-команд для всех сессий.

Роутер существует потому, что владельцев у команды три: эмулятор, сетевая сессия
и слой интерфейса. Если разбор ``handler_key`` написан в каждом из них, одна и та
же команда расходится: в эмуляторе печатает текст, а в сети таблицу, или в одной
из сессий не работает вовсе, потому что разбор сверяется с ключом, которого в
реестре нет.

Разбор сделан таблицей, а не ``match``: таблицу можно перечислить. На импорте
модуля проверяется, что у каждого ключа реестра есть владелец - обработчик в
таблице или запись в ``UI_KEYS``. Новая команда без обработчика роняет импорт, а
не отвечает пользователю отказом через полгода.

Роутер не знает ни о Textual, ни о slixmpp: он вызывает операции протокола
``SessionOps``, который одинаково реализуют ``mock.MockSession`` и
``protocol.session.SlixmppSession``.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable
from xml.parsers import expat

from termisations.core.commands import REGISTRY, ParsedCommand
from termisations.core.i18n import N_, _
from termisations.core.models import Message, PresenceShow, XepEvent

__all__ = [
    "ONLINE_KEYS",
    "RETRACTED_BODY",
    "UI_KEYS",
    "SessionOps",
    "UnsupportedError",
    "is_bare_jid",
    "parse_show",
    "registry_keys",
    "replace_mark",
    "route",
    "validate_stanza",
]

# Ник в комнате по умолчанию, когда /join вызван без --nick.
DEFAULT_NICK: Final = "term"

# Сколько сообщений забирает /mam без флага --limit.
DEFAULT_MAM_LIMIT: Final = 50

# Отказ по команде, которую исполняет слой интерфейса. Текст один на все такие
# команды: у сессии нет ни буфера панели лога, ни дерева виджетов, ни тем.
# Константы с текстом помечены N_ и переводятся при выводе.
UI_REFUSAL: Final = N_("command {key} is handled by the interface layer")

# Отказ по команде, которой нужен открытый поток. Показывать данные закрытого
# соединения как текущие хуже, чем отказать.
OFFLINE_REFUSAL: Final = N_("not connected, run /connect first")

# Чем заменяется текст отозванного сообщения по XEP-0424. Запись из ленты не
# убирается: на ее месте осталась бы дыра без объяснения, а собеседник должен
# видеть, что сообщение было и его отозвали. Потребитель выводит его через _.
RETRACTED_BODY: Final = N_("[message retracted]")


class UnsupportedError(RuntimeError):
    """Операция в этой сессии невозможна. Текст пригоден для показа пользователю.

    Отличается от ошибки разбора: команда введена верно, но именно эта сессия ее
    не умеет. Причину называет сама сессия, роутер только печатает ее одним
    способом на весь проект.
    """


@runtime_checkable
class SessionOps(Protocol):
    """Операции, которые роутер вправе потребовать от любой сессии.

    Протокол структурный: общей реализации у эмулятора и адаптера нет, общее у
    них только это API. Операция, которой в сессии нет по существу, поднимает
    ``UnsupportedError`` с конкретной причиной, а не молчит и не сообщает об успехе.
    """

    # Общее.

    def feedback(self, text: str, ok: bool = True) -> None:
        """Ответ на команду в область беседы."""
        ...

    def table(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        """Табличный ответ на команду."""
        ...

    def is_online(self) -> bool:
        """Открыт ли поток к серверу."""
        ...

    def active_conversation(self) -> str | None:
        """JID активной беседы. ``None``, пока беседа не выбрана."""
        ...

    def default_target(self) -> str:
        """Цель по умолчанию для /ping и /disco: домен учетной записи."""
        ...

    # Соединение.

    async def connect(self) -> None:
        """Установить соединение."""
        ...

    async def disconnect(self) -> None:
        """Закрыть соединение без попытки возобновления."""
        ...

    async def reconnect(self) -> None:
        """Переподключиться с попыткой возобновления потока."""
        ...

    def show_account(self) -> None:
        """Сводка об учетной записи и канале."""
        ...

    def set_presence(self, show: PresenceShow, status: str) -> None:
        """Сменить присутствие."""
        ...

    def stop_session(self) -> None:
        """Завершить работу сессии."""
        ...

    # Беседы.

    def open_conversation(self, jid: str) -> None:
        """Открыть беседу и сделать ее активной."""
        ...

    def close_conversation(self, jid: str | None) -> None:
        """Закрыть беседу. ``None`` означает активную."""
        ...

    async def join_room(self, room: str, nick: str) -> None:
        """Войти в комнату."""
        ...

    def leave_room(self) -> None:
        """Выйти из активной комнаты."""
        ...

    def set_topic(self, subject: str) -> None:
        """Показать или сменить тему комнаты. Пустая строка означает показ."""
        ...

    def set_nick(self, nick: str) -> None:
        """Сменить ник в комнате."""
        ...

    async def reply_to(self, message_id: str, text: str) -> None:
        """Ответить на сообщение с указанием исходного по XEP-0461."""
        ...

    async def react_to(self, message_id: str, emoji: Sequence[str]) -> None:
        """Поставить набор реакций по XEP-0444. Пустой набор снимает реакции."""
        ...

    async def retract(self, message_id: str) -> None:
        """Отозвать свое сообщение по XEP-0424."""
        ...

    # Контакт-лист.

    def show_roster(self, *, raw: bool, groups: bool) -> None:
        """Показать контакт-лист."""
        ...

    async def roster_add(self, jid: str) -> None:
        """Добавить контакт."""
        ...

    async def roster_remove(self, jid: str) -> None:
        """Удалить контакт."""
        ...

    async def subscribe(self, jid: str) -> None:
        """Запросить подписку на присутствие."""
        ...

    async def unsubscribe(self, jid: str) -> None:
        """Отозвать подписку на присутствие."""
        ...

    async def set_blocked(self, jid: str, *, blocked: bool) -> None:
        """Заблокировать или разблокировать собеседника."""
        ...

    # Отладка протокола.

    async def ping(self, target: str) -> float | None:
        """Пинг до цели. ``None``, если ответа нет: причину называет сессия."""
        ...

    async def disco(self, jid: str, node: str) -> None:
        """Запрос возможностей."""
        ...

    async def show_caps(self, jid: str) -> None:
        """Возможности собеседника по XEP-0115."""
        ...

    def show_features(self) -> None:
        """Возможности потока, объявленные сервером."""
        ...

    def show_sm(self) -> None:
        """Состояние потокового менеджмента."""
        ...

    def show_tls(self) -> None:
        """Параметры защиты канала."""
        ...

    def show_trace(self, stanza_id: str) -> None:
        """Путь строфы по идентификатору."""
        ...

    async def fetch_mam(self, jid: str, limit: int, before: str) -> None:
        """Выборка из архива сообщений."""
        ...

    async def send_iq(self, target: str, namespace: str, kind: str) -> None:
        """Отправить IQ вручную и показать ответ."""
        ...

    # Шифрование.

    def show_omemo(self, jid: str | None) -> None:
        """Состояние OMEMO беседы."""
        ...

    def set_omemo(self, jid: str | None, *, enabled: bool) -> None:
        """Включить или выключить OMEMO в беседе."""
        ...

    def show_fingerprints(self, jid: str | None) -> None:
        """Отпечатки устройств беседы."""
        ...

    def set_trust(self, jid: str | None, fingerprint: str, *, trusted: bool) -> None:
        """Пометить отпечаток доверенным или недоверенным."""
        ...

    def omemo_purge(self, jid: str | None) -> None:
        """Удалить сессии и устройства собеседника."""
        ...

    def omemo_rotate(self, jid: str | None) -> None:
        """Сгенерировать новое устройство и опубликовать бандл."""
        ...

    def show_ox(self, action: str) -> None:
        """Состояние OpenPGP for XMPP."""
        ...

    # Файлы.

    async def upload(self, source: str) -> None:
        """Запросить слот и загрузить файл."""
        ...

    def show_slot(self) -> None:
        """Показать последний выданный слот."""
        ...


def replace_mark(message: Message, event: XepEvent, *, drop: bool = False) -> Message:
    """Заменить метку расширения у сообщения, а не добавить вторую.

    ``Message.with_xep`` повтор пропускает, и для маркеров прочтения это верно.
    Реакциям нужна именно замена: XEP-0444 передает полный набор эмодзи
    участника в каждой строфе, поэтому накопление меток показывало бы снятые
    реакции как действующие. ``drop`` убирает метку вовсе - так выражается
    снятие всех реакций.
    """
    kept = tuple(
        item
        for item in message.xeps
        if not (item.xep == event.xep and item.action == event.action and item.peer == event.peer)
    )
    return replace(message, xeps=kept if drop else (*kept, event))


def is_bare_jid(value: str) -> bool:
    """Похоже ли значение на голый JID по RFC 7622.

    Проверка одна на все команды с аргументом-адресом: без нее /add и /block
    рапортуют об успехе на любой строке.
    """
    bare = value.split("/", 1)[0]
    node, separator, domain = bare.partition("@")
    return bool(separator and node and domain and "@" not in domain and " " not in bare)


def validate_stanza(text: str) -> str | None:
    """Проверить, что строка - ровно один корректный XML-элемент.

    Возвращает текст ошибки или ``None``, если строфа пригодна к отправке. Без
    этой проверки /send разрывает поток: сервер закрывает соединение на первой же
    некорректной разметке, и это выглядит как случайный обрыв связи.

    Разбор идет без обработки пространств имен. Команда - инструмент
    отладки протокола, и строфа с префиксом вроде ``stream:`` без объявления
    ``xmlns`` здесь допустима: сервер разбирает ее в контексте открытого потока,
    где префикс уже объявлен.
    """
    payload = text.strip()
    if not payload:
        return _("stanza is empty")
    if not payload.startswith("<") or not payload.endswith(">"):
        return _("stanza must be an XML element")
    roots = 0
    depth = 0

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth, roots
        del name, attrs
        if depth == 0:
            roots += 1
        depth += 1

    def end(name: str) -> None:
        nonlocal depth
        del name
        depth -= 1

    parser = expat.ParserCreate()
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(payload, True)
    except expat.ExpatError as error:
        return _("stanza does not parse: {reason}, line {line}").format(
            reason=expat.ErrorString(error.code), line=error.lineno
        )
    if roots != 1:
        return _("stanza must be a single element")
    return None


def parse_show(value: str) -> PresenceShow | None:
    """Разобрать значение show. ``None``, если значение неизвестно."""
    try:
        return PresenceShow(value.strip().lower())
    except ValueError:
        return None


def _bare(value: str) -> str:
    """Адрес без ресурса."""
    return value.split("/", 1)[0]


def _crypto_target(ops: SessionOps, parsed: ParsedCommand) -> str | None:
    """Беседа, к которой относится команда шифрования.

    Первый аргумент подкоманды - адрес, если он похож на JID. Иначе это отпечаток
    для trust и distrust, и адресом остается активная беседа.
    """
    for value in parsed.sub_args:
        if is_bare_jid(value):
            return _bare(value)
    return ops.active_conversation()


def _fingerprint(parsed: ParsedCommand) -> str:
    """Отпечаток из аргументов подкоманды: все, что не похоже на адрес."""
    return " ".join(value for value in parsed.sub_args if not is_bare_jid(value))


def _usage_error(ops: SessionOps, parsed: ParsedCommand) -> None:
    """Отказ с ожидаемой сигнатурой вместо общего текста "неверная команда"."""
    ops.feedback(_("expected: {usage}").format(usage=parsed.usage), ok=False)


# Обработчики.


async def _connect(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/connect."""
    del parsed
    await ops.connect()


async def _disconnect(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/disconnect."""
    del parsed
    await ops.disconnect()


async def _reconnect(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/reconnect."""
    del parsed
    await ops.reconnect()


async def _account(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/account."""
    del parsed
    ops.show_account()


async def _presence(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/presence."""
    show = parse_show(parsed.arg(0) or "")
    if show is None:
        _usage_error(ops, parsed)
        return
    ops.set_presence(show, " ".join(parsed.args[1:]))


async def _quit(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/quit. Подтверждение показывает интерфейс, остановку делает сессия."""
    del parsed
    ops.feedback(_("shutting down"))
    ops.stop_session()


async def _chat_open(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/chat."""
    ops.open_conversation(parsed.arg(0) or "")


async def _chat_close(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/close."""
    ops.close_conversation(parsed.arg(0))


async def _join(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/join."""
    await ops.join_room(parsed.arg(0) or "", parsed.flag_str("nick", DEFAULT_NICK))


async def _leave(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/leave."""
    del parsed
    ops.leave_room()


async def _topic(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/topic."""
    ops.set_topic(" ".join(parsed.args))


async def _nick(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/nick."""
    ops.set_nick(parsed.arg(0) or "")


async def _reply(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/reply."""
    await ops.reply_to(parsed.arg(0) or "", parsed.arg(1) or "")


async def _react(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/react.

    Отсутствие эмодзи - это снятие реакций, а не ошибка ввода: именно так
    XEP-0444 выражает отмену.
    """
    await ops.react_to(parsed.arg(0) or "", parsed.args[1:])


async def _retract(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/retract."""
    await ops.retract(parsed.arg(0) or "")


async def _roster_show(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/roster."""
    raw = parsed.flag_bool("raw")
    groups = parsed.flag_bool("groups")
    if raw and groups:
        error = _("--raw and --groups are mutually exclusive, expected: {usage}")
        ops.feedback(error.format(usage=parsed.usage), ok=False)
        return
    ops.show_roster(raw=raw, groups=groups)


def _roster_jid(ops: SessionOps, parsed: ParsedCommand) -> str | None:
    """Адрес из первого аргумента. ``None`` означает, что отказ уже напечатан."""
    given = parsed.arg(0) or ""
    jid = _bare(given)
    if not is_bare_jid(jid):
        ops.feedback(_("invalid JID: {jid}").format(jid=given), ok=False)
        return None
    return jid


async def _roster_add(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/add."""
    jid = _roster_jid(ops, parsed)
    if jid is not None:
        await ops.roster_add(jid)


async def _roster_remove(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/remove."""
    jid = _roster_jid(ops, parsed)
    if jid is not None:
        await ops.roster_remove(jid)


async def _subscribe(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/sub."""
    jid = _roster_jid(ops, parsed)
    if jid is not None:
        await ops.subscribe(jid)


async def _unsubscribe(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/unsub."""
    jid = _roster_jid(ops, parsed)
    if jid is not None:
        await ops.unsubscribe(jid)


async def _block(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/block."""
    jid = _roster_jid(ops, parsed)
    if jid is not None:
        await ops.set_blocked(jid, blocked=True)


async def _unblock(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/unblock."""
    jid = _roster_jid(ops, parsed)
    if jid is not None:
        await ops.set_blocked(jid, blocked=False)


async def _ping(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/ping."""
    target = parsed.arg(0) or ops.default_target()
    rtt = await ops.ping(target)
    if rtt is not None:
        ops.feedback(_("pong from {target}: {rtt:.0f}ms").format(target=target, rtt=rtt))


async def _disco(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/disco."""
    await ops.disco(parsed.arg(0) or ops.default_target(), parsed.arg(1) or "")


async def _caps(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/caps."""
    await ops.show_caps(parsed.arg(0) or "")


async def _features(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/features."""
    del parsed
    ops.show_features()


async def _sm(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/sm."""
    del parsed
    ops.show_sm()


async def _tls(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/tls."""
    del parsed
    ops.show_tls()


async def _trace(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/trace."""
    ops.show_trace(parsed.arg(0) or "")


async def _mam(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/mam."""
    target = _bare(parsed.arg(0) or ops.active_conversation() or "")
    if not is_bare_jid(target):
        ops.feedback(_("invalid JID: {jid}").format(jid=parsed.arg(0) or target), ok=False)
        return
    await ops.fetch_mam(
        target, parsed.flag_int("limit", DEFAULT_MAM_LIMIT), parsed.flag_str("before", "")
    )


async def _iq(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/iq."""
    target = parsed.arg(0)
    namespace = parsed.arg(1)
    if not target or not namespace:
        _usage_error(ops, parsed)
        return
    await ops.send_iq(target, namespace, parsed.arg(2) or "get")


async def _omemo_status(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/omemo status."""
    ops.show_omemo(_crypto_target(ops, parsed))


async def _omemo_enable(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/omemo enable."""
    ops.set_omemo(_crypto_target(ops, parsed), enabled=True)


async def _omemo_disable(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/omemo disable."""
    ops.set_omemo(_crypto_target(ops, parsed), enabled=False)


async def _omemo_fingerprints(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/omemo fingerprints."""
    ops.show_fingerprints(_crypto_target(ops, parsed))


async def _omemo_trust(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/omemo trust."""
    fingerprint = _fingerprint(parsed)
    if not fingerprint:
        _usage_error(ops, parsed)
        return
    ops.set_trust(_crypto_target(ops, parsed), fingerprint, trusted=True)


async def _omemo_distrust(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/omemo distrust."""
    fingerprint = _fingerprint(parsed)
    if not fingerprint:
        _usage_error(ops, parsed)
        return
    ops.set_trust(_crypto_target(ops, parsed), fingerprint, trusted=False)


async def _omemo_purge(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/omemo purge."""
    ops.omemo_purge(_crypto_target(ops, parsed))


async def _omemo_rotate(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/omemo rotate."""
    ops.omemo_rotate(_crypto_target(ops, parsed))


async def _ox(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/ox status|enable|disable."""
    ops.show_ox(parsed.handler_key.rsplit(".", 1)[-1])


async def _upload(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/upload."""
    source = parsed.arg(0)
    if not source:
        _usage_error(ops, parsed)
        return
    await ops.upload(source)


async def _slot(ops: SessionOps, parsed: ParsedCommand) -> None:
    """/slot."""
    del parsed
    ops.show_slot()


Handler = Callable[[SessionOps, ParsedCommand], Awaitable[None]]

_TABLE: Final[Mapping[str, Handler]] = MappingProxyType(
    {
        "conn.connect": _connect,
        "conn.disconnect": _disconnect,
        "conn.reconnect": _reconnect,
        "conn.account": _account,
        "conn.presence": _presence,
        "chat.open": _chat_open,
        "chat.close": _chat_close,
        "chat.join": _join,
        "chat.leave": _leave,
        "chat.topic": _topic,
        "chat.nick": _nick,
        "chat.reply": _reply,
        "chat.react": _react,
        "chat.retract": _retract,
        "roster.show": _roster_show,
        "roster.add": _roster_add,
        "roster.remove": _roster_remove,
        "roster.subscribe": _subscribe,
        "roster.unsubscribe": _unsubscribe,
        "roster.block": _block,
        "roster.unblock": _unblock,
        "debug.ping": _ping,
        "debug.disco": _disco,
        "debug.caps": _caps,
        "debug.features": _features,
        "debug.sm": _sm,
        "debug.tls": _tls,
        "debug.trace": _trace,
        "debug.mam": _mam,
        "debug.iq": _iq,
        "crypto.omemo.status": _omemo_status,
        "crypto.omemo.enable": _omemo_enable,
        "crypto.omemo.disable": _omemo_disable,
        "crypto.omemo.fingerprints": _omemo_fingerprints,
        "crypto.omemo.trust": _omemo_trust,
        "crypto.omemo.distrust": _omemo_distrust,
        "crypto.omemo.purge": _omemo_purge,
        "crypto.omemo.rotate": _omemo_rotate,
        "crypto.ox.status": _ox,
        "crypto.ox.enable": _ox,
        "crypto.ox.disable": _ox,
        "files.upload": _upload,
        "files.slot": _slot,
        "sys.quit": _quit,
    }
)
"""Ключ реестра - обработчик сессии. Перечислимость таблицы и есть ее смысл."""

UI_KEYS: Final[frozenset[str]] = frozenset(
    {
        "chat.clear",
        "debug.panel",
        "debug.send",
        "debug.stats",
        "debug.xml",
        "debug.xml.filter",
        "sys.help",
        "sys.keys",
        "sys.lang",
        "sys.log.save",
        "sys.quit",
        "sys.theme",
    }
)
"""Ключи, которые исполняет слой интерфейса.

У него буфер панели лога, дерево виджетов и темы Textual, у сессии их нет.
``sys.quit`` стоит и здесь, и в таблице: подтверждение показывает интерфейс, а
останавливает сессию сама сессия, когда команда приходит к ней напрямую.
``debug.send`` и ``debug.stats`` интерфейс перехватывает целиком: /send требует
подтверждения, а /stats показывает еще и счетчики панели лога. ``sys.lang``
меняет язык процесса, и перерисовать уже показанные подписи может только интерфейс.

Ключа ``sys.log`` здесь нет: /log без подкоманды до роутера не доходит,
разбор заканчивается ошибкой раньше.
"""

ONLINE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "debug.ping",
        "debug.iq",
        "debug.disco",
        "debug.mam",
        "debug.tls",
        "debug.sm",
        "debug.features",
    }
)
"""Команды отладки, которым нужен живой поток.

В offline они отвечают отказом, а не устаревшими данными закрытого соединения.
Проверка стоит в роутере, а не в сессиях: иначе сетевая сессия делала бы ее в
каждом методе своим текстом.
"""


def registry_keys() -> frozenset[str]:
    """Все достижимые ключи реестра, включая ключи подкоманд.

    Ключ команды с подкомандами достижим только тогда, когда команда допустима
    без подкоманды: у /omemo, /ox и /log первый токен обязателен, и разбор такой
    строки заканчивается ошибкой раньше роутера.
    """
    keys: set[str] = set()
    for spec in REGISTRY:
        if not spec.sub_specs or spec.min_args == 0:
            keys.add(spec.handler_key)
        keys.update(sub.handler_key for sub in spec.sub_specs)
    return frozenset(keys)


def _check_coverage() -> None:
    """Убедиться, что у каждого ключа реестра есть владелец.

    Проверка на импорте, а не в тесте: команда без обработчика - это ошибка
    сборки реестра, и узнавать о ней от пользователя поздно.
    """
    known = frozenset(_TABLE) | UI_KEYS
    orphans = sorted(registry_keys() - known)
    if orphans:
        message = _("registry keys without a handler: {keys}").format(keys=", ".join(orphans))
        raise ValueError(message)
    unknown = sorted(known - registry_keys())
    if unknown:
        message = _("handlers without a registry command: {keys}").format(keys=", ".join(unknown))
        raise ValueError(message)


_check_coverage()


async def route(ops: SessionOps, parsed: ParsedCommand) -> None:
    """Исполнить разобранную команду.

    Ошибку разбора роутер не печатает: она уже известна вызывающему, и второй
    текст на одно действие дал бы две строки в беседе.
    """
    key = parsed.handler_key
    handler = _TABLE.get(key)
    if handler is None:
        ops.feedback(_(UI_REFUSAL).format(key=key), ok=False)
        return
    if key in ONLINE_KEYS and not ops.is_online():
        ops.feedback(_(OFFLINE_REFUSAL), ok=False)
        return
    try:
        await handler(ops, parsed)
    except UnsupportedError as error:
        ops.feedback(str(error), ok=False)
