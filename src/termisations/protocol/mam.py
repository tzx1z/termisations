"""Курсор постраничной выборки архива по XEP-0313 и XEP-0059.

Логика курсора вынесена из сессии. Семантика RSM у разных серверов
отличается в деталях: ejabberd и Prosody по-разному отвечают на запрос за
границей архива и по-разному заполняют ``count``. Проверять это только живым
тестом против одного контейнера - значит получить ложную уверенность, поэтому
здесь чистые функции над разобранными значениями, а не над строфами slixmpp.

Направление обхода одно: от начала архива вперед, ``after``. Обратный обход
"последняя страница" выразить нельзя: по XEP-0059 он задается пустым элементом
``<before/>``, а slixmpp в ``XEP_0313.retrieve`` приводит каждое значение RSM к
строке (``mam.py:89-93``), и пустая строка удаляет элемент, а ``True``
превращается в ``<before>True</before>`` - запрос строфы с идентификатором
"True". Проверено на строфе: оба варианта дают не то, что нужно.

Цена решения: первая синхронизация длинной беседы читает архив с начала и
упирается в потолок числа страниц. Это предсказуемо и ограничено, в отличие от
обхода, который молча возвращает не те сообщения.
"""

from dataclasses import dataclass
from typing import Any, Final

__all__ = ["MAX_PAGES", "PAGE_SIZE", "Cursor", "Page", "advance", "parse_fin", "query_args"]

# Размер страницы. Сто строф - предел, который сервера принимают без урезания,
# и он же удерживает всплеск потока в границах бюджета панели лога.
PAGE_SIZE: Final = 100

# Потолок числа страниц на одну догонку. Архив может быть очень длинным, а
# бесконечный обход при кривом ответе сервера выглядит как зависший клиент.
MAX_PAGES: Final = 50


@dataclass(frozen=True, slots=True)
class Page:
    """Итог одной страницы ответа архива."""

    first: str = ""
    """Идентификатор первой строфы страницы. Пустая строка означает пустую страницу."""

    last: str = ""
    """Идентификатор последней строфы страницы."""

    count: int | None = None
    """Общее число строф в выборке, если сервер его сообщил."""

    complete: bool = False
    """Сервер объявил выборку законченной."""

    @property
    def empty(self) -> bool:
        """Страница без строф. Пустая страница всегда означает конец обхода."""
        return not self.last


@dataclass(frozen=True, slots=True)
class Cursor:
    """Состояние синхронизации архива одной беседы."""

    last_id: str = ""
    """Последний известный ``stanza-id``. Пустая строка означает, что синхронизации не было."""

    complete: bool = False
    """Архив дочитан до конца на момент последнего обхода."""

    @property
    def fresh(self) -> bool:
        """Беседу еще ни разу не синхронизировали."""
        return not self.last_id


def parse_fin(fin: Any) -> Page:
    """Разобрать элемент ``<fin/>`` ответа архива.

    Принимает объект строфы slixmpp или любой другой, у которого индексация
    работает так же. Отсутствующие поля - штатный случай: ``count`` не обязателен,
    а ``first`` и ``last`` пусты у пустой страницы.
    """
    if fin is None:
        return Page()
    # Блок RSM лежит вложенным элементом, но у синтетического ответа теста поля
    # могут быть плоскими: разбор обязан работать с обоими.
    rsm = _sub(fin, "rsm")
    return Page(
        first=_text(rsm, "first"),
        last=_text(rsm, "last"),
        count=_number(rsm, "count"),
        complete=_flag(fin, "complete"),
    )


def query_args(cursor: Cursor, page_size: int = PAGE_SIZE) -> dict[str, Any]:
    """Аргументы RSM для следующего запроса.

    Первый обход идет с начала архива, дальше - вперед от последней известной
    строфы. Запрос последней страницы через пустой ``<before/>`` недоступен, см.
    docstring модуля.
    """
    size = max(1, min(page_size, PAGE_SIZE))
    if cursor.fresh:
        return {"max": size}
    return {"max": size, "after": cursor.last_id}


def advance(cursor: Cursor, page: Page) -> tuple[Cursor, bool]:
    """Новый курсор и признак того, что обход нужно продолжать.

    Обход прекращается на пустой странице, на объявленной сервером полноте и на
    странице, которая не сдвинула курсор: последнее означает, что сервер отдает
    одно и то же, и следующий запрос был бы бесконечным.
    """
    if page.empty:
        return Cursor(cursor.last_id, complete=True), False
    moved = page.last != cursor.last_id
    updated = Cursor(page.last, complete=page.complete)
    return updated, moved and not page.complete


def _sub(source: Any, name: str) -> Any:
    """Вложенный блок строфы. Отсутствие блока возвращает саму строфу."""
    try:
        value = source[name]
    except (KeyError, TypeError):
        return source
    return source if value is None else value


def _text(source: Any, name: str) -> str:
    """Текст поля строфы. Отсутствие поля дает пустую строку."""
    try:
        value = source[name]
    except (KeyError, TypeError):
        return ""
    return "" if value is None else str(value)


def _number(source: Any, name: str) -> int | None:
    """Число из поля строфы. ``None``, если поля нет или оно не число."""
    raw = _text(source, name)
    try:
        return int(raw)
    except ValueError:
        return None


def _flag(source: Any, name: str) -> bool:
    """Булево поле строфы. Пустое значение элемента считается истиной.

    Сервер объявляет полноту атрибутом ``complete='true'``, но slixmpp отдает его
    и строкой, и булевым значением в зависимости от версии плагина.
    """
    try:
        value = source[name]
    except (KeyError, TypeError):
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1")
