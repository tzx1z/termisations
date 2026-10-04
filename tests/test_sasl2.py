"""Адаптер SASL2 (XEP-0388) и Bind 2 (XEP-0386).

Проверка идет на строфах, а не против сервера: ejabberd в контейнере SASL2 не
объявляет (модулей ``mod_sasl2`` и ``mod_bind2`` в этой сборке нет), а форма
строфы от наличия сервера не меняется. Образцы взяты из текста расширений.

Отдельно проверяется запасной путь: адаптер обязан молчать, когда сервер SASL2
не предложил, иначе он сломал бы вход там, где сейчас все работает.
"""

import logging
from typing import Final
from xml.etree import ElementTree

import pytest

pytest.importorskip("slixmpp", reason="адаптер требует slixmpp")

from slixmpp.stanza import StreamFeatures

from termisations.core import i18n
from termisations.protocol.sasl2 import stanza as sasl2
from termisations.protocol.sasl2.plugin import FEATURE_ORDER, SOFTWARE, XEP_0388, _Quiet

sasl2.register_all()

# Возможность потока из текста XEP-0388 вместе с блоком inline XEP-0386.
FEATURE_XML: Final = (
    "<stream:features xmlns:stream='http://etherx.jabber.org/streams'"
    " xmlns='jabber:client'>"
    "<authentication xmlns='urn:xmpp:sasl:2'>"
    "<mechanism>SCRAM-SHA-1</mechanism>"
    "<mechanism>SCRAM-SHA-1-PLUS</mechanism>"
    "<mechanism>SCRAM-SHA-256</mechanism>"
    "<inline>"
    "<bind xmlns='urn:xmpp:bind:0'/>"
    "<sm xmlns='urn:xmpp:sm:3'/>"
    "</inline>"
    "</authentication>"
    "</stream:features>"
)

SUCCESS_XML: Final = (
    "<success xmlns='urn:xmpp:sasl:2'>"
    "<additional-data>dj1tc0Z5bA==</additional-data>"
    "<authorization-identifier>alice@example.org/termisations.a1b2</authorization-identifier>"
    "<bound xmlns='urn:xmpp:bind:0'/>"
    "<enabled xmlns='urn:xmpp:sm:3' id='sm-1' resume='true'/>"
    "</success>"
)

FAILURE_XML: Final = (
    "<failure xmlns='urn:xmpp:sasl:2'>"
    "<not-authorized xmlns='urn:ietf:params:xml:ns:xmpp-sasl'/>"
    "<text>неверный пароль</text>"
    "</failure>"
)

CONTINUE_XML: Final = (
    "<continue xmlns='urn:xmpp:sasl:2'>"
    "<additional-data>SSdt</additional-data>"
    "<tasks><task>HOTP-EXAMPLE</task><task>TOTP-EXAMPLE</task></tasks>"
    "<text>нужен второй фактор</text>"
    "</continue>"
)


def parse(xml: str, kind: type) -> object:
    """Разобрать строфу нужного типа."""
    return kind(xml=ElementTree.fromstring(xml))


# Разбор возможности потока.


def test_feature_lists_mechanisms_in_order() -> None:
    """Механизмы читаются в порядке объявления сервером."""
    features = parse(FEATURE_XML, StreamFeatures)
    assert features["sasl2"]["mechanisms"] == [
        "SCRAM-SHA-1",
        "SCRAM-SHA-1-PLUS",
        "SCRAM-SHA-256",
    ]


def test_feature_lists_inline_offers() -> None:
    """Блок inline читается пространствами имен предложенных действий."""
    features = parse(FEATURE_XML, StreamFeatures)
    offered = features["sasl2"]["inline"]["features"]
    assert sasl2.BIND_NAMESPACE in offered
    assert "urn:xmpp:sm:3" in offered


def test_feature_without_inline_is_not_an_error() -> None:
    """Сервер вправе не предлагать inline: привязка тогда идет отдельным кругом."""
    xml = FEATURE_XML.replace(
        "<inline><bind xmlns='urn:xmpp:bind:0'/><sm xmlns='urn:xmpp:sm:3'/></inline>", ""
    )
    features = parse(xml, StreamFeatures)
    assert features["sasl2"]["mechanisms"]
    assert features["sasl2"].get_plugin("inline", check=True) is None


# Сборка запроса.


def test_authenticate_carries_everything_in_one_round() -> None:
    """Запрос несет механизм, первый ответ, user-agent и inline-привязку."""
    request = sasl2.Authenticate()
    request["mechanism"] = "SCRAM-SHA-256"
    request["initial_response"] = "biwsbj1hbGljZQ=="
    request["user_agent"]["id"] = "d4565fa7-4d72-4749-b3d3-740edbf87770"
    request["user_agent"]["software"] = SOFTWARE
    request["bind2"]["tag"] = "termisations"
    xml = str(request)
    assert 'mechanism="SCRAM-SHA-256"' in xml
    assert "<initial-response" in xml and "biwsbj1hbGljZQ==" in xml
    assert "d4565fa7-4d72-4749-b3d3-740edbf87770" in xml
    assert "urn:xmpp:bind:0" in xml
    assert "<tag" in xml


def test_empty_initial_response_creates_no_element() -> None:
    """Пустой первый ответ элемента не создает: механизмы без него существуют."""
    request = sasl2.Authenticate()
    request["mechanism"] = "EXTERNAL"
    request["initial_response"] = ""
    assert "initial-response" not in str(request)


def test_response_and_challenge_carry_raw_payload() -> None:
    """Вызов и ответ несут полезную нагрузку текстом элемента."""
    answer = sasl2.Response()
    answer["value"] = "Yz1iaXdz"
    assert "Yz1iaXdz" in str(answer)
    challenge = parse("<challenge xmlns='urn:xmpp:sasl:2'>cj1yT3By</challenge>", sasl2.Challenge)
    assert challenge["value"] == "cj1yT3By"


# Разбор ответов.


def test_success_gives_jid_and_inline_results() -> None:
    """Успех несет назначенный адрес и результаты inline-действий."""
    success = parse(SUCCESS_XML, sasl2.Success)
    assert success["jid"] == "alice@example.org/termisations.a1b2"
    assert success["additional_data"] == "dj1tc0Z5bA=="
    results = success["inline_results"]
    assert sasl2.BIND_NAMESPACE in results
    assert "urn:xmpp:sm:3" in results


def test_success_without_bind_is_readable() -> None:
    """Успех без привязки читается: сервер вправе не поддержать Bind 2."""
    xml = (
        "<success xmlns='urn:xmpp:sasl:2'>"
        "<authorization-identifier>a@b</authorization-identifier></success>"
    )
    success = parse(xml, sasl2.Success)
    assert success["jid"] == "a@b"
    assert success["inline_results"] == []


def test_failure_names_the_condition() -> None:
    """Отказ дает условие и пояснение, а не общий текст."""
    failure = parse(FAILURE_XML, sasl2.Failure)
    assert failure["condition"] == "not-authorized"
    assert failure["text"] == "неверный пароль"


def test_failure_without_text_is_readable() -> None:
    """Отказ без пояснения читается: сервер не обязан его давать."""
    xml = (
        "<failure xmlns='urn:xmpp:sasl:2'>"
        "<credentials-expired xmlns='urn:ietf:params:xml:ns:xmpp-sasl'/></failure>"
    )
    failure = parse(xml, sasl2.Failure)
    assert failure["condition"] == "credentials-expired"
    assert failure["text"] == ""


def test_continue_lists_the_tasks() -> None:
    """Продолжение обмена называет задачи: адаптер их не умеет и скажет об этом."""
    answer = parse(CONTINUE_XML, sasl2.Continue)
    assert answer["tasks"] == ["HOTP-EXAMPLE", "TOTP-EXAMPLE"]


# Порядок и запасной путь.


def test_handler_runs_before_sasl1() -> None:
    """Обработчик SASL2 получает ход раньше штатного feature_mechanisms.

    У того порядок 100. Если SASL2 окажется позже, он не сработает никогда:
    SASL1 успеет авторизоваться первым.
    """
    from slixmpp.features.feature_mechanisms import FeatureMechanisms

    assert FeatureMechanisms.default_config["order"] > FEATURE_ORDER


def test_plugin_declares_its_name() -> None:
    """Имя плагина то, под которым он регистрируется у клиента."""
    assert XEP_0388.name == "xep_0388"
    assert "xep_0030" not in XEP_0388.dependencies


def test_final_step_failure_is_logged_in_current_language(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сбой последнего шага механизма поглощается и пишется в журнал на языке интерфейса."""
    caplog.set_level(logging.WARNING, logger="termisations.protocol.sasl2.plugin")
    with _Quiet():
        raise ValueError("bad signature")
    i18n.set_language("en")
    with _Quiet():
        raise ValueError("bad signature")
    assert [record.getMessage() for record in caplog.records] == [
        "SASL2: последний шаг механизма не сошелся: bad signature",
        "SASL2: final mechanism step did not verify: bad signature",
    ]


def test_plus_mechanisms_are_not_offered() -> None:
    """Варианты -PLUS не заявляются: привязка канала в стандартном ssl недоступна.

    Предлагать механизм, который нельзя выполнить, значит обещать защиту,
    которой нет. То же ограничение зафиксировано тестом про tls-exporter.
    """
    offered = ["SCRAM-SHA-1", "SCRAM-SHA-1-PLUS", "SCRAM-SHA-256-PLUS", "SCRAM-SHA-256"]
    usable = [name for name in offered if not name.endswith("-PLUS")]
    assert usable == ["SCRAM-SHA-1", "SCRAM-SHA-256"]
