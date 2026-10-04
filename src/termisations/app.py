"""Каркас приложения: раскладка, режимы, клавиши и соединение слоев.

Здесь и в ``cli.py`` слои сходятся вместе: создается шина, к ней подключается
источник событий, а панели подписываются на нужные им типы событий сами.
Приложение не обращается к сети и не разбирает протокол: любая введенная строка
уходит командой в шину, а исполняет ее сессия.

Переключение раскладки сделано сменой CSS-классов на контейнере ``#body``, а не
пересборкой дерева виджетов. Пересборка отрисовывает панели заново, дает заметный
кадр с пустым экраном и теряет позицию прокрутки.
"""

import os
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Final, TypeVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.command import CommandPalette, DiscoveryHit, Hit, Hits, Provider
from textual.containers import Container, Vertical
from textual.css.query import NoMatches
from textual.events import Resize
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Static

from termisations import xeps
from termisations.core import commands, i18n
from termisations.core.events import (
    ActiveConversationChanged,
    ConversationsUpdated,
    EventBus,
    NoticeLevel,
    OccupantsUpdated,
    OpenConversation,
    RosterUpdated,
    RunCommandLine,
    SendRawXml,
    SetChatState,
    SetUnsafeXml,
    SetXmlFilter,
    SetXmlMode,
    StanzaLogged,
    StateUpdated,
    Subscription,
    UnsafeModeChanged,
    XmlLogModeChanged,
    XmlMode,
)
from termisations.core.i18n import N_, _
from termisations.core.inputlog import InputLog
from termisations.core.models import (
    MUC_MARK,
    PRESENCE_LABELS,
    PRESENCE_MARKS,
    ClientState,
    Conversation,
    Direction,
    PresenceShow,
    RawStanza,
    RosterItem,
    format_latency,
    humanize_bytes,
    humanize_duration,
    stage_label,
    transport_label,
)
from termisations.core.redact import UNSAFE_WARNING, redact, redaction_summary
from termisations.core.session import Session
from termisations.layouts import LAYOUT_MODES
from termisations.ui.chat import ChatPanel
from termisations.ui.prompt import PromptInput
from termisations.ui.statusbar import StatusBar
from termisations.ui.xmllog import XmlLogPanel

__all__ = [
    "QuitConfirmScreen",
    "RosterEntry",
    "RosterProvider",
    "SlashCommandProvider",
    "TermisationsApp",
]

# Раскладка ЙЦУКЕН: какая русская буква стоит на клавише с латинской. Терминал
# с расширенным протоколом клавиатуры передает символ вместе с модификатором, а
# не управляющий код, поэтому при русской раскладке приходит ctrl+с, а не
# ctrl+c, и ни одно сочетание приложения не срабатывает.
_CYRILLIC_TWINS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "q": "й",
        "w": "ц",
        "e": "у",
        "r": "к",
        "t": "е",
        "y": "н",
        "u": "г",
        "i": "ш",
        "o": "щ",
        "p": "з",
        "a": "ф",
        "s": "ы",
        "d": "в",
        "f": "а",
        "g": "п",
        "h": "р",
        "j": "о",
        "k": "л",
        "l": "д",
        "z": "я",
        "x": "ч",
        "c": "с",
        "v": "м",
        "b": "и",
        "n": "т",
        "m": "ь",
    }
)


def with_cyrillic(bindings: Sequence[BindingType]) -> list[BindingType]:
    """Дополнить привязки их кириллическими двойниками.

    Пишется один раз списком, а не руками у каждой клавиши: пропущенный
    двойник обнаруживается не сборкой, а пользователем на русской раскладке.
    Двойники скрыты из подсказки - в ней и без них тесно.
    """
    extra: list[BindingType] = []
    for binding in bindings:
        if not isinstance(binding, Binding):
            continue
        prefix, _plus, letter = binding.key.rpartition("+")
        twin = _CYRILLIC_TWINS.get(letter)
        if not prefix or twin is None:
            continue
        # Верхний регистр нужен из-за Caps Lock: терминал шлет символ как есть,
        # и с включенным Caps приходит заглавная буква.
        for key in (twin, twin.upper()):
            extra.append(replace(binding, key=f"{prefix}+{key}", show=False))
    return [*bindings, *extra]


# Пороги адаптивной раскладки.
WIDE_WIDTH: Final = 160
"""От этой ширины панель лога становится справа от беседы."""

MIN_WIDTH: Final = 80
"""Ниже этой ширины панель лога скрывается: на 79 колонках две панели нечитаемы."""

_DEFAULT_XML_BUFFER: Final = 2000
"""Размер кольцевого буфера строф по умолчанию, совпадает с умолчанием панели."""

_W = TypeVar("_W", bound=Widget)
"""Тип виджета для поиска по селектору."""

_LOG_TITLE: Final = "RAW XML"
"""Заголовок рамки панели сырого потока."""

_LOG_TITLE_UNSAFE: Final = "RAW XML  UNSAFE"
"""Заголовок панели, когда маскирование отключено."""

_PALETTE_PLACEHOLDER: Final = N_("› search commands")
"""Подпись поля поиска в палитре: своя, на языке интерфейса, вместо подписи Textual."""

_ROSTER_PLACEHOLDER: Final = N_("› contact, room or occupant")
"""Подпись поля поиска в палитре контактов."""

# Подпись записи участника комнаты в палитре: по ней видно, что это не контакт.
_OCCUPANT_HINT: Final = N_("room occupant")

_MAX_DOMAIN_WIDTH: Final = 24
"""Предел колонки сервера в палитре контактов: длинный домен не должен занимать всю строку."""

_NAME_WIDTH: Final = 22
"""Ширина колонки имени в палитре контактов."""

_LOCAL_WIDTH: Final = 14
"""Ширина колонки локальной части JID: домен показан отдельной колонкой слева."""

_MUC_HINT: Final = N_("room")
"""Подсказка вместо присутствия у комнаты: присутствие там означает вход, а не доступность."""

# Состояния подписки, при которых обмен присутствием полный. Остальные значения
# показываются в палитре: односторонняя подписка объясняет пустое присутствие.
_FULL_SUBSCRIPTIONS: Final[frozenset[str]] = frozenset({"both", ""})

_FILTER_USAGE: Final = N_(
    "kind:<type> jid:<address> ns:<namespace> err:<yes|no> and free text, conditions joined by AND"
)
"""Синтаксис фильтра панели лога. Печатается вместе с ошибкой разбора."""

# Ответы, которые считаются подтверждением небезопасного режима.
_CONFIRMATIONS: Final[frozenset[str]] = frozenset({"yes", "да"})

_SEND_QUESTION: Final = N_("type yes to send, any other answer cancels")
_UNSAFE_QUESTION: Final = N_("type yes to confirm, any other answer cancels")
"""Вопросы, на которые ждет ответа строка ввода. Печатаются в ленте и в подсказке."""

# Клавиши, которые обрабатывает не приложение, а строка ввода или сам Textual.
# В таблице /keys они нужны, но объявлять их в BINDINGS нельзя: перехват сломает
# автодополнение и историю ввода.
_EXTERNAL_KEYS: Final[tuple[tuple[str, str], ...]] = (
    ("ctrl+p", N_("command palette with fuzzy search")),
    ("tab", N_("completion of command, JID, nick")),
    ("up / down", N_("input history")),
    ("ctrl+r", N_("reverse search in input history, ctrl+g or escape cancels")),
    ("alt+enter / ctrl+j", N_("line break in multiline input")),
    ("backspace", N_("restore the previous line of multiline input")),
    ("enter", N_("send the line")),
    ("ctrl+q", N_("quit without confirmation")),
)

# Клавиши панели RAW XML. Панель объявляет их у себя со show=False, в таблицу /keys
# они попадают отсюда вместе с единственным способом перевести на нее фокус.
# Клавиш, которые приложение уже объявило в BINDINGS (end, pageup, pagedown),
# здесь нет: они работают одинаково отовсюду, и вторая строка о них только мешает.
_PANEL_KEYS: Final[tuple[tuple[str, str], ...]] = (
    ("shift+tab", N_("focus the RAW XML panel and back")),
    ("up / down", N_("move the cursor through stanzas")),
    ("enter", N_("expand the stanza under the cursor")),
)

# Шаблон, по которому в таблице клавиш схлопываются девять одинаковых строк
# alt+1..alt+9: они об одном действии.
_ALT_NUMBER_KEYS: Final = tuple(f"alt+{number}" for number in range(1, 10))

# Названия языков для /lang. Переводятся на текущий язык интерфейса.
_LANGUAGE_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {"en": N_("English"), "ru": N_("Russian")}
)


def _language_name(code: str) -> str:
    """Название языка на текущем языке. Язык без названия показывается кодом."""
    name = _LANGUAGE_NAMES.get(code)
    return _(name) if name is not None else code


class QuitConfirmScreen(ModalScreen[bool]):
    """Подтверждение выхода по Ctrl+C.

    Textual перехватывает Ctrl+C сам и только показывает подсказку, поэтому клавиша
    переопределена в приложении, а подтверждение сделано отдельным модальным экраном.
    Экран не требует терминала и работает под ``App.run_test``.
    """

    # Буквы продублированы кириллицей: на русской раскладке латинские y и n не
    # приходят вовсе, и диалог оставался без ответа. Кириллические выбраны по
    # смыслу, а не по позиции клавиши: д - да, н - нет. Физическая клавиша y на
    # русской раскладке дает как раз "н", и трактовать ее как согласие значило
    # бы выходить из клиента по нажатию, которое пользователь считает отказом.
    # Ctrl+C продублирован по позиции: там буква не читается пользователем, а
    # служит именем клавиши. Escape от раскладки не зависит вовсе.
    BINDINGS: ClassVar[list[BindingType]] = with_cyrillic(
        [
            Binding("y", "confirm", N_("leave"), priority=True),
            Binding("д", "confirm", N_("leave"), priority=True, show=False),
            Binding("enter", "confirm", N_("leave"), priority=True),
            Binding("n", "cancel", N_("stay"), priority=True),
            Binding("н", "cancel", N_("stay"), priority=True, show=False),
            Binding("escape", "cancel", N_("stay"), priority=True),
            Binding("ctrl+c", "confirm", N_("leave"), priority=True, show=False),
        ]
    )

    def compose(self) -> ComposeResult:
        """Собрать диалог подтверждения."""
        # Кириллические клавиши подставляются, а не стоят в ключе перевода: это
        # имена клавиш, и на любом языке интерфейса они одни и те же.
        text = _(
            "Quit termisations?\n\n"
            "Ctrl+C again - quit immediately\n"
            "y, {yes_key}, Enter - quit. n, {no_key}, Esc - stay"
        ).format(yes_key="д", no_key="н")
        yield Static(text, id="quit-dialog")

    def action_confirm(self) -> None:
        """Подтвердить выход."""
        self.dismiss(True)

    def action_cancel(self) -> None:
        """Отменить выход."""
        self.dismiss(False)


class SlashCommandProvider(Provider):
    """Поставщик слэш-команд для штатной палитры Textual (Ctrl+P).

    Свой нечеткий поиск не пишется: палитра Textual уже умеет ранжировать и
    подсвечивать совпадения, нужен только источник строк. Источник - реестр
    ``core.commands.REGISTRY``, поэтому новая команда появляется в палитре
    без правок этого файла.
    """

    def _target(self) -> "TermisationsApp | None":
        """Приложение, которому адресована команда. None, если провайдер вне termisations."""
        app = self.app
        return app if isinstance(app, TermisationsApp) else None

    async def discover(self) -> Hits:
        """Показать весь реестр, пока запрос пустой."""
        target = self._target()
        if target is None:
            return
        for spec in commands.REGISTRY:
            yield DiscoveryHit(
                f"/{spec.name}",
                partial(target.run_palette_command, spec),
                text=f"/{spec.name}",
                help=_(spec.summary),
            )

    async def search(self, query: str) -> Hits:
        """Отобрать команды по запросу палитры."""
        target = self._target()
        if target is None:
            return
        matcher = self.matcher(query)
        for spec in commands.REGISTRY:
            candidate = f"/{spec.name}"
            # Алиасы участвуют в поиске, но показывается всегда основное имя:
            # иначе одна команда попадает в список несколько раз.
            score = max(
                (matcher.match(f"/{alias}") for alias in spec.aliases),
                default=0.0,
            )
            score = max(score, matcher.match(candidate))
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(candidate),
                    partial(target.run_palette_command, spec),
                    text=candidate,
                    help=_(spec.summary),
                )


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """Строка палитры контактов: контакт из roster или открытая беседа."""

    jid: str
    title: str
    show: PresenceShow
    is_muc: bool = False
    unread: int = 0
    subscription: str = ""
    status: str = ""

    @property
    def domain(self) -> str:
        """Сервер контакта. По нему записи группируются в палитре."""
        _local, _sep, domain = self.jid.partition("@")
        return domain or self.jid

    @property
    def local(self) -> str:
        """Локальная часть JID. Домен показан отдельной колонкой."""
        local, _sep, _domain = self.jid.partition("@")
        return local or self.jid

    @property
    def online(self) -> bool:
        """Контакт доступен. У комнаты означает, что вход выполнен."""
        return self.show is not PresenceShow.OFFLINE

    @property
    def hint(self) -> str:
        """Правая колонка строки: то, чего не видно по знаку присутствия.

        Подсказка вписана в саму строку, а не отдана палитре полем help: help
        Textual печатает отдельной строкой во всю ширину, и список контактов
        становится вдвое выше без единого нового слова.
        """
        parts: list[str] = []
        if self.is_muc:
            parts.append(_(_MUC_HINT))
        if self.unread:
            parts.append(f"+{self.unread}")
        if self.status:
            parts.append(self.status)
        elif not self.is_muc and self.show is not PresenceShow.AVAILABLE:
            # Текст статуса важнее подписи присутствия: знак слева ее уже передал.
            parts.append(_(PRESENCE_LABELS[self.show]))
        if self.subscription and self.subscription not in _FULL_SUBSCRIPTIONS:
            parts.append(_("subscription {state}").format(state=self.subscription))
        return ", ".join(parts)


def _entry_order(entry: RosterEntry) -> tuple[str, int, str]:
    """Порядок в палитре: сервер, затем доступные контакты, затем имя."""
    return (entry.domain, 0 if entry.show is not PresenceShow.OFFLINE else 1, entry.title.lower())


def _domain_width(entries: Sequence[RosterEntry]) -> int:
    """Ширина колонки сервера. Ограничена, чтобы длинный домен не занимал всю строку."""
    if not entries:
        return 0
    return min(max(len(entry.domain) for entry in entries), _MAX_DOMAIN_WIDTH)


def _entry_display(entry: RosterEntry, domain_width: int, show_domain: bool) -> Text:
    """Строка контакта с колонкой сервера.

    Домен печатается только у первой записи группы: повтор у каждой строки
    превращает список в столбец одинакового текста. Палитра Textual не умеет
    невыбираемых строк, поэтому группа обозначена колонкой, а не разделителем.
    """
    mark, mark_style = PRESENCE_MARKS[entry.show]
    domain = _ellipsis(entry.domain, domain_width) if show_domain else ""
    line = Text(f"{domain:<{domain_width}}  ", style="bold" if show_domain else "")
    line.append(f"{MUC_MARK if entry.is_muc else mark} ", style=mark_style)
    line.append(_ellipsis(entry.title, _NAME_WIDTH).ljust(_NAME_WIDTH))
    line.append(f"  {entry.local:<{_LOCAL_WIDTH}}", style="dim")
    if entry.hint:
        line.append(f"  {entry.hint}", style="dim italic")
    return line


def _ellipsis(text: str, width: int) -> str:
    """Усечь строку до ширины колонки, пометив усечение многоточием."""
    if width <= 0 or len(text) <= width:
        return text
    return text[: max(width - 1, 0)] + "…"


class RosterProvider(Provider):
    """Поставщик контактов для палитры перехода по Ctrl+O.

    Устроен так же, как палитра команд: нечеткий поиск и ранжирование берутся у
    Textual, отсюда приходит только источник строк. Источник - контакт-лист плюс
    открытые беседы, которых в контакт-листе нет: комнаты в roster не попадают,
    но перейти в них нужно так же.
    """

    def _target(self) -> "TermisationsApp | None":
        """Приложение, которому адресован переход. None, если провайдер вне termisations."""
        app = self.app
        return app if isinstance(app, TermisationsApp) else None

    async def discover(self) -> Hits:
        """Показать весь список, пока запрос пустой: записи сгруппированы по серверу."""
        target = self._target()
        if target is None:
            return
        entries = target.roster_entries()
        width = _domain_width(entries)
        previous = ""
        for entry in entries:
            display = _entry_display(entry, width, show_domain=entry.domain != previous)
            previous = entry.domain
            yield DiscoveryHit(
                display,
                partial(target.open_from_palette, entry.jid),
                text=entry.jid,
            )

    async def search(self, query: str) -> Hits:
        """Отобрать контакты по запросу. Ищется и имя, и адрес целиком.

        Группировка по серверу здесь не показывается: строки отбираются по
        совпадению, соседство по домену случайно, а колонка домена только
        мешала бы подсветке. Вместо нее печатается полный JID.
        """
        target = self._target()
        if target is None:
            return
        matcher = self.matcher(query)
        for entry in target.roster_entries():
            candidate = entry.jid if entry.title == entry.jid else f"{entry.title} {entry.jid}"
            score = matcher.match(candidate)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(candidate),
                    partial(target.open_from_palette, entry.jid),
                    text=entry.jid,
                    help=entry.hint or None,
                )


class TermisationsApp(App[None]):
    """Textual-приложение клиента: раскладка, клавиши, маршрутизация ввода."""

    TITLE = "termisations"
    SUB_TITLE = N_("XMPP with a transparent protocol")
    CSS_PATH = "app.tcss"

    # Фокус ставится вручную на строку ввода, автоподбор первого виджета не нужен.
    AUTO_FOCUS = None

    ENABLE_COMMAND_PALETTE = True
    COMMANDS: ClassVar[set[type[Provider] | Callable[[], type[Provider]]]] = {
        *App.COMMANDS,
        SlashCommandProvider,
    }

    # priority=True обязателен там, где клавиша занята полем ввода: Input держит
    # ctrl+d на удаление символа, а ctrl+c на копирование. Без приоритета
    # приложение эти клавиши не увидит, пока фокус в строке ввода.
    BINDINGS: ClassVar[list[BindingType]] = with_cyrillic(
        [
            Binding("ctrl+d", "cycle_layout", N_("layout"), priority=True),
            Binding("ctrl+l", "clear_log", N_("clear log"), priority=True),
            Binding("ctrl+f", "filter_log", N_("log filter"), priority=True),
            # Ctrl+O, а не Ctrl+R: обратный поиск по истории ввода занял Ctrl+R,
            # как в readline и bash. Буква тут по действию - палитра открывает
            # беседу с выбранным контактом.
            Binding("ctrl+o", "roster_palette", N_("contact list"), priority=True),
            Binding("ctrl+n", "next_conversation", N_("next conversation"), priority=True),
            Binding("ctrl+b", "prev_conversation", N_("previous conversation"), priority=True),
            Binding("ctrl+c", "request_quit", N_("quit"), priority=True),
            Binding("escape", "reset", N_("reset filter and mode"), priority=True, show=False),
            Binding("pageup", "page_up", N_("scroll up"), show=False),
            Binding("pagedown", "page_down", N_("scroll down"), show=False),
            # end работает из строки ввода: иначе из pause, вызванного курсором панели,
            # выйти нечем, кроме ctrl+l, а он очищает буфер.
            Binding(
                "end",
                "follow_end",
                N_("go to the end of the feed and the stream, leave pause"),
                priority=True,
                show=False,
            ),
            *[
                Binding(
                    f"alt+{number}",
                    f"goto_conversation({number})",
                    N_("conversation by number"),
                    show=False,
                )
                for number in range(1, 10)
            ],
        ]
    )

    layout_mode: reactive[str] = reactive("split", init=False)
    """Текущая раскладка: focus, split или debug."""

    def __init__(
        self,
        bus: EventBus,
        session: Session,
        *,
        layout: str = "split",
        xml_buffer: int | None = None,
        input_log: InputLog | None = None,
    ) -> None:
        """Собрать приложение поверх готовой шины и сессии.

        Шина и сессия создаются снаружи (в ``cli.main``), приложение только связывает
        их с виджетами. Так те же объекты поднимаются в тесте без терминала.

        ``input_log`` - общая для всех профилей история ввода, уже открытая и
        прочитанная. ``None`` означает работу без нее: так запускается клиент с
        ``--no-history`` и клиент, которому файл истории оказался недоступен.
        """
        super().__init__()
        self._bus = bus
        self._session = session
        self._initial_layout = layout if layout in LAYOUT_MODES else "split"
        self._xml_buffer = xml_buffer
        self._input_log = input_log
        self._subs = Subscription()

        # Ссылки на панели заполняются в on_mount: до монтирования дерева их нет.
        self._chat: ChatPanel | None = None
        self._xmllog: XmlLogPanel | None = None
        self._prompt: PromptInput | None = None
        self._status: StatusBar | None = None

        # Локальная копия состояния только для вывода команд и навигации.
        # Источник правды - сессия, здесь хранится последний присланный снимок.
        self._state = ClientState()
        self._conversations: tuple[Conversation, ...] = ()
        self._active: str | None = None
        self._roster: tuple[RosterItem, ...] = ()
        # Участники комнат по JID комнаты: источник вариантов дополнения ника.
        self._occupants: dict[str, tuple[str, ...]] = {}
        self._xml_mode = XmlMode.BOTH
        self._xml_filter = ""
        self._unsafe = False

        # Собственный буфер строф нужен команде /log save: панель лога хранит
        # буфер у себя, но публичного метода для его чтения у нее нет. Хранятся
        # ссылки на те же неизменяемые объекты, копирования текста не происходит.
        self._stanzas: deque[RawStanza] = deque(maxlen=xml_buffer or _DEFAULT_XML_BUFFER)

        self._pending_unsafe: XmlMode | None = None
        self._awaiting_unsafe = False
        # Строфа, ожидающая подтверждения командой /send. Механизм подтверждения
        # один и тот же: ввод yes, любой другой ответ отменяет.
        self._pending_send: str | None = None
        self._startup_notices: list[str] = []
        self._log_hidden = False
        self._started = time.monotonic()

        # Команда исполняется сессией, приложение только кладет ее в очередь.
        self._bus.set_command_handler(self._session.handle_command)

        # Разбор по handler_key вместо цепочки if: реестр команд - это данные.
        # Набор ключей обязан совпадать с router.UI_KEYS: там записано, какие
        # команды исполняет интерфейс, и сессия на них отвечает отказом. Равенство
        # проверяет tests/test_router.py, расхождение означает команду без хозяина.
        self._local: dict[str, Callable[[commands.ParsedCommand], None]] = {
            "sys.help": self._cmd_help,
            "sys.keys": self._cmd_keys,
            "sys.theme": self._cmd_theme,
            "sys.lang": self._cmd_lang,
            "sys.quit": self._cmd_quit,
            "sys.log.save": self._cmd_log_save,
            "chat.clear": self._cmd_clear,
            "debug.send": self._cmd_send,
            "debug.panel": self._cmd_debug_panel,
            "debug.xml": self._cmd_xml,
            "debug.xml.filter": self._cmd_xml_filter,
            "debug.stats": self._cmd_stats,
        }

    # Сборка дерева.

    def _create_xml_log(self) -> XmlLogPanel:
        """Создать панель лога с размером кольцевого буфера из аргументов запуска."""
        if self._xml_buffer is None:
            return XmlLogPanel(self._bus, id="xmllog")
        return XmlLogPanel(self._bus, capacity=self._xml_buffer, id="xmllog")

    def _create_prompt(self) -> PromptInput:
        """Создать строку ввода с историей прошлых запусков."""
        if self._input_log is None:
            return PromptInput(self._bus, id="prompt")
        return PromptInput(
            self._bus,
            id="prompt",
            history=self._input_log.lines,
            record=self._record_input,
        )

    def _record_input(self, line: str) -> None:
        """Записать введенную строку в общую историю.

        Запись идет воркером: файл истории лежит на том же диске, что база
        переписки, и синхронная дозапись превратилась бы в паузу на каждый Enter.
        Отказ записи переписке не мешает и наружу не идет: ``InputLog`` отмечает
        его в журнале сам.
        """
        log = self._input_log
        if log is None or not log.writable:
            return
        self.run_worker(log.append(line), name="input-history", group="io", exit_on_error=False)

    def compose(self) -> ComposeResult:
        """Дерево виджетов. Состав постоянный, меняются только классы ``#body``."""
        with Vertical(id="root"):
            with Container(id="body", classes="-mode-split -log-right"):
                yield ChatPanel(self._bus, id="chat")
                yield self._create_xml_log()
            yield self._create_prompt()
            yield StatusBar(self._bus, id="statusbar")

    def on_mount(self) -> None:
        """Запомнить панели, подписаться на шину и запустить фоновые задачи."""
        self._chat = self._find("#chat", ChatPanel)
        self._xmllog = self._find("#xmllog", XmlLogPanel)
        self._prompt = self._find("#prompt", PromptInput)
        self._status = self._find("#statusbar", StatusBar)

        self.apply_language()

        self._subs.add(self._bus.subscribe(StateUpdated, self._on_state))
        self._subs.add(self._bus.subscribe(ConversationsUpdated, self._on_conversations))
        self._subs.add(self._bus.subscribe(ActiveConversationChanged, self._on_active))
        self._subs.add(self._bus.subscribe(RosterUpdated, self._on_roster))
        self._subs.add(self._bus.subscribe(OccupantsUpdated, self._on_occupants))
        self._subs.add(self._bus.subscribe(UnsafeModeChanged, self._on_unsafe))
        self._subs.add(self._bus.subscribe(XmlLogModeChanged, self._on_xml_mode))
        self._subs.add(self._bus.subscribe(StanzaLogged, self._on_stanza))

        self.layout_mode = self._initial_layout
        self._apply_layout_classes()
        self._apply_width(self.size.width)

        # Шина и сессия живут воркерами Textual: их отменяет сам фреймворк при
        # завершении приложения, отдельная сборка мусора не нужна.
        self.run_worker(self._bus.run(), name="bus", group="core", exit_on_error=False)
        self.run_worker(self._session.run(), name="session", group="core", exit_on_error=False)

        if self._prompt is not None:
            self._prompt.focus_input()
        for text in self._startup_notices:
            self._notice(text, NoticeLevel.WARNING)
        self._startup_notices.clear()

    def apply_language(self) -> None:
        """Выставить статический текст приложения на текущем языке.

        Заголовки рамок рисует приложение: рамки заданы в app.tcss, а текста
        заголовка в CSS нет, он задается свойством виджета. Панели выставляют
        свой текст сами, их ``apply_language`` вызывает команда /lang.
        """
        self.sub_title = _(self.SUB_TITLE)
        if self._chat is not None:
            self._chat.border_title = self.TITLE
        if self._xmllog is not None:
            self._xmllog.border_title = _LOG_TITLE_UNSAFE if self._unsafe else _LOG_TITLE

    def on_unmount(self) -> None:
        """Снять подписки и остановить фоновые циклы."""
        self._subs.close()
        self._session.stop()
        self._bus.stop()

    def _find(self, selector: str, kind: type[_W]) -> _W | None:
        """Найти виджет по селектору. None вместо исключения, если его еще нет."""
        try:
            return self.query_one(selector, kind)
        except NoMatches:
            return None

    # Раскладка.

    def watch_layout_mode(self, mode: str) -> None:
        """Применить классы раскладки при смене режима."""
        del mode
        self._apply_layout_classes()

    def _apply_layout_classes(self) -> None:
        """Проставить классы режима на контейнере ``#body``."""
        body = self._find("#body", Container)
        if body is None:
            return
        for name in LAYOUT_MODES:
            body.set_class(name == self.layout_mode, f"-mode-{name}")

    def _apply_width(self, width: int) -> None:
        """Выбрать положение панели лога по ширине терминала."""
        body = self._find("#body", Container)
        if body is None:
            return
        if width < MIN_WIDTH:
            target = "-log-hidden"
        elif width >= WIDE_WIDTH:
            target = "-log-right"
        else:
            target = "-log-bottom"
        for name in ("-log-right", "-log-bottom", "-log-hidden"):
            body.set_class(name == target, name)

        hidden = target == "-log-hidden"
        if hidden and not self._log_hidden:
            self.notify(
                _(
                    "width of {width} columns is below {minimum}: the RAW XML panel is hidden"
                ).format(width=width, minimum=MIN_WIDTH),
                title=_("layout"),
                severity="warning",
            )
        self._log_hidden = hidden

    def on_resize(self, event: Resize) -> None:
        """Пересчитать раскладку под новую ширину терминала."""
        self._apply_width(event.size.width)

    # Действия клавиш.

    def action_cycle_layout(self) -> None:
        """Циклически переключить раскладку: focus, split, debug."""
        index = LAYOUT_MODES.index(self.layout_mode) if self.layout_mode in LAYOUT_MODES else 1
        self.layout_mode = LAYOUT_MODES[(index + 1) % len(LAYOUT_MODES)]
        self._notice(_("layout: {mode}").format(mode=self.layout_mode) + self._layout_reason())

    def _layout_reason(self) -> str:
        """Причина, по которой выбранный режим не меняет картинку."""
        if not self._log_hidden:
            return ""
        return _(" (log hidden, width {width} is below {minimum})").format(
            width=self.size.width, minimum=MIN_WIDTH
        )

    def action_follow_end(self) -> None:
        """Вернуть обе панели к концу потока.

        Курсор панели лога переводит ее в pause, и без этой клавиши выйти из
        pause из строки ввода нечем.
        """
        if self._xmllog is not None:
            self._xmllog.action_follow()
        if self._chat is not None:
            self._chat.scroll_to_end()

    def action_clear_log(self) -> None:
        """Очистить панель сырого XML."""
        if self._xmllog is None:
            return
        self._xmllog.clear()
        self._stanzas.clear()
        self._notice(_("RAW XML panel cleared"))

    def action_filter_log(self) -> None:
        """Подставить в строку ввода заготовку фильтра лога."""
        self.prefill_prompt("/xml filter ")

    def action_reset(self) -> None:
        """Сбросить фильтр лога, отменить ожидание подтверждения, вернуть фокус."""
        if CommandPalette.is_open(self.app):
            # Палитра закрывается сама, но привязка Escape у приложения стоит с
            # priority и перехватывает клавишу раньше нее. Без этой ветки из
            # списка контактов по Ctrl+O выйти было бы нечем.
            self.screen.dismiss(None)
            return
        if isinstance(self.screen, QuitConfirmScreen):
            self.screen.dismiss(False)
            return
        # Обратный поиск по истории отменяется отсюда: привязка Escape у
        # приложения стоит с priority, и до строки ввода клавиша не доходит.
        if self._prompt is not None and self._prompt.cancel_search():
            return
        if self._awaiting_unsafe:
            self._cancel_unsafe()
            return
        if self._xml_filter:
            self._set_filter("")
        if self._prompt is not None:
            self._prompt.focus_input()

    def action_next_conversation(self) -> None:
        """Перейти к следующей беседе."""
        self._step_conversation(1)

    def action_prev_conversation(self) -> None:
        """Перейти к предыдущей беседе."""
        self._step_conversation(-1)

    def action_goto_conversation(self, index: int) -> None:
        """Перейти к беседе по ее номеру в списке, нумерация с единицы."""
        if not self._conversations:
            self._notice(_("conversation list is empty"), NoticeLevel.WARNING)
            return
        if not 1 <= index <= len(self._conversations):
            self._notice(
                _("there is no conversation number {index}").format(index=index),
                NoticeLevel.WARNING,
            )
            return
        self._bus.dispatch(OpenConversation(self._conversations[index - 1].jid))

    def action_page_up(self) -> None:
        """Прокрутить активную панель на экран вверх."""
        target = self._scroll_target()
        if target is not None:
            target.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        """Прокрутить активную панель на экран вниз.

        Панель лога сама вернется в follow, когда прокрутка дойдет до конца:
        признак ``_paused_by_scroll`` ставится и при уходе курсором.
        """
        target = self._scroll_target()
        if target is not None:
            target.scroll_page_down(animate=False)

    def action_request_quit(self) -> None:
        """Копировать выделенное, а если выделения нет - запросить выход.

        Ctrl+C в терминале означает две разные вещи, и обе нужны. Textual вешает
        на него копирование выделенного текста, а клиент выходит по этой же
        клавише. Приоритетная привязка приложения забирала бы клавишу целиком, и
        скопировать выделенное мышью было бы нечем.
        """
        if isinstance(self.screen, QuitConfirmScreen):
            # Второе нажатие подряд: подтверждение уже на экране, спрашивать
            # снова незачем. Ctrl+C не зависит от раскладки, поэтому такой выход
            # работает там, где буквы диалога недоступны.
            self.exit()
            return
        selected = self.screen.get_selected_text()
        if selected:
            self.copy_to_clipboard(selected)
            # Выделение снимается сразу: иначе второе нажатие снова копировало
            # бы тот же текст, и до выхода дело не дошло бы никогда.
            self.screen.clear_selection()
            self._notice(
                _("characters copied: {count}").format(count=len(selected)), NoticeLevel.SUCCESS
            )
            return
        self.push_screen(QuitConfirmScreen(), self._on_quit_answer)

    def _on_quit_answer(self, confirmed: bool | None) -> None:
        """Обработать ответ модального экрана подтверждения."""
        if confirmed:
            self.exit()

    def _step_conversation(self, delta: int) -> None:
        """Сдвинуться по списку бесед на delta позиций по кругу."""
        if not self._conversations:
            self._notice(_("conversation list is empty"), NoticeLevel.WARNING)
            return
        jids = [item.jid for item in self._conversations]
        current = jids.index(self._active) if self._active in jids else 0
        self._bus.dispatch(OpenConversation(jids[(current + delta) % len(jids)]))

    def _scroll_target(self) -> Widget | None:
        """Панель, которую прокручивают PgUp и PgDn.

        В режиме debug это лог, в остальных - беседа: прокручивается то, что видно.
        """
        selector = "#xmllog-body" if self.layout_mode == "debug" else "#chat-log"
        target = self._find(selector, Widget)
        if target is None:
            target = self._find("#chat-log", Widget)
        return target

    # Подписки.

    def _on_state(self, event: StateUpdated) -> None:
        """Запомнить снимок состояния для команд /stats и /xml."""
        self._state = event.state

    def _on_conversations(self, event: ConversationsUpdated) -> None:
        """Обновить список бесед для навигации и автодополнения."""
        self._conversations = event.items
        self._push_context()

    def _on_active(self, event: ActiveConversationChanged) -> None:
        """Запомнить активную беседу и передать ее в строку ввода."""
        self._active = event.jid
        self._push_context()

    def _on_roster(self, event: RosterUpdated) -> None:
        """Обновить контакт-лист для автодополнения."""
        self._roster = event.items
        self._push_context()

    def _on_occupants(self, event: OccupantsUpdated) -> None:
        """Запомнить состав комнаты для автодополнения ника."""
        self._occupants[event.jid] = event.nicks
        self._push_context()

    @property
    def _nicks(self) -> tuple[str, ...]:
        """Ники участников активной комнаты. Для личной беседы пусто."""
        if self._active is None:
            return ()
        return self._occupants.get(self._active, ())

    def _on_unsafe(self, event: UnsafeModeChanged) -> None:
        """Принять смену режима маскирования, пришедшую от сессии."""
        self._unsafe = event.enabled
        self._mark_unsafe(event.enabled)
        if self._xmllog is not None:
            self._xmllog.set_unsafe(event.enabled)

    def _mark_unsafe(self, enabled: bool) -> None:
        """Показать или убрать признак отключенного маскирования.

        Признак дублируется. Класс на статус-баре виджет ставит и сам,
        по полю ``ClientState.unsafe_xml``, и очередной снимок состояния его
        перебьет. Класс на ``#root`` и заголовок панели принадлежат приложению,
        их никто не перезапишет, поэтому предупреждение не исчезнет незаметно.
        """
        self.set_class(enabled, "-unsafe")
        root = self._find("#root", Vertical)
        if root is not None:
            root.set_class(enabled, "-unsafe")
        if self._status is not None:
            self._status.set_class(enabled, "-unsafe")
        if self._xmllog is not None:
            self._xmllog.border_title = _LOG_TITLE_UNSAFE if enabled else _LOG_TITLE

    def _on_xml_mode(self, event: XmlLogModeChanged) -> None:
        """Запомнить режим лога и применить его к панели."""
        self._xml_mode = event.mode
        if self._xmllog is not None:
            self._xmllog.set_mode(event.mode)

    def _on_stanza(self, event: StanzaLogged) -> None:
        """Горячий путь: только положить строфу в буфер для /log save."""
        self._stanzas.append(event.stanza)

    def _push_context(self) -> None:
        """Передать строке ввода контекст автодополнения целиком.

        Кроме roster передаются открытые беседы, ники участников активной комнаты,
        множество контактов в сети и фактический список тем оформления. Без них
        дополнение ника и порядок "контакты в сети первыми" не работают вовсе.
        """
        if self._prompt is None:
            return
        known = [item.jid for item in self._roster]
        known.extend(item.jid for item in self._conversations if item.jid not in known)
        self._prompt.set_context(
            self._active,
            known,
            conversations=[item.jid for item in self._conversations],
            nicks=self._nicks,
            online=[item.jid for item in self._roster if item.online],
            themes=sorted(self.available_themes),
        )

    # Ввод.

    @on(PromptInput.Typing)
    def _on_typing(self, event: PromptInput.Typing) -> None:
        """Состояние набора уходит собеседнику активной беседы.

        Решение принимает приложение: строка ввода не знает ни об активной
        беседе, ни о сети. Без активной беседы отправлять состояние некому.
        """
        event.stop()
        if self._active is None:
            return
        self._bus.dispatch(SetChatState(self._active, "composing" if event.active else "paused"))

    @on(PromptInput.Submitted)
    def _on_submitted(self, event: PromptInput.Submitted) -> None:
        """Принять строку из поля ввода."""
        event.stop()
        self.submit_line(event.line)

    def submit_line(self, line: str) -> None:
        """Обработать введенную строку.

        Порядок разбора: подтверждение небезопасного режима, затем разбор команды,
        затем локальные обработчики, и только потом отправка команды в шину.
        Обычный текст и экранированный ``//`` уходят в шину целиком: снимает слэш
        и решает, кому адресован текст, роутер сессии.
        """
        text = line.strip()
        if not text:
            return
        # Отправка строки означает возврат к концу ленты: пользователь ждет ответ
        # именно на нее, и он не должен уйти за нижнюю границу экрана.
        if self._chat is not None:
            self._chat.scroll_to_end()
        if self._awaiting_unsafe:
            self._resolve_unsafe(text)
            return
        if self._pending_send is not None:
            self._resolve_send(text)
            return

        parsed = commands.parse(text)
        if parsed is None:
            self._bus.dispatch(RunCommandLine(text))
            return
        if not parsed.ok:
            # Текст ошибки уже содержит ожидаемую сигнатуру, общего текста
            # "неверная команда" клиент не выводит.
            error = parsed.error or _("unknown command: {text}").format(text=text)
            self._notice(error, NoticeLevel.ERROR)
            return

        handler = self._local.get(parsed.handler_key)
        if handler is not None:
            handler(parsed)
            return
        self._bus.dispatch(RunCommandLine(text))

    def action_command_palette(self) -> None:
        """Открыть палитру команд с подписью поля поиска на языке интерфейса."""
        # is_open объявлен на App[object]; дженерик App[None] к нему не приводится
        # автоматически, хотя тип параметра здесь роли не играет.
        opened = CommandPalette.is_open(self.app)
        if self.use_command_palette and not opened:
            palette = CommandPalette(placeholder=_(_PALETTE_PLACEHOLDER), id="--command-palette")
            self.push_screen(palette)

    @property
    def active_conversation(self) -> str | None:
        """JID активной беседы. None, пока беседа не выбрана."""
        return self._active

    def roster_entries(self) -> tuple[RosterEntry, ...]:
        """Контакты для палитры перехода, отсортированные по серверу.

        Берется контакт-лист и открытые беседы, которых в нем нет: комната в
        roster не попадает, а перейти в нее нужно так же. Данные о непрочитанном
        и о комнате есть только у беседы, присутствие и подписка - только у
        записи контакт-листа, поэтому источники объединяются по JID.
        """
        opened = {item.jid: item for item in self._conversations}
        entries = [
            RosterEntry(
                jid=item.jid,
                title=item.display_name,
                show=item.show,
                is_muc=opened[item.jid].is_muc if item.jid in opened else False,
                unread=opened[item.jid].unread if item.jid in opened else 0,
                subscription=item.subscription,
                status=item.status,
            )
            for item in self._roster
        ]
        known = {item.jid for item in self._roster}
        entries.extend(
            RosterEntry(
                jid=item.jid,
                title=item.display_title,
                show=item.show,
                is_muc=item.is_muc,
                unread=item.unread,
            )
            for item in self._conversations
            if item.jid not in known
        )
        entries.extend(self._occupant_entries())
        return tuple(sorted(entries, key=_entry_order))

    def _occupant_entries(self) -> list[RosterEntry]:
        """Участники активной комнаты отдельными записями палитры.

        Отдельной панели участников нет: на десяти строках она отняла
        бы место у ленты, а вопрос "кто здесь" решается тем же Ctrl+O, что и
        переход по беседам. Число участников видно в заголовке беседы.
        """
        active = self._active
        if active is None:
            return []
        nicks = self._occupants.get(active, ())
        if not nicks:
            return []
        return [
            RosterEntry(
                jid=f"{active}/{nick}",
                title=nick,
                show=PresenceShow.AVAILABLE,
                is_muc=False,
                unread=0,
                status=_(_OCCUPANT_HINT),
            )
            for nick in nicks
        ]

    def action_roster_palette(self) -> None:
        """Ctrl+O - палитра контактов с переходом в выбранную беседу."""
        if CommandPalette.is_open(self.app):
            return
        if not self.roster_entries():
            self._notice(_("roster is empty, no conversations open"), NoticeLevel.WARNING)
            return
        palette = CommandPalette(
            providers=[RosterProvider],
            placeholder=_(_ROSTER_PLACEHOLDER),
            id="--roster-palette",
        )
        self.push_screen(palette)

    def open_from_palette(self, jid: str) -> None:
        """Открыть беседу, выбранную в палитре контактов."""
        self._bus.dispatch(OpenConversation(jid))

    def run_palette_command(self, spec: commands.CommandSpec) -> None:
        """Выполнить команду, выбранную в палитре.

        Команда без аргументов выполняется сразу, команда с аргументами
        подставляется в строку ввода: палитра не умеет спрашивать параметры.
        Выход из палитры проходит то же подтверждение, что и ctrl+c: выбрать
        /quit нечетким поиском слишком легко.
        """
        if spec.handler_key == "sys.quit":
            self.action_request_quit()
            return
        if spec.min_args == 0 and not spec.subcommands:
            self.submit_line(f"/{spec.name}")
        else:
            self.prefill_prompt(f"/{spec.name} ")

    def prefill_prompt(self, text: str) -> None:
        """Подставить заготовку в строку ввода и перевести туда фокус."""
        field = self._find("#prompt-input", Input)
        if field is None:
            return
        field.value = text
        field.cursor_position = len(text)
        field.focus()

    # Локальные команды.

    def _cmd_help(self, parsed: commands.ParsedCommand) -> None:
        """/help [команда] - справка из реестра команд."""
        self._notice(commands.help_text(parsed.sub_args[0] if parsed.sub_args else None))

    def _cmd_keys(self, parsed: commands.ParsedCommand) -> None:
        """/keys - таблицы клавиш, собранные из фактических привязок.

        Девять одинаковых строк alt+1..alt+9 схлопываются в одну: они об одном
        действии. Клавиши строки ввода и панели лога объявлены не в BINDINGS
        приложения, поэтому идут отдельными таблицами: одна и та же клавиша в
        разных областях делает разное, и без заголовка это не читается.
        """
        del parsed
        rows: list[tuple[str, str]] = [
            (binding.key, _(binding.description))
            for binding in self.BINDINGS
            if isinstance(binding, Binding)
            and binding.description
            and binding.key not in _ALT_NUMBER_KEYS
        ]
        rows.append(("alt+1..alt+9", _("conversation by number")))
        self._block(_("application keys"), rows)
        self._block(_("input line"), [(key, _(text)) for key, text in _EXTERNAL_KEYS])
        self._block(_("RAW XML panel"), [(key, _(text)) for key, text in _PANEL_KEYS])
        self._block(_("marks in the conversation feed"), list(xeps.compact_legend()))

    def _cmd_theme(self, parsed: commands.ParsedCommand) -> None:
        """/theme <имя> - сменить тему Textual."""
        available = sorted(self.available_themes)
        names = ", ".join(available)
        if not parsed.sub_args:
            self._notice(_("current theme: {name}").format(name=self.theme))
            self._notice(_("available themes: {names}").format(names=names))
            return
        name = parsed.sub_args[0]
        if name not in self.available_themes:
            self._notice(_("no theme {name!r}").format(name=name), NoticeLevel.ERROR)
            self._notice(_("available themes: {names}").format(names=names), NoticeLevel.ERROR)
            return
        self.theme = name
        self._notice(_("theme: {name}").format(name=name), NoticeLevel.SUCCESS)

    def _cmd_lang(self, parsed: commands.ParsedCommand) -> None:
        """/lang [en|ru] - показать или сменить язык интерфейса.

        Язык меняется до выхода из клиента, постоянный выбор задается в конфиге.
        Переводятся подписи панелей и все новые строки. Уже выведенные строки
        ленты остаются на прежнем языке: это история, а не подписи.
        """
        if not parsed.sub_args:
            current = _("interface language: {name}").format(
                name=_language_name(i18n.get_language())
            )
            languages = _("available languages: {languages}").format(
                languages=", ".join(i18n.LANGUAGES)
            )
            self._notice(f"{current}\n{languages}")
            return
        value = parsed.sub_args[0]
        language = i18n.normalize_language(value)
        if language is None:
            self._notice(
                _("language {value!r} not recognized, expected: {usage}").format(
                    value=value, usage=parsed.usage
                ),
                NoticeLevel.ERROR,
            )
            return
        i18n.set_language(language)
        self.apply_language()
        for panel in (self._chat, self._xmllog, self._prompt):
            if panel is not None:
                panel.apply_language()
        if self._status is not None:
            self._status.refresh()
        self.screen.refresh()
        self._notice(
            _("interface language: {name}").format(name=_language_name(language)),
            NoticeLevel.SUCCESS,
        )

    def _cmd_quit(self, parsed: commands.ParsedCommand) -> None:
        """/quit - выход без подтверждения: команда введена явно."""
        del parsed
        self.exit()

    def _cmd_clear(self, parsed: commands.ParsedCommand) -> None:
        """/clear - очистить область беседы."""
        del parsed
        if self._chat is None:
            return
        hidden = self._chat.clear_conversation()
        self._notice(
            _("conversation area cleared, entries hidden: {count}").format(count=hidden),
            NoticeLevel.SUCCESS,
        )

    def _cmd_send(self, parsed: commands.ParsedCommand) -> None:
        """/send <raw-xml> - отправить произвольную строфу с подтверждением.

        Подтверждение обязательно, его обещает справка: строфа уходит в поток как
        есть, опечатка в ней разрывает соединение.
        """
        payload = " ".join(parsed.args).strip()
        if not payload:
            self._notice(_("expected: {usage}").format(usage=parsed.usage), NoticeLevel.ERROR)
            return
        self._pending_send = payload
        question = _(_SEND_QUESTION)
        # Одно действие - один блок в ленте: три отдельных уведомления давали три
        # строки времени на один вопрос.
        self._notice(
            _("the stanza will be sent to the stream as is:\n  {payload}\n{question}").format(
                payload=payload, question=question
            ),
            NoticeLevel.WARNING,
        )
        self._await_answer(question)

    def _resolve_send(self, answer: str) -> None:
        """Обработать ответ на подтверждение отправки строфы."""
        payload = self._pending_send
        self._pending_send = None
        self._clear_answer()
        if payload is None:
            return
        if answer.strip().lower() not in _CONFIRMATIONS:
            self._notice(_("stanza not sent: {answer}").format(answer=answer), NoticeLevel.SUCCESS)
            return
        self._bus.dispatch(SendRawXml(payload))

    def _cmd_debug_panel(self, parsed: commands.ParsedCommand) -> None:
        """/debug - развернуть панель лога на весь экран."""
        del parsed
        self.layout_mode = "debug" if self.layout_mode != "debug" else "split"
        self._notice(_("layout: {mode}").format(mode=self.layout_mode))

    def _cmd_stats(self, parsed: commands.ParsedCommand) -> None:
        """/stats - счетчики потока, буфера и очереди команд."""
        del parsed
        metrics = self._state.metrics
        rows: list[tuple[str, str]] = [
            (_("stanzas total"), str(metrics.stanzas_total)),
            (_("stanzas per second"), f"{metrics.stanzas_per_sec:.1f}"),
            (
                _("traffic"),
                f"in {humanize_bytes(metrics.bytes_in)} / out {humanize_bytes(metrics.bytes_out)}",
            ),
            (_("latency"), format_latency(metrics.latency_ms)),
            ("RSS", humanize_bytes(metrics.rss_bytes)),
            (_("session uptime"), humanize_duration(metrics.uptime_s)),
            (_("process uptime"), humanize_duration(time.monotonic() - self._started)),
            (_("reconnects"), str(metrics.reconnects)),
            (_("stage"), stage_label(self._state.stage)),
            (_("transport"), transport_label(self._state.transport)),
            (_("commands in queue"), str(self._bus.pending_commands)),
        ]
        if self._xmllog is not None:
            stats = self._xmllog.stats()
            buffer = _(
                "{buffered} buffered, {pending} awaiting render, evicted {dropped}, pause={paused}"
            ).format(
                buffered=stats.buffered,
                pending=stats.pending,
                dropped=stats.dropped,
                paused=_("yes") if stats.paused else _("no"),
            )
            rows.append((_("log buffer"), buffer))
        self._block(_("statistics"), rows)

    def _cmd_log_save(self, parsed: commands.ParsedCommand) -> None:
        """/log save <файл> - выгрузить буфер строф с текущим маскированием.

        Снимок буфера и флаг маскирования снимаются здесь, в цикле событий, а сама
        запись уходит в поток: маскирование двух тысяч строф и запись файла заняли бы
        десятки миллисекунд и дали бы заметный провал отрисовки под нагрузкой.
        """
        if not parsed.sub_args:
            self._notice(_("expected: {usage}").format(usage=parsed.usage), NoticeLevel.ERROR)
            return
        path = Path(parsed.sub_args[0]).expanduser()
        self.run_worker(
            partial(self._save_log, path, tuple(self._stanzas), self._unsafe),
            name="log-save",
            group="io",
            thread=True,
            exit_on_error=False,
        )

    def _save_log(self, path: Path, snapshot: tuple[RawStanza, ...], unsafe: bool) -> None:
        """Тело фоновой записи лога. Выполняется вне цикла событий."""
        try:
            self._write_log(path, snapshot, unsafe)
        except OSError as error:
            self.call_from_thread(
                self._notice, _("write failed: {error}").format(error=error), NoticeLevel.ERROR
            )
            return
        mode = _("without masking") if unsafe else _("with masking")
        self.call_from_thread(
            self._notice,
            _("stanzas saved: {count}, file {path} ({mode}, permissions 0600)").format(
                count=len(snapshot), path=path, mode=mode
            ),
            NoticeLevel.SUCCESS,
        )

    def _write_log(self, path: Path, snapshot: tuple[RawStanza, ...], unsafe: bool) -> None:
        """Записать снимок буфера в файл с правами 0600.

        O_NOFOLLOW обязателен: без него запись идет по символической ссылке, и
        команда с подставленным путем усекает и перезаписывает чужой файл, а chmod
        по пути меняет права цели ссылки. С флагом открытие такого пути завершается
        ошибкой, она уходит в область беседы. Права выставляются по дескриптору
        через fchmod, а не по пути: путь между открытием и chmod можно подменить.
        """
        # Файл сразу создается с правами 0600: писать секреты во временно
        # доступный всем файл нельзя даже на доли секунды.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            # Существующий файл мог иметь другие права: O_CREAT их не меняет.
            os.fchmod(handle.fileno(), 0o600)
            for stanza in snapshot:
                stamp = time.strftime("%H:%M:%S", time.localtime(stanza.ts))
                millis = int(stanza.ts % 1 * 1000)
                arrow = "OUT" if stanza.direction is Direction.OUT else "IN "
                handle.write(
                    f"{stamp}.{millis:03d} {arrow} {stanza.kind.value:<8}"
                    f" {redact(stanza.xml, unsafe)}\n"
                )

    def _cmd_xml_filter(self, parsed: commands.ParsedCommand) -> None:
        """/xml filter <выражение> - фильтр панели лога.

        Ответ один: панель сама сообщает, что разобрала и сколько строф подошло.
        Опечатка в поле не применяется, а объясняется перечнем допустимых значений.
        """
        self._set_filter(" ".join(parsed.sub_args))

    def _cmd_xml(self, parsed: commands.ParsedCommand) -> None:
        """/xml [in|out|both|off] [--unsafe] - режим лога и небезопасный показ."""
        args = parsed.sub_args
        mode: XmlMode | None = None
        if args:
            try:
                mode = XmlMode(args[0].lower())
            except ValueError:
                self._notice(
                    _("mode {mode!r} not recognized, expected: {usage}").format(
                        mode=args[0], usage=parsed.usage
                    ),
                    NoticeLevel.ERROR,
                )
                return

        unsafe = parsed.flag_bool("unsafe")
        if unsafe and not self._unsafe:
            self._request_unsafe(mode)
            return

        if mode is not None:
            self._set_mode(mode)
        if not unsafe and self._unsafe and mode is not None:
            # Команда без флага описывает желаемое состояние целиком, поэтому
            # явная смена режима возвращает маскирование. Отдельного ключа
            # выключения в реестре нет.
            self._set_unsafe(False)
        if mode is None and not unsafe:
            self._notice(
                _("log: mode {mode}, filter {filter}, unsafe {unsafe}").format(
                    mode=self._xml_mode.value,
                    filter=self._xml_filter or _("no"),
                    unsafe=_("yes") if self._unsafe else _("no"),
                )
            )

    def _request_unsafe(self, mode: XmlMode | None) -> None:
        """Показать предупреждение и перейти в ожидание подтверждения вводом."""
        self._awaiting_unsafe = True
        self._pending_unsafe = mode
        rules = "\n".join(f"  - {rule}" for rule in redaction_summary())
        question = _(_UNSAFE_QUESTION)
        self._notice(
            _(
                "{warning}\nthe following masking rules will be disabled:\n{rules}\n{question}"
            ).format(warning=_(UNSAFE_WARNING), rules=rules, question=question),
            NoticeLevel.WARNING,
        )
        self._await_answer(question)

    def _await_answer(self, question: str) -> None:
        """Перевести строку ввода в режим ожидания ответа.

        Сам вопрос печатает вызывающая сторона вместе с предупреждением одним
        блоком: иначе на один вопрос приходится несколько строк времени в ленте.
        """
        if self._prompt is not None:
            self._prompt.set_awaiting(question)

    def _clear_answer(self) -> None:
        """Вернуть строке ввода обычный вид."""
        if self._prompt is not None:
            self._prompt.set_awaiting("")

    def _resolve_unsafe(self, answer: str) -> None:
        """Обработать ответ на запрос подтверждения небезопасного режима."""
        if answer.strip().lower() not in _CONFIRMATIONS:
            self._notice(_("cancelled input: {answer}").format(answer=answer), NoticeLevel.INFO)
            self._cancel_unsafe()
            return
        mode = self._pending_unsafe
        self._awaiting_unsafe = False
        self._pending_unsafe = None
        self._clear_answer()
        if mode is not None:
            self._set_mode(mode)
        self._set_unsafe(True)
        self._notice(_("unsafe mode on, masking disabled"), NoticeLevel.ERROR)

    def _cancel_unsafe(self) -> None:
        """Отменить ожидание подтверждения."""
        self._awaiting_unsafe = False
        self._pending_unsafe = None
        self._clear_answer()
        self._notice(_("unsafe mode not enabled, masking kept"), NoticeLevel.SUCCESS)

    # Применение настроек лога.

    def _set_mode(self, mode: XmlMode) -> None:
        """Сменить режим лога: локально и через шину.

        Панель обновляется сразу, чтобы интерфейс не ждал сессию, и одновременно
        уходит команда: владелец состояния - сессия, она публикует событие
        для статус-бара и остальных подписчиков. Оба вызова идемпотентны.
        """
        self._xml_mode = mode
        if self._xmllog is not None:
            self._xmllog.set_mode(mode)
        self._bus.dispatch(SetXmlMode(mode))

    def _set_filter(self, expression: str) -> None:
        """Задать фильтр панели лога. Фильтр применяется только в UI."""
        if self._xmllog is None:
            self._xml_filter = expression
            self._bus.dispatch(SetXmlFilter(expression))
            return
        result = self._xmllog.set_filter(expression)
        if not result.ok:
            self._notice(result.text, NoticeLevel.ERROR)
            self._notice(_("format: {usage}").format(usage=_(_FILTER_USAGE)), NoticeLevel.ERROR)
            return
        self._xml_filter = expression
        self._bus.dispatch(SetXmlFilter(expression))
        self._notice(result.text)

    def _set_unsafe(self, enabled: bool) -> None:
        """Включить или выключить показ без маскирования."""
        self._unsafe = enabled
        if self._xmllog is not None:
            self._xmllog.set_unsafe(enabled)
        self._mark_unsafe(enabled)
        self._bus.dispatch(SetUnsafeXml(enabled))

    # Вывод.

    def _notice(self, text: str, level: NoticeLevel = NoticeLevel.INFO) -> None:
        """Вывести служебный текст в область беседы. Многострочный текст панель сверстает сама."""
        if self._chat is None or not text:
            return
        self._chat.add_notice(text, level)

    def _block(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        """Вывести таблицу имя-значение в область беседы."""
        if self._chat is None:
            return
        self._chat.add_block(title, rows)
