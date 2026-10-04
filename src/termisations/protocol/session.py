"""Сессия на настоящем соединении: адаптер поверх slixmpp.

Реализует тот же контракт, что и эмулятор (``core.session.Session``): публикует
те же события в шину и исполняет команды через ``handle_command``. Слой
интерфейса при переключении между ``--mock`` и сетью не меняется вовсе.

Сырой поток берется не из разобранных строф, а с уровня сокета: исходящий - из
``send_raw``, входящий - из ``data_received``. Повторная сериализация разобранной
строфы меняет порядок атрибутов и кавычки, а панель показывает протокол как есть.

Расширения вне slixmpp и стандартной библиотеки:

* SASL2 (XEP-0388) и Bind 2 (XEP-0386) - в slixmpp 1.17 плагинов нет, их
  реализует свой адаптер ``protocol.sasl2``. Если сервер SASL2 не объявил или он
  выключен настройкой ``sasl2``, работает SASL1 со SCRAM-SHA-256 и SCRAM-SHA-512;
* привязка канала (XEP-0440) и SCRAM-PLUS - в стандартном ssl есть только
  ``tls-unique``, запрещенный на TLS 1.3 (RFC 9266), а ``tls-exporter`` в CPython
  не реализован. Поле привязки показывает ``n/a``;
* OMEMO (XEP-0384) - подключается через необязательный пакет slixmpp-omemo и
  только при включенной истории: ключи хранятся в базе.
"""

import asyncio
import io
import logging
import ssl
import time
import uuid
from collections.abc import Callable, Coroutine, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from mimetypes import guess_type
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, cast
from xml.etree import ElementTree

import aiohttp
from slixmpp import JID, ClientXMPP
from slixmpp.exceptions import IqError, IqTimeout, PresenceError
from slixmpp.stanza import Message as SlixMessage
from slixmpp.stanza import Presence as SlixPresence

from termisations.core import commands, router
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
from termisations.core.i18n import _
from termisations.core.metrics import LatencyTracker, RateCounter, read_rss_bytes
from termisations.core.models import (
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
    RosterItem,
    SmInfo,
    TlsInfo,
    Transport,
    XepEvent,
    humanize_bytes,
    stage_label,
    transport_label,
)
from termisations.core.redact import redact
from termisations.core.router import RETRACTED_BODY, UnsupportedError, replace_mark
from termisations.core.storage import Storage, StoredMessage
from termisations.core.trace import TraceStore
from termisations.protocol import mam
from termisations.protocol.account import Account, PasswordError, Secret, resolve_password
from termisations.protocol.caps import CapsCache, CapsEntry
from termisations.protocol.tracing import StreamSplitter, make_stanza
from termisations.protocol.transport import Endpoint, resolve, tls_info_of
from termisations.xeps import NAMESPACE_XEPS

__all__ = ["SlixmppSession"]

_log = logging.getLogger(__name__)

# Подключаемые плагины slixmpp. Порядок не важен, список читается как перечень
# поддержанных расширений.
PLUGINS: Final[tuple[str, ...]] = (
    "xep_0030",  # Service Discovery
    "xep_0115",  # Entity Capabilities
    "xep_0198",  # Stream Management
    "xep_0199",  # Ping
    "xep_0280",  # Carbons
    "xep_0313",  # MAM
    "xep_0359",  # Unique and Stable Stanza IDs
    "xep_0085",  # Chat States
    "xep_0184",  # Delivery Receipts
    "xep_0333",  # Chat Markers
    "xep_0308",  # Last Message Correction
    "xep_0203",  # Delayed Delivery
    "xep_0045",  # MUC
    "xep_0191",  # Blocking
    "xep_0421",  # Occupant ID
    "xep_0402",  # PEP Native Bookmarks
    "xep_0363",  # HTTP File Upload
    "xep_0447",  # Stateless File Sharing
    "xep_0352",  # Client State Indication
    "xep_0424",  # Message Retraction
    "xep_0444",  # Message Reactions
    "xep_0461",  # Message Replies
    "xep_0334",  # Message Processing Hints
)

# Период опроса сервера пингом. XEP-0199 здесь единственный источник задержки.
PING_PERIOD: Final = 20.0

# Период публикации состояния: статус-бар обновляется раз в секунду, чаще
# публиковать бессмысленно, а реже - заметно по замершим метрикам.
STATE_PERIOD: Final = 1.0

# Предел ожидания ответа на запрос IQ.
IQ_TIMEOUT: Final = 10.0

# Предел рукопожатия: от начала подключения до события session_start. Дальше
# каждый шаг ограничен собственным IQ_TIMEOUT, поэтому сторож снимается сразу
# после установления сессии. Значение согласовано с пределом живых тестов:
# медленный сервер успевает, мертвый адрес не держит спиннер бесконечно.
CONNECT_DEADLINE: Final = 20.0

# Предел выборки архива задает модуль protocol.mam: размер страницы и потолок
# числа страниц. Отдельной константы числа сообщений здесь нет - обход идет до
# конца архива или до потолка страниц, а не до круглого числа.

# Имена уровней доверия в slixmpp-omemo. Строки, а не enum: библиотека принимает
# именно имя уровня, и импорт ради двух констант тянул бы необязательный пакет в
# модуль, который обязан работать без него.
TRUST_LEVEL_TRUSTED: Final = "TRUSTED"
TRUST_LEVEL_DISTRUSTED: Final = "DISTRUSTED"

# Паузы между попытками автоматического переподключения, в секундах. Растут и
# упираются в потолок: сервер может быть недоступен долго, а клиент не должен ни
# сдаваться после первой попытки, ни стучаться в дверь без остановки.
RECONNECT_DELAYS: Final[tuple[float, ...]] = (1.0, 3.0, 7.0, 15.0, 30.0, 60.0, 120.0)

# Предел числа подряд идущих неудачных попыток. После него клиент останавливается
# и говорит об этом: молчаливое переподключение вторые сутки хуже отказа.
MAX_RECONNECTS: Final = 12

# Состояния набора по XEP-0085. Кортеж, а не enum: значения приходят строками и
# от собеседника, и из строки ввода, и проверять их надо в обе стороны.
CHAT_STATES: Final[tuple[str, ...]] = ("active", "composing", "paused", "inactive", "gone")

# Сколько ждать завершения закрытия соединения перед повторным подключением на
# том же экземпляре клиента.
DISCONNECT_WAIT: Final = 5.0

# Сколько ждать готовности OMEMO при остановке. Создание менеджера сессий идет
# фоновой задачей библиотеки, и закрывать базу раньше нее нельзя.
OMEMO_SHUTDOWN_WAIT: Final = 5.0

# Предел ожидания загрузки файла. Больше IQ_TIMEOUT: тело идет по HTTP, и на
# медленном канале десяти секунд не хватит даже на мегабайт.
UPLOAD_TIMEOUT: Final = 300.0

# Как часто сообщать о доле выполненного. Чаще смысла нет: статус-бар
# перерисовывается раз в секунду, а отчет на каждый килобайт занял бы шину.
PROGRESS_STEP: Final = 0.3

# Приставка действия от третьего лица по XEP-0245.
ME_PREFIX: Final = "/me "

# Сколько идентификаторов строф держится в памяти ради дедупликации. Копии с
# других устройств и записи архива приходят рядом по времени, поэтому окна в
# несколько тысяч хватает; на диске за повторы отвечает уникальный индекс.
SEEN_STANZA_IDS: Final = 4096

# Сколько сообщений беседы поднимается из истории при запуске. Больше в ленту
# все равно не поместится, а чтение всей базы задерживало бы старт.
HISTORY_LIMIT: Final = 100


class SlixmppSession:
    """Сессия поверх настоящего соединения. Контракт ``core.session.Session``."""

    SCENARIOS: ClassVar[tuple[str, ...]] = ()
    """Сценариев у настоящей сессии нет: поле есть ради общего контракта."""

    def __init__(
        self,
        bus: EventBus,
        account: Account,
        secret: Secret | None = None,
        storage: Storage | None = None,
    ) -> None:
        """Собрать сессию. Соединение не устанавливается до вызова ``run``.

        Пароль приходит готовым из ``cli``: там его можно спросить с терминала,
        пока им не владеет Textual. ``None`` означает, что сессия добудет его
        сама из неинтерактивных источников при подключении.

        Хранилище необязательно: без него история живет в памяти и теряется при выходе.
        """
        self._bus = bus
        self._account = account
        self._client: ClientXMPP | None = None
        self._secret = secret
        self._storage = storage
        self._account_row = 0
        self._omemo_ready = False
        self._omemo_error = ""
        self._reconnect_attempt = 0
        self._user_offline = False
        self._resumed = False
        self._traces = TraceStore()
        self._caps: CapsCache | None = None
        self._occupants: dict[str, tuple[str, ...]] = {}
        self._last_slot = ""
        self._nicks: dict[str, str] = {}
        self._seen_stanza_ids: dict[str, None] = {}

        self._state = ClientState(jid=account.full_jid)
        self._roster: dict[str, RosterItem] = {}
        self._conversations: dict[str, Conversation] = {}
        self._messages: dict[str, Message] = {}
        self._active: str | None = None
        self._endpoint: Endpoint | None = None

        self._stanzas_total = 0
        self._stanzas_per_sec = 0.0
        self._rate = RateCounter()
        self._latency = LatencyTracker()
        self._bytes_in = 0
        self._bytes_out = 0
        self._reconnects = 0
        self._started = time.monotonic()
        self._stage_started = time.monotonic()

        self._incoming = StreamSplitter()
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._watchdog: asyncio.Task[None] | None = None
        self._unsafe = False
        self._connected_once = False

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

    def describe(self) -> Sequence[str]:
        """Сводка о сессии для команды /account."""
        endpoint = self._endpoint.describe() if self._endpoint else _("not connected")
        verification = (
            _("certificate verification: enabled")
            if self._account.tls_verify
            else _("certificate verification: disabled")
        )
        source = self._secret.source if self._secret else _("not requested yet")
        return (
            _("account: {jid}").format(jid=self._account.full_jid),
            _("server: {endpoint}").format(endpoint=endpoint),
            _("transport: {transport}").format(transport=transport_label(self._state.transport)),
            verification,
            _("password source: {source}").format(source=source),
        )

    # Жизненный цикл.

    async def run(self) -> None:
        """Подключиться и вести сессию до остановки."""
        self._stop.clear()
        self._started = time.monotonic()
        self._spawn(self._state_loop(), "state")
        try:
            await self._load_history()
            await self._open_caps()
            await self._connect()
            await self._stop.wait()
        finally:
            await self._shutdown()

    def stop(self) -> None:
        """Остановить сессию. Повторный вызов безопасен."""
        self._stop.set()

    async def _shutdown(self) -> None:
        """Закрыть соединение и снять фоновые задачи."""
        client = self._client
        self._client = None
        if client is not None:
            # Фоновая задача ротации ключей OMEMO живет в библиотеке и после
            # отключения обращается к хранилищу. Без явной остановки она
            # упирается в закрытую базу и пишет в журнал ошибку на пустом месте.
            await self._shutdown_omemo(client)
            with _SuppressAll():
                # Незавершенная попытка подключения сама себя перепланирует.
                # Без явной отмены выход во время рукопожатия закрывает цикл
                # событий с висящей задачей.
                client.cancel_connection_attempt()
                client.disconnect()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with _SuppressAll():
                await task
        self._tasks.clear()
        storage = self._storage
        self._storage = None
        if storage is not None:
            with _SuppressAll():
                await storage.close()
        caps = self._caps
        self._caps = None
        if caps is not None:
            with _SuppressAll():
                await caps.close()
        self._set_state(stage=ConnectionStage.OFFLINE, transport=Transport.NONE)

    def _spawn(self, coro: Any, name: str) -> asyncio.Task[None]:
        """Запустить фоновую задачу и следить за ее завершением."""
        task: asyncio.Task[None] = asyncio.create_task(coro, name=f"xmpp-{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # Подключение.

    async def _connect(self, *, resume: bool = False) -> None:
        """Выбрать адрес, поднять соединение и дождаться готовности сессии."""
        # Сторож взводится до первого await: внешняя команда пароля и
        # getaddrinfo висят столько, сколько отмерит система, и стадия при этом
        # не меняется вовсе.
        self._arm_watchdog()
        if self._secret is None:
            try:
                self._secret = await resolve_password(self._account)
            except PasswordError as error:
                self._fail(str(error))
                return

        self._stage(
            ConnectionStage.RESOLVING_SRV, _("domain {domain}").format(domain=self._account.domain)
        )
        endpoint = await self._pick_endpoint()
        if endpoint is None:
            self._fail(
                _("server address for {domain} not found").format(domain=self._account.domain)
            )
            return
        self._endpoint = endpoint
        self._stage_done(
            ConnectionStage.RESOLVING_SRV,
            f"{endpoint.describe()}, {transport_label(endpoint.transport)}",
        )

        existing = self._client
        if resume and existing is not None:
            # Экземпляр клиента переживает разрыв: sm_id живет в плагине
            # XEP-0198, и новый ClientXMPP всегда шлет <enable/> вместо <resume/>.
            # Плагин выставляет end_session_on_disconnect в False, как только
            # поток управляется, поэтому состояние до сюда доживает.
            client = existing
        else:
            client = self._build_client(endpoint)
            self._client = client
        self._stage(
            ConnectionStage.TLS_HANDSHAKE,
            _("direct TLS") if endpoint.direct_tls else "STARTTLS",
        )
        if resume:
            self._reconnects += 1
            self._stage(ConnectionStage.SM_RESUME, _("attempting to resume the stream"))
        else:
            self._stage(ConnectionStage.SM_ENABLE, _("enabling stream management"))
        # Задача подключения создается отложенной фабрикой. slixmpp в
        # connect() присваивает _current_connection_attempt уже после
        # ensure_future(_connect_loop()) (xmlstream.py:444-447). При жадном
        # старте тело цикла успевает отработать до присваивания: точек
        # приостановки в нем нет, _attempt_connection видит None на проверке
        # 517-518 и выходит до create_connection. Ни сокета, ни записи в
        # журнале, ни события connection_failed.
        with _lazy_tasks():
            attempt = client.connect(endpoint.host, endpoint.port)
        if attempt.done():
            # Сюда можно попасть, только если попытка снова выполнилась
            # синхронно. Молчаливого ожидания не будет: причина названа сразу.
            _log.warning(_("connect() returned a finished task, socket not opened"))
            self._fail(_("connection finished instantly without a socket"))

    async def _pick_endpoint(self) -> Endpoint | None:
        """Адрес подключения: явно заданный в настройках или найденный по SRV."""
        account = self._account
        if account.host:
            direct = bool(account.direct_tls)
            port = account.port or (5223 if direct else 5222)
            transport = Transport.DIRECT_TLS if direct else Transport.STARTTLS
            return Endpoint(account.host, port, transport, _("launch argument"))
        endpoints = await resolve(account.domain)
        if not endpoints:
            return None
        if account.direct_tls is not None:
            wanted = Transport.DIRECT_TLS if account.direct_tls else Transport.STARTTLS
            chosen = [item for item in endpoints if item.transport is wanted]
            if chosen:
                return chosen[0]
        return endpoints[0]

    def _schedule_reconnect(self) -> None:
        """Запланировать переподключение после неожиданного обрыва.

        Своими силами, а не средствами slixmpp: ``reschedule_connection_attempt``
        работает только внутри незавершенной попытки подключения, а после
        установленного соединения библиотека не переподключается вовсе.
        """
        if self._stop.is_set() or self._user_offline or not self._connected_once:
            return
        if self._client is None:
            return
        if self._reconnect_attempt >= MAX_RECONNECTS:
            self._bus.publish(
                Notice(
                    _("reconnection stopped after {count} attempts, retry: /reconnect").format(
                        count=MAX_RECONNECTS
                    ),
                    NoticeLevel.ERROR,
                )
            )
            return
        delay = RECONNECT_DELAYS[min(self._reconnect_attempt, len(RECONNECT_DELAYS) - 1)]
        self._reconnect_attempt += 1
        self._spawn(self._reconnect_after(delay), "reconnect")

    async def _reconnect_after(self, delay: float) -> None:
        """Подождать и попробовать возобновить поток."""
        self._bus.publish(
            ConnectionStageChanged(
                ConnectionStage.OFFLINE,
                _("reconnecting in {delay:.0f} s, attempt {attempt}").format(
                    delay=delay, attempt=self._reconnect_attempt
                ),
            )
        )
        await asyncio.sleep(delay)
        if self._stop.is_set() or self._user_offline:
            return
        await self._connect(resume=True)

    def _arm_watchdog(self) -> None:
        """Взвести сторожевой таймер подключения.

        Предыдущий сторож снимается: попытка подключения у сессии всегда одна,
        и /reconnect обязан начинать отсчет заново, а не наследовать чужой.
        """
        self._disarm_watchdog()
        self._watchdog = self._spawn(self._watch_connect(), "watchdog")

    def _disarm_watchdog(self) -> None:
        """Снять сторожевой таймер: исход подключения уже известен."""
        watchdog = self._watchdog
        self._watchdog = None
        if watchdog is not None:
            watchdog.cancel()

    async def _watch_connect(self) -> None:
        """Прервать молчаливое ожидание, если рукопожатие не завершилось.

        Сторож нужен потому, что ``client.connect`` синхронный и сам ни о чем не
        сообщает, а событие ``connection_failed`` приходит не всегда: его нет ни
        при зависшем резолве, ни при сервере, который принял сокет и молчит, ни
        при попытке, снятой внутри библиотеки. Без сторожа интерфейс держит
        спиннер стадии рукопожатия бесконечно, то есть отказывает без причины и
        без выхода.
        """
        await asyncio.sleep(CONNECT_DEADLINE)
        # Ссылка снимается до _fail: иначе _fail отменит задачу, внутри которой
        # сам и выполняется.
        self._watchdog = None
        stage = self._state.stage
        if stage is ConnectionStage.READY:
            return
        client = self._client
        self._client = None
        if client is not None:
            with _SuppressAll():
                client.cancel_connection_attempt()
                client.disconnect(wait=0.0)
        self._fail(
            _(
                "connection did not complete in {deadline:.0f} s at stage {stage},"
                " retry: /reconnect"
            ).format(deadline=CONNECT_DEADLINE, stage=stage_label(stage))
        )

    def _build_client(self, endpoint: Endpoint) -> ClientXMPP:
        """Создать клиента slixmpp, повесить перехват потока и обработчики."""
        client = ClientXMPP(self._account.full_jid, self._secret.value if self._secret else "")
        # Цикл событий задается явно. slixmpp вычисляет его лениво, при первом
        # обращении к свойству loop, и в приложении Textual это обращение может
        # прийти не из того потока: клиент тогда привязывается к постороннему
        # циклу, задача подключения в него не попадает, и сессия молча висит на
        # стадии рукопожатия.
        client.loop = asyncio.get_running_loop()
        client.default_domain = self._account.domain
        # Способ защиты канала выбран заранее, и пробовать второй нельзя:
        # молчаливый откат с прямого TLS на STARTTLS - это понижение защиты.
        client.enable_direct_tls = endpoint.direct_tls
        client.enable_starttls = not endpoint.direct_tls
        if not self._account.tls_verify:
            client.ssl_context.check_hostname = False
            client.ssl_context.verify_mode = ssl.CERT_NONE

        for plugin in PLUGINS:
            client.register_plugin(plugin)
        self._register_sasl2(client)
        self._register_omemo(client)

        self._trace(client)
        client.add_event_handler("session_start", self._on_session_start)
        client.add_event_handler("session_resumed", self._on_session_resumed)
        client.add_event_handler("sm_enabled", self._on_sm_enabled)
        client.add_event_handler("sm_failed", self._on_sm_failed)
        client.add_event_handler("stanza_acked", self._on_stanza_acked)
        client.add_event_handler("carbon_received", self._on_carbon)
        client.add_event_handler("carbon_sent", self._on_carbon)
        client.add_event_handler("disconnected", self._on_disconnected)
        client.add_event_handler("failed_auth", self._on_failed_auth)
        client.add_event_handler("connection_failed", self._on_connection_failed)
        client.add_event_handler("message", self._on_message)
        client.add_event_handler("presence", self._on_presence)
        client.add_event_handler("roster_update", self._on_roster_update)
        client.add_event_handler("marker_displayed", self._on_marker)
        client.add_event_handler("groupchat_message", self._on_groupchat_message)
        client.add_event_handler("groupchat_subject", self._on_muc_subject)
        for state in CHAT_STATES:
            client.add_event_handler(f"chatstate_{state}", self._on_chat_state)
        client.add_event_handler("receipt_received", self._on_receipt)
        return client

    def _register_sasl2(self, client: ClientXMPP) -> None:
        """Подключить адаптер SASL2 и Bind 2.

        Плагина в slixmpp нет, поэтому это свой тонкий адаптер поверх точек
        расширения. Если сервер SASL2 не объявит, обработчик не сработает вовсе
        и отработает штатный SASL1 - запасной путь.
        """
        if not self._account.sasl2:
            return
        from slixmpp.plugins.base import register_plugin

        from termisations.protocol.sasl2 import XEP_0388

        register_plugin(XEP_0388)
        client.register_plugin(
            "xep_0388",
            {
                # Идентификатор установки устойчив между запусками: по нему
                # сервер отличает устройства в списке сессий.
                "user_agent_id": self._user_agent_id(),
                "resource_tag": self._account.resource,
            },
        )

    def _user_agent_id(self) -> str:
        """Устойчивый идентификатор установки для user-agent SASL2.

        Выводится из учетной записи и ресурса, а не случайный: случайное значение
        на каждый запуск добавляет запись в список сессий на сервере.
        """
        seed = f"{self._account.full_jid}|termisations".encode()
        return str(uuid.uuid5(uuid.NAMESPACE_URL, seed.decode()))

    def _register_omemo(self, client: ClientXMPP) -> None:
        """Подключить OMEMO, если пакет установлен и база доступна.

        Импорт внутри метода: slixmpp-omemo лежит в необязательной группе
        ``omemo`` и тянет нативную криптографию. Без него клиент обязан работать,
        просто без сквозного шифрования.

        Ключевой материал невозможно держать в памяти: при каждом запуске это
        новое устройство и новый бандл, а собеседник видит нового непроверенного
        участника беседы. Поэтому без базы OMEMO не подключается вовсе.
        """
        storage = self._storage
        if storage is None:
            self._omemo_error = _(
                "history is disabled, and without it OMEMO keys do not survive a restart"
            )
            return
        try:
            from slixmpp.plugins.base import register_plugin

            from termisations.protocol import omemo as omemo_module
            from termisations.protocol.omemo_storage import SqliteOmemoStorage
        except ImportError as error:
            self._omemo_error = _("slixmpp-omemo package is not installed: {error}").format(
                error=error
            )
            return
        register_plugin(omemo_module.OmemoPlugin)
        client.register_plugin(
            "xep_0384",
            {
                omemo_module.STORAGE_KEY: SqliteOmemoStorage(storage, self._account_row),
                omemo_module.REPORT_KEY: self._on_omemo_report,
                "fallback_message": _("This message is encrypted with OMEMO."),
            },
        )
        client.add_event_handler("omemo_initialized", self._on_omemo_initialized)

    async def _shutdown_omemo(self, client: ClientXMPP) -> None:
        """Остановить фоновые задачи OMEMO до закрытия хранилища."""
        plugin: Any = client.plugin.get("xep_0384", None)
        if plugin is None:
            return
        self._omemo_ready = False
        # Без привязки ресурса создание менеджера не запускалось, и ждать нечего.
        # get_session_manager в этом случае не ждет, а запускает создание: на
        # отключенном клиенте оно простояло бы весь предел ожидания.
        if not plugin.manager_requested:
            return
        # Признак готовности здесь не проверяется: создание менеджера
        # сессий идет фоновой задачей библиотеки и обращается к хранилищу. Если
        # закрыть базу, не дождавшись ее, задача упирается в закрытую базу и
        # пишет в журнал лишнюю ошибку. Ожидание ограничено: задерживать выход
        # из-за необязательной операции нельзя.
        with _SuppressAll():
            manager = await asyncio.wait_for(plugin.get_session_manager(), OMEMO_SHUTDOWN_WAIT)
            await manager.shutdown()

    def _on_omemo_report(self, action: str, detail: dict[str, str]) -> None:
        """События плагина OMEMO: слепое доверие и требование ручного решения."""
        self._xep("0384", action, **detail)
        if action == "blind-trust":
            self._bus.publish(
                Notice(
                    _(
                        "OMEMO: trust granted automatically, devices {devices} of {peers}."
                        " Verify /omemo fingerprints"
                    ).format(devices=detail.get("devices", "?"), peers=detail.get("peers", "")),
                    NoticeLevel.WARNING,
                )
            )

    def _on_omemo_initialized(self, _event: Any) -> None:
        """Бандл опубликован, ключи готовы."""
        self._omemo_ready = True
        self._omemo_error = ""
        self._xep("0384", "bundle-published")
        self._spawn(self._refresh_omemo(), "omemo-refresh")
        # OMEMO объявляет свои узлы в disco уже после старта сессии, и строка
        # проверки снова расходится. Пересчет обязателен, иначе собеседник
        # отбросит наши возможности.
        self._spawn(self._refresh_caps(), "caps-refresh")

    def _trace(self, client: ClientXMPP) -> None:
        """Перехватить сырой поток в обе стороны.

        Перехват стоит на уровне сокета, а не на разобранных строфах: панели
        нужен текст ровно такой, каким он прошел по проводу.
        """
        send_raw = client.send_raw
        data_received = client.data_received

        def traced_send(data: bytes | str, *args: Any, **kwargs: Any) -> Any:
            text = data if isinstance(data, str) else data.decode("utf-8", "replace")
            self._bytes_out += len(text.encode("utf-8"))
            for chunk in StreamSplitter().feed(text):
                self._log_stanza(chunk, Direction.OUT)
            return send_raw(data, *args, **kwargs)

        def traced_receive(data: bytes | str) -> None:
            text = data if isinstance(data, str) else data.decode("utf-8", "replace")
            self._bytes_in += len(text.encode("utf-8"))
            for chunk in self._incoming.feed(text):
                self._log_stanza(chunk, Direction.IN)
            data_received(data)

        client.send_raw = traced_send  # type: ignore[method-assign]
        client.data_received = traced_receive  # type: ignore[method-assign]

    # Публикация событий.

    def _log_stanza(self, xml: str, direction: Direction) -> None:
        """Отдать строфу в панель сырого потока."""
        if not xml.strip():
            return
        stanza = make_stanza(xml, direction, time.time())
        self._stanzas_total += 1
        self._rate.tick()
        self._bus.publish(StanzaLogged(stanza))

    def _xep(self, xep: str, action: str, **detail: str) -> None:
        """Опубликовать факт срабатывания расширения."""
        peer = detail.pop("peer", None)
        stanza_id = detail.pop("stanza_id", None)
        direction = Direction(detail.pop("direction", Direction.LOCAL.value))
        self._bus.publish(
            XepActivity(
                XepEvent(
                    xep=xep,
                    action=action,
                    direction=direction,
                    peer=peer,
                    stanza_id=stanza_id,
                    detail=dict(detail),
                )
            )
        )

    def _stage(self, stage: ConnectionStage, detail: str = "") -> None:
        """Отметить начало стадии подключения."""
        self._stage_started = time.monotonic()
        self._set_state(stage=stage)
        self._bus.publish(ConnectionStageChanged(stage, detail))

    def _stage_done(self, stage: ConnectionStage, detail: str = "") -> None:
        """Отметить завершение стадии вместе с ее длительностью."""
        duration = (time.monotonic() - self._stage_started) * 1000
        self._bus.publish(ConnectionStageChanged(stage, detail, duration))

    def _feedback(self, text: str, ok: bool = True) -> None:
        """Ответ на команду в область беседы."""
        self._bus.publish(CommandFeedback(text, ok))

    def _table(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        """Табличный ответ отладочной команды."""
        self._bus.publish(CommandTable(title, tuple(rows)))

    def _fail(self, reason: str) -> None:
        """Сообщить о невозможности продолжить и перевести стадию в ошибку."""
        self._disarm_watchdog()
        self._set_state(stage=ConnectionStage.ERROR, transport=Transport.NONE)
        self._bus.publish(ConnectionStageChanged(ConnectionStage.ERROR, reason))
        self._bus.publish(Notice(reason, NoticeLevel.ERROR))

    def _set_state(self, **changes: Any) -> None:
        """Обновить состояние и опубликовать его."""
        self._state = replace(self._state, **changes)
        self._bus.publish(StateUpdated(self._state))

    def _publish_roster(self) -> None:
        """Отдать контакт-лист интерфейсу."""
        self._bus.publish(RosterUpdated(tuple(self._roster.values())))

    def _publish_conversations(self) -> None:
        """Отдать список бесед интерфейсу и запомнить их на диске.

        Беседа переживает перезапуск целиком: без записи возвращались голые
        адреса, а название, тема, признак комнаты, тип шифрования и счетчик
        непрочитанного терялись.
        """
        self._bus.publish(ConversationsUpdated(tuple(self._conversations.values())))
        self._save_conversations()

    def _save_conversations(self) -> None:
        """Записать беседы в базу фоновой задачей."""
        storage = self._storage
        if storage is None or not self._account_row:
            return
        items = tuple(self._conversations.values())
        self._spawn(self._write_conversations(items), "conversations")

    async def _write_conversations(self, items: tuple[Conversation, ...]) -> None:
        """Запись бесед. Отказ диска переписку не рвет."""
        storage = self._storage
        if storage is None:
            return
        try:
            for item in items:
                await storage.save_conversation(self._account_row, item)
        except Exception:
            _log.exception(_("conversations not saved"))

    # Обработчики slixmpp.

    async def _on_session_start(self, _event: Any) -> None:
        """Сессия установлена: забрать roster, объявить присутствие, поднять метрики."""
        client = self._client
        if client is None:
            return
        # Рукопожатие позади: дальше каждый запрос ограничен своим IQ_TIMEOUT,
        # и общий сторож только мешал бы медленному контакт-листу.
        self._disarm_watchdog()
        self._connected_once = True
        self._stage_done(ConnectionStage.TLS_HANDSHAKE, self._tls_detail())
        self._stage_done(ConnectionStage.SASL, self._sasl_detail())
        self._stage_done(ConnectionStage.BINDING, str(client.boundjid.full))
        self._set_state(
            jid=str(client.boundjid.full),
            transport=self._endpoint.transport if self._endpoint else Transport.NONE,
            tls=self._tls_info(),
            presence_show=PresenceShow.AVAILABLE,
        )
        self._xep("0368" if self._is_direct_tls() else "0030", "direct-tls")

        self._stage(ConnectionStage.FETCHING_ROSTER, _("requesting the roster"))
        try:
            await client.get_roster(timeout=IQ_TIMEOUT)
        except (IqError, IqTimeout) as error:
            self._feedback(_("roster not received: {error}").format(error=error), ok=False)
        else:
            self._load_roster()
            self._stage_done(
                ConnectionStage.FETCHING_ROSTER,
                _("contacts: {count}").format(count=len(self._roster)),
            )

        await self._enable_carbons(client)
        self._stage(ConnectionStage.INITIAL_PRESENCE, _("announcing presence"))
        await self._refresh_caps()
        client.send_presence()
        self._stage_done(ConnectionStage.INITIAL_PRESENCE, _("presence announced"))
        self._set_csi(active=True)
        self._update_sm()
        self._stage(ConnectionStage.READY, _("session ready"))
        self._stage_done(ConnectionStage.READY, _("session ready"))
        self._reconnect_attempt = 0
        self._spawn(self._ping_loop(), "ping")
        self._spawn(self._catch_up(), "mam-catchup")
        self._spawn(self._restore_bookmarks(), "bookmarks")

    def _set_csi(self, *, active: bool) -> None:
        """Сообщить серверу состояние клиента по XEP-0352.

        Сервер в неактивном состоянии придерживает необязательные строфы вроде
        присутствия и состояний набора. На телефоне это батарея, в терминале -
        трафик и объем панели лога.
        """
        client = self._client
        if client is None or not client.is_connected():
            return
        plugin: Any = client.plugin["xep_0352"]
        with _SuppressAll():
            if active:
                plugin.send_active()
            else:
                plugin.send_inactive()
            self._xep("0352", "active" if active else "inactive", direction=Direction.OUT.value)

    async def _enable_carbons(self, client: ClientXMPP) -> None:
        """Включить копии сообщений по XEP-0280.

        Плагин зарегистрирован, но копии сервер шлет только после явного
        включения. Без него переписка с телефона в ленте не появляется вовсе, и
        выглядит это как потерянные сообщения.
        """
        try:
            await client.plugin["xep_0280"].enable(timeout=IQ_TIMEOUT)
        except (IqError, IqTimeout) as error:
            self._feedback(_("message carbons not enabled: {error}").format(error=error), ok=False)
            return
        self._xep("0280", "enabled")

    def _on_sm_enabled(self, stanza: Any) -> None:
        """Поток управляется: сервер принял <enable/>.

        Стадия публикуется здесь, а не в _on_session_start: включение SM идет
        до старта сессии, и отметить его задним числом значит показать неверную
        длительность.
        """
        self._resumed = False
        resumable = bool(stanza["resume"]) if stanza is not None else False
        self._stage_done(
            ConnectionStage.SM_ENABLE,
            _("stream managed, resumption allowed")
            if resumable
            else _("stream managed, resumption unavailable"),
        )
        self._xep("0198", "enabled", resume=str(resumable).lower())
        self._update_sm()

    def _on_session_resumed(self, stanza: Any) -> None:
        """Поток возобновлен: сессия продолжилась, а не началась заново.

        Неподтвержденные строфы ядро плагина переотправляет само, поэтому здесь
        нужно только сообщить об этом и не запускать догон архива: пропуска нет.
        """
        del stanza
        # Рукопожатие позади. Снять сторож обязательно: событие session_start
        # при возобновлении не приходит, сессия та же, и сторож, взведенный на
        # время переподключения, через свои двадцать секунд оборвал бы уже
        # работающее соединение.
        self._disarm_watchdog()
        self._resumed = True
        self._reconnect_attempt = 0
        self._connected_once = True
        self._stage_done(ConnectionStage.SM_RESUME, _("stream resumed, same session"))
        self._xep("0198", "resumed")
        self._set_state(stage=ConnectionStage.READY, presence_show=PresenceShow.AVAILABLE)
        self._bus.publish(Notice(_("stream resumed, no messages lost"), NoticeLevel.SUCCESS))
        self._update_sm()

    def _on_sm_failed(self, stanza: Any) -> None:
        """Возобновление не удалось: дальше пойдет новая сессия."""
        del stanza
        self._resumed = False
        self._stage_done(ConnectionStage.SM_RESUME, _("resumption rejected, new session"))
        self._xep("0198", "resume-failed")

    def _on_stanza_acked(self, stanza: Any) -> None:
        """Сервер подтвердил прием строфы: отметка пути для /trace."""
        message_id = str(stanza["id"] or "") if stanza is not None else ""
        if not message_id:
            return
        self._traces.note(message_id, "0198", "acked", detail=_("acked by server"))
        self._mark(message_id, DeliveryState.ACKED, "0198", "acked", "")

    async def _on_carbon(self, stanza: SlixMessage) -> None:
        """Копия сообщения с другого своего устройства по XEP-0280.

        Копия приходит отдельным событием и в обработчик message не попадает,
        поэтому разбирается тем же путем: иначе переписка с телефона в ленте не
        появляется вовсе.
        """
        received = stanza.get_plugin("carbon_received", check=True)
        inner = received or stanza.get_plugin("carbon_sent", check=True)
        if inner is None:
            return
        forwarded = inner["forwarded"]["stanza"]
        if not forwarded:
            return
        # Метка идет на самом сообщении, а не отдельной строкой события: копия и
        # так появляется в ленте, и строка "Carbon" рядом с ней не добавляла
        # ничего, кроме шума. Действие именуется как в таблице xeps, иначе
        # подпись собиралась капитализацией и теряла детали.
        await self._on_message(
            forwarded, carbon=True, carbon_action="carbon-received" if received else "carbon-sent"
        )

    def _is_direct_tls(self) -> bool:
        """Канал зашифрован с первого байта."""
        return self._endpoint is not None and self._endpoint.direct_tls

    def _socket(self) -> Any:
        """Объект сокета TLS или None, пока соединения нет."""
        client = self._client
        return getattr(client, "socket", None) if client is not None else None

    def _tls_info(self) -> TlsInfo:
        """Параметры канала для статус-бара."""
        return tls_info_of(self._socket(), verified=self._account.tls_verify)

    def _tls_detail(self) -> str:
        """Короткое описание канала для журнала подключения."""
        info = self._tls_info()
        if info.version is None:
            return _("channel not encrypted")
        checked = (
            _("certificate verified") if info.valid else _("certificate verification disabled")
        )
        return f"{info.version}, {checked}"

    def _sasl_detail(self) -> str:
        """Механизм SASL, которым прошла авторизация.

        SASL2 и SASL1 различаются явно: клиент диагностический, и пользователь
        обязан видеть, какой путь отработал, а не догадываться по косвенным
        признакам.
        """
        client = self._client
        if client is None:
            return ""
        sasl2 = client.plugin.get("xep_0388", None)
        mech = getattr(sasl2, "mech", None)
        version = "SASL2 + Bind2"
        if mech is None:
            plugin = client.plugin.get("feature_mechanisms", None)
            mech = getattr(plugin, "mech", None)
            version = "SASL1 + bind"
        name = getattr(mech, "name", None) or "SASL"
        binding = self._tls_info().channel_binding or _("no channel binding")
        return f"{version}, {name}, {binding}"

    def _load_roster(self) -> None:
        """Перенести контакт-лист slixmpp в доменные модели."""
        client = self._client
        if client is None:
            return
        self._roster.clear()
        for jid in client.client_roster:
            item = client.client_roster[jid]
            if str(jid) == str(client.boundjid.bare):
                continue
            self._roster[str(jid)] = RosterItem(
                jid=str(jid),
                name=str(item["name"] or ""),
                subscription=str(item["subscription"] or "none"),
                groups=tuple(str(group) for group in item["groups"] or ()),
            )
        self._publish_roster()
        self._xep("0030", "roster-received", count=str(len(self._roster)))

    def _on_disconnected(self, reason: Any) -> None:
        """Соединение закрыто."""
        self._incoming.reset()
        text = str(reason) if reason else _("connection closed")
        if self._state.stage is ConnectionStage.ERROR and not self._connected_once:
            # Закрытие сокета здесь - следствие уже названной причины (отказ
            # авторизации, сторожевой таймер), а не новое состояние. Перевод в
            # offline стер бы с экрана единственное объяснение отказа.
            self._set_state(
                transport=Transport.NONE,
                tls=TlsInfo(),
                sm=SmInfo(),
                presence_show=PresenceShow.OFFLINE,
            )
            self._bus.publish(ConnectionStageChanged(ConnectionStage.ERROR, text))
            return
        self._set_state(
            stage=ConnectionStage.OFFLINE,
            transport=Transport.NONE,
            tls=TlsInfo(),
            sm=SmInfo(),
            presence_show=PresenceShow.OFFLINE,
        )
        self._bus.publish(ConnectionStageChanged(ConnectionStage.OFFLINE, text))
        if self._connected_once:
            self._bus.publish(
                Notice(_("connection lost: {reason}").format(reason=text), NoticeLevel.WARNING)
            )
        self._schedule_reconnect()

    def _on_failed_auth(self, _event: Any) -> None:
        """Сервер отклонил учетные данные."""
        self._fail(_("authentication rejected: check the address and password"))

    def _on_connection_failed(self, reason: Any) -> None:
        """Соединение не установлено."""
        self._fail(_("connection not established: {reason}").format(reason=reason))

    async def _on_message(
        self, stanza: SlixMessage, *, carbon: bool = False, carbon_action: str = ""
    ) -> None:
        """Входящее сообщение.

        Обработчик асинхронный: расшифровка OMEMO ходит в сеть за бандлами и
        может дослать служебную строфу для восстановления сессии.

        ``carbon`` отмечает копию с другого своего устройства: у нее направление
        определяется адресом отправителя, а не тем, что строфа пришла к нам.
        """
        if str(stanza["type"] or "") == "groupchat":
            # Комнату разбирает свой обработчик: slixmpp поднимает и общее
            # событие message, и groupchat_message, и без этой проверки реплика
            # попадала бы в ленту дважды, второй раз от имени самой комнаты.
            return
        client = self._client
        own = str(client.boundjid.bare) if client is not None else ""
        sender = JID(stanza["from"]).bare
        outgoing = carbon and sender == own
        peer = JID(stanza["to"]).bare if outgoing else sender
        # Дедупликация по XEP-0359: то же сообщение приходит живой доставкой,
        # копией с другого устройства и записью из архива. База отсекает повтор
        # уникальным индексом, лента - этим множеством.
        archive_id = self._stanza_id_of(stanza)
        if archive_id and not self._remember_stanza_id(archive_id):
            return
        if self._apply_reference_marks(stanza, peer):
            return
        message_id = str(stanza["id"] or "") or f"in-{self._stanzas_total}"
        replace_id = (
            str(stanza["replace"]["id"] or "") if stanza.get_plugin("replace", check=True) else ""
        )
        known = self._messages.get(replace_id or message_id)
        if known is not None and known.direction is Direction.OUT and not replace_id:
            # Отражение своей же строфы: так выглядит переписка с самим собой и
            # доставка в комнату. Второй записи в ленте быть не должно, а текст
            # брать из отражения нельзя - оно старше отзыва и корректировки.
            #
            # Проверка стоит до расшифровки. Свое зашифрованное сообщение
            # расшифровать нечем: ключи в нем лежат для устройств собеседника и
            # для других своих устройств, но не для того, которое отправило.
            # Попытка дала бы в ленте "сообщение не расшифровано" на собственную
            # же реплику.
            if archive_id:
                self._remember_own_stanza_id(known, archive_id, peer)
            return
        encryption = Encryption.PLAIN
        plugin = self._omemo_plugin()
        if plugin is not None and plugin.is_encrypted(stanza):
            decrypted = await self._decrypt(stanza)
            if decrypted is None:
                return
            body, sender_fingerprint = decrypted
            encryption = Encryption.OMEMO
            self._xep(
                "0384",
                "message-decrypted",
                peer=peer,
                direction=Direction.IN.value,
                fingerprint=sender_fingerprint,
            )
        else:
            body = str(stanza["body"] or "")
        if not body:
            return
        self._ensure_conversation(peer)
        message = Message(
            message_id=replace_id or message_id,
            conversation=peer,
            sender=peer,
            body=body,
            ts=time.time(),
            direction=Direction.OUT if outgoing else Direction.IN,
            encryption=encryption,
            state=DeliveryState.SENT if outgoing else DeliveryState.RECEIVED,
            corrected=bool(replace_id),
        )
        if outgoing:
            message = replace(message, sender=own)
        if _is_action(body):
            message = message.with_xep(XepEvent("0245", "me", message.direction, peer))
        if carbon_action:
            message = message.with_xep(
                XepEvent("0280", carbon_action, Direction.IN, peer, message_id)
            )
        message = _with_extension_marks(message, stanza, peer)
        if archive_id:
            message = message.with_xep(
                XepEvent("0359", "stanza-id", Direction.IN, peer, message.message_id)
            )
            self._traces.start(message.message_id, peer)
            self._traces.set_stanza_id(message.message_id, archive_id)
        if replace_id:
            message = message.with_xep(
                XepEvent("0308", "corrected", Direction.IN, peer, replace_id)
            )
            self._messages[message.message_id] = message
            self._bus.publish(MessageUpdated(message))
            self._remember(message, update=True)
        else:
            self._messages[message.message_id] = message
            self._bus.publish(MessageAdded(message))
            self._remember(message, stanza_id=archive_id)
        if not outgoing:
            if peer == self._active:
                # Беседа открыта, значит сообщение прочитано: маркер уходит
                # сразу. Ждать переключения беседы неверно - пользователь уже
                # смотрит в эту ленту.
                self._send_marker(peer)
            else:
                self._bump_unread(peer)
        self._update_sm()

    def _on_presence(self, stanza: SlixPresence) -> None:
        """Присутствие контакта."""
        jid = JID(stanza["from"]).bare
        item = self._roster.get(jid)
        if item is None:
            return
        ptype = str(stanza["type"] or "available")
        show_value = str(stanza["show"] or "")
        if ptype == "unavailable":
            show = PresenceShow.OFFLINE
        else:
            try:
                show = PresenceShow(show_value) if show_value else PresenceShow.AVAILABLE
            except ValueError:
                show = PresenceShow.AVAILABLE
        self._roster[jid] = replace(item, show=show, status=str(stanza["status"] or ""))
        self._publish_roster()
        conversation = self._conversations.get(jid)
        if conversation is not None:
            self._conversations[jid] = replace(conversation, show=show)
            self._publish_conversations()

    def _on_roster_update(self, _event: Any) -> None:
        """Сервер прислал обновление контакт-листа."""
        self._load_roster()

    def _note_trace(self, message_id: str, xep: str, action: str, peer: str) -> None:
        """Добавить отметку пути строфы для команды /trace."""
        self._traces.note(message_id, xep, action, peer=peer)

    def _send_chat_state(self, conversation: str | None, state: str) -> None:
        """Отправить состояние набора по XEP-0085.

        Отдельной строфой, а не полем сообщения: состояние меняется, пока текст
        еще не готов, и ждать отправки незачем.
        """
        client = self._client
        if client is None or not client.is_connected() or not conversation:
            return
        if state not in CHAT_STATES:
            return
        stanza = client.make_message(mto=JID(conversation), mtype="chat")
        stanza["chat_state"] = state
        stanza.send()
        self._xep("0085", state, direction=Direction.OUT.value, peer=conversation)

    def _on_chat_state(self, stanza: SlixMessage) -> None:
        """Состояние набора собеседника.

        Публикуется событием расширения, а не сообщением: у состояния набора нет
        текста, и в ленте ему места нет - его показывает метка беседы.
        """
        peer = JID(stanza["from"]).bare
        state = str(stanza["chat_state"] or "")
        if state not in CHAT_STATES:
            return
        self._xep("0085", state, direction=Direction.IN.value, peer=peer)

    def _on_marker(self, stanza: SlixMessage) -> None:
        """Маркер прочтения по XEP-0333.

        В комнате участник называется ником, а не адресом: в детали события
        уходит именно он, иначе метка у сообщения не показывает, кто прочитал.
        """
        sender = JID(stanza["from"])
        room = str(sender.bare)
        in_room = room in self._nicks
        peer = str(sender.resource) if in_room and sender.resource else room
        target = str(stanza["displayed"]["id"] or "")
        self._mark(target, DeliveryState.DISPLAYED, "0333", "displayed", peer)

    def _on_receipt(self, stanza: SlixMessage) -> None:
        """Квитанция о доставке по XEP-0184."""
        peer = JID(stanza["from"]).bare
        target = str(stanza["receipt"] or "")
        self._mark(target, DeliveryState.RECEIVED, "0184", "receipt-received", peer)

    def _mark(
        self, message_id: str, state: DeliveryState, xep: str, action: str, peer: str
    ) -> None:
        """Отметить состояние доставки своего сообщения."""
        message = self._messages.get(message_id)
        if message is None:
            return
        self._note_trace(message_id, xep, action, peer)
        updated = message.with_state(state).with_xep(
            XepEvent(xep, action, Direction.IN, peer, message_id)
        )
        self._messages[message_id] = updated
        self._bus.publish(MessageUpdated(updated))
        self._remember(updated, update=True)

    def _apply_reference_marks(self, stanza: SlixMessage, peer: str, nick: str = "") -> bool:
        """Разобрать строфу, которая относится к другому сообщению.

        Реакция по XEP-0444 и отзыв по XEP-0424 приходят строфой без тела и
        несут идентификатор цели. Разбирать их надо до проверки тела, иначе они
        отбрасываются целиком, и своим сообщением в ленте они быть не должны:
        меняется уже показанная запись.

        Истина означает, что строфа была такой ссылкой и обычный путь для нее
        не нужен.
        """
        author = nick or peer
        reactions = stanza.get_plugin("reactions", check=True)
        if reactions is not None:
            target = str(reactions["id"] or "")
            # Набор неупорядочен, а метка не должна меняться от порядка обхода.
            emoji = " ".join(sorted(str(value) for value in reactions["values"]))
            detail = {"emoji": emoji, "nick": nick} if nick else {"emoji": emoji}
            event = XepEvent("0444", "reaction", Direction.IN, author, target, detail)
            # Пустой набор - это снятие реакций, а не пустая метка.
            if not self._replace_mark(target, event, drop=not emoji):
                self._xep(
                    "0444", "reaction", peer=author, emoji=emoji or _("cleared"), target=target
                )
            return True
        retract = stanza.get_plugin("retract", check=True)
        if retract is not None:
            target = str(retract["id"] or "")
            reason = str(retract["reason"] or "")
            if not self._retract_message(target, author, reason):
                self._xep("0424", "retracted", peer=author, target=target)
            return True
        return False

    def _remember_own_stanza_id(self, message: Message, archive_id: str, peer: str) -> None:
        """Записать архивный идентификатор у своего сообщения, вернувшегося копией."""
        updated = message.with_xep(XepEvent("0359", "stanza-id", Direction.IN, peer, archive_id))
        if updated is message:
            return
        self._messages[message.message_id] = updated
        self._traces.set_stanza_id(message.message_id, archive_id)
        self._bus.publish(MessageUpdated(updated))

    def _replace_mark(self, message_id: str, event: XepEvent, *, drop: bool = False) -> bool:
        """Заменить метку расширения у уже показанного сообщения.

        ``Message.with_xep`` повтор пропускает, а здесь нужна именно замена:
        новый набор эмодзи от того же участника отменяет прежний. Ложь означает,
        что цели нет в окне истории - тогда событие показывается строкой.
        """
        message = self._messages.get(message_id)
        if message is None:
            return False
        updated = replace_mark(message, event, drop=drop)
        self._messages[message_id] = updated
        self._bus.publish(MessageUpdated(updated))
        self._remember(updated, update=True)
        return True

    def _retract_message(self, message_id: str, author: str, reason: str) -> bool:
        """Заменить текст отозванного сообщения пометкой."""
        message = self._messages.get(message_id)
        if message is None:
            return False
        # Пометка переводится при отзыве и в таком виде уходит в историю: в базе
        # остается язык, на котором сообщение было отозвано.
        retracted = _(RETRACTED_BODY)
        body = f"{retracted}: {reason}" if reason else retracted
        updated = replace(message, body=body).with_xep(
            XepEvent("0424", "retracted", Direction.IN, author, message_id)
        )
        self._messages[message_id] = updated
        self._bus.publish(MessageUpdated(updated))
        self._remember(updated, update=True)
        return True

    def _ensure_conversation(self, jid: str, *, activate: bool = False) -> Conversation:
        """Создать беседу, если ее еще нет."""
        conversation = self._conversations.get(jid)
        if conversation is None:
            item = self._roster.get(jid)
            conversation = Conversation(
                jid=jid,
                title=(item.display_name if item else jid),
                show=item.show if item else PresenceShow.OFFLINE,
            )
            self._conversations[jid] = conversation
            self._publish_conversations()
        if activate or self._active is None:
            self._activate(jid)
        return conversation

    def _activate(self, jid: str) -> None:
        """Сделать беседу активной, обнулить непрочитанное и отметить прочтение."""
        self._active = jid
        conversation = self._conversations.get(jid)
        if conversation is not None and conversation.unread:
            self._conversations[jid] = replace(conversation, unread=0)
            self._publish_conversations()
            # Маркер прочтения уходит ровно тогда, когда пользователь увидел
            # накопившееся: обнуление счетчика и есть момент прочтения.
            self._send_marker(jid)
        self._bus.publish(ActiveConversationChanged(jid))

    def _send_marker(self, jid: str) -> None:
        """Маркер прочтения последнего сообщения беседы по XEP-0333."""
        client = self._client
        if client is None or not client.is_connected():
            return
        last = next(
            (
                message
                for message in reversed(list(self._messages.values()))
                if message.conversation == jid and message.direction is Direction.IN
            ),
            None,
        )
        if last is None:
            return
        with _SuppressAll():
            client.plugin["xep_0333"].send_marker(JID(jid), last.message_id, "displayed")
        self._xep(
            "0333", "displayed", direction=Direction.OUT.value, peer=jid, stanza_id=last.message_id
        )

    def _bump_unread(self, jid: str) -> None:
        """Увеличить счетчик непрочитанного у неактивной беседы."""
        if jid == self._active:
            return
        conversation = self._conversations.get(jid)
        if conversation is not None:
            self._conversations[jid] = replace(conversation, unread=conversation.unread + 1)
            self._publish_conversations()

    # Комнаты.

    def _publish_occupants(self, room: str) -> None:
        """Отдать состав комнаты интерфейсу.

        Список нужен автодополнению ника и заголовку беседы: сессия
        единственная, кто его знает. Источник - состояние плагина XEP-0045: он
        ведет его сам по присутствию участников.
        """
        client = self._client
        known = client.plugin["xep_0045"].rooms.get(None, {}) if client is not None else {}
        ordered = tuple(sorted(str(item) for item in known.get(JID(room), {})))
        if self._occupants.get(room) == ordered:
            return
        self._occupants[room] = ordered
        self._bus.publish(OccupantsUpdated(room, ordered))

    def _on_muc_presence(self, presence: SlixPresence) -> None:
        """Вход и выход участника комнаты.

        Подписка идет на покомнатное событие ``muc::<room>::presence``, а не на
        общее ``groupchat_presence``: общее плагин поднимает ДО того, как
        обновит состав, и обработчик видел бы состояние до изменения. Смена ника
        при этом выглядела как уход участника.
        """
        self._publish_occupants(str(JID(presence["from"]).bare))

    async def _on_groupchat_message(self, stanza: SlixMessage) -> None:
        """Сообщение комнаты.

        Разбирается отдельно от личных: отправитель здесь - участник, а не
        собеседник, и беседой считается сама комната.
        """
        room = str(JID(stanza["from"]).bare)
        nick = str(JID(stanza["from"]).resource)
        body = str(stanza["body"] or "")
        archive_id = self._stanza_id_of(stanza)
        if archive_id and not self._remember_stanza_id(archive_id):
            return
        if self._apply_reference_marks(stanza, room, nick):
            # Реакция и отзыв приходят от своего же ника тоже: свою реакцию
            # показать надо, в отличие от отражения своего сообщения.
            return
        if not body or nick == self._nicks.get(room):
            # Свое сообщение приходит отражением: в ленте оно уже есть.
            return
        message_id = str(stanza["id"] or "") or f"muc-{self._stanzas_total}"
        self._ensure_conversation(room)
        message = Message(
            message_id=message_id,
            conversation=room,
            sender=nick or room,
            body=body,
            ts=time.time(),
            direction=Direction.IN,
            state=DeliveryState.RECEIVED,
        )
        occupant = stanza.get_plugin("occupant-id", check=True)
        if occupant is not None:
            # XEP-0421 дает устойчивый идентификатор участника: ник в комнате
            # сменить можно, а этот - нет, и по нему собеседник узнаваем.
            message = message.with_xep(
                XepEvent("0421", "occupant-id", Direction.IN, nick, str(occupant["id"] or ""))
            )
        if _is_action(body):
            message = message.with_xep(XepEvent("0245", "me", Direction.IN, nick))
        self._messages[message_id] = message
        self._bus.publish(MessageAdded(message))
        self._remember(message, stanza_id=archive_id)
        if room != self._active:
            self._bump_unread(room)
        self._update_sm()

    def _on_muc_subject(self, stanza: SlixMessage) -> None:
        """Смена темы комнаты."""
        room = str(JID(stanza["from"]).bare)
        subject = str(stanza["subject"] or "")
        conversation = self._conversations.get(room)
        if conversation is None:
            return
        self._conversations[room] = replace(conversation, topic=subject)
        self._publish_conversations()
        self._xep("0045", "subject", direction=Direction.IN.value, peer=room, subject=subject)

    async def _save_bookmark(self, room: str, nick: str) -> None:
        """Записать закладку комнаты в PEP по XEP-0402.

        Закладка переживает перезапуск клиента и видна другим устройствам: без
        нее список комнат приходится набирать заново на каждом.
        """
        client = self._client
        if client is None or not client.is_connected():
            return
        # Плагин XEP-0402 в slixmpp только объявляет строфы, своего API у него
        # нет. Публикация идет через PubSub напрямую: это ровно то, что делает
        # само расширение, и лишней прослойки не нужно.
        from slixmpp.plugins.xep_0402 import stanza as bookmarks

        payload = bookmarks.Conference()
        payload["name"] = room.split("@", 1)[0]
        payload["autojoin"] = True
        if nick:
            payload["nick"] = nick
        try:
            pubsub: Any = client.plugin["xep_0060"]
            await pubsub.publish(
                client.boundjid.bare,
                bookmarks.NS,
                id=room,
                payload=payload.xml,
                timeout=IQ_TIMEOUT,
            )
        except (IqError, IqTimeout) as error:
            _log.debug(_("bookmark %s not saved: %s"), room, error)
            return
        self._xep("0402", "bookmark-saved", peer=room)

    async def _drop_bookmark(self, room: str) -> None:
        """Убрать закладку комнаты: выход означает, что входить заново не нужно."""
        client = self._client
        if client is None or not client.is_connected():
            return
        from slixmpp.plugins.xep_0402 import stanza as bookmarks

        with _SuppressAll():
            pubsub: Any = client.plugin["xep_0060"]
            await pubsub.retract(client.boundjid.bare, bookmarks.NS, room, timeout=IQ_TIMEOUT)
            self._xep("0402", "bookmark-removed", peer=room)

    async def _restore_bookmarks(self) -> None:
        """Войти в комнаты, помеченные автоматическим входом.

        Закладка с ``autojoin`` - это явно выраженное желание пользователя быть
        в комнате. Игнорировать его значит терять сообщения комнаты молча.
        """
        client = self._client
        if client is None or not client.is_connected():
            return
        from slixmpp.plugins.xep_0402 import stanza as bookmarks

        try:
            pubsub: Any = client.plugin["xep_0060"]
            answer = await pubsub.get_items(client.boundjid.bare, bookmarks.NS, timeout=IQ_TIMEOUT)
        except (IqError, IqTimeout) as error:
            _log.debug(_("bookmarks not received: %s"), error)
            return
        for item in answer["pubsub"]["items"]:
            conference = item["conference"]
            if not conference["autojoin"]:
                continue
            room = str(item["id"] or "")
            if not room or room in self._nicks:
                continue
            with _SuppressAll():
                await self._enter_room(room, str(conference["nick"] or ""), activate=False)

    # Возможности собеседников.

    async def _refresh_caps(self) -> None:
        """Пересчитать собственную строку проверки по XEP-0115.

        Плагины дописывают свои возможности в disco по ходу подключения: Carbons
        объявляют себя на привязке ресурса, OMEMO - после публикации бандла.
        Строка проверки, посчитанная раньше, перестает сходиться, и собеседник,
        пересчитав ее, отбрасывает наши возможности целиком. Выглядит это так,
        будто кэш XEP-0115 не работает ни у кого.
        """
        client = self._client
        if client is None or not client.is_connected():
            return
        caps: Any = client.plugin["xep_0115"]
        with _SuppressAll():
            await caps.update_caps()
            self._xep("0115", "caps-updated", ver=str(await caps.get_verstring() or ""))

    async def _open_caps(self) -> None:
        """Открыть кэш возможностей. Его недоступность не мешает работе."""
        try:
            self._caps = await CapsCache.open()
        except Exception:
            _log.exception(_("capabilities cache not opened"))
            self._caps = None

    async def _caps_of(self, jid: str) -> tuple[CapsEntry | None, bool]:
        """Возможности собеседника и признак того, что они взяты из кэша.

        Хэш берется у плагина XEP-0115: он собирает его из присутствия, которое
        и так приходит. Совпал с записанным - круг disco не нужен вовсе, и это
        единственная причина, по которой кэш существует.
        """
        client = self._client
        if client is None or not client.is_connected():
            self._feedback(_("no connection"), ok=False)
            return None, False
        verstring = await self._verstring_of(jid)
        cache = self._caps
        if cache is not None and verstring:
            known = await cache.verstring_of(jid)
            if known == verstring:
                entry = await cache.get(verstring)
                if entry is not None:
                    self._xep("0115", "cache-hit", peer=jid, ver=verstring)
                    return entry, True
        try:
            info = await client.plugin["xep_0030"].get_info(jid=JID(jid), timeout=IQ_TIMEOUT)
        except (IqError, IqTimeout) as error:
            self._feedback(_("disco to {jid}: {error}").format(jid=jid, error=error), ok=False)
            return None, False
        payload = info["disco_info"]
        entry = CapsEntry(
            verstring,
            tuple(sorted(str(item) for item in payload["features"])),
            tuple(f"{item[0]}/{item[1]}" for item in sorted(payload["identities"])),
        )
        self._xep("0115", "cache-miss", peer=jid, ver=verstring or _("none"))
        if cache is not None and verstring:
            with _SuppressAll():
                await cache.put(verstring, entry.features, entry.identities)
                await cache.bind(jid, verstring)
        return entry, False

    async def _verstring_of(self, jid: str) -> str:
        """Строка проверки собеседника по XEP-0115. Пустая, если ее нет."""
        client = self._client
        if client is None:
            return ""
        try:
            value = await client.plugin["xep_0115"].get_verstring(jid)
        except Exception:
            return ""
        return "" if value is None else str(value)

    # Архив.

    async def _catch_up(self) -> None:
        """Догнать архив по беседам, у которых есть курсор.

        После возобновления потока догон не нужен: пропуска нет, сервер досылает
        то, что не успел. После новой сессии пропуск есть всегда, и его размер
        равен времени, которое клиент был выключен.
        """
        storage = self._storage
        if storage is None or self._resumed:
            return
        try:
            cursors = await storage.mam_cursors(self._account_row)
        except Exception:
            _log.exception(_("archive cursors not read"))
            return
        for jid, last_id, _complete in cursors:
            if self._stop.is_set():
                return
            await self._sync_archive(jid, mam.Cursor(last_id=last_id), quiet=True)

    async def _sync_archive(self, jid: str, cursor: mam.Cursor, *, quiet: bool = False) -> int:
        """Постраничная выборка архива беседы от курсора. Возвращает число сообщений.

        Страницы считаются и показываются: пользователь должен видеть, что идет
        работа, а не замерший спиннер. Потолок числа страниц защищает от сервера,
        который отдает одну и ту же страницу бесконечно.
        """
        client = self._client
        if client is None or not client.is_connected():
            return 0
        received = 0
        pages = 0
        while pages < mam.MAX_PAGES:
            args = mam.query_args(cursor)
            try:
                answer = await client.plugin["xep_0313"].retrieve(
                    with_jid=JID(jid), rsm=args, timeout=int(IQ_TIMEOUT)
                )
            except (IqError, IqTimeout) as error:
                if not quiet:
                    self._feedback(
                        _("archive {jid} unavailable: {error}").format(jid=jid, error=error),
                        ok=False,
                    )
                return received
            pages += 1
            received += await self._store_page(jid, answer)
            page = mam.parse_fin(answer["mam_fin"])
            cursor, more = mam.advance(cursor, page)
            if not quiet:
                # Общее число страниц заранее неизвестно: сервер отдает их по
                # одной, пока не скажет, что архив кончился.
                self._progress(
                    _("archive {jid}").format(jid=jid),
                    received,
                    0,
                    _("page {page}, messages {count}").format(page=pages, count=received),
                )
            if not more:
                break
        await self._save_cursor(jid, cursor)
        self._xep("0313", "synced", peer=jid, pages=str(pages), messages=str(received))
        return received

    async def _store_page(self, jid: str, answer: Any) -> int:
        """Разобрать страницу архива и положить сообщения в ленту и в базу."""
        stored = 0
        for wrapper in answer["mam"]["results"]:
            result = wrapper["mam_result"]
            archive_id = str(result["id"] or "")
            inner = result["forwarded"]["stanza"]
            if not inner:
                continue
            body = str(inner["body"] or "")
            if not body:
                continue
            if archive_id and not self._remember_stanza_id(archive_id):
                continue
            sender = JID(inner["from"]).bare
            client = self._client
            own = str(client.boundjid.bare) if client is not None else ""
            message = Message(
                message_id=str(inner["id"] or "") or archive_id,
                conversation=jid,
                sender=sender,
                body=body,
                ts=_archive_time(result),
                direction=Direction.OUT if sender == own else Direction.IN,
                state=DeliveryState.RECEIVED,
            ).with_xep(XepEvent("0313", "archived", Direction.IN, jid, archive_id))
            self._ensure_conversation(jid)
            self._messages[message.message_id] = message
            self._bus.publish(MessageAdded(message))
            self._remember(message, stanza_id=archive_id)
            stored += 1
        return stored

    async def _save_cursor(self, jid: str, cursor: mam.Cursor) -> None:
        """Записать курсор синхронизации архива."""
        storage = self._storage
        if storage is None or not cursor.last_id:
            return
        try:
            await storage.save_mam_cursor(
                self._account_row, jid, cursor.last_id, complete=cursor.complete
            )
        except Exception:
            _log.exception(_("archive cursor not saved"))

    # Идентификаторы строф.

    @staticmethod
    def _stanza_id_of(stanza: SlixMessage) -> str:
        """``stanza-id``, присвоенный сервером. Пустая строка, если его нет.

        Читается через ``get_plugin(check=True)``: обычная индексация создала бы
        пустой элемент в строфе, которую мы потом показываем в панели лога.
        """
        element = stanza.get_plugin("stanza_id", check=True)
        return "" if element is None else str(element["id"] or "")

    def _remember_stanza_id(self, stanza_id: str) -> bool:
        """Отметить идентификатор увиденным. ``False`` означает повтор.

        Кольцо по числу записей: копии и записи архива приходят рядом по времени,
        и хранить весь архив в памяти ради дедупликации незачем - на диске за это
        отвечает уникальный индекс.
        """
        if stanza_id in self._seen_stanza_ids:
            return False
        self._seen_stanza_ids[stanza_id] = None
        while len(self._seen_stanza_ids) > SEEN_STANZA_IDS:
            self._seen_stanza_ids.pop(next(iter(self._seen_stanza_ids)))
        return True

    # OMEMO.

    def _omemo_plugin(self) -> Any:
        """Плагин OMEMO или None, если он не подключен."""
        client = self._client
        if client is None:
            return None
        return client.plugin.get("xep_0384", None)

    def _omemo_unavailable(self) -> str:
        """Причина, по которой OMEMO недоступен. Пустая строка означает, что доступен."""
        if self._omemo_plugin() is None:
            return self._omemo_error or _("OMEMO is not loaded")
        if not self._omemo_ready:
            return _("OMEMO keys are not ready yet, wait for the bundle to be published")
        return ""

    async def _omemo_devices(self, jid: str) -> Any:
        """Список устройств собеседника вместе со своими."""
        plugin = self._omemo_plugin()
        if plugin is None:
            return frozenset()
        manager = await plugin.get_session_manager()
        return await manager.get_device_information(jid)

    async def _refresh_omemo(self, jid: str | None = None) -> None:
        """Пересчитать состояние шифрования бесед и отдать его в шину.

        Счетчик доверенных устройств - свойство беседы: при нескольких открытых
        беседах величина от одной из них в заголовке другой просто неверна.
        """
        plugin = self._omemo_plugin()
        if plugin is None or not self._omemo_ready:
            return
        from termisations.protocol.omemo import fingerprint, trust_summary

        try:
            manager = await plugin.get_session_manager()
            own, _others = await manager.get_own_device_information()
            own_fingerprint = fingerprint(own.identity_key)
        except Exception:
            _log.exception(_("OMEMO state not collected"))
            return
        targets = [jid] if jid is not None else list(self._conversations)
        changed = False
        for target in targets:
            conversation = self._conversations.get(target)
            if conversation is None:
                continue
            try:
                devices = await manager.get_device_information(target)
            except Exception:
                _log.exception(_("devices of %s not received"), target)
                continue
            trusted, total = trust_summary(devices)
            info = OmemoInfo(
                enabled=conversation.encryption is Encryption.OMEMO,
                trusted_devices=trusted,
                total_devices=total,
                own_fingerprint=own_fingerprint,
            )
            if info != conversation.omemo:
                self._conversations[target] = replace(conversation, omemo=info)
                changed = True
        if changed:
            self._publish_conversations()

    def _omemo_wanted(self, conversation: str) -> bool:
        """Включен ли OMEMO в этой беседе."""
        item = self._conversations.get(conversation)
        return item is not None and item.encryption is Encryption.OMEMO

    async def _encrypt(self, stanza: SlixMessage, peer: str) -> SlixMessage | None:
        """Зашифровать сообщение. ``None`` означает, что причина уже названа."""
        plugin = self._omemo_plugin()
        if plugin is None:
            self._feedback(self._omemo_unavailable(), ok=False)
            return None
        try:
            encrypted, errors = await plugin.encrypt_message(stanza, {JID(peer)})
        except Exception as error:
            self._feedback(_("OMEMO: message not encrypted: {error}").format(error=error), ok=False)
            return None
        for item in errors:
            # Ошибка по одному устройству отправку не отменяет, но молчать о ней
            # нельзя: часть устройств собеседника сообщение не получит.
            self._bus.publish(
                Notice(
                    _("OMEMO: device {device} of {jid} skipped: {error}").format(
                        device=item.device_id, jid=item.bare_jid, error=item.exception
                    ),
                    NoticeLevel.WARNING,
                )
            )
        if encrypted is None:
            self._feedback(_("OMEMO: nothing to encrypt"), ok=False)
            return None
        self._xep("0384", "message-encrypted", peer=peer, direction=Direction.OUT.value)
        result: SlixMessage = encrypted
        return result

    async def _decrypt(self, stanza: SlixMessage) -> tuple[str, str] | None:
        """Расшифровать входящее. Возвращает текст и отпечаток отправителя."""
        plugin = self._omemo_plugin()
        if plugin is None:
            return None
        from termisations.protocol.omemo import WORKING_NAMESPACE, fingerprint

        namespaces = plugin.is_encrypted(stanza)
        if WORKING_NAMESPACE not in namespaces:
            # Библиотека загружает оба бэкенда, но работает только с oldmemo:
            # разбор omemo:2 после расшифровки поднимает NotImplementedError.
            # Отказ с причиной лучше пустой строки в ленте.
            self._bus.publish(
                Notice(
                    _(
                        "OMEMO: message in namespace {namespaces},"
                        " slixmpp-omemo does not support it"
                    ).format(namespaces=", ".join(sorted(namespaces))),
                    NoticeLevel.WARNING,
                )
            )
            return None
        try:
            decrypted, device = await plugin.decrypt_message(stanza)
        except Exception as error:
            self._bus.publish(
                Notice(
                    _("OMEMO: message not decrypted: {error}").format(error=error),
                    NoticeLevel.ERROR,
                )
            )
            return None
        return str(decrypted["body"] or ""), fingerprint(device.identity_key)

    # Команды OMEMO.

    def _omemo_target(self, jid: str | None) -> str | None:
        """Беседа команды шифрования. ``None`` означает, что отказ уже напечатан."""
        problem = self._omemo_unavailable()
        if problem:
            self._feedback(f"OMEMO: {problem}", ok=False)
            return None
        if not jid:
            self._feedback(_("no active conversation: /chat <jid>"), ok=False)
            return None
        return jid

    async def _command_omemo_status(self, jid: str | None) -> None:
        """Состояние OMEMO беседы вместе с рабочим пространством имен."""
        from termisations.protocol.omemo import (
            OLDMEMO_NAMESPACE,
            TWOMEMO_NAMESPACE,
            WORKING_NAMESPACE,
            fingerprint,
            trust_summary,
        )

        target = self._omemo_target(jid)
        if target is None:
            return
        plugin = self._omemo_plugin()
        manager = await plugin.get_session_manager()
        own, own_others = await manager.get_own_device_information()
        devices = await manager.get_device_information(target)
        trusted, total = trust_summary(devices)
        conversation = self._conversations.get(target)
        enabled = conversation is not None and conversation.encryption is Encryption.OMEMO
        # Пространство имен называется прямо: библиотека публикует бандлы обоих,
        # а работает только с одним, и знать об этом пользователь обязан.
        supported = ", ".join(
            (
                _("{name} (working)") if name == WORKING_NAMESPACE else _("{name} (bundle only)")
            ).format(name=name)
            for name in (OLDMEMO_NAMESPACE, TWOMEMO_NAMESPACE)
        )
        self._table(
            f"OMEMO {target}",
            (
                (_("enabled in conversation"), _("yes") if enabled else _("no")),
                (
                    _("trusted devices"),
                    _("{trusted} of {total}").format(trusted=trusted, total=total),
                ),
                (_("own fingerprint"), fingerprint(own.identity_key)),
                (_("own device ID"), str(own.device_id)),
                (_("other own devices"), str(len(own_others))),
                (_("namespaces"), supported),
            ),
        )

    async def _command_set_omemo(self, jid: str | None, *, enabled: bool) -> None:
        """Включить или выключить шифрование беседы."""
        target = self._omemo_target(jid)
        if target is None:
            return
        conversation = self._ensure_conversation(target)
        self._conversations[target] = replace(
            conversation, encryption=Encryption.OMEMO if enabled else Encryption.PLAIN
        )
        self._publish_conversations()
        await self._refresh_omemo(target)
        self._xep("0384", "session-built" if enabled else "session-closed", peer=target)
        state = _("OMEMO for {jid}: enabled") if enabled else _("OMEMO for {jid}: disabled")
        self._feedback(state.format(jid=target))

    async def _command_fingerprints(self, jid: str | None) -> None:
        """Отпечатки устройств беседы вместе со своими."""
        from termisations.protocol.omemo import fingerprint

        target = self._omemo_target(jid)
        if target is None:
            return
        plugin = self._omemo_plugin()
        manager = await plugin.get_session_manager()
        own, own_others = await manager.get_own_device_information()
        rows: list[tuple[str, str]] = [
            (
                _("own"),
                _("{fingerprint}  device {device}").format(
                    fingerprint=fingerprint(own.identity_key), device=own.device_id
                ),
            )
        ]
        for device in sorted(own_others, key=lambda item: item.device_id):
            rows.append(
                (
                    _("own device {device}").format(device=device.device_id),
                    f"{fingerprint(device.identity_key)}  {device.trust_level_name.lower()}",
                )
            )
        devices = await manager.get_device_information(target)
        for device in sorted(devices, key=lambda item: item.device_id):
            rows.append(
                (
                    _("device {device}").format(device=device.device_id),
                    f"{fingerprint(device.identity_key)}  {device.trust_level_name.lower()}",
                )
            )
        self._table(_("fingerprints {jid}").format(jid=target), rows)

    async def _command_set_trust(self, jid: str | None, typed: str, *, trusted: bool) -> None:
        """Пометить отпечаток доверенным или недоверенным.

        Принимается только отпечаток из списка /omemo fingerprints: произвольная
        строка давала подтверждение доверия неизвестно чему. Пробелы не значимы -
        список печатает отпечаток группами, и строку из него копируют как есть.
        """
        from termisations.protocol.omemo import fingerprint

        target = self._omemo_target(jid)
        if target is None:
            return
        plugin = self._omemo_plugin()
        manager = await plugin.get_session_manager()
        wanted = _compact(typed)
        own, own_others = await manager.get_own_device_information()
        devices = await manager.get_device_information(target)
        for device in (*devices, *own_others, own):
            if _compact(fingerprint(device.identity_key)) != wanted:
                continue
            level = TRUST_LEVEL_TRUSTED if trusted else TRUST_LEVEL_DISTRUSTED
            await manager.set_trust(device.bare_jid, device.identity_key, level)
            await self._refresh_omemo(target)
            self._xep(
                "0384",
                "device-trusted" if trusted else "device-untrusted",
                peer=device.bare_jid,
                fingerprint=fingerprint(device.identity_key),
            )
            verdict = (
                _("fingerprint {fingerprint}: trusted")
                if trusted
                else _("fingerprint {fingerprint}: untrusted")
            )
            self._feedback(verdict.format(fingerprint=fingerprint(device.identity_key)))
            return
        self._feedback(
            _("fingerprint {fingerprint} not found for the peer").format(fingerprint=typed),
            ok=False,
        )
        await self._command_fingerprints(target)

    async def _command_omemo_purge(self, jid: str | None) -> None:
        """Удалить сессии и устройства собеседника."""
        target = self._omemo_target(jid)
        if target is None:
            return
        plugin = self._omemo_plugin()
        manager = await plugin.get_session_manager()
        await manager.purge_bare_jid(target)
        await self._refresh_omemo(target)
        self._xep("0384", "sessions-purged", peer=target)
        self._feedback(_("sessions and devices of {jid} removed").format(jid=target))

    async def _command_omemo_rotate(self) -> None:
        """Сгенерировать новое устройство и опубликовать бандл заново.

        Операция разрушительная: старый ключевой материал стирается, и все
        собеседники увидят новое непроверенное устройство. Поэтому она снимает
        весь ключевой материал учетной записи и требует переподключения.
        """
        problem = self._omemo_unavailable()
        if problem:
            self._feedback(f"OMEMO: {problem}", ok=False)
            return
        storage = self._storage
        if storage is None:
            self._feedback(_("OMEMO: database unavailable"), ok=False)
            return
        await storage.omemo_clear(self._account_row)
        self._omemo_ready = False
        self._xep("0384", "device-rotated")
        self._feedback(
            _(
                "OMEMO key material removed, a new device will be created on the next"
                " connection: /reconnect"
            ),
            ok=False,
        )

    # Хранилище.

    def _remember(
        self, message: Message, *, origin_id: str = "", stanza_id: str = "", update: bool = False
    ) -> None:
        """Записать сообщение в историю, не задерживая обработку строфы.

        Запись идет фоновой задачей: диск не должен стоять на пути входящего
        потока. Ошибка записи не роняет сессию - переписка важнее истории, и
        отказ диска не повод рвать соединение.
        """
        storage = self._storage
        if storage is None:
            return
        self._spawn(self._write_message(message, origin_id, stanza_id, update=update), "history")

    async def _write_message(
        self, message: Message, origin_id: str, stanza_id: str, *, update: bool
    ) -> None:
        """Одна запись в историю."""
        storage = self._storage
        if storage is None:
            return
        try:
            if update:
                await storage.update_message(self._account_row, message)
            else:
                await storage.save_message(
                    self._account_row,
                    StoredMessage(message, origin_id=origin_id, stanza_id=stanza_id),
                )
        except Exception:
            _log.exception(_("message not saved to history"))

    async def _load_history(self) -> None:
        """Поднять историю бесед из базы до подключения.

        История показывается сразу при запуске, а не после рукопожатия: читать
        накопленное можно и без сети, и ждать сервера ради этого незачем.
        """
        storage = self._storage
        if storage is None:
            return
        try:
            self._account_row = await storage.account_id(self._account.jid)
            saved = await storage.load_conversations(self._account_row)
            known = {item.jid: item for item in saved}
            # Беседы без записи в таблице остались от старых версий базы: их
            # адреса видны только по сообщениям.
            order = [item.jid for item in saved] + [
                jid for jid in await storage.conversations(self._account_row) if jid not in known
            ]
            for jid in order:
                messages = await storage.load_messages(self._account_row, jid, HISTORY_LIMIT)
                if not messages and jid not in known:
                    continue
                item = known.get(jid)
                if item is not None:
                    self._conversations[jid] = item
                    if self._active is None:
                        self._activate(jid)
                else:
                    self._ensure_conversation(jid)
                for message in messages:
                    self._messages[message.message_id] = message
                    self._bus.publish(MessageAdded(message))
            self._publish_conversations()
        except Exception:
            _log.exception(_("history not loaded"))

    # Фоновые циклы.

    async def _state_loop(self) -> None:
        """Публиковать метрики раз в секунду."""
        while not self._stop.is_set():
            await asyncio.sleep(STATE_PERIOD)
            self._stanzas_per_sec = self._rate.per_second()
            self._set_state(metrics=self._metrics())

    def _metrics(self) -> Metrics:
        """Снимок метрик для статус-бара."""
        return Metrics(
            latency_ms=self._latency.average(),
            rss_bytes=read_rss_bytes(),
            stanzas_total=self._stanzas_total,
            stanzas_per_sec=self._stanzas_per_sec,
            bytes_in=self._bytes_in,
            bytes_out=self._bytes_out,
            uptime_s=time.monotonic() - self._started,
            reconnects=self._reconnects,
        )

    async def _ping_loop(self) -> None:
        """Измерять задержку до сервера пингом XEP-0199."""
        while not self._stop.is_set():
            await self._ping(self._account.domain, quiet=True)
            self._update_sm()
            await asyncio.sleep(PING_PERIOD)

    async def _ping(self, target: str, *, quiet: bool = False) -> float | None:
        """Один пинг. Возвращает время ответа в миллисекундах или None."""
        client = self._client
        if client is None or not client.is_connected():
            if not quiet:
                self._feedback(_("no connection"), ok=False)
            return None
        self._xep("0199", "ping-sent", peer=target, direction=Direction.OUT.value)
        try:
            seconds = await client.plugin["xep_0199"].ping(JID(target), timeout=IQ_TIMEOUT)
        except (IqError, IqTimeout) as error:
            if not quiet:
                self._feedback(
                    _("ping to {target} failed: {error}").format(target=target, error=error),
                    ok=False,
                )
            return None
        rtt = float(seconds) * 1000
        self._latency.add(rtt)
        self._xep("0199", "pong", peer=target, rtt=f"{rtt:.0f}ms")
        self._set_state(metrics=replace(self._metrics(), latency_ms=self._latency.average()))
        return rtt

    def _update_sm(self) -> None:
        """Обновить показатели потокового менеджмента."""
        client = self._client
        if client is None:
            return
        plugin = client.plugin["xep_0198"]
        enabled = bool(getattr(plugin, "enabled_out", False))
        # Признак возобновления ведется своим полем: у плагина такого атрибута
        # нет, факт возобновления виден только по событию session_resumed.
        info = SmInfo(
            enabled=enabled,
            resumed=self._resumed,
            outbound_unacked=len(getattr(plugin, "unacked_queue", ()) or ()),
            inbound_handled=int(getattr(plugin, "handled", 0) or 0),
        )
        if info != self._state.sm:
            self._set_state(sm=info)

    # Команды.

    async def handle_command(self, command: Command) -> None:
        """Исполнить команду шины."""
        try:
            await self._dispatch(command)
        except (IqError, IqTimeout) as error:
            self._feedback(_("server returned an error: {error}").format(error=error), ok=False)
        except Exception as error:
            _log.exception(_("command failed"))
            self._feedback(_("command failed: {error}").format(error=error), ok=False)

    async def _dispatch(self, command: Command) -> None:
        """Разбор команды по типу."""
        match command:
            case RunCommandLine(line=line):
                await self._run_command_line(line)
            case SendText(conversation=conversation, text=text):
                await self._send_text(conversation or self._active, text)
            case PingServer(target=target):
                rtt = await self._ping(target or self._account.domain)
                if rtt is not None:
                    self._feedback(
                        _("pong from {target}: {rtt:.0f}ms").format(
                            target=target or self._account.domain, rtt=rtt
                        )
                    )
            case RequestMam(jid=jid, limit=limit, before=before):
                await self._fetch_mam(jid or self._active or "", limit, before)
            case RequestDisco(jid=jid, node=node):
                await self._disco(jid or self._account.domain, node or "")
            case SetPresence(show=show, status=status):
                self._set_own_presence(show, status)
            case SetChatState(conversation=conversation, state=state):
                self._send_chat_state(conversation or self._active, state)
            case Connect():
                await self._command_connect()
            case Reconnect():
                await self._command_reconnect()
            case Disconnect():
                self._command_disconnect()
            case OpenConversation(jid=jid):
                self._open_conversation(jid)
            case CloseConversation(jid=jid):
                self._close_conversation(jid)
            case SetXmlMode(mode=mode):
                self._bus.publish(XmlLogModeChanged(mode))
                self._feedback(_("log panel mode: {mode}").format(mode=mode.value))
            case SetXmlFilter():
                pass
            case SetUnsafeXml(enabled=enabled):
                self._unsafe = enabled
                self._bus.publish(UnsafeModeChanged(enabled))
            case ClearXmlLog():
                self._feedback(_("log panel cleared"))
            case SendRawXml(xml=xml):
                self._send_raw(xml)
            case Quit():
                self.stop()
            case _:
                self._feedback(
                    _("command {name} is not supported in network mode").format(
                        name=type(command).__name__
                    ),
                    ok=False,
                )

    async def _command_connect(self) -> None:
        """Команда /connect."""
        client = self._client
        if client is not None and client.is_connected():
            self._feedback(_("session already connected"), ok=False)
            return
        # Прошлый клиент мог остаться с незавершенной попыткой подключения:
        # slixmpp повторяет ее сам с нарастающей паузой.
        self._command_disconnect(quiet=True)
        self._user_offline = False
        self._reconnect_attempt = 0
        self._feedback(_("connection started"))
        await self._connect()

    async def _command_reconnect(self) -> None:
        """Команда /reconnect: переподключение с попыткой возобновить поток.

        Экземпляр клиента сохраняется: ``sm_id`` живет в плагине XEP-0198,
        и новый ``ClientXMPP`` всегда начинал бы сессию заново.
        """
        client = self._client
        self._user_offline = False
        self._reconnect_attempt = 0
        if client is not None:
            with _SuppressAll():
                client.cancel_connection_attempt()
                if client.is_connected():
                    # Ждется не завершение disconnect, а разбор транспорта:
                    # future disconnected выставляется в connection_lost, то
                    # есть тогда, когда сокет действительно закрыт. Новое
                    # подключение поверх недоразобранного транспорта молча не
                    # открывает сокет. Ссылка берется заранее: на разъединении
                    # slixmpp заменяет future новым.
                    closed = client.disconnected
                    client.disconnect(wait=0.0)
                    await asyncio.wait_for(asyncio.shield(closed), DISCONNECT_WAIT)
        self._feedback(_("reconnecting with an attempt to resume the stream"))
        await self._connect(resume=True)

    def _command_disconnect(self, *, quiet: bool = False) -> None:
        """Команда /disconnect: отключиться без попытки возобновления."""
        client = self._client
        self._disarm_watchdog()
        if client is None:
            if not quiet:
                self._feedback(_("no connection"), ok=False)
            return
        connected = client.is_connected()
        self._client = None
        self._user_offline = True
        with _SuppressAll():
            # Незавершенная попытка живет своей жизнью и сама себя
            # перепланирует с нарастающей паузой. Без явной отмены после
            # /reconnect в цикле окажутся два клиента на одну учетную запись.
            client.cancel_connection_attempt()
            # Явное отключение закрывает сессию начисто: плагин XEP-0198
            # выставляет end_session_on_disconnect в False, и без этой строки
            # sm_id пережил бы логаут, а следующий вход попробовал бы возобновить
            # поток, которого уже нет.
            client.end_session_on_disconnect = True
            client.disconnect()
        if not quiet:
            self._feedback(
                _("session disconnected") if connected else _("no connection"), ok=connected
            )

    async def _send_text(
        self, conversation: str | None, text: str, *, reply_to: str = "", attachment: Any = None
    ) -> None:
        """Отправить сообщение в беседу.

        Метод асинхронный из-за шифрования: OMEMO ходит за списком устройств и
        за бандлами собеседника, и сделать это синхронно нельзя.

        ``reply_to`` превращает сообщение в ответ по XEP-0461. Отдельного пути
        отправки для ответа нет: он отличается одним элементом, а шифрование,
        квитанция, origin-id и запись пути нужны ему те же.
        """
        client = self._client
        if client is None or not client.is_connected():
            self._feedback(_("no connection: message not sent"), ok=False)
            return
        if not conversation:
            self._feedback(_("no conversation selected: /chat <jid>"), ok=False)
            return
        item = self._conversations.get(conversation)
        # Комнате сообщение идет типом groupchat: с типом chat сервер доставит
        # его одному участнику, а не всем, и это выглядит как пропавшая реплика.
        kind: Literal["chat", "groupchat"] = (
            "groupchat" if item is not None and item.is_muc else "chat"
        )
        stanza = client.make_message(mto=JID(conversation), mbody=text, mtype=kind)
        encryption = Encryption.PLAIN
        if kind == "chat" and self._omemo_wanted(conversation):
            encrypted = await self._encrypt(stanza, conversation)
            if encrypted is None:
                return
            stanza = encrypted
            encryption = Encryption.OMEMO
        # Запрос подтверждения ставится после шифрования: библиотека собирает
        # строфу заново и все, кроме тела, теряет. В комнате квитанции не
        # запрашиваются: их прислал бы каждый участник.
        if kind == "chat":
            stanza["request_receipt"] = True
        if kind == "chat" and conversation == str(client.boundjid.bare):
            # Заметка самому себе. Без этой подсказки сервер доставляет строфу
            # каждому своему ресурсу дважды: один раз как адресату, второй раз
            # копией по XEP-0280, потому что отправитель - тот же аккаунт.
            # Клиент, который не связывает копию с оригиналом, показывает два
            # сообщения вместо одного.
            stanza.enable("no-copy")
        if reply_to:
            stanza["reply"]["id"] = reply_to
            stanza["reply"]["to"] = JID(conversation)
        if attachment is not None:
            stanza.append(attachment)
        message_id = str(stanza["id"] or "")
        # origin-id ставится нами: slixmpp его не проставляет, а без него путь
        # строфы в /trace начинается только с ответа сервера.
        stanza["origin_id"]["id"] = message_id
        stanza.send()
        self._traces.start(message_id, conversation, origin_id=message_id)
        self._traces.note(message_id, "0359", "origin-id", detail=message_id)
        message = Message(
            message_id=message_id,
            conversation=conversation,
            sender=str(client.boundjid.bare),
            body=text,
            ts=time.time(),
            direction=Direction.OUT,
            encryption=encryption,
            state=DeliveryState.SENT,
        ).with_xep(XepEvent("0184", "receipt-requested", Direction.OUT, conversation, message_id))
        if _is_action(text):
            message = message.with_xep(XepEvent("0245", "me", Direction.OUT, conversation))
        if reply_to:
            message = message.with_xep(
                XepEvent("0461", "reply", Direction.OUT, conversation, reply_to)
            )
        self._messages[message_id] = message
        self._ensure_conversation(conversation)
        self._bus.publish(MessageAdded(message))
        self._remember(message, origin_id=message_id)
        self._update_sm()

    def _send_raw(self, xml: str) -> None:
        """Отправить произвольную строфу командой /send.

        Разметка проверяется до отправки: сервер закрывает поток на первой же
        некорректной строфе, и выглядит это как случайный обрыв связи.
        """
        client = self._client
        if client is None or not client.is_connected():
            self._feedback(_("no connection: stanza not sent"), ok=False)
            return
        problem = router.validate_stanza(xml)
        if problem is not None:
            self._feedback(f"{problem}: /send <raw-xml>", ok=False)
            return
        client.send_raw(xml)
        self._feedback(_("stanza sent"))

    def _set_own_presence(self, show: PresenceShow, status: str) -> None:
        """Сменить собственное присутствие."""
        client = self._client
        if client is None or not client.is_connected():
            self._feedback(_("no connection"), ok=False)
            return
        # Уход в offline и возврат в сеть - естественные границы активности
        # клиента: именно их и ждет сервер, чтобы придержать поток.
        self._set_csi(active=show is not PresenceShow.AWAY and show is not PresenceShow.XA)
        if show is PresenceShow.OFFLINE:
            client.send_presence(ptype="unavailable")
            self._feedback(_("presence withdrawn, the stream stays open"))
            self._set_state(presence_show=PresenceShow.OFFLINE)
            return
        value = None if show is PresenceShow.AVAILABLE else show.value
        client.send_presence(pshow=value, pstatus=status or None)
        self._set_state(presence_show=show)
        text = _("presence: {show}").format(show=show.value)
        self._feedback(f"{text}, {status}" if status else text)

    def _open_conversation(self, jid: str) -> None:
        """Открыть беседу по адресу."""
        target = jid.strip()
        if not target:
            self._feedback(_("expected: /chat <jid>"), ok=False)
            return
        self._ensure_conversation(target, activate=True)

    def _close_conversation(self, jid: str | None) -> None:
        """Закрыть беседу."""
        target = (jid or self._active or "").strip()
        if target not in self._conversations:
            self._feedback(_("conversation {jid} is not open").format(jid=target), ok=False)
            return
        del self._conversations[target]
        if self._active == target:
            self._active = next(iter(self._conversations), None)
            if self._active is not None:
                self._activate(self._active)
        self._forget_conversation(target)
        self._publish_conversations()
        self._feedback(_("conversation {jid} closed").format(jid=target))

    def _forget_conversation(self, jid: str) -> None:
        """Убрать закрытую беседу из базы, оставив ее сообщения.

        Без этого закрытая беседа возвращалась бы при следующем запуске: список
        читается из таблицы, а не выводится из сообщений.
        """
        storage = self._storage
        if storage is None or not self._account_row:
            return
        self._spawn(
            _quietly(storage.forget_conversation(self._account_row, jid)), "conversation-forget"
        )

    async def _disco(self, jid: str, node: str) -> None:
        """Запрос сведений об узле по XEP-0030."""
        client = self._client
        if client is None or not client.is_connected():
            self._feedback(_("no connection"), ok=False)
            return
        self._xep("0030", "info-requested", peer=jid, direction=Direction.OUT.value)
        info = await client.plugin["xep_0030"].get_info(
            jid=JID(jid), node=node or None, timeout=IQ_TIMEOUT
        )
        features = sorted(str(item) for item in info["disco_info"]["features"])
        identities = [f"{item[0]}/{item[1]}" for item in info["disco_info"]["identities"]]
        self._xep("0030", "info-received", peer=jid, features=str(len(features)))
        rows = [(_("node"), jid), (_("features"), str(len(features)))]
        rows.extend((_("type"), value) for value in identities[:4])
        rows.extend(("", value) for value in features[:24])
        if len(features) > 24:
            rows.append(("", _("... {count} more").format(count=len(features) - 24)))
        self._table(f"disco {jid}", rows)

    async def _fetch_mam(self, jid: str, limit: int, before: str) -> None:
        """Запрос архива по XEP-0313 с постраничным обходом.

        Курсор берется из базы: повторный вызов догоняет пропущенное, а не тянет
        архив заново. Флаг ``--before`` начинает обход с указанной строфы, минуя
        курсор: так смотрят то, что уже прочитано.
        """
        client = self._client
        if client is None or not client.is_connected():
            self._feedback(_("no connection"), ok=False)
            return
        if not jid:
            self._feedback(_("expected: /mam <jid>"), ok=False)
            return
        del limit
        cursor = mam.Cursor(last_id=before) if before else await self._load_cursor(jid)
        self._stage(ConnectionStage.FETCHING_MAM, _("archive {jid}").format(jid=jid))
        self._xep("0313", "fetch-started", peer=jid, after=cursor.last_id or _("from the end"))
        try:
            received = await self._sync_archive(jid, cursor)
        finally:
            self._set_state(stage=ConnectionStage.READY)
        self._stage_done(
            ConnectionStage.FETCHING_MAM, _("messages: {count}").format(count=received)
        )
        self._feedback(
            _("archive {jid}: messages received {count}").format(jid=jid, count=received)
        )

    async def _load_cursor(self, jid: str) -> mam.Cursor:
        """Курсор синхронизации архива беседы из базы."""
        storage = self._storage
        if storage is None:
            return mam.Cursor()
        try:
            saved = await storage.mam_cursor(self._account_row, jid)
        except Exception:
            _log.exception(_("archive cursor not read"))
            return mam.Cursor()
        return mam.Cursor() if saved is None else mam.Cursor(saved[0], complete=saved[1])

    # Слэш-команды.

    async def _run_command_line(self, line: str) -> None:
        """Разобрать строку ввода и исполнить команду."""
        raw = line.strip()
        if not raw:
            return
        parsed = commands.parse(raw)
        if parsed is None:
            await self._send_text(self._active, raw[1:] if raw.startswith("//") else raw)
            return
        if parsed.error is not None:
            self._feedback(parsed.error, ok=False)
            return
        await router.route(self, parsed)

    # Операции роутера.

    def feedback(self, text: str, ok: bool = True) -> None:
        """Операция роутера: ответ на команду в область беседы."""
        self._feedback(text, ok)

    def table(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        """Операция роутера: табличный ответ."""
        self._table(title, rows)

    def is_online(self) -> bool:
        """Операция роутера: открыт ли поток."""
        client = self._client
        return client is not None and bool(client.is_connected())

    def active_conversation(self) -> str | None:
        """Операция роутера: JID активной беседы."""
        return self._active

    def default_target(self) -> str:
        """Операция роутера: домен учетной записи."""
        return self._account.domain

    async def connect(self) -> None:
        """Операция роутера: /connect."""
        await self._command_connect()

    async def disconnect(self) -> None:
        """Операция роутера: /disconnect."""
        self._command_disconnect()

    async def reconnect(self) -> None:
        """Операция роутера: /reconnect."""
        await self._command_reconnect()

    def show_account(self) -> None:
        """Операция роутера: /account."""
        self._table(_("account"), [(item, "") for item in self.describe()])

    def set_presence(self, show: PresenceShow, status: str) -> None:
        """Операция роутера: /presence."""
        self._set_own_presence(show, status)

    def stop_session(self) -> None:
        """Операция роутера: /quit."""
        self.stop()

    def open_conversation(self, jid: str) -> None:
        """Операция роутера: /chat."""
        self._open_conversation(jid)

    def close_conversation(self, jid: str | None) -> None:
        """Операция роутера: /close."""
        self._close_conversation(jid)

    def show_roster(self, *, raw: bool, groups: bool) -> None:
        """Операция роутера: /roster."""
        if raw:
            # Строфа запрашивается заново, а не собирается из разобранного
            # состояния: смысл --raw в том, чтобы увидеть ответ сервера, а не
            # наше представление о нем.
            self._spawn(self._show_roster_raw(), "roster-raw")
            return
        if groups:
            self._show_roster_groups()
            return
        self._show_roster()

    async def roster_add(self, jid: str) -> None:
        """Операция роутера: /add."""
        client = self._online_client()
        await _awaitable(client.update_roster(JID(jid), subscription="none"))
        client.send_presence_subscription(pto=JID(jid))
        self._feedback(_("{jid}: subscription request sent").format(jid=jid))

    async def roster_remove(self, jid: str) -> None:
        """Операция роутера: /remove."""
        client = self._online_client()
        await _awaitable(client.update_roster(JID(jid), subscription="remove"))
        self._roster.pop(jid, None)
        self._publish_roster()
        self._feedback(_("{jid}: removed from the roster").format(jid=jid))

    async def subscribe(self, jid: str) -> None:
        """Операция роутера: /sub."""
        self._online_client().send_presence_subscription(pto=JID(jid))
        self._feedback(_("{jid}: subscription requested").format(jid=jid))

    async def unsubscribe(self, jid: str) -> None:
        """Операция роутера: /unsub."""
        self._online_client().send_presence_subscription(pto=JID(jid), ptype="unsubscribe")
        self._feedback(_("{jid}: subscription cancelled").format(jid=jid))

    async def ping(self, target: str) -> float | None:
        """Операция роутера: /ping."""
        return await self._ping(target)

    async def disco(self, jid: str, node: str) -> None:
        """Операция роутера: /disco."""
        await self._disco(jid, node)

    def show_features(self) -> None:
        """Операция роутера: /features."""
        self._show_features()

    def show_sm(self) -> None:
        """Операция роутера: /sm."""
        self._show_sm()

    def show_tls(self) -> None:
        """Операция роутера: /tls."""
        self._show_tls()

    async def fetch_mam(self, jid: str, limit: int, before: str) -> None:
        """Операция роутера: /mam."""
        await self._fetch_mam(jid, limit, before)

    def _online_client(self) -> ClientXMPP:
        """Клиент при открытом потоке. Иначе отказ с той же формулировкой везде."""
        client = self._client
        if client is None or not client.is_connected():
            raise UnsupportedError(_("no connection, run /connect first"))
        return client

    # Операции комнат, файлов и шифрования.
    #
    # Отказ здесь есть у одной команды - /ox, и причина у него внешняя:
    # поддерживаемой реализации XEP-0373 для Python нет. Отказ называет
    # причину: молчаливое "команда не поддержана" не позволяет отличить
    # незаконченную работу от опечатки.

    async def join_room(self, room: str, nick: str) -> None:
        """/join: вход в комнату по XEP-0045."""
        await self._enter_room(room, nick, activate=True)

    async def _enter_room(self, room: str, nick: str, *, activate: bool) -> None:
        """Вход в комнату.

        ``activate`` снимается при автовходе по закладкам: комната там не
        выбрана пользователем прямо сейчас, и перехватывать активную беседу она
        не должна - иначе набранный текст уходит не туда.
        """
        client = self._online_client()
        target = str(JID(room).bare)
        if "@" not in target:
            self._feedback(_("invalid room JID: {room}").format(room=room), ok=False)
            return
        chosen = nick or self._account.username
        # Вход в комнату - операция, а не стадия подключения. Раньше он занимал
        # чужую стадию FETCHING_ROSTER и по завершении переводил ее в
        # "выполнено": автовход по закладкам после возобновления потока сбивал
        # этим готовую сессию обратно в стадию получения контакт-листа.
        operation = _("joining {room}").format(room=target)
        self._progress(operation, 0, 0, _("waiting for the room to answer"))
        try:
            _presence, subject, occupants, _history = await client.plugin["xep_0045"].join_muc_wait(
                JID(target), chosen, maxstanzas=0, timeout=IQ_TIMEOUT
            )
        except (IqError, IqTimeout, PresenceError, TimeoutError) as error:
            self._progress(
                operation, 0, 0, _("join failed: {error}").format(error=error), finished=True
            )
            self._feedback(
                _("joining {room} failed: {error}").format(room=target, error=error), ok=False
            )
            return
        self._nicks[target] = chosen
        client.add_event_handler(f"muc::{target}::presence", self._on_muc_presence)
        conversation = self._ensure_conversation(target, activate=activate)
        self._conversations[target] = replace(
            conversation,
            title=target.split("@", 1)[0],
            is_muc=True,
            topic=str(subject or ""),
            show=PresenceShow.AVAILABLE,
        )
        self._publish_conversations()
        # Состав берется из состояния плагина, а не из ответа входа: там строфы
        # присутствия целиком, а в ленте и в автодополнении нужны ники.
        del occupants
        self._publish_occupants(target)
        self._xep("0045", "joined", peer=target, nick=chosen)
        self._progress(
            operation,
            0,
            0,
            _("occupants: {count}").format(count=len(self._occupants.get(target, ()))),
            finished=True,
        )
        self._feedback(_("joined {room} as {nick}").format(room=target, nick=chosen))
        await self._save_bookmark(target, chosen)

    def leave_room(self) -> None:
        """/leave: выход из активной комнаты."""
        client = self._online_client()
        target = self._active_room()
        client.plugin["xep_0045"].leave_muc(JID(target), self._nicks.get(target, ""))
        client.del_event_handler(f"muc::{target}::presence", self._on_muc_presence)
        self._occupants.pop(target, None)
        self._nicks.pop(target, None)
        self._bus.publish(OccupantsUpdated(target, ()))
        self._xep("0045", "left", direction=Direction.OUT.value, peer=target)
        self._close_conversation(target)
        self._spawn(self._drop_bookmark(target), "bookmark")

    def set_topic(self, subject: str) -> None:
        """/topic: показать или сменить тему комнаты.

        Без аргумента команда печатает текущую тему и ничего не отправляет:
        пустая строфа subject стерла бы тему.
        """
        client = self._online_client()
        target = self._active_room()
        conversation = self._conversations.get(target)
        if not subject:
            current = conversation.topic if conversation is not None else ""
            self._feedback(
                _("room topic: {topic}").format(topic=current)
                if current
                else _("room topic: not set")
            )
            return
        client.plugin["xep_0045"].set_subject(JID(target), subject)
        self._xep("0045", "subject", direction=Direction.OUT.value, peer=target, subject=subject)
        self._feedback(_("room topic: {topic}").format(topic=subject))

    async def reply_to(self, message_id: str, text: str) -> None:
        """/reply: ответ с указанием исходного сообщения по XEP-0461."""
        target = self._messages.get(message_id)
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
        await self._send_text(target.conversation, text, reply_to=message_id)

    async def react_to(self, message_id: str, emoji: Sequence[str]) -> None:
        """/react: набор реакций по XEP-0444.

        Набор передается целиком и заменяет прежний: пустой означает снятие.
        Реакция уходит без тела, поэтому клиент без поддержки расширения не
        покажет ее вовсе - так и задумано расширением.
        """
        client = self._online_client()
        target = self._messages.get(message_id)
        if target is None:
            self._feedback(
                _("message {message_id} is not in the conversation feed").format(
                    message_id=message_id
                ),
                ok=False,
            )
            return
        conversation = target.conversation
        item = self._conversations.get(conversation)
        kind: Literal["chat", "groupchat"] = (
            "groupchat" if item is not None and item.is_muc else "chat"
        )
        stanza = client.make_message(mto=JID(conversation), mtype=kind)
        stanza["reactions"]["id"] = message_id
        stanza["reactions"]["values"] = set(emoji)
        stanza.send()
        own = self._own_mark(conversation)
        marks = " ".join(sorted(emoji))
        self._xep(
            "0444",
            "reaction",
            direction=Direction.OUT.value,
            peer=conversation,
            emoji=marks or _("cleared"),
        )
        self._replace_mark(
            message_id,
            XepEvent("0444", "reaction", Direction.OUT, own, message_id, {"emoji": marks}),
            drop=not marks,
        )
        self._feedback(
            _("reactions updated: {emoji}").format(emoji=marks)
            if marks
            else _("reactions updated: removed")
        )
        self._update_sm()

    async def retract(self, message_id: str) -> None:
        """/retract: отзыв своего сообщения по XEP-0424."""
        client = self._online_client()
        target = self._messages.get(message_id)
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
        conversation = target.conversation
        item = self._conversations.get(conversation)
        kind: Literal["chat", "groupchat"] = (
            "groupchat" if item is not None and item.is_muc else "chat"
        )
        stanza = client.make_message(mto=JID(conversation), mtype=kind)
        stanza["retract"]["id"] = message_id
        stanza.send()
        self._xep("0424", "retracted", direction=Direction.OUT.value, peer=conversation)
        self._retract_message(message_id, self._own_mark(conversation), "")
        self._feedback(_("message {message_id} retracted").format(message_id=message_id))
        self._update_sm()

    def _own_mark(self, conversation: str) -> str:
        """Чем подписана своя метка в беседе: ник в комнате, адрес в личной."""
        item = self._conversations.get(conversation)
        if item is not None and item.is_muc:
            return self._nicks.get(conversation, "")
        client = self._client
        return str(client.boundjid.bare) if client is not None else ""

    def set_nick(self, nick: str) -> None:
        """/nick: смена ника в комнате."""
        target = self._active_room()
        if not nick:
            self._feedback(
                _("room nick: {nick}").format(nick=self._nicks.get(target, "")), ok=False
            )
            return
        self._spawn(self._change_nick(target, nick), "muc-nick")

    async def _change_nick(self, room: str, nick: str) -> None:
        """Смена ника с ожиданием ответа комнаты.

        Комната вправе назначить другой ник, поэтому результат берется из
        ответа, а не из того, что мы попросили.
        """
        client = self._client
        if client is None or not client.is_connected():
            return
        try:
            chosen = await client.plugin["xep_0045"].set_self_nick(
                JID(room), nick, timeout=IQ_TIMEOUT
            )
        except (IqError, IqTimeout, PresenceError, TimeoutError) as error:
            self._feedback(_("nick not changed: {error}").format(error=error), ok=False)
            return
        self._nicks[room] = str(chosen)
        self._xep("0045", "nick-changed", direction=Direction.OUT.value, peer=room, nick=chosen)
        self._feedback(_("room nick: {nick}").format(nick=chosen))

    def _active_room(self) -> str:
        """Адрес активной комнаты. Отказ, если активная беседа не комната."""
        target = self._active or ""
        conversation = self._conversations.get(target)
        if conversation is None or not conversation.is_muc:
            raise UnsupportedError(_("the active conversation is not a room"))
        return target

    async def set_blocked(self, jid: str, *, blocked: bool) -> None:
        """/block и /unblock по XEP-0191."""
        client = self._online_client()
        plugin: Any = client.plugin["xep_0191"]
        try:
            if blocked:
                await plugin.block(JID(jid), timeout=IQ_TIMEOUT)
            else:
                await plugin.unblock(JID(jid), timeout=IQ_TIMEOUT)
        except (IqError, IqTimeout) as error:
            self._feedback(_("block list not changed: {error}").format(error=error), ok=False)
            return
        self._xep(
            "0191", "blocked" if blocked else "unblocked", direction=Direction.OUT.value, peer=jid
        )
        verdict = _("{jid}: blocked") if blocked else _("{jid}: unblocked")
        self._feedback(verdict.format(jid=jid))

    async def show_caps(self, jid: str) -> None:
        """/caps: возможности собеседника по XEP-0115, из кэша или кругом disco."""
        target = jid or self._active or ""
        if not target:
            self._feedback(_("expected: /caps <jid>"), ok=False)
            return
        entry, cached = await self._caps_of(target)
        if entry is None:
            self._feedback(_("capabilities of {jid} not received").format(jid=target), ok=False)
            return
        rows: list[tuple[str, str]] = [
            (_("verification string"), entry.verstring or _("the server did not announce it")),
            (_("source"), _("cache") if cached else _("disco query")),
            (_("identities"), str(len(entry.identities))),
            (_("features"), str(len(entry.features))),
        ]
        rows.extend((_("type"), item) for item in entry.identities[:4])
        rows.extend((_feature_title(item), item) for item in entry.features[:24])
        if len(entry.features) > 24:
            rows.append(("...", _("{count} more").format(count=len(entry.features) - 24)))
        self._table(f"caps {target}", rows)

    def show_trace(self, stanza_id: str) -> None:
        """/trace: путь строфы по origin-id, stanza-id или идентификатору сообщения."""
        if not stanza_id:
            self._feedback(_("expected: /trace <stanza-id>"), ok=False)
            return
        trace = self._traces.find(stanza_id)
        if trace is None:
            self._feedback(
                _("stanza {stanza_id} not found among recent ones").format(stanza_id=stanza_id),
                ok=False,
            )
            return
        self._table(_("stanza path {message_id}").format(message_id=trace.message_id), trace.rows())

    async def send_iq(self, target: str, namespace: str, kind: str) -> None:
        """/iq: собрать IQ с одним элементом query и показать ответ.

        Команда, которая только отправляет строфу и молчит, для отладки
        бесполезна: смысл в ответе сервера. Полный ответ виден в панели сырого
        потока, здесь - его тип, отправитель и состав.
        """
        client = self._online_client()
        # Набор типов закрыт реестром команд, но mypy об этом не знает: у
        # make_iq тип аргумента - Literal.
        iq_type = cast(Literal["get", "set", "result", "error"], kind)
        iq: Any = client.make_iq(ito=JID(target), itype=iq_type)
        iq.append(ElementTree.Element(f"{{{namespace}}}query"))
        try:
            answer: Any = await iq.send(timeout=IQ_TIMEOUT)
        except IqError as error:
            condition = str(error.iq["error"]["condition"] or _("unknown error"))
            self._feedback(
                _("{target} returned an error: {condition}").format(
                    target=target, condition=condition
                ),
                ok=False,
            )
            return
        except IqTimeout:
            self._feedback(
                _("{target} did not answer within {timeout:.0f} s").format(
                    target=target, timeout=IQ_TIMEOUT
                ),
                ok=False,
            )
            return
        rows = [
            (_("type"), str(answer["type"] or "")),
            (_("from"), str(answer["from"] or target)),
            (_("namespace"), namespace),
        ]
        children = [str(child.tag) for child in answer.xml if child.tag]
        rows.append((_("contents"), ", ".join(children) if children else _("empty answer")))
        self._table(_("answer to IQ {kind} to {target}").format(kind=kind, target=target), rows)

    def show_omemo(self, jid: str | None) -> None:
        """/omemo status."""
        self._spawn(self._command_omemo_status(jid), "omemo-status")

    def set_omemo(self, jid: str | None, *, enabled: bool) -> None:
        """/omemo enable и /omemo disable."""
        self._spawn(self._command_set_omemo(jid, enabled=enabled), "omemo-toggle")

    def show_fingerprints(self, jid: str | None) -> None:
        """/omemo fingerprints."""
        self._spawn(self._command_fingerprints(jid), "omemo-fingerprints")

    def set_trust(self, jid: str | None, fingerprint: str, *, trusted: bool) -> None:
        """/omemo trust и /omemo distrust."""
        self._spawn(self._command_set_trust(jid, fingerprint, trusted=trusted), "omemo-trust")

    def omemo_purge(self, jid: str | None) -> None:
        """/omemo purge."""
        self._spawn(self._command_omemo_purge(jid), "omemo-purge")

    def omemo_rotate(self, jid: str | None) -> None:
        """/omemo rotate."""
        self._spawn(self._command_omemo_rotate(), "omemo-rotate")

    def show_ox(self, action: str) -> None:
        """/ox: поддерживаемой реализации XEP-0373 для Python нет."""
        del action
        raise UnsupportedError(_("/ox: there is no supported XEP-0373 implementation for Python"))

    async def upload(self, source: str) -> None:
        """/upload: загрузка файла по XEP-0363 с показом доли выполненного.

        Слот берется у плагина, а PUT делается своими руками. Причина не в
        красоте: ``upload_file`` создает ``ClientSession`` без ssl-контекста,
        поэтому ``--no-tls-verify`` до HTTP-части не доходит, а флаг заявлен
        как общий для клиента. Заодно видно код ответа сервера хранилища.
        """
        client = self._online_client()
        path = Path(source).expanduser()
        try:
            size = path.stat().st_size
        except OSError:
            self._feedback(_("file not accessible: {path}").format(path=path), ok=False)
            return
        if not size:
            self._feedback(_("file is empty: {path}").format(path=path), ok=False)
            return
        self._xep("0363", "slot-requested", file=path.name, size=str(size))
        slot = await self._request_slot(client, path, size)
        if slot is None:
            return
        put_url, get_url, headers = slot
        self._last_slot = get_url
        self._xep("0363", "slot-received", file=path.name)
        try:
            handle = path.open("rb")
        except OSError as error:
            self._feedback(_("file not read: {error}").format(error=error), ok=False)
            return
        progress = _Progress(path, size, self._on_upload_progress)
        progress.attach(handle)
        try:
            await self._put_file(put_url, progress, headers)
        except Exception as error:
            # Ошибка сети приходит подклассом OSError, и отличить ее от ошибки
            # чтения файла по типу нельзя. Файл уже открыт выше, поэтому все,
            # что падает здесь, относится к загрузке.
            failure = _("upload failed: {error}").format(error=error)
            self._progress(path.name, size, size, failure, finished=True)
            self._feedback(failure, ok=False)
            return
        finally:
            handle.close()
        self._xep("0363", "uploaded", file=path.name, size=str(size))
        self._progress(
            path.name,
            size,
            size,
            _("uploaded {size}").format(size=humanize_bytes(size)),
            finished=True,
        )
        self._feedback(_("file uploaded: {url}").format(url=get_url))
        await self._send_attachment(get_url, path)

    async def _request_slot(
        self, client: ClientXMPP, path: Path, size: int
    ) -> tuple[str, str, dict[str, str]] | None:
        """Слот у службы загрузки: адреса PUT и GET плюс заголовки запроса."""
        upload: Any = client.plugin["xep_0363"]
        content_type = guess_type(path.name)[0] or "application/octet-stream"
        try:
            service = (
                upload.upload_service
                or (await upload.find_upload_service(timeout=IQ_TIMEOUT))["from"]
            )
            answer: Any = await upload.request_slot(
                service, path.name, size, content_type, timeout=IQ_TIMEOUT
            )
        except (IqError, IqTimeout, TimeoutError, TypeError) as error:
            self._feedback(_("upload slot not granted: {error}").format(error=error), ok=False)
            return None
        slot = answer["http_upload_slot"]
        headers = {
            "Content-Length": str(size),
            "Content-Type": content_type,
            # Заголовки слота обязательны: сервер хранилища проверяет по ним
            # право на запись, и без них PUT отвечает отказом.
            **{str(header["name"]): str(header["value"]) for header in slot["put"]["headers"]},
        }
        return str(slot["put"]["url"]), str(slot["get"]["url"]), headers

    async def _put_file(self, url: str, body: io.RawIOBase, headers: dict[str, str]) -> None:
        """Тело файла в хранилище одним PUT."""
        timeout = aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT)
        ssl_setting: ssl.SSLContext | bool = (
            True if self._account.tls_verify else ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        )
        if not self._account.tls_verify and isinstance(ssl_setting, ssl.SSLContext):
            ssl_setting.check_hostname = False
            ssl_setting.verify_mode = ssl.CERT_NONE
        async with aiohttp.ClientSession(timeout=timeout) as http:
            response = await http.put(url, data=body, headers=headers, ssl=ssl_setting)
            async with response:
                if response.status >= 400:
                    text = (await response.text())[:200]
                    raise RuntimeError(
                        _("storage answered {status}: {text}").format(
                            status=response.status, text=text
                        )
                    )

    def _on_upload_progress(self, sent: int, total: int) -> None:
        """Доля выполненного у загрузки файла."""
        self._progress(_("upload"), sent, total, humanize_bytes(sent))

    def _progress(
        self, operation: str, done: int, total: int, detail: str, *, finished: bool = False
    ) -> None:
        """Опубликовать долю выполненного длинной операции."""
        self._bus.publish(OperationProgress(operation, done, total, detail, finished))

    async def _send_attachment(self, url: str, path: Path) -> None:
        """Отправить ссылку на загруженный файл вместе с описанием вложения.

        Описание по XEP-0446 и ссылка по XEP-0447 идут рядом с обычным телом,
        одним сообщением: клиент без этих расширений увидит ссылку текстом,
        клиент с ними - вложение с именем, размером и хэшем. Отдельной строфой
        вложение слать нельзя - в ленте собеседника получилось бы два
        сообщения на один файл.
        """
        client = self._client
        conversation = self._active
        if client is None or not conversation:
            return
        sharing: Any = client.plugin["xep_0447"]
        try:
            attached = sharing.get_sfs(path=path, uris=[url], disposition="inline")
        except Exception:
            # Хэш считается чтением файла целиком, и он может не прочитаться.
            # Ссылку при этом отправить все равно надо.
            _log.exception(_("attachment description not built"))
            await self._send_text(conversation, url)
            return
        self._xep(
            "0447", "file-shared", direction=Direction.OUT.value, peer=conversation, file=path.name
        )
        await self._send_text(conversation, url, attachment=attached)

    def show_slot(self) -> None:
        """/slot: последняя выданная ссылка загрузки."""
        if not self._last_slot:
            self._feedback(_("no slot has been requested"), ok=False)
            return
        # Ссылка может нести подпись в query string. Маскирование здесь то же,
        # что и в панели лога: полный вид доступен только в режиме unsafe.
        self._feedback(f"get: {redact(self._last_slot, self._unsafe)}")

    def _show_roster(self) -> None:
        """Команда /roster."""
        if not self._roster:
            self._feedback(_("roster is empty"), ok=False)
            return
        rows = [
            (
                item.jid,
                _("{show}, subscription {subscription}").format(
                    show=item.show.value, subscription=item.subscription
                ),
            )
            for item in sorted(self._roster.values(), key=lambda entry: entry.jid)
        ]
        self._table(_("roster ({count})").format(count=len(rows)), rows)

    async def _show_roster_raw(self) -> None:
        """Команда /roster --raw: ответ сервера как есть."""
        client = self._online_client()
        iq: Any = client.make_iq_get(queryxmlns="jabber:iq:roster")
        try:
            answer: Any = await iq.send(timeout=IQ_TIMEOUT)
        except (IqError, IqTimeout) as error:
            self._feedback(_("roster not received: {error}").format(error=error), ok=False)
            return
        self._feedback(ElementTree.tostring(answer.xml, encoding="unicode"))

    def _show_roster_groups(self) -> None:
        """Команда /roster --groups: контакты по группам."""
        if not self._roster:
            self._feedback(_("roster is empty"), ok=False)
            return
        buckets: dict[str, list[RosterItem]] = {}
        for item in self._roster.values():
            for group in item.groups or (_("no group"),):
                buckets.setdefault(group, []).append(item)
        rows: list[tuple[str, str]] = []
        for group in sorted(buckets):
            rows.append((group, ""))
            rows.extend(
                (
                    f"  {entry.jid}",
                    _("{show}, subscription {subscription}").format(
                        show=entry.show.value, subscription=entry.subscription
                    ),
                )
                for entry in sorted(buckets[group], key=lambda entry: entry.jid)
            )
        self._table(_("roster by groups ({count})").format(count=len(self._roster)), rows)

    def _show_tls(self) -> None:
        """Команда /tls."""
        info = self._tls_info()
        endpoint = self._endpoint
        binding = info.channel_binding or _(
            "n/a (standard ssl has only tls-unique, which is forbidden on TLS 1.3)"
        )
        self._table(
            _("channel"),
            [
                (_("address"), endpoint.describe() if endpoint else "n/a"),
                (_("transport"), transport_label(self._state.transport)),
                (_("version"), info.version or "n/a"),
                (_("cipher"), info.cipher or "n/a"),
                (_("channel binding"), binding),
                (_("chain verification"), _("on") if info.valid else _("off")),
            ],
        )

    def _show_sm(self) -> None:
        """Команда /sm."""
        self._update_sm()
        sm = self._state.sm
        self._table(
            "XEP-0198",
            [
                (_("enabled"), _("yes") if sm.enabled else _("no")),
                (_("resumed"), _("yes") if sm.resumed else _("no")),
                (_("unacknowledged"), str(sm.outbound_unacked)),
                (_("inbound handled"), str(sm.inbound_handled)),
            ],
        )

    def _show_features(self) -> None:
        """Команда /features: то, что сервер объявил в потоке."""
        client = self._client
        if client is None:
            self._feedback(_("no connection"), ok=False)
            return
        features = sorted(str(item) for item in client.features)
        if not features:
            self._feedback(_("the server announced no stream features"), ok=False)
            return
        self._table(_("stream features"), [(item, "") for item in features])


async def _awaitable(result: Any) -> None:
    """Дождаться результата, если вызов вернул задачу.

    ``update_roster`` отдает Future при подключенном потоке и None, когда
    изменение применено локально. Ветвление на каждом вызове читается хуже.
    """
    if result is not None:
        await result


async def _quietly(work: Coroutine[Any, Any, None]) -> None:
    """Выполнить фоновую запись, не роняя сессию отказом диска."""
    try:
        await work
    except Exception:
        _log.exception(_("background write failed"))


def _with_extension_marks(message: Message, stanza: SlixMessage, peer: str) -> Message:
    """Метки расширений, которые относятся к самому сообщению.

    Здесь разбирается только ответ по XEP-0461: он приходит вместе с телом и
    меняет смысл именно этой реплики - без метки она читается как сказанная не
    в тему. Реакция и отзыв относятся к другому сообщению и разбираются
    отдельно, в ``_apply_reference_marks``.
    """
    reply = stanza.get_plugin("reply", check=True)
    if reply is not None:
        message = message.with_xep(
            XepEvent("0461", "reply", Direction.IN, peer, str(reply["id"] or ""))
        )
    return message


def _is_action(body: str) -> bool:
    """Сообщение является действием от третьего лица по XEP-0245."""
    return body.startswith(ME_PREFIX)


def _feature_title(feature: str) -> str:
    """Подпись строки возможности: номер расширения, если он известен."""
    number = NAMESPACE_XEPS.get(feature)
    return f"XEP-{number}" if number else _("feature")


def _archive_time(result: Any) -> float:
    """Время записи архива. Отсутствие отметки дает текущее время."""
    stamp = result["forwarded"]["delay"]["stamp"]
    if stamp is None:
        return time.time()
    try:
        return float(stamp.timestamp())
    except (AttributeError, TypeError, ValueError):
        return time.time()


def _compact(fingerprint: str) -> str:
    """Отпечаток без пробелов и регистра: список печатает его группами."""
    return "".join(fingerprint.split()).lower()


def _parse_show(value: str) -> PresenceShow | None:
    """Разобрать значение show. ``None``, если значение неизвестно."""
    try:
        return PresenceShow(value.strip().lower())
    except ValueError:
        return None


@contextmanager
def _lazy_tasks() -> Iterator[None]:
    """Вернуть циклу обычную фабрику задач на время одного синхронного вызова.

    Textual в ``App.run_async`` ставит циклу ``asyncio.eager_task_factory``
    (textual/app.py:2281-2283), и корутина начинает выполняться прямо внутри
    ``ensure_future``, до возврата задачи вызывающему. ``App.run_test`` этого не
    делает, поэтому в headless-режиме разница не видна.

    Окно обязано оставаться свободным от ``await``: тогда цикл не отдает
    управление никому и чужих задач в нем не создается. Межпотоковые вызовы
    драйвера Textual идут через ``run_coroutine_threadsafe``, а он только кладет
    колбэк в очередь цикла, поэтому фабрику к моменту создания задачи уже вернут.
    """
    loop = asyncio.get_running_loop()
    factory = loop.get_task_factory()
    loop.set_task_factory(None)
    try:
        yield
    finally:
        loop.set_task_factory(factory)


class _Progress(io.RawIOBase):
    """Файл, который считает прочитанное и сообщает долю выполненного.

    Плагин XEP-0363 отдает тело загрузки потоком, и своего отчета о прогрессе у
    него нет. Обертка считает байты на чтении: это единственная точка, через
    которую проходит все тело.

    Наследование от ``io.RawIOBase`` обязательно: тело уходит через aiohttp, а
    он принимает только потоки этой иерархии и отказывается от произвольного
    объекта с методом ``read``.
    """

    def __init__(self, path: Path, size: int, report: Callable[[int, int], None]) -> None:
        """Собрать обертку. Файл подставляется отдельно, в ``attach``."""
        self._path = path
        self._size = size
        self._report = report
        self._sent = 0
        self._handle: Any = None
        self._last = -1.0

    def attach(self, handle: Any) -> None:
        """Подставить открытый файл."""
        self._handle = handle

    def readable(self) -> bool:
        """Поток открыт на чтение."""
        return True

    def seekable(self) -> bool:
        """Перемотка поддержана: она нужна плагину для определения размера."""
        return True

    def read(self, amount: int | None = -1) -> bytes:
        """Прочитать очередной кусок и отчитаться о доле не чаще, чем раз в шаг."""
        chunk: bytes = self._handle.read(-1 if amount is None else amount)
        self._sent += len(chunk)
        now = time.monotonic()
        if chunk and (now - self._last >= PROGRESS_STEP or self._sent >= self._size):
            self._last = now
            self._report(self._sent, self._size)
        return chunk

    def seek(self, offset: int, whence: int = 0) -> int:
        """Перемотка: плагин пользуется ею, чтобы узнать размер."""
        return int(self._handle.seek(offset, whence))

    def tell(self) -> int:
        """Текущая позиция."""
        return int(self._handle.tell())


class _SuppressAll:
    """Контекст, гасящий любое исключение при остановке.

    При закрытии сессии важно снять все ресурсы, а не остановиться на первой
    ошибке: соединение к этому моменту может быть уже разорвано.
    """

    def __enter__(self) -> None:
        """Ничего не подготавливает."""

    def __exit__(self, *exc: object) -> bool:
        """Поглотить исключение выхода."""
        return True
