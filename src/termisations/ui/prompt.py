"""Строка ввода: слэш-команды, история, автодополнение, черновики, накопление строк.

Виджет ничего не разбирает и в сеть не ходит. Готовая строка уходит наружу сообщением
``PromptInput.Submitted``, разбор делает ``core.commands.parse`` на стороне роутера.

Как сделан multiline. Стандартный ``Input`` однострочный, поэтому Alt+Enter не
превращает поле в редактор, а закрывает текущую строку и начинает новую: завершенные
строки лежат в буфере виджета, поле ввода всегда содержит только последнюю. Enter
отправляет буфер и поле, склеенные переводом строки, подсказка показывает число
накопленных строк, Backspace в начале пустого поля возвращает предыдущую строку
обратно в поле для правки. ``TextArea`` вместо ``Input`` не используется: app.py ищет
узел ``#prompt-input`` как ``Input``, смена типа узла на лету сломала бы стили и
обработчики в app.py.

Вставка из буфера идет всеми строками, а не первой. Стандартный ``Input`` берет
из буфера ровно одну строку (``_on_paste`` в textual), и вставленный кусок лога
молча терялся весь, кроме первой строки. Поле ввода здесь - подкласс
``PromptField``, который многострочную вставку отдает владельцу, а тот
раскладывает ее по тому же буферу, что и Alt+Enter.

История ввода переживает перезапуск. Виджет диска не касается и здесь: готовые
строки он получает при создании (``history``), а запись отдает наружу колбэком
``record``. Кто и куда пишет, решает ``app.py``; сам файл общий для всех профилей,
см. ``core/inputlog.py``.

Ctrl+R включает обратный поиск по истории, как в readline. Поле ввода на время
поиска становится строкой запроса, а найденная команда показывается в подсказке
под ним: отдельного попапа нет по той же причине, что и у вариантов дополнения -
под подсказку отведен узел ``#prompt-hint``, и новых виджетов не нужно.
Enter принимает найденную строку в поле и ничего не отправляет: в реестре есть
``/quit``, ``/send`` и ``/remove``, и отправка вслепую здесь недопустима.

Ограничение терминалов. Alt+Enter доходит до приложения только там, где работает
kitty keyboard protocol (kitty, ghostty, foot, WezTerm, xterm с включенным режимом).
В остальных терминалах Alt+Enter приходит как ESC CR, и парсер Textual сводит
последовательность к обычному enter, то есть строка отправится. Поэтому на перенос
строки повешена вторая клавиша Ctrl+J: она приходит одиночным LF и распознается везде.
"""

import re
from collections.abc import Callable, Sequence
from typing import ClassVar, Final

from rich.highlighter import Highlighter
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.suggester import Suggester
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Input, Static

from termisations.core import commands
from termisations.core.commands import CompletionContext
from termisations.core.events import EventBus
from termisations.core.i18n import N_, _
from termisations.core.inputlog import HISTORY_LIMIT

__all__ = ["PromptInput"]

# Пауза молчания, после которой собеседнику уходит состояние "остановился".
# XEP-0085 не задает величину; три секунды - то, что показывает Conversations.
TYPING_PAUSE: Final = 3.0

PENDING_LIMIT: Final = 50
"""Предел накопленных строк в одном multiline-сообщении."""

VARIANTS_SHOWN: Final = 8
"""Сколько вариантов дополнения печатается в подсказке."""

DRAFT_KEY_NONE: Final = ""
"""Ключ черновика, когда активной беседы нет."""

PLACEHOLDER: Final = N_("› message or /command")
"""Подпись пустого поля ввода."""

PLACEHOLDER_AWAITING: Final = N_("› answer")
"""Подпись поля, когда ввод ждет ответа на вопрос.

Сам вопрос стоит строкой подсказки под полем. Повторять его еще и в поле незачем:
одно и то же предложение занимало две строки экрана подряд.
"""

STYLE_COMMAND: Final = "bold cyan"
"""Известная команда в начале строки."""

STYLE_UNKNOWN: Final = "bold red"
"""Неизвестная команда в начале строки."""

STYLE_SUBCOMMAND: Final = "cyan"
"""Подкоманда из реестра, например ``fingerprints`` у ``/omemo``."""

STYLE_FLAG: Final = "yellow"
"""Флаг вида ``--limit``."""

STYLE_ESCAPE: Final = "dim"
"""Экранирующий двойной слэш."""

STYLE_HINT: Final = "dim"
"""Служебный текст подсказки."""

SEARCH_PREFIX: Final = N_("search: ")
"""Начало строки подсказки в режиме обратного поиска по истории."""

SEARCH_EMPTY: Final = N_("search: no matches")
"""Подсказка, когда искомой подстроки в истории нет."""

SEARCH_EXHAUSTED: Final = N_("  no more")
"""Хвост подсказки, когда более старых совпадений не осталось."""

_FLAG_RE: Final = re.compile(r"--[A-Za-z][\w-]*")
"""Флаг в строке команды. Компилируется один раз, подсветка идет на каждый символ."""


def _token_start(line: str) -> int:
    """Возвращает позицию начала последнего токена строки."""
    for index in range(len(line) - 1, -1, -1):
        if line[index].isspace():
            return index + 1
    return 0


def _common_prefix(values: Sequence[str]) -> str:
    """Общий префикс вариантов дополнения."""
    if not values:
        return ""
    prefix = values[0]
    for value in values[1:]:
        limit = min(len(prefix), len(value))
        length = 0
        while length < limit and prefix[length] == value[length]:
            length += 1
        prefix = prefix[:length]
        if not prefix:
            break
    return prefix


class SlashHighlighter(Highlighter):
    """Подсветка слэш-команды: имя, подкоманда, флаги.

    Команда распознается только в начале строки.
    Двойной слэш экранирует команду, поэтому подсвечивается как обычный текст.
    """

    def highlight(self, text: Text) -> None:
        """Расставляет стили по тексту строки ввода."""
        raw = text.plain
        if not raw.startswith("/"):
            return
        if raw.startswith("//"):
            text.stylize(STYLE_ESCAPE, 0, 2)
            return
        end = 1
        while end < len(raw) and not raw[end].isspace():
            end += 1
        spec = commands.find(raw[1:end])
        text.stylize(STYLE_COMMAND if spec is not None else STYLE_UNKNOWN, 0, end)
        if spec is None:
            return
        start = end
        while start < len(raw) and raw[start].isspace():
            start += 1
        stop = start
        while stop < len(raw) and not raw[stop].isspace():
            stop += 1
        if start < stop and raw[start:stop] in spec.subcommands:
            text.stylize(STYLE_SUBCOMMAND, start, stop)
        for match in _FLAG_RE.finditer(raw, end):
            text.stylize(STYLE_FLAG, match.start(), match.end())


class PromptSuggester(Suggester):
    """Inline-подсказка поля ввода: первый вариант дополнения целиком."""

    def __init__(self, owner: "PromptInput") -> None:
        # Кэш отключен: варианты зависят от активной беседы и roster, а они меняются
        # без изменения самой строки.
        super().__init__(use_cache=False, case_sensitive=True)
        self._owner = owner

    async def get_suggestion(self, value: str) -> str | None:
        """Отдает продолжение строки или ``None``, если продолжать нечем."""
        for candidate in self._owner.completion_candidates(value):
            if candidate != value and candidate.startswith(value):
                return candidate
        return None


class PromptField(Input):
    """Поле ввода, отдающее многострочную вставку владельцу.

    Стандартный ``Input`` однострочный и из буфера берет только первую строку.
    Для клиента этого мало: вставленный кусок лога или чужого сообщения терялся
    целиком, кроме первой строки, и молча - человек видел ее в поле и считал,
    что вставилось все.

    Сам виджет ничего не раскладывает: он снимает выделение, если оно было, и
    отдает текст наверх сообщением. Буфер накопленных строк живет в
    ``PromptInput``, и разбирать его двум местам сразу незачем.
    """

    class Pasted(Message):
        """Многострочный текст из буфера. Раскладывает его ``PromptInput``."""

        def __init__(self, field: "PromptField", text: str) -> None:
            super().__init__()
            self.field = field
            self.text = text

        @property
        def control(self) -> "PromptField":
            """Виджет-источник, нужен декоратору ``on`` с селектором."""
            return self.field

    def _on_paste(self, event: events.Paste) -> None:
        """Вставка из буфера: однострочная - как обычно, многострочная - наверх.

        Обработчики Textual идут по MRO, поэтому этот вызывается раньше базового,
        а не вместо него. Однострочная вставка так и достается ``Input`` без
        изменений. Многострочная подавляет базовый обработчик через
        ``prevent_default``: иначе он вставил бы первую строку еще раз, уже
        поверх разложенного текста.
        """
        text = event.text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        if "\n" not in text:
            return
        event.stop()
        event.prevent_default()
        selection = self.selection
        if not selection.is_empty:
            # Выделение снимается здесь: владелец работает уже с готовым
            # значением поля и позицией курсора, а не с диапазоном замены.
            self.replace("", *selection)
        self.post_message(self.Pasted(self, text))


class PromptInput(Widget):
    """Поле ввода сообщений и команд.

    Хранит историю, черновики по беседам и буфер строк multiline-сообщения.
    Наружу отдает только ``PromptInput.Submitted`` с готовой строкой.
    """

    DEFAULT_CSS = """
    PromptInput {
        height: auto;
        layout: vertical;
    }
    PromptInput > #prompt-input {
        height: 1;
        border: none;
        padding: 0 1;
        background: transparent;
    }
    PromptInput > #prompt-hint {
        /* Высота постоянная: появление и исчезновение подсказки по ходу набора
           меняло высоту панелей, и вывод команды уходил за нижнюю границу. */
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("tab", "complete", N_("Complete"), show=False),
        Binding("up", "history_prev", N_("History back"), show=False),
        Binding("down", "history_next", N_("History forward"), show=False),
        # Ctrl+R - обратный поиск по истории, как в readline. Палитра контактов
        # открывается по Ctrl+O. Кириллические двойники объявлены руками: функция
        # with_cyrillic живет в app.py, а он импортирует этот модуль.
        Binding("ctrl+r", "history_search", N_("History search"), show=False),
        Binding("ctrl+к", "history_search", N_("History search"), show=False),
        Binding("ctrl+К", "history_search", N_("History search"), show=False),
        # Ctrl+G отменяет поиск, тоже по readline. Escape сюда не доходит: его
        # перехватывает приложение с priority, и отмену оно зовет само.
        Binding("ctrl+g", "cancel_search", N_("Cancel search"), show=False),
        Binding("ctrl+п", "cancel_search", N_("Cancel search"), show=False),
        Binding("ctrl+П", "cancel_search", N_("Cancel search"), show=False),
        # Alt+Enter работает не во всех терминалах, Ctrl+J - совместимый дубль.
        # Кириллический двойник нужен по той же причине, что и в app: терминал с
        # расширенным протоколом клавиатуры шлет символ раскладки, а не
        # управляющий код, и Ctrl+J на русской превращается в Ctrl+О.
        Binding("alt+enter", "newline", N_("New line"), show=False),
        Binding("ctrl+j", "newline", N_("New line"), show=False),
        Binding("ctrl+о", "newline", N_("New line"), show=False),
        # priority нужен, чтобы перехватить backspace раньше самого Input;
        # когда возвращать нечего, check_action отдает клавишу обратно полю.
        Binding("backspace", "pop_line", N_("Restore line"), show=False, priority=True),
    ]

    class Typing(Message):
        """Пользователь печатает или перестал печатать.

        Сообщение виджета, а не команда шины: строка ввода не знает ни о сети,
        ни об активной беседе. Решение, кому и что отправить, принимает ``app``.
        """

        def __init__(self, prompt: "PromptInput", *, active: bool) -> None:
            super().__init__()
            self.prompt = prompt
            self.active = active

        @property
        def control(self) -> "PromptInput":
            """Виджет-источник, нужен декоратору ``on`` с селектором."""
            return self.prompt

    class Submitted(Message):
        """Строка, введенная пользователем. Обрабатывается в app.py."""

        def __init__(self, prompt: "PromptInput", line: str) -> None:
            super().__init__()
            self.prompt = prompt
            self.line = line

        @property
        def control(self) -> "PromptInput":
            """Виджет-источник, нужен декоратору ``on`` с селектором."""
            return self.prompt

    def __init__(
        self,
        bus: EventBus,
        *,
        id: str | None = None,  # имя параметра задано общим контрактом виджетов
        classes: str | None = None,
        history_limit: int = HISTORY_LIMIT,
        history: Sequence[str] = (),
        record: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        # Шина принимается для единообразия конструкторов панелей. Ввод не публикует
        # событий и не отправляет команд: строку в RunCommandLine превращает app.py.
        self._bus = bus
        self._history_limit = max(1, history_limit)
        # История прошлых запусков приезжает готовой: читает файл cli, кладет в
        # виджет app. Строки текущего сеанса добавляются в тот же список, поэтому
        # стрелка вверх не различает, что набрано сегодня, а что неделю назад.
        self._history: list[str] = list(history)[-self._history_limit :]
        self._record = record
        self._history_index: int | None = None
        self._stash = ""
        # Обратный поиск: сохраненный текст поля до входа в режим, индекс
        # найденной строки и признак, что глубже совпадений нет. None в первом
        # поле означает, что поиск не идет.
        self._search_saved: str | None = None
        self._search_match = -1
        self._search_exhausted = False
        self._pending: list[str] = []
        self._drafts: dict[str, str] = {}
        self._conversation: str | None = None
        self._completion_context = CompletionContext()
        self._tab_hint: Text | None = None
        self._tab_line = ""
        # Вопрос, на который ждут ответа: подтверждение unsafe или отправки строфы.
        self._awaiting = ""
        self._synced_value = ""
        self._typing = False
        self._pause_timer: Timer | None = None
        # Текст, выставленный до монтирования: реактивные свойства Input требуют
        # активного приложения, поэтому значение применяется в on_mount.
        self._deferred_line: str | None = None
        # Ссылка нужна, чтобы вернуть подсказку после поиска: на время поиска
        # поле ввода остается без suggester.
        self._suggester = PromptSuggester(self)
        self._input = PromptField(
            placeholder=_(PLACEHOLDER),
            highlighter=SlashHighlighter(),
            suggester=self._suggester,
            select_on_focus=False,
            id="prompt-input",
        )
        self._hint = Static("", id="prompt-hint")

    def compose(self) -> ComposeResult:
        """Поле ввода и строка подсказки под ним."""
        yield self._input
        yield self._hint

    def on_mount(self) -> None:
        """Применяет отложенный текст и приводит подсказку в исходное состояние.

        Поле ввода монтируется после самого виджета, поэтому применение отложено
        до ближайшей перерисовки.
        """
        self.call_after_refresh(self._apply_deferred)
        self.apply_language()

    def _apply_deferred(self) -> None:
        """Переносит в поле ввода текст, выставленный до монтирования."""
        if self._deferred_line is None or not self._input.is_mounted:
            return
        deferred, self._deferred_line = self._deferred_line, None
        self._replace_line(deferred)

    # Публичный интерфейс.

    def set_context(
        self,
        conversation: str | None,
        roster: Sequence[str],
        *,
        conversations: Sequence[str] = (),
        nicks: Sequence[str] = (),
        online: Sequence[str] = (),
        themes: Sequence[str] = (),
    ) -> None:
        """Меняет активную беседу и источник вариантов дополнения.

        Черновик предыдущей беседы сохраняется, черновик новой восстанавливается.
        Повторный вызов с той же беседой только обновляет контекст дополнения.
        Обязательные параметры - ``conversation`` и ``roster``, остальные
        необязательны: без них дополняются команды и roster, с ними добавляются
        открытые беседы, ники участников комнаты, порядок по presence и список тем
        оформления.
        """
        # Поиск относится к набранной строке, а строка уезжает вместе с беседой.
        self.cancel_search()
        if conversation != self._conversation:
            self._store_draft()
            self._conversation = conversation
            self._restore_draft()
        self._completion_context = CompletionContext(
            conversation=conversation,
            roster=tuple(roster),
            conversations=tuple(conversations),
            nicks=tuple(nicks),
            online=frozenset(online),
            themes=tuple(themes),
        )

    def set_awaiting(self, question: str) -> None:
        """Переводит ввод в режим ответа на вопрос и обратно.

        Пустая строка возвращает обычный вид. Без явного признака поле выглядит
        как обычно, и набранный текст молча уходит в ответ на подтверждение.
        """
        self.cancel_search()
        self._awaiting = question
        self.apply_language()

    def apply_language(self) -> None:
        """Выставить подпись поля и строку подсказки на текущем языке.

        Вызывается при монтировании, при смене режима ответа и командой /lang.
        Текст вопроса в режиме ответа приходит от приложения уже переведенным.
        """
        if self._input.is_mounted:
            self._input.placeholder = _(PLACEHOLDER_AWAITING if self._awaiting else PLACEHOLDER)
        self._update_hint()

    def focus_input(self) -> None:
        """Ставит фокус в поле ввода."""
        if self._input.is_mounted:
            self._input.focus()

    def completion_candidates(self, line: str) -> list[str]:
        """Полные варианты подстановки для строки.

        Используется и клавишей Tab, и inline-подсказкой. ``commands.complete``
        может вернуть как целую строку, так и один токен, поэтому вариант,
        который уже начинается с текущей строки, берется целиком, а остальные
        подставляются на место последнего токена.
        """
        if not line.strip():
            return []
        cut = _token_start(line)
        result: list[str] = []
        for match in commands.complete(line, self._completion_context):
            candidate = match if match.startswith(line) else line[:cut] + match
            if candidate not in result:
                result.append(candidate)
        return result

    # Клавиши.

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Отключает привязки, которым сейчас нечего делать.

        Выключенная привязка не съедает клавишу: она достается полю ввода или
        приложению. Так history и backspace не мешают общим клавишам app.py.
        """
        if action == "history_prev":
            return bool(self._history) or self._search_saved is not None
        if action == "history_next":
            return self._history_index is not None or self._search_saved is not None
        if action == "history_search":
            # В режиме ответа на вопрос искать нечего: ответ не команда.
            return bool(self._history) and not self._awaiting
        if action == "cancel_search":
            return self._search_saved is not None
        if action == "pop_line":
            return bool(self._pending) and not self._current_value()
        return True

    def action_complete(self) -> None:
        """Tab: дополняет строку по реестру команд и контексту беседы.

        В режиме поиска не делает ничего: поле там - строка запроса, и дополнять
        ее командой незачем. Привязка при этом остается включенной, иначе Tab
        ушел бы приложению и увел фокус из строки ввода.
        """
        if self._search_saved is not None:
            return
        line = self._current_value()
        candidates = self.completion_candidates(line)
        if not candidates:
            self._set_tab_hint(line, Text(_("no completions"), style=STYLE_HINT))
            return
        if len(candidates) == 1:
            completed = candidates[0]
            if not completed.endswith(" "):
                completed += " "
            self._replace_line(completed)
            self._set_tab_hint(completed, None)
            return
        common = _common_prefix(candidates)
        if len(common) > len(line):
            self._replace_line(common)
            line = common
        self._set_tab_hint(line, self._variants_text(candidates))

    def action_history_prev(self) -> None:
        """Стрелка вверх: предыдущая строка истории.

        Во время поиска стрелка принимает найденное и выходит из режима. Одно
        нажатие - одно действие: листать дальше человек будет следующим.
        """
        if self._accept_search():
            return
        if not self._history:
            return
        if self._history_index is None:
            self._stash = self._compose_line()
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self._set_line(self._history[self._history_index])

    def action_history_next(self) -> None:
        """Стрелка вниз: следующая строка истории, в конце - возврат черновика."""
        if self._accept_search():
            return
        if self._history_index is None:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._set_line(self._history[self._history_index])
            return
        self._history_index = None
        restored = self._stash
        self._stash = ""
        self._set_line(restored)

    # Обратный поиск по истории.

    def action_history_search(self) -> None:
        """Ctrl+R: включить обратный поиск, повторное нажатие - совпадение глубже."""
        if self._search_saved is not None:
            self._step_search()
            return
        if not self._history:
            return
        # Поле ввода на время поиска становится строкой запроса, поэтому набранное
        # откладывается целиком, вместе с накопленными строками multiline.
        self._search_saved = self._compose_line()
        self._search_match = len(self._history)
        self._search_exhausted = False
        self._pending = []
        self._history_index = None
        self._stash = ""
        # Inline-подсказка дополнения на время поиска выключается: в поле стоит
        # запрос, и серый хвост с продолжением команды к нему не относится.
        self._input.suggester = None
        self._replace_line("")

    def action_cancel_search(self) -> None:
        """Ctrl+G: отменить поиск и вернуть текст, который был до него."""
        self.cancel_search()

    def cancel_search(self) -> bool:
        """Отменить поиск. Ложь означает, что поиска и не было.

        Метод публичный, потому что Escape до виджета не доходит: приложение
        объявляет его с priority, и отмену зовет оттуда.
        """
        if self._search_saved is None:
            return False
        self._set_line(self._end_search())
        return True

    @property
    def searching(self) -> bool:
        """Идет ли сейчас обратный поиск по истории."""
        return self._search_saved is not None

    def _accept_search(self) -> bool:
        """Принять найденную строку в поле. Ложь означает, что поиска не было.

        Отправки нет: в реестре есть ``/quit``, ``/send`` и ``/remove``, и
        отправлять вслепую найденное подстрокой слишком дорого.
        Когда совпадения нет, в поле возвращается текст, набранный до поиска.
        """
        if self._search_saved is None:
            return False
        found = self._history[self._search_match] if self._search_match >= 0 else ""
        saved = self._end_search()
        self._set_line(found or saved)
        return True

    def _end_search(self) -> str:
        """Закончить поиск и вернуть текст, отложенный при входе в него."""
        saved = self._search_saved or ""
        self._search_saved = None
        self._search_match = -1
        self._search_exhausted = False
        self._input.suggester = self._suggester
        return saved

    def _step_search(self) -> None:
        """Перейти к следующему совпадению вглубь истории."""
        start = self._search_match if self._search_match >= 0 else len(self._history)
        found = self._find_match(self._current_value(), start)
        if found < 0:
            # Глубже совпадений нет: остаемся на текущем и говорим об этом.
            # По кругу поиск не ходит: возврат к самой свежей строке после
            # последней старой выглядит как потеря места.
            self._search_exhausted = True
        else:
            self._search_match = found
            self._search_exhausted = False
        self._update_hint()

    def _find_match(self, query: str, start: int) -> int:
        """Индекс ближайшего совпадения строго ниже ``start``, или -1.

        Поиск подстрокой без учета регистра, от свежих записей к старым.
        """
        needle = query.lower()
        if not needle.strip():
            return -1
        for index in range(min(start, len(self._history)) - 1, -1, -1):
            if needle in self._history[index].lower():
                return index
        return -1

    def action_newline(self) -> None:
        """Alt+Enter и Ctrl+J: закрывают текущую строку и начинают новую."""
        if len(self._pending) >= PENDING_LIMIT:
            self._set_tab_hint(self._current_value(), Text(_("line limit"), style=STYLE_UNKNOWN))
            return
        self._pending.append(self._current_value())
        self._history_index = None
        self._replace_line("")

    def action_pop_line(self) -> None:
        """Backspace в начале пустого поля: возвращает последнюю строку на правку."""
        if not self._pending:
            return
        self._replace_line(self._pending.pop())

    # Состояние набора.

    def _note_typing(self, typing: bool) -> None:
        """Сообщить о наборе и завести таймер паузы.

        Состояние отправляется собеседнику, то есть это трафик: без ограничения
        оно уходило бы на каждое нажатие. Повторное ``composing`` не шлется, а
        ``paused`` уходит один раз по таймеру молчания.
        """
        if self._pause_timer is not None:
            self._pause_timer.stop()
            self._pause_timer = None
        if not typing:
            if self._typing:
                self._typing = False
                self.post_message(self.Typing(self, active=False))
            return
        if not self._typing:
            self._typing = True
            self.post_message(self.Typing(self, active=True))
        self._pause_timer = self.set_timer(TYPING_PAUSE, self._on_typing_pause)

    def _on_typing_pause(self) -> None:
        """Молчание затянулось: собеседнику пора сказать, что набор остановлен."""
        self._pause_timer = None
        if self._typing:
            self._typing = False
            self.post_message(self.Typing(self, active=False))

    # Сообщения поля ввода.

    @on(Input.Changed, "#prompt-input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        """Строка изменилась: снимаем подсказку Tab и выходим из истории.

        Правка руками означает, что листание истории закончилось: следующая
        стрелка вверх должна сохранить текущий текст как незавершенный ввод.
        """
        event.stop()
        if self._search_saved is not None:
            # В режиме поиска поле - это строка запроса. Совпадение ищется заново
            # от самых свежих записей: иначе результат зависел бы от того,
            # сколько раз до правки запроса нажали Ctrl+R.
            self._search_match = self._find_match(event.value, len(self._history))
            self._search_exhausted = False
            self._update_hint()
            return
        if event.value != self._tab_line:
            self._tab_hint = None
            self._tab_line = ""
        if event.value != self._synced_value:
            self._synced_value = event.value
            self._history_index = None
        self._update_hint()
        self._note_typing(bool(event.value))

    @on(Input.Submitted, "#prompt-input")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter: собирает накопленные строки и отдает результат наружу.

        В режиме поиска Enter не отправляет ничего: он принимает найденную
        строку в поле ввода и закрывает поиск.
        """
        event.stop()
        if self._accept_search():
            return
        line = self._compose_line()
        if not line.strip():
            self._reset_line()
            return
        self._remember(line)
        self._reset_line()
        self._drafts.pop(self._draft_key(self._conversation), None)
        self.post_message(self.Submitted(self, line))

    @on(PromptField.Pasted, "#prompt-input")
    def _on_pasted(self, event: PromptField.Pasted) -> None:
        """Многострочная вставка: строки ложатся в буфер, последняя - в поле."""
        event.stop()
        if self._awaiting or self._search_saved is not None:
            # Ответ на вопрос и строка поиска однострочные по смыслу: там берется
            # первая строка, как это делает сам Input.
            self._input.insert_text_at_cursor(event.text.split("\n", 1)[0])
            return
        self._insert_lines(event.text)

    def _insert_lines(self, text: str) -> None:
        """Вставить текст с переводами строк в позицию курсора.

        Разложение то же, что у Alt+Enter: завершенные строки уходят в буфер,
        последняя остается в поле. Курсор встает после вставленного, чтобы текст,
        который был справа от него, остался справа.
        """
        value = self._current_value()
        cursor = (
            min(self._input.cursor_position, len(value)) if self._input.is_mounted else len(value)
        )
        tail = value[cursor:]
        lines = (value[:cursor] + text + tail).split("\n")
        pending = [*self._pending, *lines[:-1]]
        self._history_index = None
        if len(pending) > PENDING_LIMIT:
            # Лишние строки отбрасываются, а не режут буфер молча: человек должен
            # увидеть, что вставилось не все.
            self._pending = pending[:PENDING_LIMIT]
            self._replace_line("")
            limit = _("line limit: {limit}").format(limit=PENDING_LIMIT)
            self._set_tab_hint("", Text(limit, style=STYLE_UNKNOWN))
            return
        self._pending = pending
        self._replace_line(lines[-1])
        if tail and self._input.is_mounted:
            self._input.cursor_position = len(lines[-1]) - len(tail)

    # Внутреннее состояние.

    def _current_value(self) -> str:
        """Текст последней строки: из поля ввода или из отложенного значения."""
        return self._input.value if self._deferred_line is None else self._deferred_line

    def _compose_line(self) -> str:
        """Накопленные строки и текущее поле, склеенные переводом строки."""
        return "\n".join([*self._pending, self._current_value()])

    def _replace_line(self, value: str) -> None:
        """Меняет содержимое поля ввода, курсор уходит в конец."""
        self._synced_value = value
        if not self._input.is_mounted:
            self._deferred_line = value
            return
        self._input.value = value
        self._input.cursor_position = len(value)
        self._update_hint()

    def _set_line(self, text: str) -> None:
        """Раскладывает многострочный текст на буфер и поле ввода."""
        parts = text.split("\n")
        self._pending = parts[:-1]
        self._replace_line(parts[-1])

    def _reset_line(self) -> None:
        """Полностью очищает ввод, буфер строк, поиск и навигацию по истории."""
        self._pending = []
        self._history_index = None
        self._stash = ""
        self._end_search()
        self._tab_hint = None
        self._tab_line = ""
        self._replace_line("")

    def _remember(self, line: str) -> None:
        """Кладет строку в историю сеанса и отдает ее наружу для записи.

        Повтор подряд не записывается. Ответ на вопрос подтверждения не
        записывается вовсе: это реакция на диалог, а не команда, и в истории
        ей не место - ни в сеансовой, ни в общем файле.
        """
        if self._awaiting:
            return
        if not self._history or self._history[-1] != line:
            self._history.append(line)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]
        if self._record is not None:
            self._record(line)

    def _draft_key(self, conversation: str | None) -> str:
        """Ключ черновика по беседе."""
        return conversation if conversation is not None else DRAFT_KEY_NONE

    def _store_draft(self) -> None:
        """Сохраняет незавершенный ввод текущей беседы."""
        key = self._draft_key(self._conversation)
        text = self._compose_line()
        if text.strip():
            self._drafts[key] = text
        else:
            self._drafts.pop(key, None)

    def _restore_draft(self) -> None:
        """Подставляет черновик новой беседы и сбрасывает навигацию по истории."""
        self._history_index = None
        self._stash = ""
        self._tab_hint = None
        self._tab_line = ""
        self._set_line(self._drafts.get(self._draft_key(self._conversation), ""))

    # Подсказка.

    def _set_tab_hint(self, line: str, hint: Text | None) -> None:
        """Запоминает подсказку Tab и строку, к которой она относится."""
        self._tab_hint = hint
        self._tab_line = line
        self._update_hint()

    def _variants_text(self, candidates: Sequence[str]) -> Text:
        """Компактный список вариантов дополнения.

        Отдельного попапа нет: варианты печатаются в узел ``#prompt-hint``, который
        и так отведен под подсказку. Это не добавляет виджетов, не требует публикации
        событий из UI вниз по слоям и не перекрывает чат.
        """
        shown = [candidate.rsplit(" ", 1)[-1] or candidate for candidate in candidates]
        text = Text(_("completions: "), style=STYLE_HINT)
        text.append("  ".join(shown[:VARIANTS_SHOWN]), style=STYLE_SUBCOMMAND)
        hidden = len(shown) - VARIANTS_SHOWN
        if hidden > 0:
            text.append(_("  ({count} more)").format(count=hidden), style=STYLE_HINT)
        return text

    def _hint_text(self) -> Text:
        """Собирает текст подсказки: варианты Tab, счетчик строк, сигнатура команды."""
        if self._awaiting:
            return Text(self._awaiting, style=STYLE_UNKNOWN)
        if self._search_saved is not None:
            return self._search_text()
        if self._tab_hint is not None:
            return self._tab_hint
        text = Text()
        if self._pending:
            lines = _("lines: {count}  ").format(count=len(self._pending) + 1)
            text.append(lines, style=STYLE_HINT)
        value = self._current_value()
        if value.startswith("/") and not value.startswith("//"):
            tokens = value[1:].split()
            name = tokens[0] if tokens else ""
            spec = commands.find(name) if name else None
            if spec is not None:
                text.append(_(spec.usage), style=STYLE_COMMAND)
                text.append("  ")
                text.append(_(spec.summary), style=STYLE_HINT)
            elif name:
                unknown = _("unknown command: /{name}").format(name=name)
                text.append(unknown, style=STYLE_UNKNOWN)
        return text

    def _search_text(self) -> Text:
        """Строка подсказки в режиме обратного поиска: запрос стоит в самом поле."""
        if not self._current_value().strip():
            return Text(_(SEARCH_PREFIX), style=STYLE_HINT)
        if not 0 <= self._search_match < len(self._history):
            return Text(_(SEARCH_EMPTY), style=STYLE_UNKNOWN)
        text = Text(_(SEARCH_PREFIX), style=STYLE_HINT)
        text.append(self._history[self._search_match], style=STYLE_COMMAND)
        if self._search_exhausted:
            text.append(_(SEARCH_EXHAUSTED), style=STYLE_HINT)
        return text

    def _update_hint(self) -> None:
        """Перерисовывает строку подсказки.

        Строка занимает место всегда, даже пустая: иначе высота панелей меняется
        на втором символе команды, и автопрокрутка ленты срывается.
        """
        self._hint.update(self._hint_text())
