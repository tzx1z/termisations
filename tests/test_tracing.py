"""Разделитель сырого потока: границы строф и их разбор.

Проверяется не парсинг XML, а нарезка потока: панель показывает то, что прошло
по сокету, и ошибка здесь означает разорванную или склеенную строфу на экране.
"""

import pytest

from termisations.core.models import Direction, StanzaKind
from termisations.protocol.tracing import (
    MAX_BUFFER,
    StreamSplitter,
    classify,
    make_stanza,
    peer_of,
    stanza_id_of,
)

STREAM_HEADER = (
    "<stream:stream to='localhost' xmlns:stream='http://etherx.jabber.org/streams' "
    "xmlns='jabber:client' version='1.0'>"
)


def test_declaration_and_stream_header_are_separate() -> None:
    """Пролог и объявление потока отдаются сразу, закрытия у них нет."""
    splitter = StreamSplitter()
    chunks = splitter.feed("<?xml version='1.0'?>" + STREAM_HEADER)
    assert len(chunks) == 2
    assert chunks[0] == "<?xml version='1.0'?>"
    assert chunks[1] == STREAM_HEADER
    assert splitter.buffer == ""


def test_stream_features_stays_whole() -> None:
    """stream:features не путается с объявлением потока и не рвется на части."""
    features = (
        "<stream:features><mechanisms xmlns='urn:ietf:params:xml:ns:xmpp-sasl'>"
        "<mechanism>SCRAM-SHA-256</mechanism></mechanisms></stream:features>"
    )
    assert StreamSplitter().feed(features) == [features]


def test_several_stanzas_in_one_chunk() -> None:
    """Один кусок сокета может принести несколько строф."""
    data = (
        "<presence from='a@x'/><message to='b@y'><body>hi</body></message>"
        "<r xmlns='urn:xmpp:sm:3'/>"
    )
    chunks = StreamSplitter().feed(data)
    assert len(chunks) == 3
    assert [classify(item) for item in chunks] == [
        StanzaKind.PRESENCE,
        StanzaKind.MESSAGE,
        StanzaKind.STREAM,
    ]


def test_stanza_split_across_chunks() -> None:
    """Строфа, разорванная между вызовами, собирается целиком."""
    splitter = StreamSplitter()
    assert splitter.feed("<message to='b@y'><bo") == []
    assert splitter.feed("dy>текст</body></mes") == []
    chunks = splitter.feed("sage>")
    assert chunks == ["<message to='b@y'><body>текст</body></message>"]


def test_byte_by_byte_stream() -> None:
    """Поток по одному символу дает тот же результат, что и целиком."""
    whole = (
        "<iq type='result' id='1'><query xmlns='jabber:iq:roster'/></iq>"
        "<a xmlns='urn:xmpp:sm:3' h='2'/>"
    )
    splitter = StreamSplitter()
    chunks: list[str] = []
    for char in whole:
        chunks.extend(splitter.feed(char))
    assert "".join(chunks) == whole
    assert len(chunks) == 2


@pytest.mark.parametrize(
    "data",
    [
        "<message note='a > b'><body>1</body></message>",
        '<message note="a > b"><body>1</body></message>',
        "<message><body><![CDATA[<not-a-tag/>]]></body></message>",
        "<message><body>&lt;tag&gt;</body></message>",
    ],
)
def test_angle_bracket_inside_content(data: str) -> None:
    """Угловая скобка внутри значения атрибута и CDATA не завершает строфу."""
    assert StreamSplitter().feed(data) == [data]


def test_top_level_comment_is_its_own_chunk() -> None:
    """Комментарий верхнего уровня не склеивается со следующей строфой."""
    chunks = StreamSplitter().feed("<!-- служебное --><iq type='result' id='1'/>")
    assert len(chunks) == 2
    assert chunks[0] == "<!-- служебное -->"


def test_buffer_overflow_is_counted_not_hidden() -> None:
    """Незакрытая строфа сверх предела сбрасывает буфер и увеличивает счетчик.

    Молча расти буфер не должен: поток без закрывающего тега расходовал бы
    память без ограничения.
    """
    splitter = StreamSplitter()
    splitter.feed("<message>" + "x" * (MAX_BUFFER + 10))
    assert splitter.overflow == 1
    assert len(splitter.buffer) <= MAX_BUFFER


def test_reset_forgets_partial_stanza() -> None:
    """Разрыв потока не оставляет хвост, который склеится со следующей сессией."""
    splitter = StreamSplitter()
    splitter.feed("<message to='b@y'><bo")
    splitter.reset()
    assert splitter.feed("<presence/>") == ["<presence/>"]


@pytest.mark.parametrize(
    ("xml", "kind"),
    [
        ("<message/>", StanzaKind.MESSAGE),
        ("<presence type='unavailable'/>", StanzaKind.PRESENCE),
        ("<iq type='get'/>", StanzaKind.IQ),
        ("<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl'/>", StanzaKind.SASL),
        ("<challenge/>", StanzaKind.SASL),
        ("<starttls/>", StanzaKind.TLS),
        ("<proceed/>", StanzaKind.TLS),
        ("<r xmlns='urn:xmpp:sm:3'/>", StanzaKind.STREAM),
        ("<stream:features/>", StanzaKind.STREAM),
        ("<что-то/>", StanzaKind.OTHER),
    ],
)
def test_classify(xml: str, kind: StanzaKind) -> None:
    """Тип строфы определяется по имени корневого элемента."""
    assert classify(xml) is kind


def test_attributes_of_stanza() -> None:
    """Идентификатор и собеседник берутся по направлению строфы."""
    incoming = "<message from='bob@srv/res' to='alice@srv' id='m1'><body>x</body></message>"
    assert stanza_id_of(incoming) == "m1"
    assert peer_of(incoming, Direction.IN) == "bob@srv/res"
    assert peer_of(incoming, Direction.OUT) == "alice@srv"


def test_make_stanza_marks_errors() -> None:
    """Ошибочная строфа помечается независимо от кавычек в атрибуте."""
    for xml in ("<iq type='error' id='1'/>", '<iq type="error" id="1"/>'):
        stanza = make_stanza(xml, Direction.IN, 1.0)
        assert stanza.is_error
        assert stanza.size_bytes == len(xml.encode("utf-8"))


def test_make_stanza_counts_bytes_in_utf8() -> None:
    """Размер считается в байтах, а не в символах: кириллица занимает больше."""
    stanza = make_stanza("<message><body>привет</body></message>", Direction.OUT, 1.0)
    assert stanza.size_bytes > len("<message><body>привет</body></message>")
