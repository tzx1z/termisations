"""Строка ввода: история прошлых запусков, запись наружу и поиск по Ctrl+R.

Проверяется поведение, видимое пользователю: что стоит в поле после нажатия, что
написано в подсказке под ним и что ушло наружу сообщением. Виджет монтируется в
приложение-обертку, настоящий терминал не нужен.
"""

from collections.abc import Sequence
from typing import Final

import pytest
from textual import events, on
from textual.app import App, ComposeResult
from textual.geometry import Region
from textual.pilot import Pilot
from textual.selection import Selection
from textual.widgets import Input, Static

from termisations.app import _CYRILLIC_TWINS
from termisations.core.events import EventBus
from termisations.ui.prompt import PENDING_LIMIT, PromptInput

TEST_SIZE: Final = (100, 6)

HISTORY: Final = (
    "/roster",
    "/mam bob@example.org --limit 50",
    "/ping",
    "/mam erin@example.org",
)


class PromptHost(App[None]):
    """Обертка с одной строкой ввода и сбором отправленных строк."""

    def __init__(self, prompt: PromptInput) -> None:
        super().__init__()
        self.prompt = prompt
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield self.prompt

    @on(PromptInput.Submitted)
    def _collect(self, event: PromptInput.Submitted) -> None:
        event.stop()
        self.submitted.append(event.line)


@pytest.fixture
def recorded() -> list[str]:
    """Строки, которые виджет отдал наружу для записи в общую историю."""
    return []


def make_prompt(
    bus: EventBus, recorded: list[str], history: Sequence[str] = HISTORY
) -> PromptInput:
    """Строка ввода с готовой историей и колбэком записи."""
    return PromptInput(bus, id="prompt", history=history, record=recorded.append)


def field(host: PromptHost) -> Input:
    """Само поле ввода внутри виджета."""
    return host.query_one("#prompt-input", Input)


def hint(host: PromptHost) -> str:
    """Текст подсказки под полем в том виде, в котором он на экране."""
    node = host.query_one("#prompt-hint", Static)
    size = node.size
    if not size.width or not size.height:
        return ""
    strips = node.render_lines(Region(0, 0, size.width, size.height))
    return "\n".join(strip.text for strip in strips).strip()


async def settle(pilot: Pilot[None]) -> None:
    """Дать виджету отрисоваться."""
    await pilot.pause()
    await pilot.pause()


async def test_history_of_past_runs_is_available(bus: EventBus, recorded: list[str]) -> None:
    """Стрелка вверх поднимает команду, набранную в прошлый запуск."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press("up")
        assert field(host).value == "/mam erin@example.org"
        await pilot.press("up")
        assert field(host).value == "/ping"


async def test_entered_line_goes_out_for_recording(bus: EventBus, recorded: list[str]) -> None:
    """Введенная строка уходит наружу для записи и отправляется дальше."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press(*"/help")
        await pilot.press("enter")
        await settle(pilot)

        assert recorded == ["/help"]
        assert host.submitted == ["/help"]


async def test_answer_to_a_question_is_not_remembered(bus: EventBus, recorded: list[str]) -> None:
    """Ответ на вопрос подтверждения не попадает ни в историю, ни наружу."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        host.prompt.set_awaiting("введите yes для подтверждения")
        await pilot.press(*"yes")
        await pilot.press("enter")
        await settle(pilot)

        assert recorded == []
        assert host.submitted == ["yes"], "ответ обязан дойти до приложения"
        # История осталась прежней: стрелка вверх поднимает команду, а не ответ.
        await pilot.press("up")
        assert field(host).value == "/mam erin@example.org"


async def test_search_finds_by_substring(bus: EventBus, recorded: list[str]) -> None:
    """Ctrl+R ищет подстрокой и показывает найденное в подсказке."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press("ctrl+r")
        assert host.prompt.searching
        await pilot.press(*"mam")
        await settle(pilot)

        assert "/mam erin@example.org" in hint(host)


async def test_repeated_search_goes_deeper(bus: EventBus, recorded: list[str]) -> None:
    """Повторный Ctrl+R переходит к следующему совпадению вглубь истории."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press("ctrl+r")
        await pilot.press(*"mam")
        await settle(pilot)
        assert "/mam erin@example.org" in hint(host)

        await pilot.press("ctrl+r")
        await settle(pilot)
        assert "/mam bob@example.org --limit 50" in hint(host)

        # Глубже совпадений нет: поиск остается на месте и говорит об этом.
        await pilot.press("ctrl+r")
        await settle(pilot)
        assert "/mam bob@example.org --limit 50" in hint(host)
        assert "дальше нет" in hint(host)


async def test_search_without_matches_says_so(bus: EventBus, recorded: list[str]) -> None:
    """Запрос без совпадений не подставляет ничего и сообщает об этом."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press("ctrl+r")
        await pilot.press(*"zzz")
        await settle(pilot)

        assert "нет совпадений" in hint(host)


async def test_enter_accepts_without_sending(bus: EventBus, recorded: list[str]) -> None:
    """Enter в поиске кладет найденное в поле и ничего не отправляет."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press("ctrl+r")
        await pilot.press(*"ping")
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)

        assert field(host).value == "/ping"
        assert host.submitted == [], "поиск отправил команду вместо подстановки"
        assert recorded == []
        assert not host.prompt.searching


async def test_cancel_restores_the_typed_text(bus: EventBus, recorded: list[str]) -> None:
    """Ctrl+G отменяет поиск и возвращает текст, набранный до него."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press(*"черновик")
        await pilot.press("ctrl+r")
        await pilot.press(*"ping")
        await settle(pilot)
        await pilot.press("ctrl+g")
        await settle(pilot)

        assert field(host).value == "черновик"
        assert not host.prompt.searching


async def test_cancel_search_is_available_to_the_app(bus: EventBus, recorded: list[str]) -> None:
    """Приложение отменяет поиск само: Escape до виджета не доходит."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        assert host.prompt.cancel_search() is False
        await pilot.press("ctrl+r")
        await pilot.press(*"ping")
        await settle(pilot)

        assert host.prompt.cancel_search() is True
        assert not host.prompt.searching
        assert field(host).value == ""


async def test_arrow_accepts_the_match(bus: EventBus, recorded: list[str]) -> None:
    """Стрелка в режиме поиска принимает найденное и выходит из режима."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press("ctrl+r")
        await pilot.press(*"roster")
        await settle(pilot)
        await pilot.press("up")
        await settle(pilot)

        assert field(host).value == "/roster"
        assert not host.prompt.searching


async def test_tab_does_not_complete_in_search(bus: EventBus, recorded: list[str]) -> None:
    """Tab в поиске не дополняет запрос и не уводит фокус из строки ввода."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press("ctrl+r")
        await pilot.press(*"/pi")
        await settle(pilot)
        await pilot.press("tab")
        await settle(pilot)

        assert field(host).value == "/pi", "Tab дополнил строку запроса"
        assert host.prompt.searching
        assert host.focused is field(host), "Tab увел фокус из строки ввода"


async def test_search_needs_history(bus: EventBus, recorded: list[str]) -> None:
    """Без истории Ctrl+R ничего не включает: искать нечего."""
    host = PromptHost(make_prompt(bus, recorded, history=()))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press("ctrl+r")
        assert not host.prompt.searching


def test_every_ctrl_binding_has_a_cyrillic_twin() -> None:
    """У сочетаний строки ввода есть кириллические двойники.

    Двойники тут объявлены руками, а не через ``app.with_cyrillic``: та функция
    живет в ``app.py``, а он импортирует этот модуль. Проверка структурная -
    пропущенный двойник обнаружил бы пользователь на русской раскладке.
    """
    keys = {binding.key for binding in PromptInput.BINDINGS if hasattr(binding, "key")}
    latin = {key for key in keys if key.startswith("ctrl+") and key[-1].isascii()}
    for key in latin:
        prefix, _, letter = key.rpartition("+")
        twin = _CYRILLIC_TWINS.get(letter)
        assert twin is not None, f"для {key} нет буквы в карте двойников"
        assert f"{prefix}+{twin}" in keys, f"у {key} нет двойника {prefix}+{twin}"


async def paste(host: PromptHost, text: str) -> None:
    """Вставка из буфера: событие приходит полю ввода, как от терминала."""
    field(host).post_message(events.Paste(text))


async def test_paste_keeps_all_lines(bus: EventBus, recorded: list[str]) -> None:
    """Многострочная вставка не теряет строки: уходит весь текст, а не первая строка."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await paste(host, "первая\nвторая\nтретья")
        await settle(pilot)

        assert field(host).value == "третья", "в поле должна остаться последняя строка"
        assert "строк: 3" in hint(host)

        await pilot.press("enter")
        await settle(pilot)
        assert host.submitted == ["первая\nвторая\nтретья"]


async def test_paste_respects_the_cursor(bus: EventBus, recorded: list[str]) -> None:
    """Вставка идет в позицию курсора, текст справа от него остается справа."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press(*"АБ")
        field(host).cursor_position = 1
        await paste(host, "один\nдва")
        await settle(pilot)

        assert field(host).value == "дваБ"
        assert field(host).cursor_position == 3, "курсор встал не после вставленного"
        await pilot.press("enter")
        await settle(pilot)
        assert host.submitted == ["Аодин\nдваБ"]


async def test_paste_of_one_line_stays_inline(bus: EventBus, recorded: list[str]) -> None:
    """Однострочная вставка, в том числе с переводом в конце, буфер не заводит."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await paste(host, "/ping bob@example.org\n")
        await settle(pilot)

        assert field(host).value == "/ping bob@example.org"
        assert "строк:" not in hint(host)


async def test_paste_over_the_limit_is_refused(bus: EventBus, recorded: list[str]) -> None:
    """Вставка сверх предела строк не режет буфер молча, а говорит о пределе."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await paste(host, "\n".join(f"строка {number}" for number in range(PENDING_LIMIT + 5)))
        await settle(pilot)

        assert "предел строк" in hint(host)


async def test_paste_in_search_takes_the_first_line(bus: EventBus, recorded: list[str]) -> None:
    """В строке поиска вставляется одна строка: запрос многострочным не бывает."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press("ctrl+r")
        await paste(host, "ping\nвторая строка")
        await settle(pilot)

        assert field(host).value == "ping"
        assert host.prompt.searching
        assert "/ping" in hint(host)


async def test_paste_replaces_the_selection(bus: EventBus, recorded: list[str]) -> None:
    """Вставка поверх выделения заменяет выделенное, а не добавляется к нему."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press(*"АБВГ")
        field(host).selection = Selection(1, 3)
        await paste(host, "один\nдва")
        await settle(pilot)

        assert field(host).value == "дваГ"
        await pilot.press("enter")
        await settle(pilot)
        assert host.submitted == ["Аодин\nдваГ"]


async def test_paste_adds_to_the_pending_buffer(bus: EventBus, recorded: list[str]) -> None:
    """Вставка поверх накопленных строк продолжает буфер, а не затирает его."""
    host = PromptHost(make_prompt(bus, recorded))
    async with host.run_test(size=TEST_SIZE) as pilot:
        host.prompt.focus_input()
        await settle(pilot)

        await pilot.press(*"начало")
        await pilot.press("ctrl+j")
        await paste(host, "один\nдва")
        await settle(pilot)

        assert "строк: 3" in hint(host)
        await pilot.press("enter")
        await settle(pilot)
        assert host.submitted == ["начало\nодин\nдва"]
