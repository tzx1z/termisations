"""Реестр слэш-команд: состав, разбор, автодополнение, справка.

Проверяется реестр команд. Реестр - это данные, поэтому тесты проходят по всему
реестру целиком, а не по выборочным командам.
"""

from typing import Final

import pytest

from termisations.core import i18n
from termisations.core.commands import (
    REGISTRY,
    XML_FILTER_FIELDS,
    CommandSpec,
    CompletionContext,
    complete,
    find,
    help_text,
    parse,
)
from termisations.core.i18n import LANGUAGES, _
from termisations.core.redact import redaction_summary
from termisations.xeps import UNKNOWN_XEP, normalize_number, xep_info

# Ожидаемый набор команд. Подкоманды (/xml filter, /log save) живут в поле subcommands
# своей команды, поэтому в списке имен их нет.
EXPECTED_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "connect",
        "disconnect",
        "reconnect",
        "account",
        "presence",
        "chat",
        "join",
        "leave",
        "close",
        "topic",
        "nick",
        "clear",
        "roster",
        "add",
        "remove",
        "sub",
        "unsub",
        "block",
        "unblock",
        "xml",
        "debug",
        "send",
        "iq",
        "ping",
        "disco",
        "caps",
        "features",
        "sm",
        "mam",
        "trace",
        "tls",
        "stats",
        "omemo",
        "ox",
        "upload",
        "slot",
        "help",
        "keys",
        "theme",
        "log",
        "quit",
    }
)

EXPECTED_ALIASES: Final[dict[str, str]] = {
    "q": "quit",
    "j": "join",
    "msg": "chat",
    "?": "help",
    "part": "leave",
}

EXPECTED_GROUPS: Final[frozenset[str]] = frozenset(
    {"connection", "conversation", "roster", "debug", "crypto", "files", "system"}
)

CONTEXT: Final = CompletionContext(
    conversation="bob@srv",
    roster=("alice@srv", "bob@srv", "carol@srv"),
    conversations=("bob@srv", "devops@conf.srv"),
    nicks=("admin", "deploy-bot"),
    online=frozenset({"bob@srv"}),
)


def _tail(candidate: str) -> str:
    """Последний токен подстановки без ведущего слэша.

    Контракт не фиксирует, отдает ли complete всю строку ("/omemo trust") или только
    последний токен, поэтому сравнение идет по хвосту.
    """
    return candidate.rsplit(" ", 1)[-1].lstrip("/")


def _tails(candidates: list[str]) -> list[str]:
    return [_tail(candidate) for candidate in candidates]


# Состав реестра.


def test_registry_covers_all_commands() -> None:
    """В реестре есть все ожидаемые команды."""
    names = {spec.name for spec in REGISTRY}
    assert names >= EXPECTED_COMMANDS, f"нет команд: {sorted(EXPECTED_COMMANDS - names)}"


def test_registry_names_are_unique() -> None:
    """Имена, алиасы и ключи обработчиков не пересекаются."""
    names = [spec.name for spec in REGISTRY]
    assert len(names) == len(set(names))

    handlers = [spec.handler_key for spec in REGISTRY]
    assert len(handlers) == len(set(handlers))

    tokens: list[str] = []
    for spec in REGISTRY:
        tokens.append(spec.name)
        tokens.extend(spec.aliases)
    assert len(tokens) == len(set(tokens)), "алиас совпадает с другим именем"


@pytest.mark.parametrize(("alias", "name"), sorted(EXPECTED_ALIASES.items()))
def test_aliases_resolve(alias: str, name: str) -> None:
    """Алиасы ведут на свою команду."""
    spec = find(alias)
    assert spec is not None, f"алиас /{alias} не найден"
    assert spec.name == name


@pytest.mark.parametrize("spec", REGISTRY, ids=lambda item: item.name)
def test_spec_is_filled(spec: CommandSpec) -> None:
    """Каждая запись реестра заполнена: usage, summary, handler_key, группа."""
    assert spec.name and not spec.name.startswith("/")
    assert spec.usage.startswith(f"/{spec.name}"), f"usage не совпадает с именем: {spec.usage}"
    assert spec.summary.strip()
    assert spec.handler_key.strip()
    assert spec.group in EXPECTED_GROUPS, f"неизвестная группа: {spec.group}"
    assert spec.min_args >= 0
    if spec.max_args is not None:
        assert spec.max_args >= spec.min_args


@pytest.mark.parametrize("spec", REGISTRY, ids=lambda item: item.name)
def test_spec_xeps_are_known(spec: CommandSpec) -> None:
    """Номера расширений в реестре нормализованы и есть в справочнике."""
    for number in spec.xeps:
        assert number == normalize_number(number), f"ненормализованный номер: {number}"
        assert xep_info(number).title != UNKNOWN_XEP.title, f"номер вне справочника: {number}"


def test_subcommands_cover_two_word_commands() -> None:
    """Двухсловные команды оформлены подкомандами, а не отдельными записями."""
    xml_spec = find("xml")
    assert xml_spec is not None
    assert "filter" in xml_spec.subcommands

    log_spec = find("log")
    assert log_spec is not None
    assert "save" in log_spec.subcommands

    omemo_spec = find("omemo")
    assert omemo_spec is not None
    assert {"status", "enable", "disable", "fingerprints", "trust", "distrust"} <= set(
        omemo_spec.subcommands
    )


# Разбор.


@pytest.mark.parametrize("line", ["", "   ", "\t", "привет", "просто текст", "//help", "//"])
def test_not_a_command(line: str) -> None:
    """Пустая строка, обычный текст и экранирование двойным слэшем командой не являются."""
    assert parse(line) is None


def test_unknown_command_reports_name() -> None:
    """Неизвестная команда дает ошибку с именем, а не общий текст."""
    result = parse("/nosuchcommand")
    assert result is not None
    assert result.spec is None
    assert not result.ok
    assert result.error is not None
    assert "неизвестная команда" in result.error
    assert "/nosuchcommand" in result.error


def test_missing_argument_reports_usage() -> None:
    """Нехватка аргумента дает ожидаемую сигнатуру из usage."""
    spec = find("chat")
    assert spec is not None

    result = parse("/chat")
    assert result is not None
    assert result.spec is spec
    assert not result.ok
    assert result.error is not None
    assert spec.usage in result.error


@pytest.mark.parametrize(
    "spec", [item for item in REGISTRY if item.min_args > 0], ids=lambda item: item.name
)
def test_every_required_argument_reports_usage(spec: CommandSpec) -> None:
    """Все команды с обязательными аргументами сообщают свою сигнатуру."""
    result = parse(f"/{spec.name}")
    assert result is not None
    assert result.error is not None
    assert _(spec.usage) in result.error


def test_valid_command_parsed() -> None:
    """Корректная команда разбирается без ошибки, аргументы сохраняются."""
    result = parse("/chat bob@srv")
    assert result is not None
    assert result.ok
    assert result.error is None
    assert result.spec is not None
    assert result.spec.name == "chat"
    assert result.args == ("bob@srv",)


def test_boolean_flag() -> None:
    """Флаг без значения дает True."""
    result = parse("/roster --raw --groups")
    assert result is not None
    assert result.ok
    assert result.flags["raw"] is True
    assert result.flags["groups"] is True


def test_flag_with_equals() -> None:
    """Форма --flag=value дает строковое значение."""
    result = parse("/mam bob@srv --limit=20")
    assert result is not None
    assert result.ok
    assert result.args == ("bob@srv",)
    assert result.flags["limit"] == "20"


def test_value_flag_takes_next_token() -> None:
    """Флаг из value_flags забирает следующий токен и не попадает в аргументы."""
    spec = find("mam")
    assert spec is not None
    assert "limit" in spec.value_flags

    result = parse("/mam bob@srv --limit 20 --before id-42")
    assert result is not None
    assert result.ok
    assert result.args == ("bob@srv",)
    assert result.flags["limit"] == "20"
    assert result.flags["before"] == "id-42"


def test_quoted_argument_stays_whole() -> None:
    """Аргумент в кавычках не разбивается по пробелам."""
    result = parse('/presence away "перерыв на обед"')
    assert result is not None
    assert result.ok
    assert result.args == ("away", "перерыв на обед")


def test_single_quotes_supported() -> None:
    """Одинарные кавычки работают так же, как двойные."""
    result = parse("/topic 'утренний стендап'")
    assert result is not None
    assert result.ok
    assert result.args[0] == "утренний стендап"


def test_alias_parsing() -> None:
    """Команда по алиасу разбирается в ту же запись реестра."""
    result = parse("/j devops@conf.srv")
    assert result is not None
    assert result.ok
    assert result.spec is not None
    assert result.spec.name == "join"


def test_unsafe_flag_on_xml() -> None:
    """Флаг --unsafe у /xml распознается как булев."""
    result = parse("/xml both --unsafe")
    assert result is not None
    assert result.ok
    assert result.args == ("both",)
    assert result.flags["unsafe"] is True


# Автодополнение.


def test_complete_command_prefix() -> None:
    """Префикс дополняется до имени команды."""
    tails = _tails(complete("/he", CONTEXT))
    assert "help" in tails
    assert "chat" not in tails


def test_complete_lists_commands_on_slash() -> None:
    """Один слэш дает список команд."""
    tails = set(_tails(complete("/", CONTEXT)))
    assert {"help", "quit", "xml"} <= tails


def test_complete_subcommands() -> None:
    """После команды с подкомандами дополняются подкоманды."""
    tails = _tails(complete("/omemo ", CONTEXT))
    assert {"status", "fingerprints", "trust"} <= set(tails)

    partial = _tails(complete("/omemo fing", CONTEXT))
    assert partial == ["fingerprints"]


def test_complete_xml_subcommands() -> None:
    """У /xml дополняются и режимы, и подкоманда filter."""
    tails = set(_tails(complete("/xml ", CONTEXT)))
    assert "filter" in tails


def test_complete_filter_fields() -> None:
    """После /xml filter дополняются поля выражения фильтра.

    Язык фильтра нигде не подсказывался, и опечатка в имени поля давала молча
    пустую панель. Набор полей один на реестр и на разбор в панели.
    """
    tails = _tails(complete("/xml filter ", CONTEXT))
    assert tails == [f"{field}:" for field in XML_FILTER_FIELDS]

    partial = _tails(complete("/xml filter k", CONTEXT))
    assert partial == ["kind:"]


def test_complete_filter_fields_after_first_term() -> None:
    """Второе условие фильтра дополняется так же, как первое: хвост - один аргумент."""
    variants = complete("/xml filter kind:iq j", CONTEXT)
    assert variants == ["/xml filter kind:iq jid:"]


def test_complete_jid_from_roster() -> None:
    """После /chat дополняются JID из roster."""
    tails = _tails(complete("/chat ", CONTEXT))
    assert "bob@srv" in tails
    assert "alice@srv" in tails


def test_complete_jid_prefix_filters() -> None:
    """Частичный JID сужает список."""
    tails = _tails(complete("/chat ali", CONTEXT))
    assert tails == ["alice@srv"]


def test_complete_online_contacts_first() -> None:
    """Контакты в сети идут раньше остальных."""
    tails = _tails(complete("/chat ", CONTEXT))
    assert "bob@srv" in tails
    assert "alice@srv" in tails
    assert tails.index("bob@srv") < tails.index("alice@srv")


def test_complete_unknown_command_gives_nothing() -> None:
    """Для неизвестной команды подстановок нет, исключения тоже."""
    assert complete("/zzz", CONTEXT) == []
    assert complete("/zzz ", CONTEXT) == []


def test_complete_plain_text_gives_nothing() -> None:
    """Обычный текст не дополняется как команда."""
    assert complete("привет", CONTEXT) == []
    assert complete("//help", CONTEXT) == []


def test_complete_is_deterministic() -> None:
    """Два одинаковых вызова дают одинаковый порядок."""
    assert complete("/chat ", CONTEXT) == complete("/chat ", CONTEXT)


# Справка.


@pytest.mark.parametrize("spec", REGISTRY, ids=lambda item: item.name)
def test_help_for_each_command(spec: CommandSpec) -> None:
    """Справка по каждой команде непустая и содержит сигнатуру."""
    text = help_text(spec.name)
    assert text.strip(), f"пустая справка для /{spec.name}"
    assert _(spec.usage) in text
    assert _(spec.summary) in text


def test_help_overview_lists_registry() -> None:
    """Общая справка перечисляет команды и помещается на один экран.

    Правила маскирования из сводки вынесены в отдельную тему: со списком правил
    сводка занимала 66 строк, и на экране от нее оставался только хвост.
    """
    text = help_text(None)
    assert text.strip()
    for name in ("help", "quit", "xml", "omemo"):
        assert f"/{name}" in text
    assert len(text.splitlines()) <= 24, "сводка справки не помещается на экран"
    assert "/help redact" in text


def test_help_redact_lists_rules() -> None:
    """Правила маскирования доступны отдельной темой справки."""
    text = help_text("redact")
    assert redaction_summary()[0] in text


def test_help_group_expands_commands() -> None:
    """Имя группы раскрывает ее команды вместе с описаниями."""
    text = help_text("debug")
    assert "/stats" in text
    assert "счетчики" in text or "строф" in text


def test_help_shows_aliases_in_overview() -> None:
    """Алиасы видны в сводке: иначе о /msg и /j из справки не узнать."""
    text = help_text(None)
    assert "/chat|msg" in text


def test_help_for_alias() -> None:
    """Справка по алиасу совпадает со справкой по имени."""
    assert help_text("q") == help_text("quit")


def test_help_for_unknown_command_does_not_raise() -> None:
    """Справка по несуществующей команде отдает текст, а не исключение."""
    text = help_text("nosuchcommand")
    assert isinstance(text, str)
    assert text.strip()


def test_help_mentions_xeps() -> None:
    """В справке по команде перечислены названия задействованных расширений."""
    spec = find("ping")
    assert spec is not None
    assert "0199" in spec.xeps
    assert "XMPP Ping" in help_text("ping")


def test_help_group_counts_commands_in_russian() -> None:
    """Число команд в заголовке группы согласуется с существительным."""
    assert help_text("crypto").splitlines()[0] == "Шифрование: 2 команды"
    assert help_text("conversation").splitlines()[0] == "Беседы: 10 команд"


# Команда /lang.


def test_lang_is_registered() -> None:
    """/lang живет в группе system, ключ обработчика исполняет интерфейс."""
    spec = find("lang")
    assert spec is not None
    assert spec.group == "system"
    assert spec.handler_key == "sys.lang"
    assert spec.usage == "/lang [en|ru]"


@pytest.mark.parametrize(
    ("line", "args"),
    [("/lang", ()), ("/lang ru", ("ru",)), ("/lang en", ("en",)), ("/lang RU", ("ru",))],
)
def test_lang_parsed(line: str, args: tuple[str, ...]) -> None:
    """Без аргумента /lang показывает язык, код языка приводится к нижнему регистру."""
    result = parse(line)
    assert result is not None
    assert result.ok, result.error
    assert result.handler_key == "sys.lang"
    assert result.args == args


@pytest.mark.parametrize("line", ["/lang de", "/lang ru en"])
def test_lang_rejects_unknown_language(line: str) -> None:
    """Неизвестный код или лишний аргумент дают ошибку с сигнатурой."""
    result = parse(line)
    assert result is not None
    assert not result.ok
    assert result.error is not None
    assert "/lang [en|ru]" in result.error


def test_complete_lang() -> None:
    """Аргумент /lang дополняется из списка поддерживаемых языков."""
    assert complete("/lang ", CONTEXT) == [f"/lang {code}" for code in LANGUAGES]
    assert complete("/lang r", CONTEXT) == ["/lang ru"]
    assert complete("/la", CONTEXT) == ["/lang"]


def test_help_for_lang() -> None:
    """Справка по /lang на русском."""
    text = help_text("lang")
    assert "/lang [en|ru]" in text
    assert "показать или сменить язык интерфейса" in text
    assert "Группа: Система" in text


# Английский интерфейс.


def test_help_in_english() -> None:
    """После смены языка справка и ошибки разбора выводятся по-английски."""
    i18n.set_language("en")
    text = help_text("presence")
    assert text.splitlines()[0] == "/presence <show> [text]"
    assert "change presence and status text" in text
    assert "Group: Connection and account" in text
    assert "Group: Соединение" not in text

    overview = help_text(None)
    assert overview.startswith(f"Slash commands: {len(REGISTRY)}.")
    assert "system  System" in overview

    assert help_text("crypto").splitlines()[0] == "Encryption: 2 commands"
    assert "--nick <nick>" in help_text("join")
    assert "Subcommands:" in help_text("omemo")

    result = parse("/presence")
    assert result is not None
    assert result.error == "expected: /presence <show> [text]"
    unknown = parse("/quiet")
    assert unknown is not None
    assert unknown.error == "unknown command: /quiet, maybe: /quit"
