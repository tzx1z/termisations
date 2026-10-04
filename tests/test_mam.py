"""Курсор постраничной выборки архива и путь строфы.

Ответы сервера здесь синтетические. Семантика RSM у ejabberd и Prosody отличается в
деталях, и проверка только живым тестом против одного контейнера дает ложную
уверенность. Заведомо некорректные ответы проверяются тоже: обход не должен становиться
бесконечным из-за сервера.
"""

from typing import Any, Final

import pytest

from termisations.core.models import DeliveryState
from termisations.core.trace import MAX_TRACES, TraceStore
from termisations.protocol.mam import PAGE_SIZE, Cursor, Page, advance, parse_fin, query_args

PEER: Final = "bob@example.org"


class Fin:
    """Синтетический ответ ``<fin/>`` с индексацией, как у строфы slixmpp."""

    def __init__(self, rsm: dict[str, Any] | None = None, **fields: Any) -> None:
        self._fields: dict[str, Any] = dict(fields)
        if rsm is not None:
            self._fields["rsm"] = dict(rsm)

    def __getitem__(self, name: str) -> Any:
        if name not in self._fields:
            raise KeyError(name)
        value = self._fields[name]
        return Fin(**value) if isinstance(value, dict) else value


# Курсор.


def test_first_pass_reads_from_the_beginning() -> None:
    """Без курсора архив читается с начала и идет вперед.

    Запрос последней страницы выразить нельзя: slixmpp приводит значения RSM к
    строке, и пустой элемент ``<before/>`` через него не проходит.
    """
    args = query_args(Cursor())
    assert "before" not in args
    assert "after" not in args
    assert args["max"] == PAGE_SIZE


def test_rsm_values_survive_string_coercion() -> None:
    """Аргументы переживают приведение к строке, которое делает slixmpp.

    Библиотека кладет в строфу ``str(value)`` по каждому ключу. Значение,
    которое после этого меняет смысл, здесь появиться не должно.
    """
    for cursor in (Cursor(), Cursor(last_id="s-1")):
        for key, value in query_args(cursor).items():
            assert str(value) == value if isinstance(value, str) else str(value).isdigit(), (
                f"{key}={value!r} после str() означает не то же самое"
            )


def test_catch_up_goes_forward_from_the_cursor() -> None:
    """С курсором обход идет вперед: догоняется пропущенное между запусками."""
    args = query_args(Cursor(last_id="s-100"))
    assert args["after"] == "s-100"
    assert "before" not in args


def test_page_size_is_clamped() -> None:
    """Размер страницы не выходит за предел: сервер урежет молча, а мы явно."""
    assert query_args(Cursor(), page_size=10_000)["max"] == PAGE_SIZE
    assert query_args(Cursor(), page_size=0)["max"] == 1


def test_full_page_asks_for_more() -> None:
    """Полная страница сдвигает курсор и требует продолжения."""
    cursor, more = advance(Cursor(), Page(first="s-1", last="s-50"))
    assert cursor.last_id == "s-50"
    assert cursor.complete is False
    assert more is True


def test_complete_page_stops_the_walk() -> None:
    """Объявленная сервером полнота заканчивает обход."""
    cursor, more = advance(Cursor(last_id="s-1"), Page(first="s-2", last="s-9", complete=True))
    assert cursor == Cursor(last_id="s-9", complete=True)
    assert more is False


def test_empty_page_means_the_end() -> None:
    """Пустая страница означает конец архива, а курсор остается прежним."""
    cursor, more = advance(Cursor(last_id="s-7"), Page())
    assert cursor == Cursor(last_id="s-7", complete=True)
    assert more is False


def test_page_that_does_not_move_the_cursor_stops_the_walk() -> None:
    """Сервер, отдающий ту же страницу, не должен давать бесконечный обход.

    Именно так выглядит расхождение реализаций RSM: запрос за границей архива
    возвращает последнюю страницу вместо пустой.
    """
    cursor, more = advance(Cursor(last_id="s-9"), Page(first="s-1", last="s-9"))
    assert cursor.last_id == "s-9"
    assert more is False


# Разбор ответа.


def test_parse_full_answer() -> None:
    """Все поля RSM разбираются: first, last, count и признак полноты."""
    page = parse_fin(Fin(rsm={"first": "s-1", "last": "s-50", "count": "137"}, complete="false"))
    assert page == Page(first="s-1", last="s-50", count=137, complete=False)


def test_parse_last_page() -> None:
    """Последняя страница помечена признаком complete."""
    page = parse_fin(Fin(rsm={"first": "s-90", "last": "s-99"}, complete="true"))
    assert page.complete is True
    assert page.last == "s-99"


def test_parse_empty_archive() -> None:
    """Пустой архив: полей нет вовсе, разбор не падает."""
    page = parse_fin(Fin(rsm={}))
    assert page.empty is True
    assert page.count is None


def test_parse_missing_fin() -> None:
    """Ответа нет вовсе: считается пустой страницей, а не ошибкой."""
    assert parse_fin(None).empty is True


def test_parse_count_that_is_not_a_number() -> None:
    """Нечисловой count не роняет разбор: поле необязательное."""
    assert parse_fin(Fin(rsm={"last": "s-1", "count": "много"})).count is None


@pytest.mark.parametrize(
    ("value", "expected"), [("true", True), ("1", True), ("false", False), ("", False)]
)
def test_complete_flag_forms(value: str, expected: bool) -> None:
    """Признак полноты приходит строкой в разных видах."""
    assert parse_fin(Fin(rsm={"last": "s-1"}, complete=value)).complete is expected


def test_walk_reaches_the_end_of_a_paged_archive() -> None:
    """Обход трех страниц заканчивается ровно на объявленной полноте."""
    pages = [
        Page(first="s-1", last="s-50"),
        Page(first="s-51", last="s-100"),
        Page(first="s-101", last="s-120", complete=True),
    ]
    cursor = Cursor()
    visited = 0
    for page in pages:
        cursor, more = advance(cursor, page)
        visited += 1
        if not more:
            break
    assert visited == 3
    assert cursor == Cursor(last_id="s-120", complete=True)


# Путь строфы.


def test_trace_collects_the_whole_path() -> None:
    """Путь собирается из четырех источников и дает состояние доставки."""
    store = TraceStore()
    store.start("m-1", PEER, origin_id="o-1")
    store.set_stanza_id("m-1", "s-1")
    store.note("m-1", "0359", "origin-id")
    store.note("m-1", "0198", "acked", detail="h=12")
    store.note("m-1", "0184", "receipt-received", peer=PEER)
    trace = store.note("m-1", "0333", "displayed", peer=PEER)
    assert trace is not None
    assert trace.state is DeliveryState.DISPLAYED
    labels = [name for name, _ in trace.rows()]
    assert "XEP-0198 acked" in labels
    assert "XEP-0184 receipt-received" in labels
    assert "XEP-0333 displayed" in labels


@pytest.mark.parametrize(
    ("actions", "expected"),
    [
        ((), DeliveryState.PENDING),
        ((("0359", "origin-id"),), DeliveryState.SENT),
        ((("0198", "acked"),), DeliveryState.ACKED),
        ((("0184", "receipt-received"),), DeliveryState.RECEIVED),
        ((("0333", "displayed"),), DeliveryState.DISPLAYED),
    ],
)
def test_state_follows_the_furthest_mark(
    actions: tuple[tuple[str, str], ...], expected: DeliveryState
) -> None:
    """Состояние доставки определяет самая дальняя отметка пути."""
    store = TraceStore()
    store.start("m-1", PEER)
    for xep, action in actions:
        store.note("m-1", xep, action)
    trace = store.find("m-1")
    assert trace is not None
    assert trace.state is expected


def test_missing_stanza_id_is_reported_not_hidden() -> None:
    """Отсутствие stanza-id - это ответ, а не пустая строка.

    Без него сообщение не попало в архив, и искать его в MAM бесполезно. Клиент
    обязан сказать об этом, а не показать пустую таблицу.
    """
    store = TraceStore()
    store.start("m-1", PEER, origin_id="o-1")
    trace = store.find("m-1")
    assert trace is not None
    rows = dict(trace.rows())
    assert "в архиве сообщения нет" in rows["stanza-id"]


def test_repeated_mark_is_not_duplicated() -> None:
    """Повтор подтверждения не занимает вывод: сервер шлет их пачкой."""
    store = TraceStore()
    store.start("m-1", PEER)
    store.note("m-1", "0198", "acked")
    trace = store.note("m-1", "0198", "acked")
    assert trace is not None
    assert len([step for step in trace.steps if step.xep == "0198"]) == 1


def test_marks_from_different_peers_are_kept() -> None:
    """Маркеры прочтения от разных участников комнаты не схлопываются."""
    store = TraceStore()
    store.start("m-1", "room@conference.example.org")
    store.note("m-1", "0333", "displayed", peer="alice")
    trace = store.note("m-1", "0333", "displayed", peer="bob")
    assert trace is not None
    assert len(trace.steps) == 2


def test_lookup_works_by_any_identifier() -> None:
    """Идентификатор копируют из панели лога и не обязаны знать, чей он."""
    store = TraceStore()
    store.start("m-1", PEER, origin_id="o-1")
    store.set_stanza_id("m-1", "s-1")
    assert store.find("m-1") is not None
    assert store.find("o-1") is not None
    assert store.find("s-1") is not None
    assert store.find("нет такого") is None
    assert store.find("  ") is None


def test_note_for_unknown_message_is_ignored() -> None:
    """Отметка по неизвестному сообщению не создает путь из ниоткуда."""
    assert TraceStore().note("m-1", "0198", "acked") is None


def test_store_is_bounded() -> None:
    """Хранилище путей ограничено: оно смотрит недавнее, а не всю историю."""
    store = TraceStore(limit=3)
    for index in range(10):
        store.start(f"m-{index}", PEER)
    assert len(store) == 3
    assert store.find("m-0") is None
    assert store.find("m-9") is not None


def test_default_limit_is_not_unbounded() -> None:
    """Предел по умолчанию задан числом, а не оставлен на усмотрение памяти."""
    assert MAX_TRACES > 0
