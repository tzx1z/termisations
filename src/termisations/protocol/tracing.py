"""Разбор сырого потока XMPP на отдельные строфы.

Панели сырого потока нужен текст строфы ровно в том виде, в каком он прошел по
сокету. Ни slixmpp, ни стандартный парсер такой строки не отдают: slixmpp
скармливает байты инкрементальному парсеру и наружу выдает уже разобранные
объекты, а повторная сериализация меняет порядок атрибутов, кавычки и
пространства имен. Для клиента, который показывают как учебное пособие по
протоколу, это неприемлемо: на экране должно быть то, что реально ушло.

Поэтому здесь стоит разделитель потока. Это не парсер: содержимое элементов он
не разбирает и структуру не проверяет. Его работа - найти границы элементов
верхнего уровня, устояв на кавычках с угловыми скобками внутри, на комментариях,
на CDATA и на пришедшем по частям куске.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from termisations.core.models import Direction, RawStanza, StanzaKind

__all__ = ["StreamSplitter", "classify", "peer_of", "stanza_id_of"]

# Пролог и объявление потока в конец не собираются: закрывающего тега у
# <stream:stream> нет до самого конца сессии, и ждать его нельзя.
# После имени обязателен пробел или закрывающая скобка: иначе выражение
# поймает <stream:features> и разорвет его на куски.
_OPEN_STREAM: Final = re.compile(r"<(?:\w+:)?stream(?=[\s>])[^>]*>", re.ASCII)
_DECLARATION: Final = re.compile(r"<\?xml\b[^>]*\?>", re.ASCII)

# Имя элемента в начале строки: с префиксом пространства имен или без него.
_ELEMENT_NAME: Final = re.compile(r"<\s*(?:([\w.-]+):)?([\w.-]+)", re.ASCII)

_ATTR: Final = re.compile(r"""(\w[\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.ASCII)

# Соответствие имени элемента типу строфы. Ключ - имя без префикса.
_KINDS: Final[dict[str, StanzaKind]] = {
    "message": StanzaKind.MESSAGE,
    "presence": StanzaKind.PRESENCE,
    "iq": StanzaKind.IQ,
    "auth": StanzaKind.SASL,
    "authenticate": StanzaKind.SASL,
    "challenge": StanzaKind.SASL,
    "response": StanzaKind.SASL,
    "success": StanzaKind.SASL,
    "failure": StanzaKind.SASL,
    "abort": StanzaKind.SASL,
    "starttls": StanzaKind.TLS,
    "proceed": StanzaKind.TLS,
    "stream": StanzaKind.STREAM,
    "features": StanzaKind.STREAM,
    "enable": StanzaKind.STREAM,
    "enabled": StanzaKind.STREAM,
    "resume": StanzaKind.STREAM,
    "resumed": StanzaKind.STREAM,
    "r": StanzaKind.STREAM,
    "a": StanzaKind.STREAM,
    "sm": StanzaKind.STREAM,
    "error": StanzaKind.STREAM,
}

# Предел длины одного куска, который держится в буфере в ожидании закрытия.
# Строфа больше этого размера почти наверняка означает, что закрывающий тег
# потерян, и держать буфер дальше бессмысленно: он растет без предела.
MAX_BUFFER: Final = 1 << 20


@dataclass(slots=True)
class StreamSplitter:
    """Инкрементальный разделитель потока на элементы верхнего уровня.

    Данные приходят кусками произвольного размера: один вызов может принести
    половину строфы или три строфы подряд. Разделитель копит остаток и отдает
    только завершенные элементы.
    """

    buffer: str = ""
    overflow: int = 0
    """Сколько раз буфер сбрасывался по переполнению. Видно в /stats."""

    def feed(self, data: str) -> list[str]:
        """Добавить кусок потока и забрать завершенные элементы."""
        self.buffer += data
        if len(self.buffer) > MAX_BUFFER:
            # Отбрасывается именно начало: хвост может содержать начало
            # следующей корректной строфы, и по нему поток восстановится.
            self.buffer = self.buffer[-MAX_BUFFER // 2 :]
            self.overflow += 1
        return list(self._drain())

    def reset(self) -> None:
        """Забыть незавершенный остаток. Вызывается при разрыве потока."""
        self.buffer = ""

    def _drain(self) -> Iterator[str]:
        """Выбрать из буфера все завершенные элементы."""
        while True:
            chunk = self._next_chunk()
            if chunk is None:
                return
            yield chunk

    def _next_chunk(self) -> str | None:
        """Отрезать от буфера один завершенный элемент или вернуть None."""
        start = self.buffer.find("<")
        if start < 0:
            # Текста вне элементов в потоке XMPP не бывает: это пробелы между
            # строфами, keepalive-пробел сервера или мусор. Копить его незачем.
            self.buffer = ""
            return None
        if start:
            self.buffer = self.buffer[start:]

        declaration = _DECLARATION.match(self.buffer)
        if declaration:
            self.buffer = self.buffer[declaration.end() :]
            return declaration.group(0)

        opening = _OPEN_STREAM.match(self.buffer)
        if opening and not self.buffer.startswith("</"):
            # Объявление потока живет до конца сессии, закрытия ждать нельзя.
            self.buffer = self.buffer[opening.end() :]
            return opening.group(0)

        end = _scan_element(self.buffer)
        if end < 0:
            return None
        chunk = self.buffer[:end]
        self.buffer = self.buffer[end:]
        return chunk


def _scan_element(text: str) -> int:
    """Найти конец первого элемента. Возвращает -1, если элемент не завершен.

    Сканер держит глубину вложенности и пропускает участки, внутри которых
    угловая скобка не является разметкой: значения атрибутов в кавычках,
    комментарии, CDATA и инструкции обработки.
    """
    depth = 0
    index = 0
    size = len(text)
    while index < size:
        if text.startswith("<!--", index):
            close = text.find("-->", index + 4)
            if close < 0:
                return -1
            index = close + 3
            # Комментарий верхнего уровня - самостоятельный кусок потока.
            # Иначе он склеится со следующей строфой в одну строку панели.
            if depth == 0:
                return index
            continue
        if text.startswith("<![CDATA[", index):
            close = text.find("]]>", index + 9)
            if close < 0:
                return -1
            index = close + 3
            continue
        if text.startswith("</", index):
            close = _scan_tag(text, index)
            if close < 0:
                return -1
            depth -= 1
            index = close
            if depth <= 0:
                return index
            continue
        if text[index] == "<":
            close = _scan_tag(text, index)
            if close < 0:
                return -1
            if text[close - 2 : close] == "/>":
                if depth == 0:
                    return close
            else:
                depth += 1
            index = close
            continue
        index += 1
    return -1


def _scan_tag(text: str, start: int) -> int:
    """Найти позицию сразу за закрывающей скобкой тега. -1, если тег не закрыт."""
    index = start + 1
    size = len(text)
    quote = ""
    while index < size:
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == ">":
            return index + 1
        index += 1
    return -1


def classify(xml: str) -> StanzaKind:
    """Тип строфы по имени корневого элемента."""
    match = _ELEMENT_NAME.match(xml.lstrip())
    if match is None:
        return StanzaKind.OTHER
    prefix, name = match.group(1), match.group(2).lower()
    if prefix == "stream" and name != "stream":
        # stream:features и stream:error относятся к управлению потоком.
        return StanzaKind.STREAM
    return _KINDS.get(name, StanzaKind.OTHER)


def _attributes(xml: str) -> dict[str, str]:
    """Атрибуты корневого элемента. Разбирается только открывающий тег."""
    end = xml.find(">")
    head = xml if end < 0 else xml[: end + 1]
    return {
        match.group(1): match.group(2) if match.group(2) is not None else match.group(3) or ""
        for match in _ATTR.finditer(head)
    }


def stanza_id_of(xml: str) -> str | None:
    """Идентификатор строфы из атрибута id, если он есть."""
    return _attributes(xml).get("id") or None


def peer_of(xml: str, direction: Direction) -> str | None:
    """Собеседник строфы: from у входящей, to у исходящей."""
    attributes = _attributes(xml)
    key = "from" if direction is Direction.IN else "to"
    value = attributes.get(key)
    return value or None


def make_stanza(xml: str, direction: Direction, ts: float) -> RawStanza:
    """Собрать запись для панели сырого потока."""
    text = xml.strip()
    return RawStanza(
        ts=ts,
        direction=direction,
        kind=classify(text),
        xml=text,
        stanza_id=stanza_id_of(text),
        peer=peer_of(text, direction),
        is_error="type='error'" in text or 'type="error"' in text,
        size_bytes=len(text.encode("utf-8")),
    )
