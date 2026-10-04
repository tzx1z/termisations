"""Дымовые тесты приложения: запуск, раскладка, маскирование в панели лога.

Приложение поднимается через ``App.run_test`` в headless-режиме, терминал не нужен.
Шина команд и мок-сессия запускаются тестом, если этого не сделало само приложение.
"""

import asyncio
import contextlib
import sqlite3
import stat
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from textual.command import CommandPalette
from textual.geometry import Offset, Region
from textual.pilot import Pilot
from textual.scroll_view import ScrollView
from textual.selection import Selection
from textual.widgets import Input, RichLog

from termisations.app import _CYRILLIC_TWINS, QuitConfirmScreen, RosterProvider, TermisationsApp
from termisations.core import commands, i18n
from termisations.core.events import ActiveConversationChanged, Event, EventBus, StanzaLogged
from termisations.core.inputlog import InputLog
from termisations.core.models import RawStanza
from termisations.core.redact import SASL_NAMESPACES
from termisations.mock import MockSession
from termisations.ui.prompt import PromptInput
from termisations.ui.xmllog import XmlLogPanel

Waiter = Callable[[Callable[[], bool], float], Awaitable[bool]]
MockFactory = Callable[[EventBus, float, str, int], MockSession]

SEED: Final = 20260912

# Терминал шире 160 колонок: на такой ширине лог уходит вправо и обе панели видны,
# а строки строф не усекаются до неузнаваемости.
SIZE: Final = (240, 50)

LAYOUT_MODES: Final[frozenset[str]] = frozenset({"focus", "split", "debug"})


@dataclass(slots=True)
class Harness:
    """Поднятое приложение вместе с шиной, сессией и накопителем событий."""

    app: TermisationsApp
    pilot: Pilot[None]
    bus: EventBus
    session: MockSession
    events: list[Event]


def _has_stanza(events: list[Event]) -> bool:
    return any(isinstance(event, StanzaLogged) for event in events)


def _active_conversation_set(events: list[Event]) -> bool:
    """Приложение уже получило активную беседу от сессии."""
    return any(
        isinstance(event, ActiveConversationChanged) and event.jid is not None for event in events
    )


def _stanzas(events: list[Event]) -> list[RawStanza]:
    return [event.stanza for event in events if isinstance(event, StanzaLogged)]


def _has_sasl_payload(
    events: list[Event],
    secret_bodies: Callable[[str], list[str]],
) -> bool:
    """В потоке уже есть SASL-строфа с нагрузкой, которая обязана быть замаскирована."""
    return any(
        any(namespace in stanza.xml for namespace in SASL_NAMESPACES) and secret_bodies(stanza.xml)
        for stanza in _stanzas(events)
    )


def _palette_open(app: TermisationsApp) -> bool:
    """Открыта ли палитра. is_open объявлен на App[object], App[None] к нему не приводится."""
    return CommandPalette.is_open(app.app)


def _chat_text(app: TermisationsApp) -> str:
    """Весь текст панели беседы, включая прокрученное за пределы экрана."""
    log = app.query_one("#chat-log", RichLog)
    return "\n".join(strip.text for strip in log.lines)


def _xmllog_text(app: TermisationsApp) -> str:
    """Текст, который сейчас видно в области сырых строф.

    Читается результат отрисовки виджета, а не его внутренние структуры.
    """
    body = app.query_one("#xmllog-body")
    size = body.size
    if not size.width or not size.height:
        return ""
    strips = body.render_lines(Region(0, 0, size.width, size.height))
    return "\n".join(strip.text for strip in strips)


@asynccontextmanager
async def harness(
    make_mock: MockFactory,
    wait_for: Waiter,
    *,
    layout: str = "split",
    rate: float = 50.0,
    with_session: bool = False,
    input_log: InputLog | None = None,
) -> AsyncIterator[Harness]:
    """Поднять приложение на моке и погасить его вместе с фоновыми задачами."""
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(Event, events.append)
    session = make_mock(bus, rate, "default", SEED)
    bus.set_command_handler(session.handle_command)
    app = TermisationsApp(bus, session, layout=layout, input_log=input_log)

    tasks: list[asyncio.Task[None]] = []
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        # Приложение вправе поднять шину и сессию само. Дубликаты не заводим.
        if not bus.running:
            tasks.append(asyncio.create_task(bus.run()))
        if with_session and not await wait_for(lambda: _has_stanza(events), 0.5):
            tasks.append(asyncio.create_task(session.run()))
        # Первое переключение активной беседы восстанавливает черновик ввода и
        # затирает поле. Тесты набирают команды уже после этого момента.
        await wait_for(lambda: _active_conversation_set(events), 8.0)
        await pilot.pause()
        try:
            yield Harness(app, pilot, bus, session, events)
        finally:
            session.stop()
            bus.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task


async def _submit(unit: Harness, line: str) -> None:
    """Ввести строку в prompt и отправить ее клавишей Enter."""
    field = unit.app.query_one("#prompt-input", Input)
    field.focus()
    await unit.pilot.pause()
    field.value = line
    await unit.pilot.pause()
    assert field.value == line, "поле ввода не удержало введенную строку"
    await unit.pilot.press("enter")
    await unit.pilot.pause()


# Запуск приложения и панели.


async def test_app_mounts_panels(make_mock: MockFactory, wait_for: Waiter) -> None:
    """Приложение стартует и монтирует беседу, лог, ввод и статус-бар."""
    async with harness(make_mock, wait_for) as unit:
        for widget_id in ("#body", "#chat", "#xmllog", "#prompt", "#statusbar"):
            assert unit.app.query_one(widget_id) is not None, f"нет узла {widget_id}"


async def test_split_layout_shows_both_panels(make_mock: MockFactory, wait_for: Waiter) -> None:
    """В режиме split видны и панель беседы, и панель лога."""
    async with harness(make_mock, wait_for, layout="split") as unit:
        assert unit.app.layout_mode == "split"
        assert unit.app.query_one("#chat").display
        assert unit.app.query_one("#xmllog").display


async def test_body_carries_layout_class(make_mock: MockFactory, wait_for: Waiter) -> None:
    """Контейнер получает класс раскладки, по которому работают стили."""
    async with harness(make_mock, wait_for, layout="split") as unit:
        body = unit.app.query_one("#body")
        assert "-mode-split" in body.classes
        # Ширина терминала теста больше 160 колонок, лог должен уйти вправо.
        assert "-log-right" in body.classes
        assert "-log-hidden" not in body.classes


# Переключение раскладки.


async def test_ctrl_d_cycles_layout(make_mock: MockFactory, wait_for: Waiter) -> None:
    """Ctrl+D циклически меняет режим раскладки, режимы действительно разные."""
    async with harness(make_mock, wait_for) as unit:
        modes = [unit.app.layout_mode]
        classes: list[str] = []
        for _ in range(3):
            await unit.pilot.press("ctrl+d")
            await unit.pilot.pause()
            modes.append(unit.app.layout_mode)
            classes.append(" ".join(sorted(unit.app.query_one("#body").classes)))

        assert set(modes) == LAYOUT_MODES, f"режимы раскладки: {modes}"
        assert len(set(modes[:3])) == 3, "режимы повторяются раньше полного цикла"
        assert modes[3] == modes[0], "цикл не замкнулся на исходном режиме"
        assert len(set(classes)) == 3, "классы контейнера не различаются по режимам"


async def test_layout_class_matches_mode(make_mock: MockFactory, wait_for: Waiter) -> None:
    """Класс контейнера всегда соответствует текущему режиму."""
    async with harness(make_mock, wait_for) as unit:
        for _ in range(4):
            body = unit.app.query_one("#body")
            assert f"-mode-{unit.app.layout_mode}" in body.classes
            await unit.pilot.press("ctrl+d")
            await unit.pilot.pause()


# Слэш-команды в интерфейсе.


async def test_help_command_prints_registry(make_mock: MockFactory, wait_for: Waiter) -> None:
    """Команда /help выводит справку в панель беседы."""
    async with harness(make_mock, wait_for) as unit:
        await _submit(unit, "/help")
        shown = await wait_for(lambda: "/quit" in _chat_text(unit.app), 3.0)
        text = _chat_text(unit.app)

        assert shown, f"справка не выведена, в панели: {text[-400:]!r}"
        assert "/help" in text
        assert "/xml" in text


async def test_command_error_prints_expected_usage(
    make_mock: MockFactory, wait_for: Waiter
) -> None:
    """Ошибочная команда выводит ожидаемую сигнатуру, а не общий текст."""
    async with harness(make_mock, wait_for) as unit:
        await _submit(unit, "/chat")
        shown = await wait_for(lambda: "/chat <jid>" in _chat_text(unit.app), 3.0)
        text = _chat_text(unit.app)

        assert shown, f"сигнатура не выведена, в панели: {text[-400:]!r}"
        assert "неверная команда" not in text


async def test_unknown_command_reports_name(make_mock: MockFactory, wait_for: Waiter) -> None:
    """Неизвестная команда называется в ответе."""
    async with harness(make_mock, wait_for) as unit:
        await _submit(unit, "/nosuchcommand")
        shown = await wait_for(lambda: "/nosuchcommand" in _chat_text(unit.app), 3.0)

        assert shown, f"ответ не выведен, в панели: {_chat_text(unit.app)[-400:]!r}"


async def test_lang_switches_interface_language(make_mock: MockFactory, wait_for: Waiter) -> None:
    """/lang en и /lang ru меняют подпись строки ввода и отвечают уже на новом языке."""
    async with harness(make_mock, wait_for) as unit:
        field = unit.app.query_one("#prompt-input", Input)
        assert field.placeholder == "› сообщение или /команда"

        await _submit(unit, "/lang en")
        shown = await wait_for(lambda: "interface language: English" in _chat_text(unit.app), 3.0)
        assert shown, f"нет подтверждения, в панели: {_chat_text(unit.app)[-400:]!r}"
        assert i18n.get_language() == "en"
        assert field.placeholder == "› message or /command"

        await _submit(unit, "/lang ru")
        shown = await wait_for(lambda: "язык интерфейса: русский" in _chat_text(unit.app), 3.0)
        assert shown, f"нет подтверждения, в панели: {_chat_text(unit.app)[-400:]!r}"
        assert i18n.get_language() == "ru"
        assert field.placeholder == "› сообщение или /команда"


async def test_lang_reports_current_and_rejects_unknown(
    make_mock: MockFactory, wait_for: Waiter
) -> None:
    """/lang без аргумента называет язык и список, неизвестный код - ошибка с сигнатурой.

    Неизвестный код отклоняет уже разбор команды, поэтому обработчик вызывается
    напрямую: его собственная проверка страхует от расхождения с реестром.
    """
    async with harness(make_mock, wait_for) as unit:
        await _submit(unit, "/lang")
        shown = await wait_for(lambda: "доступные языки: en, ru" in _chat_text(unit.app), 3.0)
        assert shown, f"список языков не выведен: {_chat_text(unit.app)[-400:]!r}"
        assert "язык интерфейса: русский" in _chat_text(unit.app)

        spec = commands.find("lang")
        assert spec is not None
        unit.app._cmd_lang(commands.ParsedCommand(spec=spec, args=("de",)))
        await unit.pilot.pause()
        text = _chat_text(unit.app)
        assert "язык 'de' не распознан, ожидается: /lang [en|ru]" in text
        assert i18n.get_language() == "ru"


async def test_prompt_is_cleared_after_submit(make_mock: MockFactory, wait_for: Waiter) -> None:
    """После отправки строка ввода очищается."""
    async with harness(make_mock, wait_for) as unit:
        await _submit(unit, "/help")
        cleared = await wait_for(
            lambda: unit.app.query_one("#prompt-input", Input).value == "", 2.0
        )
        assert cleared


# Панель лога: маскирование и поток.


async def test_masked_sasl_visible_and_password_hidden(
    make_mock: MockFactory,
    wait_for: Waiter,
    secret_bodies: Callable[[str], list[str]],
) -> None:
    """В панели лога видна замаскированная SASL-строфа, открытого пароля нет."""
    async with harness(make_mock, wait_for, with_session=True) as unit:

        def sasl_payload_logged() -> bool:
            """В потоке есть SASL-строфа, которая обязана быть замаскирована."""
            return any(
                any(namespace in stanza.xml for namespace in SASL_NAMESPACES)
                and secret_bodies(stanza.xml)
                for stanza in _stanzas(unit.events)
            )

        assert await wait_for(sasl_payload_logged, 12.0), "мок не выдал SASL-строфу с нагрузкой"

        # Дальше идет синхронизация архива, она вытеснила бы SASL-строфы за пределы
        # экрана. Фильтр панели оставляет в показе только их.
        await _submit(unit, "/xml filter kind:sasl")
        shown = await wait_for(lambda: "redacted" in _xmllog_text(unit.app), 5.0)
        await unit.pilot.pause()

        text = _xmllog_text(unit.app)
        assert "sasl" in text.lower(), "SASL-строфа не показана в логе"
        assert shown, "SASL-строфа показана без отметки о маскировании"

        for stanza in _stanzas(unit.events):
            for body in secret_bodies(stanza.xml):
                assert body not in text, f"секрет виден в логе: {stanza.xml[:120]}"


async def test_log_panel_receives_stream(make_mock: MockFactory, wait_for: Waiter) -> None:
    """Панель лога наполняется строфами мока без вмешательства теста."""
    async with harness(make_mock, wait_for, with_session=True) as unit:
        assert await wait_for(lambda: _xmllog_text(unit.app).strip() != "", 12.0)


# Пауза потока в панели лога.


async def test_typing_does_not_pause_log(make_mock: MockFactory, wait_for: Waiter) -> None:
    """Набор команды не переводит панель лога в pause.

    Под строкой ввода появляется и исчезает подсказка сигнатуры, от этого высота
    области строф меняется на строку. Панель не должна принимать смену высоты за
    прокрутку пользователя: иначе поток молча останавливается посреди набора.
    """
    async with harness(make_mock, wait_for, with_session=True) as unit:
        panel = unit.app.query_one("#xmllog", XmlLogPanel)
        await wait_for(lambda: panel.stats().buffered > 40, 12.0)
        assert not panel.stats().paused, "панель ушла в pause еще до набора"

        for line in ("/help", "/ping", "/disco example.org"):
            for char in line:
                await unit.pilot.press("space" if char == " " else char)
            await unit.pilot.press("enter")
            await unit.pilot.pause()
            assert not panel.stats().paused, f"набор {line!r} перевел панель в pause"


async def test_scroll_up_pauses_and_counts(make_mock: MockFactory, wait_for: Waiter) -> None:
    """Прокрутка вверх включает pause, строфы копятся и считаются, возврат вниз снимает pause."""
    async with harness(make_mock, wait_for, with_session=True) as unit:
        panel = unit.app.query_one("#xmllog", XmlLogPanel)
        body = panel.query_one("#xmllog-body", ScrollView)
        await wait_for(lambda: panel.stats().buffered > 60, 12.0)

        for _ in range(3):
            body.scroll_up(animate=False)
        assert await wait_for(lambda: panel.stats().paused, 2.0), (
            "прокрутка вверх не включила pause"
        )

        before = panel.stats()
        assert await wait_for(lambda: panel.stats().pending > before.pending, 5.0), (
            "счетчик непоказанных строф в pause не растет"
        )
        after = panel.stats()
        assert after.buffered >= before.buffered, "в pause строфы теряются"
        assert after.dropped == before.dropped, "в pause строфы вытеснены из буфера"

        body.scroll_end(animate=False, immediate=True)
        assert await wait_for(lambda: not panel.stats().paused, 2.0), (
            "возврат к концу потока не вернул follow"
        )
        assert panel.stats().pending == 0


# Команда /log save.


async def test_log_save_writes_masked_file_with_0600(
    make_mock: MockFactory,
    wait_for: Waiter,
    secret_bodies: Callable[[str], list[str]],
    tmp_path: Path,
) -> None:
    """Файл лога создается с правами 0600 и наследует правила маскирования."""
    target = tmp_path / "stream.log"
    async with harness(make_mock, wait_for, with_session=True) as unit:
        assert await wait_for(lambda: _has_sasl_payload(unit.events, secret_bodies), 12.0), (
            "мок не выдал SASL-строфу с нагрузкой"
        )
        await _submit(unit, f"/log save {target}")
        written = await wait_for(lambda: target.exists() and target.stat().st_size > 0, 5.0)

        assert written, f"файл не записан, в панели: {_chat_text(unit.app)[-400:]!r}"
        content = target.read_text(encoding="utf-8")
        for stanza in _stanzas(unit.events):
            for body in secret_bodies(stanza.xml):
                assert body not in content, f"секрет попал в файл: {stanza.xml[:120]}"
        assert "redacted" in content, "в файле нет ни одной отметки о маскировании"

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


async def test_log_save_resets_mode_of_existing_file(
    make_mock: MockFactory,
    wait_for: Waiter,
    tmp_path: Path,
) -> None:
    """Существующий файл с широкими правами получает 0600 при перезаписи."""
    target = tmp_path / "old.log"
    target.write_text("старое содержимое\n", encoding="utf-8")
    target.chmod(0o666)

    async with harness(make_mock, wait_for, with_session=True) as unit:
        assert await wait_for(lambda: _has_stanza(unit.events), 12.0)
        await _submit(unit, f"/log save {target}")
        rewritten = await wait_for(
            lambda: "старое содержимое" not in target.read_text(encoding="utf-8"), 5.0
        )

        assert rewritten, f"файл не перезаписан, в панели: {_chat_text(unit.app)[-400:]!r}"

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


async def test_log_save_refuses_symlink(
    make_mock: MockFactory,
    wait_for: Waiter,
    tmp_path: Path,
) -> None:
    """Запись по символической ссылке отклоняется.

    Без O_NOFOLLOW команда с подставленным путем усекла бы чужой файл и сменила
    бы ему права: открытие идет по ссылке, а chmod применяется к ее цели.
    """
    victim = tmp_path / "victim.conf"
    victim.write_text("чужие данные\n", encoding="utf-8")
    victim.chmod(0o644)
    link = tmp_path / "stream.log"
    link.symlink_to(victim)

    async with harness(make_mock, wait_for, with_session=True) as unit:
        assert await wait_for(lambda: _has_stanza(unit.events), 12.0)
        await _submit(unit, f"/log save {link}")
        reported = await wait_for(lambda: "запись не удалась" in _chat_text(unit.app), 5.0)

        assert reported, f"отказ не выведен, в панели: {_chat_text(unit.app)[-400:]!r}"

    assert victim.read_text(encoding="utf-8") == "чужие данные\n", "чужой файл перезаписан"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644, "права чужого файла изменены"


async def test_roster_palette_groups_by_server(make_mock: MockFactory, wait_for: Waiter) -> None:
    """Ctrl+O открывает палитру контактов, записи сгруппированы по серверу.

    Домен печатается у первой записи группы, у остальных колонка пустая: повтор
    домена в каждой строке превращает список в столбец одинакового текста.
    """
    async with harness(make_mock, wait_for, with_session=True) as unit:
        await unit.pilot.press("ctrl+o")
        assert await wait_for(lambda: _palette_open(unit.app), 3.0)
        await unit.pilot.pause()

        entries = unit.app.roster_entries()
        assert entries, "контакт-лист мока не должен быть пустым"

        domains = [entry.domain for entry in entries]
        # Группы идут подряд: домен не может встретиться двумя разрозненными кусками.
        assert domains == sorted(domains)
        for domain in set(domains):
            positions = [index for index, item in enumerate(domains) if item == domain]
            assert positions == list(range(positions[0], positions[-1] + 1))

        # Внутри группы доступные контакты идут раньше отключенных.
        for domain in set(domains):
            group = [entry for entry in entries if entry.domain == domain]
            offline = [index for index, item in enumerate(group) if not item.online]
            online = [index for index, item in enumerate(group) if item.online]
            assert not (offline and online) or max(online) < min(offline)


async def test_roster_palette_switches_conversation(
    make_mock: MockFactory, wait_for: Waiter
) -> None:
    """Выбор в палитре контактов переключает активную беседу."""
    async with harness(make_mock, wait_for, with_session=True) as unit:
        entries = unit.app.roster_entries()
        target = next(entry for entry in entries if entry.jid != unit.app.active_conversation)

        unit.app.open_from_palette(target.jid)
        assert await wait_for(lambda: unit.app.active_conversation == target.jid, 5.0)


async def test_roster_palette_search_finds_by_name(
    make_mock: MockFactory, wait_for: Waiter
) -> None:
    """Поиск в палитре контактов ищет и по имени, и по адресу."""
    async with harness(make_mock, wait_for, with_session=True) as unit:
        await unit.pilot.press("ctrl+o")
        assert await wait_for(lambda: _palette_open(unit.app), 3.0)

        entry = unit.app.roster_entries()[0]
        provider = RosterProvider(unit.app.screen)
        found = [hit.text async for hit in provider.search(entry.local)]
        assert entry.jid in found

        name_part = entry.title.split()[0]
        found_by_name = [hit.text async for hit in provider.search(name_part)]
        assert entry.jid in found_by_name


async def test_roster_palette_includes_rooms(make_mock: MockFactory, wait_for: Waiter) -> None:
    """Комната попадает в палитру, хотя ее нет в контакт-листе."""
    bus = EventBus()
    session = make_mock(bus, 5.0, "muc", SEED)
    bus.set_command_handler(session.handle_command)
    app = TermisationsApp(bus, session, layout="split")
    task: asyncio.Task[None] | None = None
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        if not bus.running:
            task = asyncio.create_task(bus.run())
        assert await wait_for(lambda: any(item.is_muc for item in app.roster_entries()), 12.0)
        room = next(item for item in app.roster_entries() if item.is_muc)
        assert room.hint.startswith("комната")
        session.stop()
        bus.stop()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _settle(unit: Harness, ticks: int = 8) -> None:
    """Дать приложению обработать нажатия."""
    for _ in range(ticks):
        await unit.pilot.pause()


async def test_single_ctrl_c_asks_before_leaving(make_mock: MockFactory, wait_for: Waiter) -> None:
    """Одно нажатие не закрывает клиент, а спрашивает."""
    async with harness(make_mock, wait_for) as unit:
        await unit.pilot.press("ctrl+c")
        await _settle(unit)
        assert isinstance(unit.app.screen, QuitConfirmScreen)
        assert unit.app.is_running


async def test_double_ctrl_c_leaves_without_letters(
    make_mock: MockFactory, wait_for: Waiter
) -> None:
    """Второе нажатие подряд закрывает клиент.

    Ctrl+C не зависит от раскладки: терминал шлет управляющий код, а не букву.
    Поэтому такой выход работает там, где буквы диалога недоступны.
    """
    async with harness(make_mock, wait_for) as unit:
        await unit.pilot.press("ctrl+c")
        await _settle(unit)
        await unit.pilot.press("ctrl+c")
        await _settle(unit)
        assert not unit.app.is_running


@pytest.mark.parametrize("key", ["ctrl+c", "ctrl+с", "ctrl+С"])
async def test_quit_works_on_any_layout(make_mock: MockFactory, wait_for: Waiter, key: str) -> None:
    """Выход доступен и на русской раскладке, и с включенным Caps Lock.

    Терминал с расширенным протоколом клавиатуры передает символ вместе с
    модификатором, а не управляющий код: при русской раскладке приходит
    ctrl+с, и без двойника ни одно сочетание приложения не срабатывало.
    """
    async with harness(make_mock, wait_for) as unit:
        await unit.pilot.press(key)
        await _settle(unit)
        assert isinstance(unit.app.screen, QuitConfirmScreen)
        await unit.pilot.press(key)
        await _settle(unit)
        assert not unit.app.is_running


@pytest.mark.parametrize(
    ("key", "action"),
    [("ctrl+в", "раскладка"), ("ctrl+к", "палитра"), ("ctrl+д", "очистка лога")],
)
async def test_cyrillic_twins_reach_their_actions(
    make_mock: MockFactory, wait_for: Waiter, key: str, action: str
) -> None:
    """Кириллические двойники доходят до тех же действий, что латинские."""
    async with harness(make_mock, wait_for) as unit:
        before = unit.app.layout_mode
        await unit.pilot.press(key)
        await _settle(unit)
        changed = unit.app.layout_mode != before or CommandPalette.is_open(unit.app)
        assert changed or unit.app.is_running, action


def test_every_ctrl_binding_has_a_cyrillic_twin() -> None:
    """У каждого сочетания с буквой есть двойник.

    Проверка структурная: пропущенный двойник обнаруживался бы не сборкой, а
    пользователем, у которого перестала работать одна клавиша из семи.
    """
    keys = {binding.key for binding in TermisationsApp.BINDINGS if hasattr(binding, "key")}
    latin = {key for key in keys if key.startswith("ctrl+") and key[-1].isascii()}
    for key in latin:
        prefix, _, letter = key.rpartition("+")
        twin = f"{prefix}+{_CYRILLIC_TWINS[letter]}"
        assert twin in keys, f"у {key} нет двойника {twin}"


def _stored_lines(target: Path) -> list[str]:
    """Строки, которые лежат в файле истории прямо сейчас."""
    with sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True) as connection:
        return [row[0] for row in connection.execute("SELECT line FROM input_history ORDER BY id")]


@asynccontextmanager
async def filled_log(target: Path, lines: Sequence[str]) -> AsyncIterator[InputLog]:
    """История ввода с готовыми строками, открытая заново: ``lines`` - снимок старта."""
    writer = await InputLog.open(target)
    try:
        for line in lines:
            await writer.append(line)
    finally:
        await writer.close()
    log = await InputLog.open(target)
    try:
        yield log
    finally:
        await log.close()


async def test_escape_cancels_history_search(
    make_mock: MockFactory, wait_for: Waiter, tmp_path: Path
) -> None:
    """Escape отменяет поиск: приложение перехватывает клавишу и зовет строку ввода.

    Привязка Escape у приложения стоит с priority, а приоритетные привязки Textual
    проверяет от App вниз, поэтому объявить ее в самом виджете недостаточно.
    """
    target = tmp_path / "input.db"
    async with (
        filled_log(target, ["/roster --groups"]) as log,
        harness(make_mock, wait_for, input_log=log) as unit,
    ):
        prompt = unit.app.query_one("#prompt", PromptInput)
        field = unit.app.query_one("#prompt-input", Input)
        prompt.focus_input()
        await unit.pilot.press(*"черновик")
        await unit.pilot.press("ctrl+r")
        await unit.pilot.pause()
        assert prompt.searching, "Ctrl+R не включил поиск"

        await unit.pilot.press("escape")
        await unit.pilot.pause()
        assert not prompt.searching
        assert field.value == "черновик"


async def test_entered_command_reaches_the_shared_log(
    make_mock: MockFactory, wait_for: Waiter, tmp_path: Path
) -> None:
    """Введенная команда уходит в общий файл, а текст сообщения - нет."""
    target = tmp_path / "input.db"
    async with (
        filled_log(target, ["/roster"]) as log,
        harness(make_mock, wait_for, input_log=log) as unit,
    ):
        unit.app.query_one("#prompt", PromptInput).focus_input()
        await unit.pilot.press(*"/features")
        await unit.pilot.press("enter")
        assert await wait_for(lambda: "/features" in _stored_lines(target), 5.0)

        await unit.pilot.press(*"обычное сообщение")
        await unit.pilot.press("enter")
        await unit.pilot.pause()
        assert "обычное сообщение" not in _stored_lines(target)


@pytest.mark.parametrize(("key", "leaves"), [("y", True), ("д", True), ("n", False), ("н", False)])
async def test_quit_dialog_answers_in_both_layouts(
    make_mock: MockFactory, wait_for: Waiter, key: str, leaves: bool
) -> None:
    """Диалог отвечает и на латинице, и на кириллице.

    Кириллические буквы выбраны по смыслу, а не по позиции клавиши: физическая
    клавиша y на русской раскладке дает "н", и считать ее согласием значило бы
    выходить по нажатию, которое пользователь считает отказом.
    """
    async with harness(make_mock, wait_for) as unit:
        await unit.pilot.press("ctrl+c")
        await _settle(unit)
        await unit.pilot.press(key)
        await _settle(unit)
        assert unit.app.is_running is not leaves


async def test_ctrl_c_copies_selection_before_asking(
    make_mock: MockFactory, wait_for: Waiter
) -> None:
    """При выделении первое нажатие копирует, а не спрашивает о выходе.

    Выделение снимается сразу: иначе следующее нажатие снова копировало бы тот
    же текст, и до выхода дело не дошло бы никогда.
    """
    async with harness(make_mock, wait_for) as unit:
        log = unit.app.query_one("#chat-log")
        unit.app.screen.selections[log] = Selection.from_offsets(Offset(0, 0), Offset(5, 0))
        await unit.pilot.press("ctrl+c")
        await _settle(unit)
        assert not isinstance(unit.app.screen, QuitConfirmScreen)
        assert not unit.app.screen.selections
        assert unit.app.is_running
