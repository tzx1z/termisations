"""Шина событий и команд - стержень архитектуры.

Направление потоков жестко задано:

* UI и другие потребители подписываются на события и отправляют только команды;
* источник данных (протокольный слой или мок) публикует события и исполняет команды.

Публикация событий синхронная, исполнение команд асинхронное. Обоснование такого
разделения ниже, у метода publish.
"""

import asyncio
import contextlib
import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, TypeVar

from termisations.core.i18n import _
from termisations.core.models import (
    ClientState,
    ConnectionStage,
    Conversation,
    Direction,
    Message,
    PresenceShow,
    RawStanza,
    RosterItem,
    XepEvent,
)

__all__ = [
    "ActiveConversationChanged",
    "ClearXmlLog",
    "CloseConversation",
    "Command",
    "CommandFeedback",
    "CommandHandler",
    "CommandTable",
    "Connect",
    "ConnectionStageChanged",
    "ConversationsUpdated",
    "Disconnect",
    "Event",
    "EventBus",
    "MessageAdded",
    "MessageUpdated",
    "Notice",
    "NoticeLevel",
    "OccupantsUpdated",
    "OpenConversation",
    "OperationProgress",
    "PingServer",
    "Quit",
    "Reconnect",
    "RequestDisco",
    "RequestMam",
    "RosterUpdated",
    "RunCommandLine",
    "SendRawXml",
    "SendText",
    "SetChatState",
    "SetOmemoEnabled",
    "SetPresence",
    "SetUnsafeXml",
    "SetXmlFilter",
    "SetXmlMode",
    "StanzaLogged",
    "StateUpdated",
    "Subscription",
    "UnsafeModeChanged",
    "XepActivity",
    "XmlLogModeChanged",
    "XmlMode",
]


class XmlMode(enum.StrEnum):
    """Режим панели сырого XML: какие направления попадают в лог."""

    IN = "in"
    OUT = "out"
    BOTH = "both"
    OFF = "off"

    def accepts(self, direction: Direction) -> bool:
        """Проходит ли строфа с таким направлением в лог при текущем режиме."""
        if self is XmlMode.OFF:
            return False
        if self is XmlMode.BOTH:
            return True
        # Локальные события (Direction.LOCAL) в односторонних режимах не показываются.
        return self.value == direction.value


class NoticeLevel(enum.StrEnum):
    """Уровень уведомления для области чата и всплывающих сообщений."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Event:
    """Базовое событие. Подписка на него получает все события без исключения."""


@dataclass(frozen=True, slots=True)
class Command:
    """Базовая команда. Исполняется обработчиком, назначенным через шину."""


# События.


@dataclass(frozen=True, slots=True)
class StanzaLogged(Event):
    """Строфа прошла через транспорт. Горячий путь, до 500 событий в секунду."""

    stanza: RawStanza


@dataclass(frozen=True, slots=True)
class MessageAdded(Event):
    """В беседу добавлено новое сообщение."""

    message: Message


@dataclass(frozen=True, slots=True)
class MessageUpdated(Event):
    """Изменилось состояние доставки, текст или набор меток у существующего сообщения."""

    message: Message


@dataclass(frozen=True, slots=True)
class XepActivity(Event):
    """Сработало протокольное расширение вне привязки к конкретному сообщению."""

    event: XepEvent


@dataclass(frozen=True, slots=True)
class ConnectionStageChanged(Event):
    """Смена стадии подключения.

    ``duration_ms`` заполняется только при завершении стадии, у начатой стадии он None.
    """

    stage: ConnectionStage
    detail: str = ""
    duration_ms: float | None = None


@dataclass(frozen=True, slots=True)
class OperationProgress(Event):
    """Доля выполненного у длинной операции: загрузка файла, обход архива.

    Отдельно от ``ConnectionStageChanged`` потому, что стадии описывают
    установку соединения. Загрузка файла ехала на стадии ``FETCHING_MAM``, и
    при отправке вложения в статус-баре стояло "получение архива".

    ``total`` равен нулю, когда общий объем неизвестен заранее: у обхода архива
    число страниц выясняется по ходу. ``done`` тогда считается сам по себе.
    """

    operation: str
    done: int
    total: int = 0
    detail: str = ""
    finished: bool = False


@dataclass(frozen=True, slots=True)
class StateUpdated(Event):
    """Обновлен агрегат состояния клиента. Публикуется не чаще 1 раза в секунду."""

    state: ClientState


@dataclass(frozen=True, slots=True)
class RosterUpdated(Event):
    """Контакт-лист перечитан целиком."""

    items: tuple[RosterItem, ...]


@dataclass(frozen=True, slots=True)
class ConversationsUpdated(Event):
    """Изменился список открытых бесед или счетчики непрочитанного."""

    items: tuple[Conversation, ...]


@dataclass(frozen=True, slots=True)
class OccupantsUpdated(Event):
    """Изменился состав участников комнаты.

    Нужен автодополнению ника: список участников знает только сессия, а строке
    ввода он нужен целиком.
    """

    jid: str
    nicks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActiveConversationChanged(Event):
    """Переключена активная беседа. None означает, что активной беседы нет."""

    jid: str | None


@dataclass(frozen=True, slots=True)
class Notice(Event):
    """Служебное сообщение для области чата."""

    text: str
    level: NoticeLevel = NoticeLevel.INFO


@dataclass(frozen=True, slots=True)
class UnsafeModeChanged(Event):
    """Переключен режим показа сырого XML без маскирования."""

    enabled: bool


@dataclass(frozen=True, slots=True)
class XmlLogModeChanged(Event):
    """Переключен режим панели лога."""

    mode: XmlMode


@dataclass(frozen=True, slots=True)
class CommandFeedback(Event):
    """Результат исполнения слэш-команды: текст ответа и признак успеха."""

    text: str
    ok: bool = True


@dataclass(frozen=True, slots=True)
class CommandTable(Event):
    """Табличный ответ команды: заголовок и пары имя-значение.

    Отдельное событие нужно, чтобы вывод всех отладочных команд шел одним путем.
    Ручные пробелы в многострочном тексте выравнивание не держат: колонка значения
    при переносе разъезжается, а отступы у разных команд получаются разными.
    """

    title: str
    rows: tuple[tuple[str, str], ...]


# Команды.


@dataclass(frozen=True, slots=True)
class Connect(Command):
    """Подключиться к серверу."""


@dataclass(frozen=True, slots=True)
class Disconnect(Command):
    """Отключиться, не пытаясь восстановить сессию."""


@dataclass(frozen=True, slots=True)
class Reconnect(Command):
    """Переподключиться с попыткой возобновления потока."""


@dataclass(frozen=True, slots=True)
class SendText(Command):
    """Отправить текст в беседу."""

    conversation: str
    text: str


@dataclass(frozen=True, slots=True)
class OpenConversation(Command):
    """Открыть беседу и сделать ее активной."""

    jid: str


@dataclass(frozen=True, slots=True)
class CloseConversation(Command):
    """Закрыть беседу. None закрывает активную."""

    jid: str | None = None


@dataclass(frozen=True, slots=True)
class SetXmlMode(Command):
    """Задать режим панели лога."""

    mode: XmlMode


@dataclass(frozen=True, slots=True)
class SetXmlFilter(Command):
    """Задать фильтр панели лога. Пустая строка снимает фильтр."""

    expression: str


@dataclass(frozen=True, slots=True)
class SetUnsafeXml(Command):
    """Включить или выключить показ сырого XML без маскирования."""

    enabled: bool


@dataclass(frozen=True, slots=True)
class ClearXmlLog(Command):
    """Очистить буфер панели лога."""


@dataclass(frozen=True, slots=True)
class SendRawXml(Command):
    """Отправить произвольную строфу как есть."""

    xml: str


@dataclass(frozen=True, slots=True)
class PingServer(Command):
    """Пинг по XEP-0199. None отправляет пинг на сервер аккаунта."""

    target: str | None = None


@dataclass(frozen=True, slots=True)
class RequestMam(Command):
    """Запросить архив беседы."""

    jid: str
    limit: int = 50
    before: str = ""
    """Страница до указанного идентификатора. Пустая строка означает последнюю."""


@dataclass(frozen=True, slots=True)
class RequestDisco(Command):
    """Запросить disco info и items у сущности."""

    jid: str
    node: str | None = None


@dataclass(frozen=True, slots=True)
class SetPresence(Command):
    """Сменить присутствие."""

    show: PresenceShow
    status: str = ""


@dataclass(frozen=True, slots=True)
class SetChatState(Command):
    """Сообщить собеседнику состояние набора по XEP-0085.

    Команда, а не событие: состояние набора уходит собеседнику, то есть это
    действие над сетью. Источник - строка ввода, она же решает, когда состояние
    сменилось: у сессии текста ввода нет.
    """

    conversation: str
    state: str
    """Значение по XEP-0085: composing, paused, active или gone."""


@dataclass(frozen=True, slots=True)
class SetOmemoEnabled(Command):
    """Включить или выключить сквозное шифрование. None означает активную беседу."""

    conversation: str | None
    enabled: bool


@dataclass(frozen=True, slots=True)
class Quit(Command):
    """Завершить работу приложения."""


@dataclass(frozen=True, slots=True)
class RunCommandLine(Command):
    """Строка из поля ввода целиком.

    Основной путь для слэш-команд: разбор выполняет роутер через
    ``termisations.core.commands.parse``, а не виджет ввода.
    """

    line: str


# Шина.

CommandHandler = Callable[[Command], Awaitable[None]]
"""Обработчик команд: одна корутина на все типы команд, разбор по isinstance."""

_E = TypeVar("_E", bound=Event)

_DEFAULT_LOGGER: Final = logging.getLogger("termisations.bus")


@dataclass(frozen=True, slots=True)
class _StopLoop(Command):
    """Внутренний маркер остановки цикла команд."""


class EventBus:
    """Публикация событий по типу и очередь команд.

    Подписка идет на класс события. Подписка на базовый ``Event`` получает все
    события: поиск обработчиков идет по всей цепочке наследования.
    """

    __slots__ = ("_command_handler", "_logger", "_queue", "_running", "_subscribers")

    def __init__(self, logger: logging.Logger | None = None) -> None:
        # Значение словаря типизировано как Callable[[Any], None]: обобщенная
        # подписка по типу события иначе не выражается в системе типов.
        self._subscribers: dict[type[Event], list[Callable[[Any], None]]] = {}
        self._queue: asyncio.Queue[Command] = asyncio.Queue()
        self._command_handler: CommandHandler | None = None
        self._logger = logger if logger is not None else _DEFAULT_LOGGER
        self._running = False

    # События.

    def subscribe(self, event_type: type[_E], handler: Callable[[_E], None]) -> Callable[[], None]:
        """Подписать обработчик на тип события.

        Возвращает функцию отписки. Повторный вызов функции отписки безопасен.
        """
        self._subscribers.setdefault(event_type, []).append(handler)

        def unsubscribe() -> None:
            bucket = self._subscribers.get(event_type)
            if bucket is None:
                return
            with contextlib.suppress(ValueError):
                bucket.remove(handler)
            if not bucket:
                self._subscribers.pop(event_type, None)

        return unsubscribe

    def publish(self, event: Event) -> None:
        """Синхронно вызвать подписчиков события.

        Публикация синхронная. Панель сырого XML принимает до 500 строф
        в секунду, и заводить корутину на каждую строфу неоправданно дорого:
        планировщик asyncio стоит дороже самого полезного действия. Поэтому
        подписчик обязан быть дешевым - положить данные в буфер и выйти.
        Любая тяжелая работа (разбор, pretty-print, рендер) выполняется
        отдельным таймером панели, а не в обработчике.

        Исключение в одном подписчике не мешает остальным: оно логируется
        и цикл продолжается.
        """
        for klass in type(event).__mro__:
            bucket = self._subscribers.get(klass)
            if not bucket:
                continue
            # Копия списка нужна, потому что подписчик вправе отписаться прямо
            # во время обработки события.
            for handler in tuple(bucket):
                try:
                    handler(event)
                except Exception:
                    self._logger.exception(
                        _("subscriber %r failed on event %s"),
                        handler,
                        type(event).__name__,
                    )

    # Команды.

    def set_command_handler(self, handler: CommandHandler) -> None:
        """Назначить обработчик команд. Активен ровно один обработчик."""
        self._command_handler = handler

    def dispatch(self, command: Command) -> None:
        """Положить команду в очередь. Вызов не блокирующий и не ждет результата."""
        self._queue.put_nowait(command)

    async def run(self) -> None:
        """Цикл разбора очереди команд. Завершается по stop() или по отмене задачи."""
        self._running = True
        try:
            while True:
                command = await self._queue.get()
                try:
                    if isinstance(command, _StopLoop):
                        break
                    handler = self._command_handler
                    if handler is None:
                        self._logger.warning(
                            _("command %s dropped: no handler assigned"),
                            type(command).__name__,
                        )
                        continue
                    try:
                        await handler(command)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self._logger.exception(
                            _("handler failed on command %s"),
                            type(command).__name__,
                        )
                finally:
                    self._queue.task_done()
        finally:
            self._running = False

    def stop(self) -> None:
        """Остановить цикл команд. Команды, уже стоящие в очереди, будут исполнены."""
        self._queue.put_nowait(_StopLoop())

    @property
    def running(self) -> bool:
        """Работает ли цикл разбора команд."""
        return self._running

    @property
    def pending_commands(self) -> int:
        """Число команд в очереди. Используется командой /stats."""
        return self._queue.qsize()


class Subscription:
    """Пачка отписок.

    Виджет складывает сюда результаты bus.subscribe в on_mount и вызывает close
    в on_unmount. Это снимает все подписки разом и исключает обращение к виджету,
    которого уже нет в дереве.
    """

    __slots__ = ("_callbacks",)

    def __init__(self) -> None:
        self._callbacks: list[Callable[[], None]] = []

    def add(self, unsubscribe: Callable[[], None]) -> None:
        """Запомнить функцию отписки."""
        self._callbacks.append(unsubscribe)

    def close(self) -> None:
        """Снять все подписки пачки. Повторный вызов безопасен."""
        for callback in self._callbacks:
            callback()
        self._callbacks.clear()

    def __len__(self) -> int:
        """Число активных подписок в пачке."""
        return len(self._callbacks)
