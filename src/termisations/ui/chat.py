"""Панель беседы: сообщения, метки расширений, системный вывод команд.

Панель не знает ни названий протокольных расширений, ни их номеров. Она получает
структуру ``XepEvent`` и берет у ``termisations.xeps`` готовую подпись
(``badge_text``), детали (``format_xep_detail``), цвет (``xep_color``) и область
действия (``xep_scope``). Поэтому добавление нового расширения в вывод правок в
этом файле не требует.

Что попадает в ленту, решает область действия расширения, а не условия в UI:

* ``TRANSPORT`` - пинги, подтверждения потока, TLS, SASL, disco - в ленту не
  попадают вовсе. Их место в панели RAW XML, статус-баре и выводе /sm, /ping;
* ``PRESENCE`` - состояние набора текста - показывается строкой состояния в
  заголовке беседы и гаснет по таймауту, историей не становится;
* ``CONVERSATION`` - квитанции, маркеры, архив, шифрование - приклеивается к
  своему сообщению, а без привязки выводится строкой в окне своей беседы.

Метка шифрования у сообщения собирается только из данных самого сообщения. Число
доверенных устройств - величина текущая, а не свойство строфы, поэтому оно
показывается в заголовке активной беседы и в статус-баре.
"""

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Self

from rich.console import Group, RenderableType
from rich.segment import Segment
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.events import Resize
from textual.geometry import Size
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import RichLog, Static

from termisations.core.colors import color_of
from termisations.core.events import (
    ActiveConversationChanged,
    CommandFeedback,
    CommandTable,
    ConnectionStageChanged,
    ConversationsUpdated,
    EventBus,
    MessageAdded,
    MessageUpdated,
    Notice,
    NoticeLevel,
    OccupantsUpdated,
    StateUpdated,
    Subscription,
    XepActivity,
)
from termisations.core.i18n import N_, _, ngettext
from termisations.core.models import (
    DELIVERY_MARKS,
    MUC_MARK,
    PRESENCE_MARKS,
    ClientState,
    ConnectionStage,
    Conversation,
    DeliveryState,
    Direction,
    Encryption,
    Message,
    XepEvent,
    stage_label,
)
from termisations.xeps import (
    XepScope,
    badge_text,
    badge_visible,
    compact_badge,
    format_xep_badge,
    format_xep_detail,
    presence_text,
    xep_color,
    xep_scope,
)

__all__ = ["ChatPanel", "MessageLog"]


# Ширина колонки времени. Одна на все строки ленты: у сообщения, уведомления,
# ответа команды и строки расширения левый край общий, иначе лента читается как
# три разных списка. В колонку помещаются обе формы времени.
_TIME_WIDTH: Final = 12

# Время сообщения: часы и минуты, к ним дата, если сообщение не сегодняшнее.
_TIME_FORMAT: Final = "%H:%M"
_DATE_TIME_FORMAT: Final = "%d.%m %H:%M"
# Время служебной строки: до миллисекунд. Стадии подключения идут плотнее секунды,
# и без миллисекунд их порядок по ленте не восстанавливается.
_SERVICE_TIME_FORMAT: Final = "%H:%M:%S"
# Разделитель календарного дня в ленте.
_DAY_FORMAT: Final = "%d.%m.%Y"
_DAY_KEY_FORMAT: Final = "%Y-%m-%d"
_DAY_RULE: Final = "─"

# Оформление табличного вывода отладочных команд.
_BLOCK_MARK: Final = "▌ "
_BLOCK_PADDING: Final = (0, 2)

# Разбор строки служебного текста на колонку имени и колонку описания. Колонки
# разделены двумя и более пробелами: так сверстаны справка, список флагов и
# таблица клавиш.
_COLUMNS_RE: Final = re.compile(r"(\S.*?)(\s{2,})(\S.*)")
# Пункт списка. Перенос уходит под текст пункта, а не под его маркер.
_BULLET_RE: Final = re.compile(r"([-*•])(\s+)(\S.*)")
# Предел ширины колонки имени. Более широкая колонка на узком терминале не
# оставляет места описанию, и строка верстается обычным переносом.
_NAME_WIDTH: Final = 32

# Стили основной строки сообщения.
_TIME_STYLE: Final = "dim"
_OWN_SENDER_STYLE: Final = "bold cyan"
_PEER_SENDER_STYLE: Final = "bold yellow"
_OWN_NAME: Final = "you"
_UNKNOWN_SENDER: Final = "?"
_EMPTY_BODY: Final = N_("<no text>")

# Пометка исправленного сообщения. Сам факт корректировки приезжает отдельной
# меткой расширения, здесь только визуальный признак у тела сообщения.
_CORRECTED_MARK: Final = "✎"

# Стиль знаков компактных меток: расширения, срабатывающие на каждом сообщении.
# Расшифровка знаков лежит в xeps.COMPACT_LEGEND и печатается командой /keys.
_COMPACT_MARK_STYLE: Final = "dim"

# Признак комнаты берется из моделей: тот же знак рисует палитра контактов.
_MUC_LABEL: Final = N_("room")

_NO_CONVERSATIONS: Final = N_("no open conversations")
_NO_ACTIVE: Final = N_("no conversation selected")
_NO_HISTORY: Final = N_("no history")

# Подсказка первого экрана. Пока беседа не выбрана, панель показывает не журнал
# подключения, а то, с чего начать работу.
_START_HINT: Final[tuple[str, ...]] = (
    N_("/chat <jid> - open a conversation with a contact"),
    N_("/join <room> - join a room"),
    N_("/help - list of commands, /keys - keys"),
    N_("the raw stanza stream goes to the RAW XML panel on the right"),
)

# Сколько бесед показывает полоса заголовка: столько же переключается
# сочетаниями alt+1..alt+9.
_TABS_LIMIT: Final = 9
# Предел длины заголовка вкладки. Без него один длинный JID занимает полосу целиком.
_TAB_TITLE_LIMIT: Final = 16
_ELLIPSIS: Final = "…"

# Сколько держится строка состояния собеседника. Клиент присылает "paused" не
# всегда, поэтому состояние обязано гаснуть само.
_PEER_STATE_TTL: Final = 12.0
_PEER_STATE_TICK: Final = 1.0

# Признак того, что лента отлистана вверх, и число непоказанных строк.
_FOLLOW_MARK: Final = "▶"
_PAUSE_MARK: Final = "⏸"

# Уровень уведомления: знак, стиль знака, стиль текста. Пустой стиль текста
# означает обычный цвет панели: заливать цветом длинный вывод команды незачем.
_NOTICE_MARKS: Final[Mapping[NoticeLevel, tuple[str, str, str]]] = MappingProxyType(
    {
        NoticeLevel.INFO: ("·", "dim", ""),
        NoticeLevel.SUCCESS: ("✓", "green", ""),
        NoticeLevel.WARNING: ("!", "bold yellow", "yellow"),
        NoticeLevel.ERROR: ("✗", "bold red", "red"),
    }
)

_STATE_STYLES: Final[Mapping[DeliveryState, str]] = MappingProxyType(
    {
        DeliveryState.PENDING: "dim",
        DeliveryState.SENT: "dim",
        DeliveryState.ACKED: "green",
        DeliveryState.RECEIVED: "green",
        DeliveryState.DISPLAYED: "bold green",
        DeliveryState.FAILED: "bold red",
    }
)

_ENCRYPTION_STYLES: Final[Mapping[Encryption, str]] = MappingProxyType(
    {
        Encryption.PLAIN: "dim",
        Encryption.OMEMO: "magenta",
        Encryption.OX: "magenta",
        Encryption.PGP: "magenta",
    }
)

_PLAIN_LABEL: Final = "plain"
# Знаки доверия к устройствам. Совпадают со знаками статус-бара: заголовок беседы
# и статус-бар показывают одну и ту же величину и обязаны выглядеть одинаково.
_TRUSTED_MARK: Final = "✓"
_PARTIAL_MARK: Final = "!"
_UNTRUSTED_MARK: Final = "✗"
_DEVICES_SUFFIX: Final = "dev"

# Подпись итоговой строки сводки подключения.
_TOTAL_LABEL: Final = N_("total")
_CONNECT_TITLE: Final = N_("connection")
_MILLIS: Final = N_("ms")


def _hanging(prefix: Text, content: RenderableType) -> Table:
    """Собрать блок с висячим отступом.

    Первая колонка фиксирована по ширине префикса, вторая занимает остаток
    строки. Перенос внутри содержимого попадает под содержимое, а не под
    префикс: так выглядят и метки расширений, и многострочный вывод команд.
    """
    grid = Table.grid(expand=True)
    grid.add_column(width=prefix.cell_len, no_wrap=True)
    grid.add_column(ratio=1, overflow="fold")
    grid.add_row(prefix, content)
    return grid


def _wrapped_text(text: str, style: str) -> RenderableType:
    """Многострочный служебный текст с сохранением колонок исходной строки.

    Строка вида "имя<два и более пробела>описание" разбирается на две колонки, и
    перенос описания уходит под описание. Так сверстаны справка по группе команд,
    список флагов и таблица клавиш: без этого на 80 колонках продолжение описания
    вставало под колонку имени и читалось как следующий пункт списка.

    Остальные строки переносятся под собственный отступ, а не под левый край
    блока: иначе продолжение пункта списка оказывается левее самого пункта.
    """
    lines = text.splitlines() or [""]
    rendered: list[RenderableType] = []
    for line in lines:
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        columns = _BULLET_RE.fullmatch(stripped) or _COLUMNS_RE.match(stripped)
        if columns is not None and len(columns.group(1)) + len(columns.group(2)) <= _NAME_WIDTH:
            head = Text(" " * indent + columns.group(1) + columns.group(2), style=style)
            body = Text(columns.group(3), style=style, no_wrap=False, overflow="fold")
            rendered.append(_hanging(head, body))
            continue
        body = Text(stripped, style=style, no_wrap=False, overflow="fold")
        rendered.append(_hanging(Text(" " * indent), body) if indent else body)
    if len(rendered) == 1:
        return rendered[0]
    return Group(*rendered)


def _format_time(ts: float) -> str:
    """Время сообщения. Не сегодняшнее сообщение показывается вместе с датой."""
    local = time.localtime(ts)
    today = time.localtime()
    same_day = (local.tm_year, local.tm_yday) == (today.tm_year, today.tm_yday)
    return time.strftime(_TIME_FORMAT if same_day else _DATE_TIME_FORMAT, local)


def _format_service_time(ts: float) -> str:
    """Время служебной строки с миллисекундами."""
    milliseconds = int((ts - int(ts)) * 1000)
    return f"{time.strftime(_SERVICE_TIME_FORMAT, time.localtime(ts))}.{milliseconds:03d}"


def _day_key(ts: float) -> str:
    """Ключ календарного дня. По его смене в ленту вставляется разделитель."""
    return time.strftime(_DAY_KEY_FORMAT, time.localtime(ts))


def _prefix(value: str) -> Text:
    """Колонка времени фиксированной ширины."""
    return Text(f"{value:<{_TIME_WIDTH}} ", style=_TIME_STYLE)


def _truncate(value: str, limit: int) -> str:
    """Усечь заголовок вкладки до предела с многоточием."""
    if len(value) <= limit:
        return value
    return value[: limit - 1] + _ELLIPSIS


def _sender_name(message: Message, is_muc: bool) -> str:
    """Короткое имя отправителя.

    В комнате значимая часть - ресурс, это ник участника. В личной беседе ресурс
    обозначает устройство и в строке сообщения не нужен, остается узел JID.
    """
    if message.direction is Direction.OUT:
        return _OWN_NAME
    sender = message.sender
    if not sender:
        return _UNKNOWN_SENDER
    if is_muc and "/" in sender:
        return sender.rsplit("/", 1)[1] or sender
    return sender.split("/", 1)[0].split("@", 1)[0] or sender


def _as_level(level: NoticeLevel | str) -> NoticeLevel:
    """Привести уровень уведомления к перечислению. Неизвестный уровень - INFO."""
    try:
        return NoticeLevel(level)
    except ValueError:
        return NoticeLevel.INFO


@dataclass(slots=True)
class _Block:
    """Отрисованный блок панели.

    ``key`` - идентификатор сообщения, по нему блок заменяется на месте.
    У системных строк ключа нет. ``strips`` - готовые полосы, они переживают
    добавление соседних блоков и повторно не рендерятся.
    """

    key: str | None
    renderable: RenderableType
    strips: list[Strip] = field(default_factory=list)


@dataclass(slots=True)
class _Entry:
    """Запись ленты панели.

    ``conversation`` - беседа, к которой запись относится. У системной записи это
    беседа, активная в момент вывода: ответ команды остается там, где команду
    ввели, и не проигрывается заново при каждом переключении. ``None`` означает
    запись сессии, она видна, пока беседа не выбрана.

    ``hidden`` ставится командой /clear. Запись остается в индексе, поэтому
    поздняя квитанция или корректировка находят свое сообщение и не всплывают в
    очищенном окне новым блоком.
    """

    conversation: str | None
    message: Message | None
    renderable: RenderableType | None
    hidden: bool = False


class MessageLog(RichLog):
    """Лог беседы с заменой уже отрисованного блока на месте.

    Обоснование выбора виджета. ``RichLog`` хранит содержимое как список готовых
    полос ``Strip`` и в ``render_line`` отдает только те строки, что попали в
    видимую область. Добавление сообщения дописывает полосы в конец списка и не
    трогает остальные: при тысяче сообщений стоимость добавления не зависит от
    длины истории. Виртуализированный список виджетов (``VerticalScroll`` с
    отдельным ``Static`` на сообщение) дал бы ту же возможность правки на месте,
    но платой были бы тысяча узлов DOM, сопоставление стилей и перестроение
    раскладки контейнера на каждое сообщение.

    Чего ``RichLog`` не умеет - заменить уже написанное. Поэтому лог ведет
    собственный список блоков: замена перерисовывает только измененный блок,
    а общий список полос пересобирается из готовых ссылок, без повторного
    рендера соседей.

    Режим следования хранится явным флагом, а не выводится из положения прокрутки.
    Высота панели меняется без участия пользователя: под строкой ввода появляется
    и исчезает подсказка сигнатуры. Признак "прокрутка не в самом конце" в этот
    момент срабатывает ложно, и вывод команды уходит за нижнюю границу экрана.

    Содержимое добавляется только методом ``append_block``. Унаследованный
    ``write`` пишет мимо списка блоков, и первая же замена такую строку потеряет.
    """

    def __init__(self, *, block_limit: int = 2000, id: str | None = None) -> None:
        # auto_scroll отключен: прокрутка к концу выполняется только тогда, когда
        # пользователь не отлистал историю вверх.
        super().__init__(id=id, markup=False, highlight=False, auto_scroll=False)
        self._blocks: list[_Block] = []
        self._block_limit = max(block_limit, 16)
        self._width = 0
        self._follow = True
        self._pending_lines = 0
        # Положение, на которое лог встал сам при последнем показе конца.
        self._end_offset = 0

    @property
    def block_count(self) -> int:
        """Число блоков в логе."""
        return len(self._blocks)

    @property
    def follow(self) -> bool:
        """Идет ли показ за концом ленты."""
        return self._follow

    @property
    def pending_lines(self) -> int:
        """Сколько строк пришло ниже видимой области после ухода вверх."""
        return self._pending_lines

    def on_mount(self) -> None:
        """Отслеживать прокрутку отдельным таймером, а не на каждое событие."""
        self.set_interval(0.1, self._poll_scroll)

    def follow_end(self) -> None:
        """Вернуться к концу ленты. Вызывается при вводе строки и клавишей."""
        self._follow = True
        self._pending_lines = 0
        self.scroll_end(animate=False, immediate=True)
        self._end_offset = self.scroll_offset.y

    def append_block(self, key: str | None, renderable: RenderableType) -> None:
        """Дописать блок в конец. Соседние блоки не перерисовываются."""
        block = _Block(key=key, renderable=renderable)
        if self._width:
            block.strips = self._render_block(renderable)
        self._blocks.append(block)
        if not self._follow:
            self._pending_lines += len(block.strips)
        if len(self._blocks) > self._block_limit:
            self._trim()
            return
        self.lines.extend(block.strips)
        self._after_change()

    def replace_block(self, key: str, renderable: RenderableType) -> bool:
        """Заменить блок с таким ключом. Возвращает False, если блока нет.

        Ищем с конца: правки почти всегда приходят на свежие сообщения.
        """
        for block in reversed(self._blocks):
            if block.key != key:
                continue
            block.renderable = renderable
            block.strips = self._render_block(renderable) if self._width else []
            self._rebuild()
            return True
        return False

    def clear(self) -> Self:
        """Очистить лог вместе со списком блоков."""
        self._blocks.clear()
        self._pending_lines = 0
        self._follow = True
        self._end_offset = 0
        return super().clear()

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Текст под выделением мышью.

        Базовая реализация достает текст только из ``Text`` или ``Content``, а
        лента хранит уже отрисованные полосы. Без этого метода выделение в
        беседе рисовалось, но копировать было нечего.
        """
        text = "\n".join(strip.text for strip in self.lines)
        return selection.extract(text), "\n"

    def on_resize(self, event: Resize) -> None:
        """Смена ширины: полосы собраны под конкретную ширину, нужен пересчет."""
        super().on_resize(event)
        width = self.scrollable_content_region.width or event.size.width
        if width <= 0 or width == self._width:
            return
        self._width = width
        for block in self._blocks:
            block.strips = self._render_block(block.renderable)
        self._rebuild()

    def _poll_scroll(self) -> None:
        """Отследить прокрутку: уход вверх снимает следование, возврат вниз ставит.

        Проверять просто "положение не в самом конце" нельзя: высота области
        меняется сама, когда под вводом появляется подсказка. Смена высоты дает
        положение ``min(_end_offset, max_y)``, и оно не считается прокруткой.
        """
        if not self.size:
            return
        offset = self.scroll_offset.y
        max_y = self.max_scroll_y
        if self._follow:
            if offset == max_y:
                self._end_offset = offset
                return
            if offset == min(self._end_offset, max_y):
                self.scroll_end(animate=False, immediate=True)
                self._end_offset = self.scroll_offset.y
                return
            self._follow = False
            self._pending_lines = 0
            return
        if offset == max_y:
            self._follow = True
            self._pending_lines = 0
            self._end_offset = offset

    def _render_block(self, renderable: RenderableType) -> list[Strip]:
        """Превратить блок в полосы под текущую ширину содержимого."""
        console = self.app.console
        options = console.options.update_width(self._width)
        lines = list(Segment.split_lines(console.render(renderable, options)))
        return [strip.adjust_cell_length(self._width) for strip in Strip.from_lines(lines)]

    def _rebuild(self) -> None:
        """Пересобрать список полос из блоков.

        Копируются только ссылки на готовые полосы, повторный рендер здесь
        не выполняется.
        """
        lines: list[Strip] = []
        for block in self._blocks:
            lines.extend(block.strips)
        self.lines = lines
        # Кэш строк адресуется номером строки, после сдвига содержимого он неверен.
        self._line_cache.clear()
        self._after_change()
        self.refresh()

    def _trim(self) -> None:
        """Выбросить самые старые блоки, когда лог перерос лимит.

        Режем пачкой, чтобы не пересобирать полосы на каждое новое сообщение.
        """
        excess = len(self._blocks) - self._block_limit
        del self._blocks[: max(excess, self._block_limit // 10)]
        self._rebuild()

    def _after_change(self) -> None:
        """Обновить размеры и, если нужно, прокрутить к последнему блоку.

        Прокрутка выполняется сразу, а не откладывается до следующей отрисовки:
        отложенная прокрутка срабатывает после того, как пользователь уже ушел
        вверх клавишей, и возвращает ленту в конец поверх его действия.
        """
        self._widest_line_width = self._width
        self.virtual_size = Size(self._width, len(self.lines))
        if self._follow:
            self.scroll_end(animate=False, immediate=True, x_axis=False)
            self._end_offset = self.scroll_offset.y


class ChatPanel(Widget):
    """Панель беседы: заголовок с полосой бесед и лог сообщений."""

    DEFAULT_CSS = """
    ChatPanel {
        layout: vertical;
        width: 1fr;
        height: 1fr;
    }
    ChatPanel > #chat-header {
        height: 2;
        padding: 0 1;
        background: $panel;
        color: $foreground;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    ChatPanel > #chat-log {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
        background: $surface;
        scrollbar-size-vertical: 1;
    }
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        id: str | None = None,
        classes: str | None = None,
        history_limit: int = 1000,
        system_limit: int = 200,
        block_limit: int = 2000,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._bus = bus
        self._subs = Subscription()
        self._header = Static("", id="chat-header", markup=False)
        self._log = MessageLog(id="chat-log", block_limit=block_limit)
        # Лимиты раздельные. С одним общим служебный поток вытесняет историю
        # беседы за пару минут: он идет на порядок чаще сообщений.
        self._history_limit = max(history_limit, 1)
        self._system_limit = max(system_limit, 1)
        self._messages_kept = 0
        self._system_kept = 0
        # Лента панели в порядке поступления. Нужна для перерисовки при смене
        # беседы. Источник правды остается за сессией, здесь лежит копия для показа.
        self._entries: list[_Entry] = []
        self._index: dict[str, _Entry] = {}
        self._conversations: tuple[Conversation, ...] = ()
        self._active: str | None = None
        self._title = ""
        self._state = ClientState()
        # Состояние собеседников: голый JID -> текст и время последнего обновления.
        self._peer_states: dict[str, tuple[str, float]] = {}
        self._occupants: dict[str, tuple[str, ...]] = {}
        # Календарный день последней строки лога, по нему ставится разделитель.
        self._last_day: str | None = None
        self._hint_shown = False
        # Накопленные стадии подключения и признак того, что сессия уже готова.
        self._stage_rows: list[tuple[str, str]] = []
        self._stage_total = 0.0
        self._ready = False

    def compose(self) -> ComposeResult:
        """Заголовок сверху, лог на всю оставшуюся высоту."""
        yield self._header
        yield self._log

    def on_mount(self) -> None:
        """Подписки на шину. Обработчики только перерисовывают свой блок."""
        self._subs.add(self._bus.subscribe(MessageAdded, self._on_message_added))
        self._subs.add(self._bus.subscribe(MessageUpdated, self._on_message_updated))
        self._subs.add(self._bus.subscribe(XepActivity, self._on_xep_activity))
        self._subs.add(self._bus.subscribe(Notice, self._on_notice))
        self._subs.add(self._bus.subscribe(CommandFeedback, self._on_feedback))
        self._subs.add(self._bus.subscribe(CommandTable, self._on_table))
        self._subs.add(self._bus.subscribe(ConversationsUpdated, self._on_conversations))
        self._subs.add(self._bus.subscribe(ActiveConversationChanged, self._on_active_changed))
        self._subs.add(self._bus.subscribe(OccupantsUpdated, self._on_occupants))
        self._subs.add(self._bus.subscribe(StateUpdated, self._on_state))
        self._subs.add(self._bus.subscribe(ConnectionStageChanged, self._on_stage))
        self.set_interval(_PEER_STATE_TICK, self._expire_peer_states)
        self.apply_language()

    def on_unmount(self) -> None:
        """Снять подписки, чтобы шина не держала удаленный виджет."""
        self._subs.close()

    # Публичный интерфейс панели.

    def add_message(self, message: Message) -> None:
        """Добавить сообщение. Повторный идентификатор заменяет блок на месте."""
        self._put_message(message)

    def update_message(self, message: Message) -> None:
        """Обновить сообщение: корректировка текста, статус доставки, метки."""
        self._put_message(message)

    def add_xep(self, event: XepEvent) -> None:
        """Показать срабатывание расширения.

        Событие, которое ссылается на известное сообщение, приклеивается к нему:
        поздняя квитанция или маркер прочтения не порождают отдельную строку.
        Остальное решается областью действия расширения: транспортное событие в
        ленту не попадает, состояние собеседника уходит в заголовок, событие
        переписки печатается строкой в окне своей беседы.
        """
        target = self._find_message(event.stanza_id)
        if target is not None:
            updated = target.with_xep(event)
            if updated is not target:
                self._put_message(updated)
            return
        scope = xep_scope(event)
        if scope is XepScope.TRANSPORT:
            return
        if scope is XepScope.PRESENCE:
            self._set_peer_state(event)
            return
        peer = event.peer.split("/", 1)[0] if event.peer else None
        self._add_system(self._render_xep_line(event), conversation=peer)

    def add_notice(self, text: str, level: NoticeLevel | str = NoticeLevel.INFO) -> None:
        """Системная строка: уведомление, ошибка команды, справка."""
        mark, mark_style, text_style = _NOTICE_MARKS[_as_level(level)]
        line = Text(f"{_format_service_time(time.time()):<{_TIME_WIDTH}} ", style=_TIME_STYLE)
        line.append(f"{mark} ", style=mark_style)
        self._add_system(_hanging(line, _wrapped_text(text, text_style)))

    def add_block(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        """Табличный вывод отладочной команды: заголовок и пары имя-значение.

        Вторая колонка занимает остаток ширины, поэтому длинное значение
        переносится под собой и не ломает выравнивание имен.
        """
        table = Table.grid(padding=_BLOCK_PADDING)
        table.add_column(no_wrap=True, style="dim")
        table.add_column(ratio=1, overflow="fold")
        for name, value in rows:
            table.add_row(name, value)
        content = Group(Text(title, style="bold"), table) if rows else Text(title, style="bold")
        line = Text(f"{_format_service_time(time.time()):<{_TIME_WIDTH}} ", style=_TIME_STYLE)
        line.append(_BLOCK_MARK, style="dim")
        self._add_system(_hanging(line, content))

    def set_conversation(self, jid: str, title: str) -> None:
        """Сделать беседу активной и задать заголовок."""
        self._activate(jid)
        self._title = title
        self._refresh_header()

    def set_conversations(self, items: Sequence[Conversation], active: str | None) -> None:
        """Обновить полосу бесед и активную беседу."""
        self._conversations = tuple(items)
        self._activate(active)
        self._refresh_header()

    def clear_conversation(self) -> int:
        """Очистить окно активной беседы. Возвращает число скрытых записей.

        Записи не удаляются, а помечаются скрытыми: иначе поздняя квитанция и
        корректировка теряют привязку и возвращают очищенное сообщение обратно
        в окно новым блоком. Окна других бесед командой не затрагиваются.
        """
        hidden = 0
        for entry in self._entries:
            if entry.hidden or not self._belongs(entry):
                continue
            entry.hidden = True
            hidden += 1
        self._log.clear()
        self._last_day = None
        self._hint_shown = False
        self._show_hint_if_empty()
        return hidden

    def scroll_to_end(self) -> None:
        """Вернуть ленту к концу. Вызывается при отправке строки из ввода."""
        self._log.follow_end()
        self._refresh_header()

    def apply_language(self) -> None:
        """Показать заголовок и подсказку пустого окна на текущем языке.

        Уже выведенные записи ленты не переводятся: это история, а не подписи.
        Подсказка пустого окна - единственный блок лога, который собран из
        подписей, поэтому она пересобирается целиком.
        """
        self._refresh_header()
        if self._hint_shown:
            self._drop_hint()
        self._show_hint_if_empty()

    # Обработчики шины.

    def _on_message_added(self, event: MessageAdded) -> None:
        self.add_message(event.message)

    def _on_message_updated(self, event: MessageUpdated) -> None:
        self.update_message(event.message)

    def _on_xep_activity(self, event: XepActivity) -> None:
        self.add_xep(event.event)

    def _on_notice(self, event: Notice) -> None:
        self.add_notice(event.text, event.level)

    def _on_feedback(self, event: CommandFeedback) -> None:
        # Успешный ответ команды печатается нейтрально, ошибка - красным.
        self.add_notice(event.text, NoticeLevel.INFO if event.ok else NoticeLevel.ERROR)

    def _on_table(self, event: CommandTable) -> None:
        self.add_block(event.title, event.rows)

    def _on_conversations(self, event: ConversationsUpdated) -> None:
        self._conversations = event.items
        self._refresh_header()

    def _on_active_changed(self, event: ActiveConversationChanged) -> None:
        self._activate(event.jid)
        self._refresh_header()

    def _on_state(self, event: StateUpdated) -> None:
        # Состояние приходит раз в секунду. Заголовок трогаем только тогда,
        # когда изменилось то, что в нем видно.
        self._state = event.state

    def _on_stage(self, event: ConnectionStageChanged) -> None:
        """Стадии подключения: результат и длительность остаются в ленте.

        Стадии проходят за десятки миллисекунд, поэтому подпись у спиннера
        прочитать невозможно. Сводка подключения печатается одним блоком, когда
        сессия готова, и переживает любую скорость прохождения.
        """
        stage = event.stage
        if stage is ConnectionStage.READY:
            rows = self._stage_rows
            total = self._stage_total
            self._stage_rows = []
            self._stage_total = 0.0
            self._ready = True
            if rows:
                rows.append((_(_TOTAL_LABEL), f"{total:.0f} {_(_MILLIS)}"))
                self.add_block(_(_CONNECT_TITLE), rows)
            return
        if stage is ConnectionStage.OFFLINE:
            self._ready = False
            self._stage_rows = []
            self._stage_total = 0.0
            label = stage_label(stage)
            text = f"{label}: {event.detail}" if event.detail else label
            self.add_notice(text, NoticeLevel.WARNING)
            return
        if stage is ConnectionStage.ERROR:
            # Обрыв: следующее подключение снова соберет сводку стадий.
            self._ready = False
            self._stage_rows = []
            self._stage_total = 0.0
            self.add_notice(f"{stage_label(stage)}: {event.detail}", NoticeLevel.ERROR)
            return
        if event.duration_ms is None:
            return
        value = f"{event.duration_ms:.0f} {_(_MILLIS)}"
        if event.detail:
            value = f"{value}  {event.detail}"
        if self._ready:
            # Подключение уже состоялось: стадия одиночная, ей место в ленте
            # отдельной строкой, а не в сводке.
            self.add_notice(f"{stage_label(stage)}  {value}")
            return
        self._stage_rows.append((stage_label(stage), value))
        self._stage_total += event.duration_ms

    # Лента и отрисовка.

    def _put_message(self, message: Message) -> None:
        """Запомнить сообщение и отрисовать его, если беседа активна."""
        merged = self._merge_xeps(message)
        entry = self._index.get(merged.message_id)
        if entry is None:
            entry = _Entry(conversation=merged.conversation, message=merged, renderable=None)
            self._index[merged.message_id] = entry
            self._append_entry(entry)
            return
        entry.conversation = merged.conversation
        entry.message = merged
        if not self._is_visible(entry):
            return
        renderable = self._render_message(merged)
        if not self._log.replace_block(merged.message_id, renderable):
            self._append_to_log(entry)

    def _add_system(self, renderable: RenderableType, conversation: str | None = None) -> None:
        """Добавить системную строку в окно беседы, к которой она относится.

        По умолчанию это активная беседа: ответ команды остается там, где команду
        ввели. Пока беседа не выбрана, строка попадает в окно сессии.
        """
        target = conversation if conversation is not None else self._active
        self._append_entry(_Entry(conversation=target, message=None, renderable=renderable))

    def _append_entry(self, entry: _Entry) -> None:
        """Дописать запись в ленту и, если она видна, в лог."""
        self._entries.append(entry)
        if entry.message is not None:
            self._messages_kept += 1
        else:
            self._system_kept += 1
        self._enforce_limits()
        if self._is_visible(entry):
            self._append_to_log(entry)

    def _enforce_limits(self) -> None:
        """Удержать раздельные лимиты ленты.

        Сообщения и служебные строки считаются отдельно. С одним общим лимитом
        служебный поток вытесняет историю беседы, и через пару минут работы в
        окне не остается ни одного сообщения.
        """
        while self._messages_kept > self._history_limit:
            self._drop_oldest(message=True)
        while self._system_kept > self._system_limit:
            self._drop_oldest(message=False)

    def _drop_oldest(self, *, message: bool) -> None:
        """Выбросить самую старую запись нужного вида."""
        for index, entry in enumerate(self._entries):
            if (entry.message is not None) != message:
                continue
            del self._entries[index]
            if entry.message is not None:
                self._index.pop(entry.message.message_id, None)
                self._messages_kept -= 1
            else:
                self._system_kept -= 1
            return

    def _append_to_log(self, entry: _Entry) -> None:
        """Дописать запись в лог, поставив при необходимости разделитель дня."""
        self._drop_hint()
        if entry.message is not None:
            day = _day_key(entry.message.ts)
            if day != self._last_day:
                self._last_day = day
                self._log.append_block(None, self._render_day(entry.message.ts))
            self._log.append_block(entry.message.message_id, self._render_message(entry.message))
        elif entry.renderable is not None:
            self._log.append_block(None, entry.renderable)

    def _merge_xeps(self, message: Message) -> Message:
        """Сохранить метки, приклеенные к сообщению раньше самим UI.

        Событие расширения может прийти отдельно от сообщения, а следующая
        версия сообщения от сессии этих меток не содержит. Метка - свершившийся
        факт, поэтому при обновлении она не теряется. Дубликаты отсекает
        ``Message.with_xep``.
        """
        previous = self._find_message(message.message_id)
        if previous is None or not previous.xeps:
            return message
        merged = message
        for item in previous.xeps:
            merged = merged.with_xep(item)
        return merged

    def _find_message(self, message_id: str | None) -> Message | None:
        """Найти сообщение ленты по идентификатору."""
        if not message_id:
            return None
        entry = self._index.get(message_id)
        return None if entry is None else entry.message

    def _belongs(self, entry: _Entry) -> bool:
        """Относится ли запись к активной беседе."""
        return entry.conversation == self._active

    def _is_visible(self, entry: _Entry) -> bool:
        """Показывается ли запись сейчас."""
        return not entry.hidden and self._belongs(entry)

    def _on_occupants(self, event: OccupantsUpdated) -> None:
        """Состав комнаты: в заголовке показывается его размер."""
        if event.nicks:
            self._occupants[event.jid] = event.nicks
        else:
            self._occupants.pop(event.jid, None)
        self._refresh_header()

    def _is_muc(self, jid: str) -> bool:
        """Комната ли это. Неизвестная беседа считается личной."""
        return any(item.jid == jid and item.is_muc for item in self._conversations)

    def _activate(self, jid: str | None) -> None:
        """Переключить активную беседу и перерисовать ленту."""
        if jid == self._active:
            return
        self._active = jid
        self._title = ""
        self._redraw()

    def _redraw(self) -> None:
        """Перерисовать лог из ленты с учетом активной беседы.

        Лента беседы начинается ее историей, а не журналом сессии: служебная
        запись привязана к той беседе, в которой она появилась.
        """
        self._log.clear()
        self._last_day = None
        self._hint_shown = False
        for entry in self._entries:
            if self._is_visible(entry):
                self._append_to_log(entry)
        self._show_hint_if_empty()

    def _show_hint_if_empty(self) -> None:
        """Показать подсказку, когда показывать больше нечего."""
        if self._log.block_count or not self._log.is_mounted:
            return
        self._hint_shown = True
        self._log.append_block(None, self._render_hint())

    def _drop_hint(self) -> None:
        """Снять подсказку перед первой настоящей записью."""
        if not self._hint_shown:
            return
        self._hint_shown = False
        self._log.clear()

    def _set_peer_state(self, event: XepEvent) -> None:
        """Запомнить состояние собеседника для строки заголовка."""
        peer = event.peer.split("/", 1)[0] if event.peer else ""
        if not peer:
            return
        text = presence_text(event)
        if text:
            self._peer_states[peer] = (text, time.monotonic())
        elif self._peer_states.pop(peer, None) is None:
            return
        if peer == self._active:
            self._refresh_header()

    def _expire_peer_states(self) -> None:
        """Погасить состояния, которые давно не обновлялись."""
        if not self._peer_states:
            return
        deadline = time.monotonic() - _PEER_STATE_TTL
        expired = [peer for peer, (_, ts) in self._peer_states.items() if ts < deadline]
        for peer in expired:
            del self._peer_states[peer]
        if self._active in expired:
            self._refresh_header()

    def _refresh_header(self) -> None:
        """Собрать заголовок заново: полоса бесед и строка активной беседы.

        Перенос и усечение заданы стилями ``text-wrap`` и ``text-overflow`` в
        DEFAULT_CSS. Атрибуты ``no_wrap`` и ``overflow`` у ``rich.text.Text`` до
        виджета не доходят: Textual преобразует Text в собственный Content.

        До монтирования ``Static.update`` обращается к консоли приложения,
        которой еще нет. Заголовок в этот момент не трогаем: он собирается
        заново в ``on_mount`` из того же состояния.
        """
        header = Text()
        header.append_text(self._render_tabs())
        header.append("\n")
        header.append_text(self._render_title())
        if self._header.is_mounted:
            self._header.update(header)

    def _active_conversation(self) -> Conversation | None:
        """Модель активной беседы. Для неизвестного JID собирается на месте."""
        if self._active is None:
            return None
        for conversation in self._conversations:
            if conversation.jid == self._active:
                return conversation
        return Conversation(jid=self._active, title=self._title)

    # Рендер.

    def _render_tabs(self) -> Text:
        """Полоса бесед с номерами для переключения по alt+N."""
        if not self._conversations:
            return Text(_(_NO_CONVERSATIONS), style="dim")
        line = Text()
        for index, conversation in enumerate(self._conversations[:_TABS_LIMIT], start=1):
            if index > 1:
                line.append("  ")
            active = conversation.jid == self._active
            style = "bold reverse" if active else "dim"
            mark, mark_style = PRESENCE_MARKS[conversation.show]
            line.append(f"{mark} ", style=style if active else mark_style)
            prefix = MUC_MARK if conversation.is_muc else ""
            title = _truncate(conversation.display_title, _TAB_TITLE_LIMIT)
            line.append(f"{index} {prefix}{title}", style=style)
            if conversation.unread:
                # Непрочитанное видно и в неактивной вкладке.
                line.append(
                    f" ({conversation.unread})",
                    style=style if active else "bold yellow",
                )
        return line

    def _render_title(self) -> Text:
        """Строка активной беседы: заголовок, шифрование, состояние, следование."""
        conversation = self._active_conversation()
        if conversation is None:
            line = Text(_(_NO_ACTIVE), style="dim")
            self._append_follow(line)
            return line
        line = Text()
        line.append(conversation.display_title, style="bold")
        if conversation.is_muc:
            line.append("  ")
            line.append(_(_MUC_LABEL), style="blue")
            occupants = self._occupants.get(conversation.jid, ())
            if occupants:
                # Число участников в заголовке, а не отдельной панелью: панель
                # на десяти строках отняла бы место у ленты, а на вопрос "кто
                # здесь" отвечает палитра по Ctrl+O.
                count = len(occupants)
                label = ngettext("{count} occupant", "{count} occupants", count)
                line.append(f"  {label.format(count=count)}", style="dim")
        line.append("  ")
        line.append(
            self._conversation_encryption(conversation),
            style=_ENCRYPTION_STYLES[conversation.encryption],
        )
        if conversation.topic:
            line.append(f"  {conversation.topic}", style="dim")
        state = self._peer_states.get(conversation.jid)
        if state is not None:
            line.append(f"  {state[0]}", style="italic cyan")
        self._append_follow(line)
        return line

    def _append_follow(self, line: Text) -> None:
        """Дописать признак следования за лентой и число новых строк."""
        if self._log.follow:
            line.append(f"  {_FOLLOW_MARK}", style="dim green")
            return
        pending = self._log.pending_lines
        suffix = f" +{pending}" if pending else ""
        line.append(f"  {_PAUSE_MARK}{suffix}", style="bold yellow")

    def _render_day(self, ts: float) -> RenderableType:
        """Разделитель календарного дня."""
        label = time.strftime(_DAY_FORMAT, time.localtime(ts))
        line = Text(f"{_DAY_RULE * 2} {label} ", style="dim")
        line.append(_DAY_RULE * 4, style="dim")
        return _hanging(Text(" " * (_TIME_WIDTH + 1)), line)

    def _render_hint(self) -> RenderableType:
        """Подсказка пустого окна."""
        if self._active is not None:
            return _hanging(Text(" " * (_TIME_WIDTH + 1)), Text(_(_NO_HISTORY), style="dim"))
        lines = Group(*(Text(_(item), style="dim") for item in _START_HINT))
        return _hanging(Text(" " * (_TIME_WIDTH + 1)), lines)

    def _render_message(self, message: Message) -> RenderableType:
        """Блок сообщения: строка с телом и строка меток под ней."""
        own = message.direction is Direction.OUT
        body = Text(no_wrap=False, overflow="fold")
        is_muc = self._is_muc(message.conversation)
        name = _sender_name(message, is_muc)
        # В комнате участники различаются цветом по XEP-0392: на двух десятках
        # человек читать ники у каждой реплики невозможно. В личной беседе цвет
        # не нужен - собеседник там один.
        style = _OWN_SENDER_STYLE if own else _PEER_SENDER_STYLE
        if is_muc and not own:
            style = f"bold {color_of(name)}"
        body.append(f"{name}: ", style=style)
        if message.body:
            body.append(message.body)
        else:
            # Пустое тело - штатный случай: сообщение могло нести только вложение
            # или маркер. Строка с одним именем отправителя неотличима от сбоя.
            body.append(_(_EMPTY_BODY), style="dim italic")
        if message.corrected:
            body.append(f" {_CORRECTED_MARK}", style="dim italic")
        if own:
            # Состояние доставки имеет смысл только у своих сообщений.
            body.append(
                f" {DELIVERY_MARKS[message.state]}",
                style=_STATE_STYLES[message.state],
            )
        marks = self._compact_marks(message)
        if marks:
            body.append(f"  {marks}", style=_COMPACT_MARK_STYLE)
        badges = self._render_badges(message)
        content: RenderableType = body if badges is None else Group(body, badges)
        return _hanging(_prefix(_format_time(message.ts)), content)

    def _compact_marks(self, message: Message) -> str:
        """Знаки расширений, которые срабатывают на каждом сообщении.

        Полная метка у них одинакова у всей ленты, поэтому она свернута в знак
        и приписана к строке сообщения. Повторы схлопываются: запрошенная и
        полученная квитанция дают один знак, а не два одинаковых.
        """
        marks: list[str] = []
        for item in message.xeps:
            mark = compact_badge(item)
            if mark is not None and mark not in marks:
                marks.append(mark)
        return " ".join(marks)

    def _render_badges(self, message: Message) -> Text | None:
        """Строка меток: расширения из справочника плюс метка шифрования.

        Метка шифрования собирается только из данных сообщения. Число доверенных
        устройств - величина текущая: подставлять ее в старое сообщение значит
        переписывать историю задним числом.
        """
        badges: list[Text] = [
            Text(f"[{badge_text(item)}]", style=xep_color(item.xep))
            for item in message.xeps
            if badge_visible(item) and compact_badge(item) is None
        ]
        if message.encryption is not Encryption.PLAIN:
            badges.append(
                Text(
                    f"[{message.encryption.value.upper()}]",
                    style=_ENCRYPTION_STYLES[message.encryption],
                )
            )
        if not badges:
            return None
        line = Text(no_wrap=False, overflow="fold")
        for index, badge in enumerate(badges):
            if index:
                line.append(" ")
            line.append_text(badge)
        return line

    def _render_xep_line(self, event: XepEvent) -> RenderableType:
        """Отдельная строка срабатывания расширения без привязки к сообщению."""
        line = Text(no_wrap=False, overflow="fold")
        line.append(f"[{format_xep_badge(event)}]", style=xep_color(event.xep))
        if event.peer:
            line.append(f" {event.peer}", style="dim")
        detail = format_xep_detail(event)
        if detail:
            line.append(f" {detail}", style="dim")
        return _hanging(_prefix(_format_service_time(event.ts)), line)

    def _conversation_encryption(self, conversation: Conversation) -> str:
        """Метка шифрования беседы с числом доверенных устройств.

        Знак доверия тот же, что в статус-баре: полное доверие, частичное и его
        отсутствие обязаны выглядеть одинаково в обоих местах, иначе один и тот
        же факт читается как два разных.
        """
        encryption = conversation.encryption
        if encryption is Encryption.PLAIN:
            return _PLAIN_LABEL
        name = encryption.value.upper()
        omemo = conversation.omemo
        if encryption is not Encryption.OMEMO or not omemo.enabled or omemo.total_devices == 0:
            return name
        trusted, total = omemo.trusted_devices, omemo.total_devices
        if trusted == total:
            return f"{name} {_TRUSTED_MARK} {trusted} {_DEVICES_SUFFIX}"
        mark = _PARTIAL_MARK if trusted else _UNTRUSTED_MARK
        return f"{name} {mark} {trusted}/{total} {_DEVICES_SUFFIX}"
