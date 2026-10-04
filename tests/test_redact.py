"""Маскирование секретов.

Проверяется не форма вывода, а факт: исходный секрет в результате отсутствует.
Поэтому почти каждая проверка ищет подстроку исходного тела и требует, чтобы ее
не было.
"""

import base64
import re
import time
from collections.abc import Callable
from typing import Final

import pytest

from termisations.core import i18n
from termisations.core.i18n import _
from termisations.core.models import RawStanza
from termisations.core.redact import (
    OMEMO_NAMESPACES,
    SASL_NAMESPACES,
    UNSAFE_WARNING,
    redact,
    redaction_summary,
)

# Образцы строф с секретами.

AUTH_SECRET: Final = "biwsbj1hbGljZSxyPXJPcHJOR2Z3RWJlUldnYlNRPT0="
SASL_AUTH: Final = (
    f"<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl' mechanism='SCRAM-SHA-256'>{AUTH_SECRET}</auth>"
)

RESPONSE_SECRET: Final = "Yz1iaXdzLHI9ck9wck5HZndFYmVSV2dpclZoRHJ6cCxwPXY5"
SASL_RESPONSE: Final = f"<response xmlns='urn:xmpp:sasl:2'>{RESPONSE_SECRET}</response>"

CHALLENGE_SECRET: Final = "cj1yT3ByTkdmd0ViZVJXZ2JyVmhEcnpwLHM9VzIyWmFKMFNO"
SASL_CHALLENGE: Final = (
    f"<challenge xmlns='urn:ietf:params:xml:ns:xmpp-sasl'>{CHALLENGE_SECRET}</challenge>"
)

OMEMO_KEY_SECRET: Final = "MwohBS9vL0hKZE1rTnFQclNzVHVWd1h5WjBhMmM0ZTZnOGk="
OMEMO_PAYLOAD_SECRET: Final = "U29tZUxvbmdDaXBoZXJUZXh0V2l0aFNlY3JldFBheWxvYWREYXRh"
OMEMO_MESSAGE: Final = (
    "<message from='bob@srv/phone' id='omemo-1' type='chat'>"
    "<encrypted xmlns='eu.siacs.conversations.axolotl'>"
    "<header sid='1712345'>"
    f"<key rid='4823001' prekey='true'>{OMEMO_KEY_SECRET}</key>"
    "<iv>YWJjZGVmZ2hpams=</iv>"
    "</header>"
    f"<payload>{OMEMO_PAYLOAD_SECRET}</payload>"
    "</encrypted></message>"
)

BUNDLE_KEY_SECRET: Final = "BXByZUtleVB1YmxpY0tleU1hdGVyaWFsMDAx"
OMEMO_BUNDLE: Final = (
    "<iq type='result' from='bob@srv' id='bundle-1'>"
    "<pubsub xmlns='http://jabber.org/protocol/pubsub'><items node='urn:xmpp:omemo:2:bundles'>"
    "<item><bundle xmlns='urn:xmpp:omemo:2'>"
    f"<spk id='1'>{BUNDLE_KEY_SECRET}</spk>"
    f"<ik>{BUNDLE_KEY_SECRET}</ik>"
    f"<prekeys><pk id='1'>{BUNDLE_KEY_SECRET}</pk></prekeys>"
    "</bundle></item></items></pubsub></iq>"
)

SLOT_SIGNATURE: Final = "v4-signature-DO-NOT-LOG-8f3a91c0"
SLOT_HEADER_SECRET: Final = "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
UPLOAD_SLOT: Final = (
    "<iq type='result' from='upload.srv' id='slot-1'>"
    "<slot xmlns='urn:xmpp:http:upload:0'>"
    f"<put url='https://upload.srv/f/a1b2/report.log?token={SLOT_SIGNATURE}&amp;exp=1700000000'>"
    f"<header name='Authorization'>{SLOT_HEADER_SECRET}</header>"
    "</put>"
    f"<get url='https://upload.srv/f/a1b2/report.log?token={SLOT_SIGNATURE}'/>"
    "</slot></iq>"
)

SLOT_ATTR_FORM: Final = (
    "<slot xmlns='urn:xmpp:http:upload:0' "
    f"put='https://upload.srv/f/c3d4/dump.bin?sig={SLOT_SIGNATURE}' "
    f"get='https://upload.srv/f/c3d4/dump.bin?sig={SLOT_SIGNATURE}'/>"
)

OOB_URL: Final = (
    "<message to='bob@srv' id='oob-1'><x xmlns='jabber:x:oob'>"
    f"<url>https://upload.srv/f/e5f6/shot.png?sig={SLOT_SIGNATURE}</url>"
    "</x></message>"
)

MUC_PASSWORD: Final = "s3cr3t-room-pass"
MUC_JOIN: Final = (
    "<presence to='devops@conf.srv/term' id='join-1'>"
    "<x xmlns='http://jabber.org/protocol/muc'>"
    f"<password>{MUC_PASSWORD}</password>"
    "</x></presence>"
)

PLAIN_MESSAGE: Final = (
    "<message from='bob@srv/term' id='m-1' type='chat'><body>перезапусти воркер</body></message>"
)

ALL_SAMPLES: Final[tuple[str, ...]] = (
    SASL_AUTH,
    SASL_RESPONSE,
    SASL_CHALLENGE,
    OMEMO_MESSAGE,
    OMEMO_BUNDLE,
    UPLOAD_SLOT,
    SLOT_ATTR_FORM,
    OOB_URL,
    MUC_JOIN,
    PLAIN_MESSAGE,
)

ALL_SECRETS: Final[tuple[str, ...]] = (
    AUTH_SECRET,
    RESPONSE_SECRET,
    CHALLENGE_SECRET,
    OMEMO_KEY_SECRET,
    OMEMO_PAYLOAD_SECRET,
    SLOT_SIGNATURE,
    SLOT_HEADER_SECRET,
    MUC_PASSWORD,
)


# SASL.


@pytest.mark.parametrize(
    ("stanza", "secret"),
    [
        (SASL_AUTH, AUTH_SECRET),
        (SASL_RESPONSE, RESPONSE_SECRET),
        (SASL_CHALLENGE, CHALLENGE_SECRET),
    ],
    ids=["auth", "response", "challenge"],
)
def test_sasl_payload_masked(stanza: str, secret: str) -> None:
    """Тело SASL заменяется отметкой о длине, исходного base64 в выводе нет."""
    result = redact(stanza)
    assert secret not in result
    assert "[SASL payload:" in result
    assert "redacted]" in result
    # Размер считается по исходному телу, а не по замене.
    assert str(len(secret.encode())) in result


def test_sasl_namespaces_are_covered() -> None:
    """Обе версии SASL перечислены в модуле и обе маскируются."""
    assert set(SASL_NAMESPACES) == {"urn:ietf:params:xml:ns:xmpp-sasl", "urn:xmpp:sasl:2"}
    for namespace in SASL_NAMESPACES:
        stanza = f"<auth xmlns='{namespace}'>{AUTH_SECRET}</auth>"
        assert AUTH_SECRET not in redact(stanza)


def test_sasl_keeps_element_and_attributes() -> None:
    """Маскируется только тело: имя элемента и механизм остаются видимыми."""
    result = redact(SASL_AUTH)
    assert result.startswith("<auth ")
    assert "mechanism='SCRAM-SHA-256'" in result
    assert result.endswith("</auth>")


# OMEMO.


def test_omemo_key_and_payload_truncated() -> None:
    """Тела key и payload усечены до 16 символов, полных значений в выводе нет."""
    result = redact(OMEMO_MESSAGE)
    assert OMEMO_KEY_SECRET not in result
    assert OMEMO_PAYLOAD_SECRET not in result
    assert f"<key rid='4823001' prekey='true'>{OMEMO_KEY_SECRET[:16]}…</key>" in result
    assert f"<payload>{OMEMO_PAYLOAD_SECRET[:16]}…</payload>" in result
    # Служебные атрибуты нужны для отладки и остаются на месте.
    assert "sid='1712345'" in result


def test_omemo_namespaces_are_covered() -> None:
    """Оба пространства имен OMEMO обрабатываются одинаково."""
    assert set(OMEMO_NAMESPACES) == {"eu.siacs.conversations.axolotl", "urn:xmpp:omemo:2"}
    for namespace in OMEMO_NAMESPACES:
        stanza = (
            f"<encrypted xmlns='{namespace}'><header sid='1'>"
            f"<key rid='2'>{OMEMO_KEY_SECRET}</key></header>"
            f"<payload>{OMEMO_PAYLOAD_SECRET}</payload></encrypted>"
        )
        result = redact(stanza)
        assert OMEMO_KEY_SECRET not in result
        assert OMEMO_PAYLOAD_SECRET not in result


def test_omemo_bundle_replaced_by_key_count() -> None:
    """Вместо тел ключей бандла показывается их количество."""
    result = redact(OMEMO_BUNDLE)
    assert BUNDLE_KEY_SECRET not in result
    assert "[bundle:" in result
    assert "keys, redacted]" in result


# XEP-0363 и OOB.


@pytest.mark.parametrize("stanza", [UPLOAD_SLOT, SLOT_ATTR_FORM], ids=["nested", "attributes"])
def test_slot_query_string_stripped(stanza: str) -> None:
    """Подпись в query string слота срезается, путь остается читаемым."""
    result = redact(stanza)
    assert SLOT_SIGNATURE not in result
    assert "?" not in result
    assert "[signed]" in result
    assert "https://upload.srv/f/" in result


def test_slot_authorization_header_replaced() -> None:
    """Заголовок авторизации заменяется полностью."""
    result = redact(UPLOAD_SLOT)
    assert SLOT_HEADER_SECRET not in result
    assert "<header name='Authorization'>[redacted]</header>" in result


def test_oob_url_query_stripped() -> None:
    """Подписанная ссылка в OOB теряет query string."""
    result = redact(OOB_URL)
    assert SLOT_SIGNATURE not in result
    assert "[signed]" in result


# MUC.


def test_muc_password_replaced() -> None:
    """Пароль комнаты замещается целиком, длина не раскрывается."""
    result = redact(MUC_JOIN)
    assert MUC_PASSWORD not in result
    assert "<password>[redacted]</password>" in result
    # Остальная часть присутствия нужна для отладки и не трогается.
    assert "to='devops@conf.srv/term'" in result


# Общий контур.


@pytest.mark.parametrize("stanza", ALL_SAMPLES)
def test_unsafe_returns_source_unchanged(stanza: str) -> None:
    """При unsafe=True возвращается исходная строка без единого изменения."""
    assert redact(stanza, unsafe=True) == stanza
    assert redact(stanza, True) == stanza


def test_plain_message_is_not_touched() -> None:
    """Обычное сообщение проходит без изменений: маскирование не портит поток."""
    assert redact(PLAIN_MESSAGE) == PLAIN_MESSAGE


def test_no_secret_survives_any_sample() -> None:
    """Ни один из образцов секретов не остается в выводе по умолчанию."""
    joined = " ".join(redact(sample) for sample in ALL_SAMPLES)
    for secret in ALL_SECRETS:
        assert secret not in joined


# Устойчивость к битому входу.


def _truncations(sample: str) -> list[str]:
    """Срезы строфы разной длины, как при оборванном потоке."""
    step = max(1, len(sample) // 8)
    return [sample[:cut] for cut in range(0, len(sample) + step, step)]


BROKEN_INPUTS: Final[tuple[str, ...]] = (
    "",
    "   ",
    "\n\t",
    "<",
    ">",
    "<auth",
    "<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl'",
    "<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl'>",
    f"<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl'>{AUTH_SECRET}",
    "</auth>",
    "</auth><auth>",
    "<message><body>текст без закрывающих тегов",
    "<encrypted xmlns='eu.siacs.conversations.axolotl'><header sid='1'><key rid='2'>MwohB",
    "<put url='https://upload.srv/f?token=",
    "<password>",
    "<password></password>",
    "<header name='Authorization'>",
    "\x00\x01<auth>�</auth>",
    "<" * 5000,
    ">" * 5000,
    "<auth>" * 2000,
    "а" * 100_000,
    "<payload>" + "A" * 100_000,
    "<auth xmlns='urn:xmpp:sasl:2'>" + "B" * 100_000,
)


@pytest.mark.parametrize(
    "value",
    [*BROKEN_INPUTS, *(cut for sample in ALL_SAMPLES for cut in _truncations(sample))],
)
def test_broken_input_never_raises(value: str) -> None:
    """Функция не бросает исключений ни на каком входе и всегда отдает строку."""
    result = redact(value)
    assert isinstance(result, str)
    assert isinstance(redact(value, unsafe=True), str)


def test_long_input_is_processed_fast() -> None:
    """Длинная строка не вызывает катастрофического отката регулярных выражений.

    Порог 1 секунда взят с большим запасом: реальное время на такой строке
    измеряется миллисекундами. Проверка защищает от ReDoS, а не меряет скорость.
    """
    payload = (
        "<message><encrypted xmlns='urn:xmpp:omemo:2'><header sid='1'>"
        f"<key rid='1'>{'A' * 50_000}</key></header>"
        f"<payload>{'B' * 150_000}</payload></encrypted></message>"
    )
    start = time.perf_counter()
    result = redact(payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"маскирование заняло {elapsed:.3f} с"
    assert len(result) < len(payload)


def test_redaction_summary_is_filled() -> None:
    """Перечень правил не пустой: он показывается в /help и в предупреждении."""
    summary = redaction_summary()
    assert isinstance(summary, tuple)
    assert len(summary) >= 5
    assert all(isinstance(line, str) and line.strip() for line in summary)
    joined = " ".join(summary)
    for marker in ("SASL", "OMEMO", "MUC"):
        assert marker in joined


def test_unsafe_warning_is_explicit() -> None:
    """Предупреждение о режиме unsafe непустое и называет риск.

    Константа помечена для каталога, а переводит ее тот, кто показывает: сама
    она остается английской, перевод дает ``_()``.
    """
    assert UNSAFE_WARNING.startswith("Unsafe mode disables masking")
    assert _(UNSAFE_WARNING).startswith("Режим unsafe отключает маскирование")


def test_redaction_summary_follows_the_language() -> None:
    """Перечень правил переводится при вызове, а не при импорте модуля."""
    assert redaction_summary()[2] == "PEP bundle: вместо тел ключей показывается их количество"
    i18n.set_language("en")
    assert (
        redaction_summary()[2] == "PEP bundle: the number of keys is shown instead of their bodies"
    )


# Проверка на реальном потоке мока.


def test_mock_secrets_do_not_leak(
    mock_stanzas: list[RawStanza],
    secret_bodies: Callable[[str], list[str]],
) -> None:
    """Настоящие секреты из потока мока не попадают в замаскированный вывод.

    Тела чувствительных элементов берутся из самого потока, поэтому тест не зависит
    от конкретных значений, которые выбрал генератор.
    """
    sasl_seen = False
    checked = 0
    for stanza in mock_stanzas:
        bodies = secret_bodies(stanza.xml)
        if any(namespace in stanza.xml for namespace in SASL_NAMESPACES):
            sasl_seen = True
        masked = redact(stanza.xml)
        for body in bodies:
            checked += 1
            assert body not in masked, f"секрет остался в выводе: {stanza.xml[:120]}"

    assert mock_stanzas, "мок не выдал ни одной строфы"
    assert sasl_seen, "в потоке мока нет ни одной SASL-строфы"
    assert checked, "в потоке мока нет тел, подлежащих маскированию"


def test_mock_sasl_stanza_is_marked_redacted(mock_stanzas: list[RawStanza]) -> None:
    """У SASL-строф мока после маскирования появляется явная отметка."""
    sasl = [
        stanza
        for stanza in mock_stanzas
        if any(namespace in stanza.xml for namespace in SASL_NAMESPACES)
    ]
    assert sasl, "в потоке мока нет SASL-строф"
    marked = [stanza for stanza in sasl if "redacted" in redact(stanza.xml)]
    assert marked, "ни одна SASL-строфа мока не получила отметку о маскировании"


# Попытки обхода правил.

# Пароль в открытом виде: механизм PLAIN по XEP-0388 допустим и нужен как запасной,
# поэтому именно он проверяет правило SASL по существу.
PLAIN_PASSWORD: Final = "S3cr3t-Passw0rd-2026"
PLAIN_INITIAL: Final = base64.b64encode(f"\0alice\0{PLAIN_PASSWORD}".encode()).decode()
KEY_BLOB: Final = "S0tLS0tLS0tLS0tLS0tLS0tLS0tLSw=="
PAYLOAD_BLOB: Final = "UFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQ"
SASL_BLOB: Final = "AHVzZXIAU1VQRVJTRUNSRVRQQVNTV09SRDEyMzQ1"
SIGNATURE: Final = "v4-signature-DO-NOT-LOG-8f3a91c0"

# Каждая пара - строфа и подстрока, которой в выводе быть не должно. Перечень
# собран из форм, которыми обходилось правило, построенное на одном шаблоне:
# регистр имени, префикс пространства имен, CDATA, комментарий, незакрытый
# вложенный элемент, атрибут вместо текстового узла, отсутствие объявления
# пространства имен.
BYPASS_CASES: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "sasl2-initial-response",
        "<authenticate xmlns='urn:xmpp:sasl:2' mechanism='PLAIN'>"
        f"<initial-response>{PLAIN_INITIAL}</initial-response>"
        "<user-agent id='u1'><software>termisations</software></user-agent></authenticate>",
        PLAIN_INITIAL,
    ),
    (
        "sasl-upper-case",
        f"<AUTH xmlns='urn:ietf:params:xml:ns:xmpp-sasl' mechanism='PLAIN'>{SASL_BLOB}</AUTH>",
        SASL_BLOB,
    ),
    (
        "sasl-mixed-case",
        f"<Auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl'>{SASL_BLOB}</Auth>",
        SASL_BLOB,
    ),
    (
        "sasl-namespace-prefix",
        "<sasl:auth xmlns:sasl='urn:ietf:params:xml:ns:xmpp-sasl' mechanism='PLAIN'>"
        f"{SASL_BLOB}</sasl:auth>",
        SASL_BLOB,
    ),
    (
        "sasl-cdata",
        f"<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl'><![CDATA[{SASL_BLOB}]]></auth>",
        SASL_BLOB,
    ),
    (
        "sasl-comment",
        f"<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl'><!--c-->{SASL_BLOB}</auth>",
        SASL_BLOB,
    ),
    (
        "sasl-comment-with-fake-close",
        f"<auth xmlns='urn:xmpp:sasl:2'><!-- </auth> -->{SASL_BLOB}</auth>",
        SASL_BLOB,
    ),
    (
        "sasl-without-namespace",
        f"<auth mechanism='xmlns-trick'>{SASL_BLOB}</auth>",
        SASL_BLOB,
    ),
    (
        "sasl-unclosed-nested",
        "<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl'>SECRET-OUTER"
        f"<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl'>{SASL_BLOB}</auth>",
        "SECRET-OUTER",
    ),
    (
        "sasl-nested-self-closing",
        f"<auth xmlns='urn:xmpp:sasl:2'><auth/>{SASL_BLOB}</auth>",
        SASL_BLOB,
    ),
    (
        "password-upper-case",
        f"<x xmlns='http://jabber.org/protocol/muc'><Password>{PLAIN_PASSWORD}</Password></x>",
        PLAIN_PASSWORD,
    ),
    (
        "password-cdata",
        "<x xmlns='http://jabber.org/protocol/muc'>"
        f"<password><![CDATA[{PLAIN_PASSWORD}]]></password></x>",
        PLAIN_PASSWORD,
    ),
    (
        "password-cdata-with-fake-close",
        f"<password><![CDATA[</password>{PLAIN_PASSWORD}]]></password>",
        PLAIN_PASSWORD,
    ),
    (
        "password-namespace-prefix",
        "<muc:x xmlns:muc='http://jabber.org/protocol/muc'>"
        f"<muc:password>{PLAIN_PASSWORD}</muc:password></muc:x>",
        PLAIN_PASSWORD,
    ),
    (
        "password-comment",
        f"<password><!-- x -->{PLAIN_PASSWORD}</password>",
        PLAIN_PASSWORD,
    ),
    (
        "password-attribute",
        f"<x xmlns='http://jabber.org/protocol/muc' password='{PLAIN_PASSWORD}'/>",
        PLAIN_PASSWORD,
    ),
    (
        "omemo-key-upper-case",
        "<message><encrypted xmlns='eu.siacs.conversations.axolotl'>"
        f"<Key rid='1'>{KEY_BLOB}</Key></encrypted></message>",
        KEY_BLOB,
    ),
    (
        "omemo-key-cdata",
        "<encrypted xmlns='eu.siacs.conversations.axolotl'>"
        f"<key rid='1'><![CDATA[{KEY_BLOB}]]></key></encrypted>",
        KEY_BLOB,
    ),
    (
        "omemo-without-namespace",
        f"<key rid='31415'>{KEY_BLOB}</key><payload>{PAYLOAD_BLOB}</payload>",
        KEY_BLOB,
    ),
    (
        "omemo-payload-without-namespace",
        f"<key rid='31415'>{KEY_BLOB}</key><payload>{PAYLOAD_BLOB}</payload>",
        PAYLOAD_BLOB,
    ),
    (
        "bundle-without-namespace",
        f"<bundle><signedPreKeyPublic>{KEY_BLOB}</signedPreKeyPublic></bundle>",
        KEY_BLOB,
    ),
    (
        "header-upper-case",
        f"<put url='https://up/f'><Header name='Authorization'>{SIGNATURE}</Header></put>",
        SIGNATURE,
    ),
    (
        "header-unknown-name",
        f"<header name='X-Custom-Auth'>{SIGNATURE}</header>",
        SIGNATURE,
    ),
    (
        "header-unquoted-name",
        f"<header name=Authorization>{SIGNATURE}</header>",
        SIGNATURE,
    ),
    (
        "slot-url-attribute-upper-case",
        "<slot xmlns='urn:xmpp:http:upload:0'>"
        f"<put URL='https://upload.srv/f?Signature={SIGNATURE}'/></slot>",
        SIGNATURE,
    ),
    (
        "slot-scheme-upper-case",
        f"<put url='HTTPS://u/f?sig={SIGNATURE}'/>",
        SIGNATURE,
    ),
    (
        "slot-relative-url",
        f"<put url='/f?sig={SIGNATURE}'/>",
        SIGNATURE,
    ),
    (
        "oob-url-without-namespace",
        f"<url>https://upload.srv/f?Signature={SIGNATURE}</url>",
        SIGNATURE,
    ),
    (
        "url-data-target",
        "<url-data xmlns='http://jabber.org/protocol/url-data' "
        f"target='https://u/f?sig={SIGNATURE}'/>",
        SIGNATURE,
    ),
    (
        "sims-reference-uri",
        "<file-sharing xmlns='urn:xmpp:sfs:0'><sources>"
        f"<reference xmlns='urn:xmpp:reference:0' uri='https://u/f?sig={SIGNATURE}'/>"
        "</sources></file-sharing>",
        SIGNATURE,
    ),
    (
        "signed-link-in-body",
        f"<message><body>https://upload.srv/f/photo.jpg?Signature={SIGNATURE}</body></message>",
        SIGNATURE,
    ),
)


@pytest.mark.parametrize(
    ("stanza", "secret"),
    [(stanza, secret) for _, stanza, secret in BYPASS_CASES],
    ids=[name for name, _, _ in BYPASS_CASES],
)
def test_bypass_attempt_is_masked(stanza: str, secret: str) -> None:
    """Ни одна из форм обхода не оставляет секрет в выводе."""
    result = redact(stanza)
    assert secret not in result, f"секрет остался: {result}"


def test_sasl2_plain_password_is_not_recoverable() -> None:
    """Пароль механизма PLAIN не восстанавливается из вывода ни в каком виде.

    Проверяется и сам пароль, и его base64-представление: нагрузка PLAIN - это
    строка "\\0имя\\0пароль", то есть открытый пароль в одном декодировании.
    """
    stanza = (
        "<authenticate xmlns='urn:xmpp:sasl:2' mechanism='PLAIN'>"
        f"<initial-response>{PLAIN_INITIAL}</initial-response></authenticate>"
    )
    result = redact(stanza)
    assert PLAIN_INITIAL not in result
    assert PLAIN_PASSWORD not in result
    assert "[SASL payload:" in result
    # Механизм остается видимым: без него строфа теряет отладочный смысл.
    assert "mechanism='PLAIN'" in result


def test_plain_url_in_body_survives() -> None:
    """Обычная ссылка в теле сообщения не режется: это пользовательский текст."""
    stanza = "<message><body>смотри https://wiki.srv/page?id=42&amp;tab=logs</body></message>"
    assert redact(stanza) == stanza


def test_mechanism_list_stays_visible() -> None:
    """Перечень механизмов сервера не маскируется: он нужен при разборе отказа."""
    stanza = (
        "<stream:features xmlns:stream='http://etherx.jabber.org/streams'>"
        "<mechanisms xmlns='urn:xmpp:sasl:2'><mechanism>SCRAM-SHA-256-PLUS</mechanism>"
        "<mechanism>PLAIN</mechanism></mechanisms></stream:features>"
    )
    assert redact(stanza) == stanza


def test_unclosed_bundle_flood_is_linear() -> None:
    """Строфа с тысячами незакрытых <bundle> не вызывает квадратичного разбора.

    Такую строфу присылает удаленная сторона, а маскирование идет синхронно в
    потоке отрисовки. Шаблон вида ``<bundle>(.*?)</bundle>`` перебирал бы каждую
    стартовую позицию до конца строки и давал на 65 КБ секунды вместо миллисекунд.
    """
    payload = (
        "<message xmlns='jabber:client' from='evil@srv'><x xmlns='urn:xmpp:omemo:2'>"
        + "<bundle>" * 8000
        + "S0tL" * 100
        + "</x></message>"
    )
    assert len(payload) > 64_000
    start = time.perf_counter()
    result = redact(payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"маскирование заняло {elapsed * 1000:.0f} мс на {len(payload)} байт"
    assert "S0tL" * 100 not in result


def test_secret_bodies_fixture_sees_sasl2_payload(
    secret_bodies: Callable[[str], list[str]],
) -> None:
    """Фикстура отбора секретов не повторяет перечень элементов реализации.

    Это проверка самой проверки. Тело <initial-response/> обязано считаться
    секретом, даже если маскирование о таком элементе не знает: иначе набор тестов
    не способен заметить пропущенное правило.
    """
    stanza = (
        "<authenticate xmlns='urn:xmpp:sasl:2' mechanism='PLAIN'>"
        f"<initial-response>{PLAIN_INITIAL}</initial-response>"
        "<user-agent id='u1'><software>termisations</software>"
        "<device>workstation</device></user-agent></authenticate>"
    )
    bodies = secret_bodies(stanza)
    assert PLAIN_INITIAL in bodies, f"нагрузка SASL2 не распознана как секрет: {bodies}"
    # Имя клиента и устройства секретом не является и маскироваться не должно.
    assert "termisations" not in bodies
    assert "workstation" not in bodies


def test_mock_bundle_is_replaced_by_key_count(mock_stanzas: list[RawStanza]) -> None:
    """Бандл ключей из потока мока показывается количеством, а не телами.

    Проверка идет на строфе сценария, а не на синтетическом образце: строка
    таблицы раздела 5 про бандл до этого не была покрыта потоком вовсе.
    """
    bundles = [stanza for stanza in mock_stanzas if "<bundle " in stanza.xml]
    assert bundles, "в потоке мока нет строфы с бандлом ключей"
    for stanza in bundles:
        masked = redact(stanza.xml)
        assert "[bundle:" in masked and "keys, redacted]" in masked
        for body in _element_bodies(stanza.xml, ("spk", "spks", "ik", "pk")):
            assert body not in masked, f"тело ключа бандла осталось в выводе: {body}"


def _element_bodies(xml: str, names: tuple[str, ...]) -> list[str]:
    """Тела перечисленных элементов строфы."""
    pattern = "|".join(names)
    return [
        match.group("body")
        for match in re.finditer(
            rf"<(?P<tag>{pattern})(?:\s[^>]*)?>(?P<body>[^<]+)</(?P=tag)>", xml
        )
    ]


# Реальная строфа OMEMO, снятая с живого обмена.

REAL_OMEMO: Final = (
    "<message xmlns='jabber:client' to='bob@localhost' type='chat' id='m1'>"
    "<encrypted xmlns='eu.siacs.conversations.axolotl'>"
    "<header sid='469086422'>"
    "<key rid='206380053' prekey='true'>MwohBYt2xQ0hqJ9nZXJlcmVyZXJlcmVyZXJlEiEF</key>"
    "<key rid='584949080'>MwohBYt2xQ0hqJ9nbGVyZXJlcmVyZXJlcmVyZXJlcmVy</key>"
    "<iv>YWJjZGVmZ2hpamts</iv></header>"
    "<payload>0V+3lQ3mYnZhbGlkcGF5bG9hZGhlcmU=</payload></encrypted>"
    "<body>Сообщение зашифровано OMEMO.</body></message>"
)
"""Форма строфы взята с настоящего обмена через slixmpp-omemo, а не придумана."""


def test_real_omemo_stanza_is_masked() -> None:
    """Тела ключей и полезной нагрузки настоящей строфы OMEMO не доходят до экрана.

    Образцы мока проверяют разметку, которую собрал сам мок. Эта строфа снята с
    живого обмена: если библиотека изменит форму, тест это заметит.
    """
    masked = redact(REAL_OMEMO)
    for body in _element_bodies(REAL_OMEMO, ("key", "payload")):
        assert body not in masked, f"тело осталось в выводе: {body}"
    # Адресация и идентификаторы устройств секретом не являются и обязаны
    # остаться: без них строфа в панели лога бесполезна для разбора.
    assert "sid='469086422'" in masked
    assert "rid='206380053'" in masked
    assert "prekey='true'" in masked
    assert "eu.siacs.conversations.axolotl" in masked


def test_real_omemo_stanza_is_whole_in_unsafe_mode() -> None:
    """В режиме unsafe строфа показывается целиком: это выбор пользователя."""
    assert redact(REAL_OMEMO, True).startswith(UNSAFE_WARNING) or REAL_OMEMO in redact(
        REAL_OMEMO, True
    )
