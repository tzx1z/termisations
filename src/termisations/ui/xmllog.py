"""Панель сырого XMPP-потока: кольцевой буфер, батч-рендер, подсветка, follow и pause.

Самый нагруженный виджет клиента. Целевой бюджет - 500 строф в секунду, поэтому
горячий путь и путь отрисовки разделены полностью:

* ``push`` вызывается из шины событий синхронно. Он только кладет строфу в кольцевой
  буфер и увеличивает счетчик. Ни маскирования, ни разбора, ни обращения к Textual;
* таймер с интервалом 0.05 секунды пересобирает индекс строк и обновляет виджет
  один раз. Двадцать обновлений в секунду - потолок частоты, которую воспринимает
  глаз в терминале, а обновление на каждую строфу означало бы 500 перерисовок
  в секунду.

Почему область строф сделана собственным ``ScrollView`` с ``render_line``, а не
``RichLog``. RichLog хранит уже отрендеренные строки, поэтому стоимость показа
пропорциональна числу прошедших строф, а не размеру экрана: замер на этом же коде дал
0.19 мс на строку, то есть около 370 мс на перерисовку буфера из 2000 строф. Такая
перерисовка нужна при смене фильтра, режима, флага unsafe, при возврате из pause и на
каждом шаге курсора - на клавишу это неприемлемо. Собственный ``render_line`` рисует
только видимые строки, а результат кладет в кэш ``Strip`` по номеру строфы, поэтому
любое изменение состояния стоит один экран, а не весь буфер. В pause и при скрытой
панели рендера нет вовсе.

Внешние узлы панели: ``#xmllog-header`` и ``#xmllog-body``, на них опираются стили,
app.py и тесты.

Маскирование вызывается здесь, при рендере строки, а не при push. В буфере лежит
исходный текст строфы, поэтому переключение режима unsafe меняет вид уже накопленного
буфера без повторного получения данных.
"""

import re
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from itertools import islice
from types import MappingProxyType
from typing import ClassVar, Final

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.geometry import Region, Size
from textual.scroll_view import ScrollView
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Static

from termisations.core.commands import XML_FILTER_FIELDS
from termisations.core.events import (
    EventBus,
    StanzaLogged,
    Subscription,
    UnsafeModeChanged,
    XmlLogModeChanged,
    XmlMode,
)
from termisations.core.i18n import N_, _, ngettext
from termisations.core.models import Direction, RawStanza, StanzaKind
from termisations.core.redact import redact

__all__ = ["DEFAULT_CAPACITY", "FLUSH_INTERVAL", "XmlLogPanel", "XmlLogStats"]

DEFAULT_CAPACITY: Final = 2000
"""Размер кольцевого буфера по умолчанию."""

FLUSH_INTERVAL: Final = 0.05
"""Интервал батч-рендера: не чаще 20 обновлений в секунду."""

# Ширина, с которой панель работает до первой раскладки.
_FALLBACK_WIDTH: Final = 80
# Минимум, который остается под тело строфы даже на очень узкой панели.
_MIN_BODY_WIDTH: Final = 12
# Отступ строк развернутого вида и отступ перенесенной части длинной строки.
_EXPAND_GUTTER: Final = "   "
_CONTINUATION_INDENT: Final = "    "
_INDENT: Final = "  "
_ELLIPSIS: Final = "…"

# Маркеры первой колонки: курсор, развернутая строфа, обычная строка.
_MARK_CURSOR: Final = "▸"
_MARK_EXPANDED: Final = "▾"
_MARK_PLAIN: Final = " "

# Стиль строки под курсором.
_CURSOR_STYLE: Final = Style(reverse=True)

# Строка индекса: номер строфы, сама строфа, номер подстроки. Подстрока 0 - свернутый
# вид, остальные - строки развернутого тела. Строфа лежит прямо в индексе, а не ищется
# в буфере: в pause вид заморожен, и показанные строфы должны оставаться доступными
# даже после вытеснения из буфера.
_Row = tuple[int, RawStanza, int]


@dataclass(frozen=True, slots=True)
class XmlLogStats:
    """Показатели панели для команды статистики."""

    buffered: int
    """Строф в кольцевом буфере."""

    dropped: int
    """Вытеснено из буфера с момента последней очистки."""

    paused: bool
    """Панель в режиме pause."""

    pending: int
    """Накоплено строф, которые еще не показаны."""


@dataclass(frozen=True, slots=True)
class _Palette:
    """Набор цветов одной группы строф."""

    prefix: str
    element: str
    attribute: str
    value: str
    namespace: str
    content: str
    punct: str


# Входящие и исходящие строфы отличаются цветом целиком, а не одним символом
# направления: при плотном потоке направление читается по цвету быстрее.
_PALETTE_IN: Final = _Palette(
    prefix="bold green",
    element="green",
    attribute="cyan",
    value="white",
    namespace="blue",
    content="bright_white",
    punct="grey50",
)
_PALETTE_OUT: Final = _Palette(
    prefix="bold cyan",
    element="cyan",
    # bright_yellow вместо magenta: в темной теме Textual magenta, red и bright_red
    # разрешаются в один и тот же цвет, и обычная исходящая строфа выглядела как
    # ошибочная.
    attribute="bright_yellow",
    value="white",
    namespace="blue",
    content="bright_white",
    punct="grey50",
)
_PALETTE_LOCAL: Final = _Palette(
    prefix="bold yellow",
    element="yellow",
    attribute="grey70",
    value="white",
    namespace="blue",
    content="bright_white",
    punct="grey50",
)
_PALETTES: Final[Mapping[Direction, _Palette]] = MappingProxyType(
    {
        Direction.IN: _PALETTE_IN,
        Direction.OUT: _PALETTE_OUT,
        Direction.LOCAL: _PALETTE_LOCAL,
    }
)

# Ошибочная строфа отделяется фоном и префиксом, а не цветом текста: если красить
# красным все токены, внутри строфы пропадает различие имени элемента, атрибута и
# пространства имен, то есть теряется сама подсветка.
_ERROR_PREFIX: Final = "bold white on red"
_ERROR_STYLE: Final = Style(bgcolor="#3a0d1e")

_PALETTES_ERROR: Final[Mapping[Direction, _Palette]] = MappingProxyType(
    {direction: replace(palette, prefix=_ERROR_PREFIX) for direction, palette in _PALETTES.items()}
)

# Подписи направления фиксированной ширины: колонки не должны прыгать.
_DIRECTION_LABELS: Final[Mapping[Direction, str]] = MappingProxyType(
    {Direction.IN: "IN ", Direction.OUT: "OUT", Direction.LOCAL: "LOC"}
)

# Короткие подписи типов строф, ширина колонки 4 символа.
_KIND_LABELS: Final[Mapping[StanzaKind, str]] = MappingProxyType(
    {
        StanzaKind.MESSAGE: "msg ",
        StanzaKind.PRESENCE: "pres",
        StanzaKind.IQ: "iq  ",
        StanzaKind.STREAM: "strm",
        StanzaKind.SASL: "sasl",
        StanzaKind.TLS: "tls ",
        StanzaKind.OTHER: "oth ",
    }
)

# Подсветка одним проходом. Порядок ветвей значим: сначала имя элемента, затем
# текстовый узел (он распознается только сразу после закрывающей скобки), и лишь
# потом атрибуты. Иначе текст вида a='b' внутри тела считался бы атрибутом.
# Скобки, слэши и кавычки остаются базовым стилем строки, отдельная ветвь им не нужна.
_XML_TOKEN_RE: Final = re.compile(
    r"(?P<comment><!--.*?-->)"
    r"|(?P<redacted>\[[^\[\]]*redacted\])"
    r"|</?(?P<element>[A-Za-z_][\w.:-]*)"
    r"|(?<=>)(?P<content>[^<>]+)"
    r"|(?P<xmlns>xmlns(?::[A-Za-z_][\w.-]*)?)\s*=\s*(?P<nsquote>[\"'])"
    r"(?P<nsvalue>[^\"']*)(?P=nsquote)"
    r"|(?P<attr>[A-Za-z_][\w.:-]*)\s*=\s*(?P<quote>[\"'])(?P<value>[^\"']*)(?P=quote)"
)

# Разбор строфы на теги и текстовые узлы для форматирования с отступами.
# Внутри тега кавычки учитываются: символ ">" в значении атрибута допустим по
# стандарту и не должен считаться концом тега. Ветви альтернативы не пересекаются
# по первому символу, поэтому отката по строке нет.
_PRETTY_TOKEN_RE: Final = re.compile(r"""<(?:[^>"']|"[^"]*"|'[^']*')*>|[^<]+""")

# Схлопывание переводов строк и повторных пробелов в свернутом виде.
_SPACES_RE: Final = re.compile(r"\s+")

# Стиль отметки о маскировании. Отдельный стиль нужен, чтобы в потоке сразу было
# видно, где вывод обрезан правилами безопасности, а где строфа показана целиком.
_REDACTED_STYLE: Final = "italic #e0d561"

# Пространство имен по умолчанию есть у каждой строфы потока и ничего не сообщает,
# а в свернутой строке занимает 22 колонки из 60.
_DEFAULT_NS_RE: Final = re.compile(r"\s+xmlns='jabber:client'")

# Первый дочерний элемент и его пространство имен: именно они несут смысл строфы.
_FIRST_CHILD_RE: Final = re.compile(
    r"><(?P<name>[A-Za-z_][\w.:-]*)(?P<attrs>(?:[^>\"']|\"[^\"]*\"|'[^']*')*)"
)
_CHILD_NS_RE: Final = re.compile(r"xmlns\s*=\s*['\"]([^'\"]*)['\"]")

# Поля фильтра. Набор берется из реестра команд: там же его читает автодополнение,
# и справка с разбором не расходятся. Все остальное считается свободным текстом.
_FILTER_FIELDS: Final = frozenset(XML_FILTER_FIELDS)
_TRUE_VALUES: Final = frozenset({"true", "1", "yes", "y", "on", "да"})
_FALSE_VALUES: Final = frozenset({"false", "0", "no", "n", "off", "нет"})
_KIND_HINT: Final = "msg, pres, iq, strm, sasl, tls, oth"
_ERR_HINT: Final = N_("yes, no")

# Подсказки пустой панели: пользователь должен понимать, почему ничего не видно.
_EMPTY_FILTER: Final = N_("no stanzas match the filter, Esc clears the filter")
_EMPTY_OFF: Final = N_("display is off, the buffer keeps filling, /xml both turns display on")

# Порог, выше которого поток глазом уже не читается.
_READABLE_RATE: Final = 30.0
# Минимум, который остается под текст фильтра в заголовке.
_MIN_FILTER_WIDTH: Final = 8
_EMPTY_BUFFER: Final = N_("no stanzas")
_EMPTY_MODE: Final = N_("no matching stanzas in {mode} mode, /xml both shows both directions")

_KIND_ALIASES: Final[Mapping[str, StanzaKind]] = MappingProxyType(
    {
        "msg": StanzaKind.MESSAGE,
        "message": StanzaKind.MESSAGE,
        "pres": StanzaKind.PRESENCE,
        "presence": StanzaKind.PRESENCE,
        "iq": StanzaKind.IQ,
        "stream": StanzaKind.STREAM,
        "strm": StanzaKind.STREAM,
        "sasl": StanzaKind.SASL,
        "tls": StanzaKind.TLS,
        "other": StanzaKind.OTHER,
        "oth": StanzaKind.OTHER,
    }
)


@dataclass(frozen=True, slots=True)
class _FilterTerm:
    """Одно условие фильтра. Условия объединяются по И."""

    field: str
    value: str


@dataclass(frozen=True, slots=True)
class XmlFilterResult:
    """Итог применения фильтра: годность выражения и что показать пользователю."""

    ok: bool
    text: str


def _parse_filter(expression: str) -> tuple[tuple[_FilterTerm, ...], tuple[str, ...]]:
    """Разобрать выражение фильтра вместе с ошибками разбора.

    Поддерживаются формы ``kind:iq``, ``jid:bob@srv``, ``ns:urn:xmpp:mam:2``,
    ``err:да`` и свободный текст. Значения полей проверяются здесь: неизвестный
    тип строфы или нечитаемое значение err не дают ни одного совпадения никогда,
    и без проверки пользователь видит молча опустевшую панель.

    Неизвестное имя поля перед двоеточием ошибкой не считается только тогда,
    когда такого поля в списке нет вовсе: ``urn:xmpp:mam:2`` - это свободный
    текст, а не поле ``urn``.
    """
    terms: list[_FilterTerm] = []
    errors: list[str] = []
    for token in expression.split():
        field, separator, value = token.partition(":")
        name = field.lower()
        if not separator or name not in _FILTER_FIELDS:
            terms.append(_FilterTerm("text", token.lower()))
            continue
        lowered = value.lower()
        if not lowered:
            errors.append(_("{name}: no value given").format(name=name))
            continue
        if name == "kind" and lowered not in _KIND_ALIASES:
            errors.append(
                _("kind:{value} not recognized, allowed: {allowed}").format(
                    value=value, allowed=_KIND_HINT
                )
            )
            continue
        if name == "err" and lowered not in _TRUE_VALUES and lowered not in _FALSE_VALUES:
            errors.append(
                _("err:{value} not recognized, allowed: {allowed}").format(
                    value=value, allowed=_(_ERR_HINT)
                )
            )
            continue
        terms.append(_FilterTerm(name, lowered))
    return tuple(terms), tuple(errors)


def _match_terms(
    terms: tuple[_FilterTerm, ...],
    stanza: RawStanza,
    searchable: Callable[[], str],
) -> bool:
    """Проверить строфу по всем условиям фильтра.

    ``searchable`` отдает текст строфы в том виде, в котором она показана на экране:
    замаскированном, если режим unsafe выключен. Искать по сырому тексту нельзя.
    Показ или скрытие строки - это ответ "да" или "нет" на подстроку, то есть оракул,
    которым замаскированное тело подбирается посимвольно без включения unsafe.

    Вызов ленивый: условия по типу строфы и по признаку ошибки текст не требуют,
    а маскирование на горячем пути стоит дороже сравнения.
    """
    lowered: str | None = None
    for term in terms:
        field = term.field
        if field == "kind":
            expected = _KIND_ALIASES.get(term.value)
            if expected is None or stanza.kind is not expected:
                return False
            continue
        if field == "err":
            if term.value in _TRUE_VALUES:
                if not stanza.is_error:
                    return False
            elif term.value in _FALSE_VALUES:
                if stanza.is_error:
                    return False
            else:
                return False
            continue
        if lowered is None:
            lowered = searchable()
        if field == "jid":
            peer = stanza.peer
            if peer is not None and term.value in peer.lower():
                continue
            if term.value not in lowered:
                return False
            continue
        # Пространство имен и свободный текст ищутся подстрокой по телу строфы.
        if term.value not in lowered:
            return False
    return True


def _format_time(ts: float) -> str:
    """Время с миллисекундами. Собирается вручную: strftime заметно дороже."""
    local = time.localtime(ts)
    milliseconds = int((ts - int(ts)) * 1000)
    return f"{local.tm_hour:02d}:{local.tm_min:02d}:{local.tm_sec:02d}.{milliseconds:03d}"


def _highlight(xml: str, palette: _Palette) -> Text:
    """Подсветить строфу одним проходом регулярного выражения.

    Разными цветами выделяются имя элемента, имя атрибута, значение атрибута,
    объявление пространства имен и текстовый узел. Внешние библиотеки подсветки
    не используются: их стоимость на 500 строфах в секунду неоправданна.
    """
    text = Text(xml, style=palette.punct, end="")
    stylize = text.stylize
    for match in _XML_TOKEN_RE.finditer(xml):
        start = match.start("comment")
        if start >= 0:
            # Локальные строфы (разбор SRV, параметры TLS) приходят комментарием.
            # Это самая содержательная часть подключения, красить ее цветом
            # пунктуации нельзя.
            stylize(palette.content, start, match.end("comment"))
            continue
        start = match.start("redacted")
        if start >= 0:
            stylize(_REDACTED_STYLE, start, match.end("redacted"))
            continue
        start = match.start("element")
        if start >= 0:
            stylize(palette.element, start, match.end("element"))
            continue
        start = match.start("content")
        if start >= 0:
            stylize(palette.content, start, match.end("content"))
            continue
        start = match.start("xmlns")
        if start >= 0:
            stylize(palette.namespace, start, match.end("xmlns"))
            stylize(palette.namespace, match.start("nsvalue"), match.end("nsvalue"))
            continue
        start = match.start("attr")
        if start >= 0:
            stylize(palette.attribute, start, match.end("attr"))
            stylize(palette.value, match.start("value"), match.end("value"))
    return text


def _pretty_print(xml: str) -> tuple[str, ...]:
    """Разложить строфу по строкам с отступами.

    Собственный форматтер вместо ``xml.dom.minidom`` выбран по двум причинам.
    Во-первых, minidom бросает исключение на неполной строфе и на строфе с
    необъявленным префиксом, а для отладочной панели это штатный вход.
    Во-вторых, minidom переписывает кавычки и порядок объявлений, а панель должна
    показывать ровно то, что было в потоке.

    Функция не бросает исключений. Если разметка не разбирается целиком, строфа
    возвращается одной строкой как есть.
    """
    tokens: list[str] = []
    position = 0
    for match in _PRETTY_TOKEN_RE.finditer(xml):
        if match.start() != position:
            # Разрыв означает обрезанный или поврежденный XML.
            return (xml,)
        position = match.end()
        tokens.append(match.group(0))
    if position != len(xml) or not tokens:
        return (xml,)

    lines: list[str] = []
    depth = 0
    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if not token.startswith("<"):
            content = " ".join(token.split())
            if content:
                lines.append(_INDENT * depth + content)
            index += 1
            continue
        if token.startswith("</"):
            depth = max(depth - 1, 0)
            lines.append(_INDENT * depth + token)
            index += 1
            continue
        if token.startswith(("<?", "<!")) or token.endswith("/>"):
            lines.append(_INDENT * depth + token)
            index += 1
            continue
        # Элемент с одним текстовым узлом держится на одной строке: разрывать
        # <body>текст</body> на три строки нечитаемо.
        if (
            index + 2 < total
            and not tokens[index + 1].startswith("<")
            and tokens[index + 2].startswith("</")
        ):
            content = " ".join(tokens[index + 1].split())
            lines.append(_INDENT * depth + token + content + tokens[index + 2])
            index += 3
            continue
        if index + 1 < total and tokens[index + 1].startswith("</"):
            lines.append(_INDENT * depth + token + tokens[index + 1])
            index += 2
            continue
        lines.append(_INDENT * depth + token)
        depth += 1
        index += 1
    return tuple(lines) if lines else (xml,)


def _child_hint(body: str) -> str:
    """Имя первого дочернего элемента строфы и его пространство имен."""
    match = _FIRST_CHILD_RE.search(body)
    if match is None:
        return ""
    namespace = _CHILD_NS_RE.search(match.group("attrs"))
    name = match.group("name")
    return f"<{name} {namespace.group(1)}>" if namespace else f"<{name}>"


def _clip(body: str, available: int) -> str:
    """Усечь тело строфы, сохранив первый дочерний элемент."""
    hint = _child_hint(body)
    if hint and hint not in body[:available]:
        keep = available - len(hint) - len(_ELLIPSIS)
        if keep >= _MIN_BODY_WIDTH:
            return body[:keep] + _ELLIPSIS + hint
    return body[: available - 1] + _ELLIPSIS


def _wrap(line: str, width: int) -> list[str]:
    """Перенести длинную строку по ширине панели.

    Отступ продолжения считается от отступа самой строки, а не фиксирован: при
    фиксированном отступе хвост элемента третьего уровня вложенности оказывался
    левее своего открывающего тега и читался как элемент верхнего уровня.

    Точка разрыва ищется по ближайшему пробелу слева, чтобы не резать имя
    атрибута пополам. Если пробела в пределах строки нет, разрыв жесткий:
    предсказуемое число строк важнее красоты переноса.
    """
    limit = max(width, _MIN_BODY_WIDTH)
    if len(line) <= limit:
        return [line]
    own_indent = " " * (len(line) - len(line.lstrip(" ")) + len(_INDENT))
    step = max(limit - len(own_indent), _MIN_BODY_WIDTH)
    chunks: list[str] = []
    rest = line
    size = limit
    while len(rest) > size:
        cut = rest.rfind(" ", size // 2, size)
        if cut <= 0:
            cut = size
        chunks.append(rest[:cut].rstrip())
        rest = own_indent + rest[cut:].lstrip(" ")
        size = max(len(own_indent) + step, _MIN_BODY_WIDTH)
    chunks.append(rest)
    return chunks


class _XmlBody(ScrollView):
    """Область строф. Рисует только видимые строки, содержимое берет у панели."""

    can_focus = False

    def __init__(self, panel: "XmlLogPanel") -> None:
        super().__init__(id="xmllog-body")
        self._panel = panel

    def render_line(self, y: int) -> Strip:
        """Отрисовать одну видимую строку."""
        width = self.scrollable_content_region.width
        strip = self._panel.render_row(self.scroll_offset.y + y, width)
        return strip.apply_style(self.rich_style)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Текст под выделением мышью.

        Строки собираются заново, а не берутся с экрана: в буфере они лежат
        целиком, а видимая часть обрезана шириной панели. Копировать обрезанную
        строфу бессмысленно - ее потом не разобрать.
        """
        width = self.scrollable_content_region.width or self.size.width
        text = "\n".join(
            self._panel.render_row(row, width).text for row in range(self._panel.row_count())
        )
        return selection.extract(text), "\n"


class XmlLogPanel(Widget):
    """Панель сырого XML: кольцевой буфер, батч-рендер, follow и pause."""

    DEFAULT_CSS = """
    XmlLogPanel {
        layout: vertical;
        height: 1fr;
    }
    XmlLogPanel > #xmllog-header {
        height: 1;
        width: 1fr;
    }
    XmlLogPanel > #xmllog-body {
        height: 1fr;
        width: 1fr;
        overflow-x: hidden;
        scrollbar-size-vertical: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor_up", N_("Stanza up"), show=False),
        Binding("down", "cursor_down", N_("Stanza down"), show=False),
        Binding("enter", "toggle_expand", N_("Expand stanza"), show=False),
        Binding("end", "follow", N_("Back to the stream"), show=False),
        Binding("pageup", "page_up", N_("Page up"), show=False),
        Binding("pagedown", "page_down", N_("Page down"), show=False),
    ]

    can_focus = True

    def __init__(
        self,
        bus: EventBus,
        *,
        capacity: int = DEFAULT_CAPACITY,
        id: str | None = None,  # имя параметра задано контрактом виджетов Textual
        classes: str | None = None,
    ) -> None:
        """Создать панель.

        ``capacity`` - размер кольцевого буфера в строфах, не меньше единицы.
        """
        if capacity < 1:
            raise ValueError(_("panel buffer size must be at least 1"))
        super().__init__(id=id, classes=classes)
        self._bus = bus
        self._subs = Subscription()
        self._capacity = capacity
        self._buffer: deque[RawStanza] = deque(maxlen=capacity)

        # Сквозная нумерация строф. Номер не переиспользуется после вытеснения,
        # поэтому по нему безопасно адресовать курсор, развернутые строфы и кэши.
        self._seq = 0
        self._dropped = 0
        self._pending = 0
        # Граница, до которой строфы уже учтены счетчиком отложенных.
        self._counted_seq = 0
        # Граница показа. В follow равна _seq, в pause заморожена на моменте перехода:
        # показанный вид не меняется, пока пользователь не вернется к потоку.
        self._shown_seq = 0

        self._mode = XmlMode.BOTH
        self._filter = ""
        self._terms: tuple[_FilterTerm, ...] = ()
        self._unsafe = False
        self._follow = True
        self._paused_by_scroll = False
        # Положение прокрутки, на которое панель встала сама при последнем показе
        # конца потока. По нему отличается уход пользователя вверх от смены высоты
        # области: строка подсказки под вводом появляется и исчезает по ходу набора,
        # панель от этого становится на строку ниже, а прокрутка остается прежней.
        self._end_offset = 0

        self._rows: list[_Row] = []
        self._cursor_seq: int | None = None
        self._expanded: set[int] = set()
        self._strip_cache: dict[tuple[int, int], Strip] = {}
        self._pretty_cache: dict[int, tuple[Text, ...]] = {}
        # Текст строф для фильтра: замаскированный и приведенный к нижнему регистру.
        self._search_cache: dict[int, str] = {}
        self._width = _FALLBACK_WIDTH
        self._header_key: tuple[object, ...] | None = None
        # Частота потока: считается по окну, а не по каждой строфе. Метрика сессии
        # сюда не доходит, а показать ее нужно именно в той панели, к которой она
        # относится.
        self._rate = 0.0
        self._rate_seq = 0
        self._rate_at = time.monotonic()

        self._header = Static(id="xmllog-header")
        self._body = _XmlBody(self)

    # Жизненный цикл.

    def compose(self) -> ComposeResult:
        """Заголовок панели и область строф."""
        yield self._header
        yield self._body

    def on_mount(self) -> None:
        """Подписаться на шину и запустить таймер батч-рендера."""
        self._subs.add(self._bus.subscribe(StanzaLogged, self._on_stanza))
        self._subs.add(self._bus.subscribe(XmlLogModeChanged, self._on_mode_changed))
        self._subs.add(self._bus.subscribe(UnsafeModeChanged, self._on_unsafe_changed))
        self.set_interval(FLUSH_INTERVAL, self._flush)
        self._sync_view(scroll_to_end=True)
        self.apply_language()

    def on_unmount(self) -> None:
        """Снять подписки: панели в дереве уже нет."""
        self._subs.close()

    def apply_language(self) -> None:
        """Перерисовать заголовок и подсказку пустой панели на текущем языке.

        Заголовок обновляется только при смене своих полей, поэтому ключ
        сбрасывается. Строки строф не содержат переводимого текста, их кэш
        остается.
        """
        self._header_key = None
        self._update_header()
        self._body.refresh()

    def on_resize(self) -> None:
        """Ширина изменилась - усечение и переносы считаются заново."""
        self._check_width()

    # Горячий путь.

    def push(self, stanza: RawStanza) -> None:
        """Положить строфу в буфер.

        Вызывается из шины на горячем пути до 500 раз в секунду. Здесь нет ни
        маскирования, ни разбора, ни обращения к Textual: только запись в буфер.
        Фильтр и режим панели тоже не проверяются, иначе смена режима теряла бы
        историю, которая уже пришла.
        """
        buffer = self._buffer
        if len(buffer) == self._capacity:
            self._dropped += 1
        buffer.append(stanza)
        self._seq += 1

    def _on_stanza(self, event: StanzaLogged) -> None:
        """Обработчик подписки. Делает ровно одно действие и выходит."""
        self.push(event.stanza)

    def _on_mode_changed(self, event: XmlLogModeChanged) -> None:
        self.set_mode(event.mode)

    def _on_unsafe_changed(self, event: UnsafeModeChanged) -> None:
        self.set_unsafe(event.enabled)

    # Управление.

    def set_mode(self, mode: XmlMode) -> None:
        """Задать режим панели.

        В режиме OFF рендер приостановлен, но буфер продолжает наполняться:
        при обратном включении история остается на месте.
        """
        if mode is self._mode:
            return
        self._mode = mode
        if self._follow:
            self._shown_seq = self._seq
        self._sync_view(scroll_to_end=self._follow)
        self._update_header()

    def set_filter(self, expression: str) -> XmlFilterResult:
        """Задать фильтр. Пустая строка снимает его.

        Возвращает разобранные условия и число подошедших строф. Опечатка в имени
        поля или в значении молча опустошала панель, поэтому неверное выражение
        не применяется, а объясняется.
        """
        normalized = expression.strip()
        terms, errors = _parse_filter(normalized)
        if errors:
            return XmlFilterResult(ok=False, text="; ".join(errors))
        if normalized != self._filter:
            self._filter = normalized
            self._terms = terms
            self._sync_view(scroll_to_end=self._follow)
            self._update_header()
        if not normalized:
            return XmlFilterResult(ok=True, text=_("log filter cleared"))
        shown = sum(1 for _stanza in self._visible())
        total = len(self._buffer)
        text = ngettext(
            "log filter: {expression} - {shown} of {total} stanza",
            "log filter: {expression} - {shown} of {total} stanzas",
            total,
        ).format(expression=normalized, shown=shown, total=total)
        return XmlFilterResult(ok=True, text=text)

    def set_unsafe(self, enabled: bool) -> None:
        """Включить или выключить показ строф без маскирования.

        Кэши сбрасываются: в них лежат уже замаскированные строки, а после
        переключения весь накопленный буфер должен показываться по новым правилам.
        """
        if enabled == self._unsafe:
            return
        self._unsafe = enabled
        self._strip_cache.clear()
        self._pretty_cache.clear()
        self._search_cache.clear()
        self._sync_view(scroll_to_end=self._follow)
        self._update_header()

    def toggle_follow(self) -> bool:
        """Переключить follow и pause. Возвращает новое состояние follow."""
        self._set_follow(not self._follow)
        return self._follow

    def clear(self) -> None:
        """Очистить буфер и панель. Счетчики вытесненных и отложенных сбрасываются."""
        self._buffer.clear()
        self._dropped = 0
        self._pending = 0
        self._counted_seq = self._seq
        self._shown_seq = self._seq
        self._cursor_seq = None
        self._expanded.clear()
        self._strip_cache.clear()
        self._pretty_cache.clear()
        self._search_cache.clear()
        self._follow = True
        self._paused_by_scroll = False
        self._sync_view(scroll_to_end=True)
        self._update_header()

    def stats(self) -> XmlLogStats:
        """Показатели панели.

        ``pending`` - число строф, пришедших с момента перехода в pause и еще не
        показанных. ``dropped`` - число строф, вытесненных из кольцевого буфера;
        они потеряны безвозвратно, и счетчик это показывает.
        """
        return XmlLogStats(
            buffered=len(self._buffer),
            dropped=self._dropped,
            paused=not self._follow,
            pending=self._pending,
        )

    # Действия клавиш.

    def action_cursor_up(self) -> None:
        """Перевести курсор на строфу выше. Навигация переводит панель в pause."""
        self._move_cursor(-1)

    def action_cursor_down(self) -> None:
        """Перевести курсор на строфу ниже."""
        self._move_cursor(1)

    def action_toggle_expand(self) -> None:
        """Развернуть или свернуть строфу под курсором."""
        if self._cursor_seq is None:
            self._move_cursor(0)
        target = self._cursor_seq
        if target is None:
            return
        if target in self._expanded:
            self._expanded.discard(target)
        else:
            self._expanded.add(target)
        self._sync_view(scroll_to_end=False)
        self._scroll_to_cursor()

    def action_follow(self) -> None:
        """Вернуться к потоку и досыпать накопленное."""
        self._set_follow(True)

    def action_page_up(self) -> None:
        """Страница вверх. Уход от конца потока включает pause."""
        self._set_follow(False, by_scroll=True)
        self._body.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        """Страница вниз."""
        self._body.scroll_page_down(animate=False)

    # Внутреннее состояние.

    def _set_follow(self, follow: bool, *, by_scroll: bool = False) -> None:
        """Переключить режим слежения за потоком."""
        if follow == self._follow:
            return
        self._follow = follow
        self._paused_by_scroll = by_scroll and not follow
        self._pending = 0
        self._counted_seq = self._seq
        if follow:
            # Возврат в follow досыпает все, что накопилось в буфере.
            self._cursor_seq = None
            self._shown_seq = self._seq
            self._sync_view(scroll_to_end=True)
        else:
            # Вид замораживается на текущей границе, счет отложенных идет с нее.
            # Пересборка нужна здесь же: строфы, пришедшие после последнего тика,
            # уже вошли в границу и должны быть показаны сразу, а не при следующем
            # изменении состояния.
            self._shown_seq = self._seq
            self._sync_view(scroll_to_end=False)
        self._update_header()

    def _move_cursor(self, step: int) -> None:
        """Сдвинуть курсор по видимым строфам.

        Пауза от курсора не помечается вызванной прокруткой: курсор на последней
        строфе не двигает прокрутку, и признак прокрутки вернул бы поток тем же
        тиком. Выход из такой паузы - клавиша end, она объявлена и в панели, и в
        приложении, поэтому работает и из строки ввода.
        """
        self._set_follow(False)
        sequences = [seq for seq, _, sub in self._rows if sub == 0]
        if not sequences:
            return
        current = self._cursor_seq
        if current is None or current not in sequences:
            index = len(sequences) - 1
        else:
            index = min(max(sequences.index(current) + step, 0), len(sequences) - 1)
        self._cursor_seq = sequences[index]
        self._body.refresh()
        self._scroll_to_cursor()

    def _scroll_to_cursor(self) -> None:
        """Подвести строфу под курсором в видимую область целиком.

        Подводится весь блок, а не одна строка: у нижнего края экрана разворот
        менял только маркер, а строки развернутого тела оставались за границей.
        Высота блока ограничена высотой области, иначе длинная строфа увела бы
        начало блока за верхний край.
        """
        target = self._cursor_seq
        if target is None:
            return
        for row, (seq, stanza, sub) in enumerate(self._rows):
            if seq != target or sub != 0:
                continue
            height = 1
            if seq in self._expanded:
                height += len(self._expand(seq, stanza))
            height = min(height, max(self._body.scrollable_content_region.height, 1))
            self._body.scroll_to_region(
                Region(0, row, self._width, height), animate=False, immediate=True
            )
            return

    # Индекс строк.

    def _searchable(self, seq: int, stanza: RawStanza) -> str:
        """Текст строфы для фильтра: тот же, что на экране, в нижнем регистре.

        Результат кэшируется по номеру строфы. Без кэша маскирование шло бы на
        каждую строфу буфера при каждой пересборке индекса, то есть до сорока тысяч
        вызовов в секунду на полном буфере. Кэш чистится вместе с остальными при
        смене режима unsafe и при вытеснении строф.
        """
        cached = self._search_cache.get(seq)
        if cached is None:
            cached = redact(stanza.xml, self._unsafe).lower()
            self._search_cache[seq] = cached
        return cached

    def _matches(self, seq: int, stanza: RawStanza) -> bool:
        """Проходит ли строфа режим и фильтр."""
        if not self._mode.accepts(stanza.direction):
            return False
        if not self._terms:
            return True
        return _match_terms(self._terms, stanza, lambda: self._searchable(seq, stanza))

    def _visible(self) -> Iterator[tuple[int, RawStanza]]:
        """Пары номер-строфа до границы показа, прошедшие режим и фильтр."""
        base = self._seq - len(self._buffer)
        limit = max(self._shown_seq - base, 0)
        for offset, stanza in enumerate(islice(self._buffer, limit)):
            seq = base + offset
            if self._matches(seq, stanza):
                yield seq, stanza

    def _sync_view(self, *, scroll_to_end: bool) -> None:
        """Пересобрать индекс строк и обновить виджет одним обновлением."""
        if not self.is_mounted:
            return
        base = self._seq - len(self._buffer)
        self._expanded = {seq for seq in self._expanded if seq >= base}

        rows: list[_Row] = []
        expanded = self._expanded
        for seq, stanza in self._visible():
            rows.append((seq, stanza, 0))
            if seq in expanded:
                for sub in range(1, len(self._expand(seq, stanza)) + 1):
                    rows.append((seq, stanza, sub))
        self._rows = rows

        body = self._body
        body.virtual_size = Size(self._width, len(rows))
        if scroll_to_end:
            body.scroll_end(animate=False, immediate=True)
            self._end_offset = body.scroll_offset.y
        body.refresh()

    def _check_width(self) -> None:
        """Отследить смену ширины области строф."""
        width = self._body.scrollable_content_region.width
        if width <= 0 or width == self._width:
            return
        self._width = width
        self._strip_cache.clear()
        self._pretty_cache.clear()
        self._sync_view(scroll_to_end=self._follow)

    # Рендер.

    def _poll_scroll(self) -> None:
        """Отследить прокрутку: уход вверх включает pause, возврат вниз - follow.

        Проверять просто "положение не в самом конце" нельзя. Высота области строф
        меняется без участия пользователя: под строкой ввода появляется и исчезает
        подсказка сигнатуры, и панель становится на строку-две ниже или выше.
        Конец потока при этом смещается, а прокрутка остается на месте, и панель
        уходила бы в pause посреди набора команды.

        Признак настоящей прокрутки - положение, которое не объясняется ни концом
        потока, ни сменой высоты области. Смена высоты либо отодвигает конец вниз
        (положение остается прежним), либо поднимает его вверх (положение
        подрезается до нового конца). Оба случая дают ``min(_end_offset, max_y)``.
        """
        body = self._body
        if not body.size:
            # Панель скрыта раскладкой: положения прокрутки просто нет.
            return
        offset = body.scroll_offset.y
        max_y = body.max_scroll_y
        if not self._follow:
            if self._paused_by_scroll and offset == max_y:
                self._set_follow(True)
            return
        if offset == max_y:
            self._end_offset = offset
            return
        if offset == min(self._end_offset, max_y):
            # Высота области изменилась: показ подтягивается к концу потока.
            body.scroll_end(animate=False, immediate=True)
            self._end_offset = body.scroll_offset.y
            return
        self._set_follow(False, by_scroll=True)

    def _flush(self) -> None:
        """Тик батч-рендера. Один тик - одно обновление виджета."""
        if not self.is_mounted:
            return
        self._check_width()
        self._poll_scroll()
        self._measure_rate()

        if self._mode is XmlMode.OFF:
            # Рендер приостановлен, буфер продолжает наполняться. Обратное
            # включение режима покажет накопленное целиком.
            self._pending = 0
            self._counted_seq = self._seq
            if self._follow:
                self._shown_seq = self._seq
            self._update_header()
            return

        new_count = min(self._seq - self._counted_seq, len(self._buffer))
        if new_count > 0:
            self._counted_seq = self._seq
            if self._follow:
                self._shown_seq = self._seq
                self._sync_view(scroll_to_end=True)
            else:
                self._pending += self._count_new(new_count)
        self._prune_caches()
        self._update_header()

    def _measure_rate(self) -> None:
        """Пересчитать частоту потока по окну не короче половины секунды."""
        now = time.monotonic()
        elapsed = now - self._rate_at
        if elapsed < 0.5:
            return
        self._rate = (self._seq - self._rate_seq) / elapsed
        self._rate_seq = self._seq
        self._rate_at = now

    def _count_new(self, new_count: int) -> int:
        """Сколько новых строф прошло бы в показ. Нужно для счетчика в pause."""
        # reversed по deque идет с правого конца за постоянное время на элемент,
        # поэтому хвост берется без обхода всего буфера. Номер последней строфы
        # буфера - _seq минус единица, дальше он убывает вместе с обходом.
        tail = islice(reversed(self._buffer), new_count)
        last = self._seq - 1
        return sum(1 for offset, stanza in enumerate(tail) if self._matches(last - offset, stanza))

    def row_count(self) -> int:
        """Сколько строк в буфере сейчас. Нужно копированию выделенного."""
        return len(self._rows)

    def render_row(self, row: int, width: int) -> Strip:
        """Отрисовать строку индекса. Вызывается областью строф на видимые строки."""
        rows = self._rows
        if not rows:
            return self._empty_row(row, width)
        if row < 0 or row >= len(rows):
            return Strip.blank(width)
        seq, stanza, sub = rows[row]
        # Курсор стоит на строфе, но инверсией выделяется только ее первая строка:
        # иначе развернутое тело целиком рисуется в инверсии и не читается.
        cursor = seq == self._cursor_seq and sub == 0
        if sub:
            mark = _MARK_PLAIN
        elif seq in self._expanded:
            mark = _MARK_EXPANDED
        elif cursor:
            mark = _MARK_CURSOR
        else:
            mark = _MARK_PLAIN

        # Кэшируется только обычный вид строки. Строк под курсором и заголовков
        # развернутых строф на экране единицы, их дешевле собрать заново.
        plain = mark == _MARK_PLAIN and not cursor
        if plain:
            cached = self._strip_cache.get((seq, sub))
            if cached is not None:
                return cached

        palette = self._palette(stanza)
        if sub:
            # Число строк развернутого вида зависит от ширины. Если запрос пришел
            # между сменой ширины и пересборкой индекса, строки может не оказаться.
            lines = self._expand(seq, stanza)
            if sub > len(lines):
                return Strip.blank(width)
            text = lines[sub - 1]
        else:
            # Стиль префикса не должен стать базовым стилем строки: базовый
            # стиль наследуют все присоединенные куски, и жирным становится весь
            # поток, а у ошибочной строфы красный фон растекается на тело.
            text = Text(end="")
            text.append(mark, style=palette.prefix)
            text.append_text(self._collapsed(stanza, palette))
        strip = self._make_strip(text, width)
        if stanza.is_error:
            strip = strip.apply_style(_ERROR_STYLE)
        if cursor:
            return strip.apply_style(_CURSOR_STYLE)
        if plain:
            self._strip_cache[(seq, sub)] = strip
        return strip

    def _empty_row(self, row: int, width: int) -> Strip:
        """Строка подсказки, когда показывать нечего.

        Пустая панель при фильтре без совпадений, при выключенном показе и при
        пустом буфере выглядела одинаково и неотличимо от остановки потока.
        """
        if row != 0:
            return Strip.blank(width)
        if self._mode is XmlMode.OFF:
            hint = _(_EMPTY_OFF)
        elif self._filter:
            hint = f"{_(_EMPTY_FILTER)}: {self._filter}"
        elif self._buffer:
            hint = _(_EMPTY_MODE).format(mode=self._mode.value)
        else:
            hint = _(_EMPTY_BUFFER)
        return self._make_strip(Text(f" {hint}", style="dim italic", end=""), width)

    def _make_strip(self, text: Text, width: int) -> Strip:
        """Превратить готовую строку в Strip нужной ширины.

        Используется низкоуровневый ``Text.render`` вместо ``Console.render``: он не
        делает разбор по переводам строк, перенос и выравнивание, которые здесь уже
        не нужны, и на замере выходит почти вдвое дешевле - 0.08 мс против 0.15 мс
        на строку. Ширина доводится через ``adjust_cell_length``, он же корректно
        обрезает строку с двухклеточными символами.
        """
        return Strip(text.render(self.app.console)).adjust_cell_length(width)

    def _collapsed(self, stanza: RawStanza, palette: _Palette) -> Text:
        """Свернутая строка строфы без колонки маркера.

        Голова строфы сжимается до усечения: пространство имен по умолчанию есть у
        каждой строфы и ничего не сообщает, а занимает треть доступной ширины.
        Если после усечения не остается ни одного дочернего элемента, его имя и
        пространство имен дописываются в хвост: иначе на реальных ширинах весь
        поток выглядит одинаковыми шапками message.
        """
        kind = _KIND_LABELS.get(stanza.kind, "oth ")
        head = f"{_format_time(stanza.ts)} {_DIRECTION_LABELS.get(stanza.direction, 'LOC')} {kind} "
        body = _SPACES_RE.sub(" ", redact(stanza.xml, self._unsafe)).strip()
        body = _DEFAULT_NS_RE.sub("", body)
        available = max(self._width - len(head) - 1, _MIN_BODY_WIDTH)
        if len(body) > available:
            body = _clip(body, available)
        # Базовый стиль строки не жирный: жирной остается только колонка времени,
        # направления и типа, иначе выделять префикс нечем - жирный весь поток.
        text = Text(end="")
        text.append(head, style=palette.prefix)
        text.append_text(_highlight(body, palette))
        return text

    def _expand(self, seq: int, stanza: RawStanza) -> tuple[Text, ...]:
        """Строки развернутого тела строфы. Форматирование ленивое и кэшируется."""
        cached = self._pretty_cache.get(seq)
        if cached is not None:
            return cached

        palette = self._palette(stanza)
        width = self._width - len(_EXPAND_GUTTER)
        lines: list[Text] = []
        for raw_line in _pretty_print(redact(stanza.xml, self._unsafe)):
            for chunk in _wrap(raw_line, width):
                text = Text(_EXPAND_GUTTER, style=palette.punct, end="")
                text.append_text(_highlight(chunk, palette))
                lines.append(text)
        result = tuple(lines)
        self._pretty_cache[seq] = result
        return result

    def _palette(self, stanza: RawStanza) -> _Palette:
        """Палитра строфы. Ошибка меняет только префикс, подсветка тела остается.

        Сигнал ошибки несут фон строки и префикс. Если красить красным все токены,
        внутри строфы пропадает различие имени элемента, атрибута и пространства
        имен, то есть теряется сама подсветка, ради которой панель и сделана.
        """
        table = _PALETTES_ERROR if stanza.is_error else _PALETTES
        return table.get(stanza.direction, _PALETTE_LOCAL)

    def _prune_caches(self) -> None:
        """Выбросить из кэшей строфы, вытесненные из буфера.

        Порог проверяется по обоим кэшам. Кэш текста для фильтра наполняется и
        тогда, когда рендера нет вовсе: панель скрыта раскладкой, а фильтр при этом
        продолжает считать непоказанные строфы.
        """
        limit = self._capacity * 2
        if len(self._strip_cache) <= limit and len(self._search_cache) <= limit:
            return
        base = self._seq - len(self._buffer)
        self._strip_cache = {
            key: strip for key, strip in self._strip_cache.items() if key[0] >= base
        }
        self._pretty_cache = {seq: text for seq, text in self._pretty_cache.items() if seq >= base}
        self._search_cache = {seq: text for seq, text in self._search_cache.items() if seq >= base}

    def _update_header(self) -> None:
        """Обновить заголовок панели, если что-то изменилось.

        Порядок полей идет от важности, а не от логики кода: маркер UNSAFE стоит
        сразу за признаком следования, потому что обрезается заголовок справа, а
        признак отключенного маскирования нужен всегда. Текст фильтра усекается по
        остатку ширины с многоточием: без него пользователь не видит, что фильтр
        показан не целиком.
        """
        key: tuple[object, ...] = (
            self._follow,
            self._pending,
            self._mode,
            len(self._buffer),
            self._dropped,
            self._filter,
            self._unsafe,
            round(self._rate),
            self._width,
        )
        if key == self._header_key:
            return
        self._header_key = key

        text = Text(no_wrap=True, end="")
        if self._follow:
            text.append("▶ follow", style="bold green")
        else:
            text.append("⏸ pause", style="bold yellow")
            if self._pending:
                text.append(f" +{self._pending}", style="bold yellow")
        if self._unsafe:
            text.append("  UNSAFE", style="bold white on red")
        text.append(f"  {self._mode.value}", style="cyan")
        if self._rate >= 1.0:
            # Частота показывает, почему поток не читается: это скорость, а не сбой
            # отрисовки. Выше порога читаемости подсказывается, чем себе помочь.
            style = "yellow" if self._rate > _READABLE_RATE else "grey62"
            text.append(f"  {self._rate:.0f}/s", style=style)
        text.append(f"  buf {len(self._buffer)}", style="grey62")
        if self._dropped and not self._follow:
            text.append(f"  drop {self._dropped}", style="yellow")
        elif self._dropped:
            # В follow вытеснение из кольцевого буфера - штатная работа, а не
            # потеря данных: тревожный цвет здесь означал бы не то, что есть.
            text.append(_("  evicted {count}").format(count=self._dropped), style="grey50")
        if self._rate > _READABLE_RATE and self._follow:
            text.append(_("  /xml filter or pause"), style="dim")
        if self._filter:
            rest = max(self._width - len(text.plain) - 4, _MIN_FILTER_WIDTH)
            shown = (
                self._filter if len(self._filter) <= rest else self._filter[: rest - 1] + _ELLIPSIS
            )
            text.append(f"  ✎ {shown}", style="magenta")
        self._header.update(text)
