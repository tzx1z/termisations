"""Конфигурация профиля, выбор профиля и приоритет источников.

Проверяется правило "флаг важнее конфига, конфиг важнее умолчания" по каждому
ключу и понятность отказа на неверном значении: конфиг правят руками, и
сообщение "ошибка разбора" без имени ключа тут бесполезно.

Отдельный слой - выбор профиля. Каталог профиля нужен до чтения конфига, поэтому
источников у JID ровно два: флаг и переменная окружения. Запуск без обоих обязан
называть оба способа, а не молча поднимать эмулятор.
"""

import asyncio
import io
import re
from pathlib import Path
from typing import Final

import pytest

from termisations import cli
from termisations.core import i18n, paths, profile
from termisations.core.i18n import _

CONFIG: Final = """
[account]
jid = "alice@example.org"
resource = "laptop"
server = "127.0.0.1"
port = 5223
password_command = "pass show xmpp/work"

[tls]
direct = true
verify = false

[mock]
rate = 42.0
scenario = "stress"

[ui]
layout = "debug"
xml_buffer = 500

[log]
level = "debug"
"""


PROFILE: Final = "alice@example.org"


def write_config(root: Path, text: str = CONFIG, profile: str = PROFILE) -> Path:
    """Положить конфиг в каталог профиля и вернуть путь."""
    target = root / "termisations" / profile / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def parse(argv: list[str], profile: str = PROFILE) -> object:
    """Разобрать аргументы и применить конфиг ровно так, как это делает main.

    Профиль выбирается заранее: в ``main`` это делает ``_select_profile`` до
    чтения конфига. Ошибка чтения конфига превращается в отказ разбора
    аргументов: иначе тест проверял бы внутренний вызов, а не то, что увидит
    пользователь.
    """
    paths.use_profile(profile)
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    try:
        cli._apply_config(parser, args)
    except ValueError as error:
        parser.error(str(error))
    return args


def select(argv: list[str]) -> str:
    """Выбрать профиль так же, как это делает main."""
    parser = cli.build_parser()
    return cli._select_profile(parser, parser.parse_args(argv))


def test_without_account_the_launch_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    """Запуск без учетной записи называет оба способа ее задать и флаг эмулятора."""
    with pytest.raises(SystemExit):
        select([])
    error = capsys.readouterr().err
    assert "--jid" in error
    assert profile.ENV_JID in error
    assert "--mock" in error


def test_no_mock_without_account_is_refused() -> None:
    """--no-mock без учетной записи: подключаться некуда, и это отказ."""
    with pytest.raises(SystemExit):
        select(["--no-mock"])


def test_environment_variable_selects_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Переменная окружения работает вместо флага, а учетная запись нормализуется."""
    monkeypatch.setenv(profile.ENV_JID, "Bob@Example.ORG/phone")
    assert select([]) == "bob@example.org"
    assert paths.current_profile() == "bob@example.org"


def test_flag_wins_over_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Флаг важнее переменной: переменная задается на весь сеанс оболочки."""
    monkeypatch.setenv(profile.ENV_JID, "bob@example.org")
    assert select(["--jid", "tom@simple.org"]) == "tom@simple.org"
    assert paths.current_profile() == "tom@simple.org"


def test_mock_needs_no_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Эмулятору учетная запись не нужна, и переменную окружения он не замечает."""
    monkeypatch.setenv(profile.ENV_JID, "bob@example.org")
    assert select(["--mock"]) == ""
    assert paths.current_profile() == ""


def test_mock_with_jid_is_an_error() -> None:
    """Эмулятор и подключение к серверу исключают друг друга."""
    with pytest.raises(SystemExit):
        select(["--mock", "--jid", "bob@example.org"])


def test_jid_unfit_for_a_directory_is_refused() -> None:
    """Адрес, не годящийся в имя каталога, отклоняется при разборе аргументов."""
    with pytest.raises(SystemExit):
        select(["--jid", "без собаки"])


def test_config_is_read_from_the_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Без флага --config читается конфиг профиля, а не общий."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path)
    args = parse([])
    assert args.resource == "laptop"
    assert args.server == "127.0.0.1"
    assert args.port == 5223
    assert args.password_command == "pass show xmpp/work"
    assert args.direct_tls is True
    assert args.tls_verify is False


def test_config_of_another_profile_is_not_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Конфиг соседнего профиля не виден: у каждой учетной записи свой каталог."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, profile="bob@example.org")
    args = parse([], profile="tom@simple.org")
    assert args.resource is None
    assert args.server is None


def test_config_jid_must_match_the_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Расхождение account.jid с профилем - отказ: правили не тот файл."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, profile="tom@simple.org")
    with pytest.raises(SystemExit):
        parse([], profile="tom@simple.org")
    error = capsys.readouterr().err
    assert "account.jid" in error
    assert "tom@simple.org" in error


def test_config_jid_is_no_longer_a_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Учетную запись задает профиль: jid из конфига не подставляется."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path)
    assert parse([]).jid is None


def test_missing_default_config_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Чистая машина без конфига запускается: файла по умолчанию может не быть."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    args = parse([])
    assert args.jid is None


def test_missing_explicit_config_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Файл, названный флагом, обязан существовать: путь задал пользователь."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(SystemExit):
        parse(["--config", str(tmp_path / "нет.toml")])


def test_explicit_config_overrides_the_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Явный --config важнее конфига профиля: общий файл на две записи законен."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path)
    shared = tmp_path / "общий.toml"
    shared.write_text('[account]\nresource = "общий"\n', encoding="utf-8")
    assert parse(["--config", str(shared)]).resource == "общий"


@pytest.mark.parametrize(
    ("argv", "field", "expected"),
    [
        (["--resource", "phone"], "resource", "phone"),
        (["--server", "10.0.0.1"], "server", "10.0.0.1"),
        (["--port", "5222"], "port", 5222),
        (["--no-direct-tls"], "direct_tls", False),
        (["--tls-verify"], "tls_verify", True),
        (["--layout", "focus"], "layout", "focus"),
        (["--rate", "7"], "rate", 7.0),
        (["--scenario", "muc"], "scenario", "muc"),
        (["--log-level", "error"], "log_level", "error"),
        (["--xml-buffer", "128"], "xml_buffer", 128),
    ],
)
def test_flag_wins_over_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], field: str, expected: object
) -> None:
    """Флаг командной строки важнее значения из конфига."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path)
    assert getattr(parse(argv), field) == expected


def test_config_wins_over_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Без флага берется значение из конфига, а не умолчание."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path)
    args = parse([])
    assert args.layout == "debug"
    assert args.rate == 42.0
    assert args.scenario == "stress"
    assert args.log_level == "debug"
    assert args.xml_buffer == 500


@pytest.mark.parametrize(
    ("text", "part"),
    [
        ('[account]\njid = "без собаки"\n', "account.jid"),
        ("[account]\nresource = 5\n", "account.resource"),
        ("[account]\nport = 70000\n", "account.port"),
        ('[tls]\ndirect = "yes"\n', "tls.direct"),
        ('[tls]\nverify = "нет"\n', "tls.verify"),
        ('[mock]\nrate = "быстро"\n', "mock.rate"),
        ('[ui]\nlayout = "боком"\n', "ui.layout"),
        ('[log]\nlevel = "громко"\n', "log.level"),
    ],
)
def test_bad_value_names_the_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    text: str,
    part: str,
) -> None:
    """Отказ называет ключ конфига: файл правят руками, и искать его надо быстро."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, text)
    with pytest.raises(SystemExit):
        parse([])
    assert part in capsys.readouterr().err


def test_broken_toml_names_the_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Испорченный TOML дает ошибку с путем файла, а не трассировку."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    target = write_config(tmp_path, "[account\njid = 1")
    with pytest.raises(SystemExit):
        parse([])
    assert target.exists()


def test_log_path_follows_the_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Журнал лежит в каталоге кэша профиля: разбирают его по одной записи."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    paths.use_profile(PROFILE)
    assert cli._log_path() == tmp_path / "termisations" / PROFILE / "client.log"


def test_log_path_without_profile_stays_in_the_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """У эмулятора профиля нет, и журнал остается в корне каталога кэша."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert cli._log_path() == tmp_path / "termisations" / "client.log"


def test_logging_rotates_and_keeps_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Файл журнала создается с правами 0600 и с ограничением размера."""
    import logging
    import stat

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    paths.use_profile(PROFILE)
    path = cli.configure_logging("debug")
    assert path is not None
    logging.getLogger("termisations").debug("проверка")
    logging.shutdown()
    assert stat.S_IMODE(path.stat().st_mode) == paths.FILE_MODE
    handler = logging.getLogger("termisations").handlers[0]
    assert getattr(handler, "maxBytes", 0) == cli.LOG_MAX_BYTES
    assert getattr(handler, "backupCount", 0) == cli.LOG_BACKUPS


@pytest.mark.parametrize("language", i18n.LANGUAGES)
def test_help_examples_use_existing_flags(language: str) -> None:
    """Примеры в справке называют только существующие флаги и помещаются в терминал.

    Текст после списка флагов пишется руками и сам не обновляется, поэтому может
    расходиться с кодом.
    Переносы строк в нем ручные, поэтому ширину проверяет тест, а не форматтер.
    Перевод пишется руками точно так же, поэтому проверка идет на обоих языках.
    """
    i18n.set_language(language)
    parser = cli.build_parser()
    known = {option for action in parser._actions for option in action.option_strings}
    help_text = parser.format_help()
    epilog = "\n\n".join(_(part) for part in cli._EPILOG)
    mentioned = set(re.findall(r"(?<![\w-])--[a-z][\w-]*", epilog))
    assert mentioned, "в примерах нет ни одного флага"
    assert mentioned <= known, f"неизвестные флаги в справке: {sorted(mentioned - known)}"
    assert all(len(line) <= 80 for line in epilog.splitlines())
    assert epilog in help_text


def test_batch_message_is_taken_as_is() -> None:
    """Текст из --message уходит без изменений."""
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--jid", "alice@example.org", "--to", "bob@example.org", "--message", "  текст с краями  "]
    )
    assert cli._batch_text(args, parser) == "  текст с краями  "


def test_batch_text_from_stdin_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Текст со стандартного ввода очищается от переводов строк.

    В конвейере строка почти всегда приходит с завершающим переводом строки, и
    отправлять его собеседнику незачем.
    """
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("строка из конвейера\n"))
    parser = cli.build_parser()
    args = parser.parse_args(["--jid", "alice@example.org", "--to", "bob@example.org", "--stdin"])
    assert cli._batch_text(args, parser) == "строка из конвейера"


def test_empty_stdin_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой стандартный ввод не превращается в пустое сообщение."""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("   \n"))
    parser = cli.build_parser()
    args = parser.parse_args(["--jid", "alice@example.org", "--to", "bob@example.org", "--stdin"])
    with pytest.raises(SystemExit):
        cli._batch_text(args, parser)


def test_stdin_and_message_exclude_each_other(monkeypatch: pytest.MonkeyPatch) -> None:
    """Два источника текста сразу - ошибка, а не молчаливый выбор одного."""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("из конвейера"))
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--jid",
            "alice@example.org",
            "--to",
            "bob@example.org",
            "--stdin",
            "--message",
            "из флага",
        ]
    )
    with pytest.raises(SystemExit):
        cli._batch_text(args, parser)


def test_without_batch_flags_the_interface_starts() -> None:
    """Без --message и --stdin пакетного режима нет."""
    parser = cli.build_parser()
    args = parser.parse_args(["--mock"])
    assert cli._batch_text(args, parser) is None


def test_file_alone_starts_batch_mode(tmp_path: Path) -> None:
    """Файл без текста - это тоже пакетный режим, а не запуск интерфейса."""
    payload = tmp_path / "otchet.txt"
    payload.write_text("содержимое", encoding="utf-8")
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--jid", "alice@example.org", "--to", "bob@example.org", "--file", str(payload)]
    )
    assert args.file == payload
    assert cli._batch_text(args, parser) is None


def test_batch_without_recipient_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без --to отправлять некуда, и это ошибка разбора, а не молчаливый запуск."""
    monkeypatch.setattr(cli.sys, "argv", ["termisations"])
    with pytest.raises(SystemExit):
        cli.main(["--jid", "alice@example.org", "--message", "текст"])


def test_batch_with_missing_file_is_an_error() -> None:
    """Недоступный файл называется до подключения к серверу."""
    with pytest.raises(SystemExit):
        cli.main(
            [
                "--jid",
                "alice@example.org",
                "--to",
                "bob@example.org",
                "--file",
                "/nonexistent/otchet.txt",
            ]
        )


def test_storage_opens_in_the_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """База открывается в каталоге профиля.

    Проверка общая для интерфейса и пакетного режима: ``_open_storage`` у них
    один, и от него зависит шифрование беседы в конвейере.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    paths.use_profile(PROFILE)
    store = cli._open_storage(False)
    assert store is not None
    asyncio.run(store.close())
    assert (tmp_path / "termisations" / PROFILE / "history.db").is_file()


def test_batch_requires_an_account() -> None:
    """Эмулятору писать некуда: пакетный режим требует учетной записи."""
    with pytest.raises(SystemExit):
        cli.main(["--mock", "--to", "bob@example.org", "--message", "текст"])


# Язык интерфейса.

# Переменные локали, которые читает detect_language. Тест, проверяющий выбор по
# локали, убирает их все: иначе на машине разработчика сработала бы его локаль.
LOCALE_VARIABLES: Final = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")


def use_locale(monkeypatch: pytest.MonkeyPatch, locale_name: str) -> None:
    """Оставить из источников языка только локаль системы."""
    monkeypatch.delenv(i18n.ENV_LANG, raising=False)
    for name in LOCALE_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", locale_name)


@pytest.mark.parametrize(
    ("argv", "variable", "configured", "expected"),
    [
        (["--lang", "en"], "ru", "ru", "en"),
        (["--lang", "ru"], "en", "en", "ru"),
        ([], "en", "ru", "en"),
        ([], "ru_RU.UTF-8", "en", "ru"),
        ([], None, "en", "en"),
        ([], None, "ru_RU.UTF-8", "ru"),
    ],
    ids=["flag-en", "flag-ru", "variable-en", "variable-locale", "config-en", "config-locale"],
)
def test_language_priority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    variable: str | None,
    configured: str,
    expected: str,
) -> None:
    """Флаг важнее переменной окружения, переменная важнее конфига.

    Язык из конфига сразу включается: остальные ошибки конфига выводятся уже на
    нем. Имя локали вида ru_RU.UTF-8 годится и в переменную, и в конфиг.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, f'[ui]\nlang = "{configured}"\n')
    if variable is None:
        monkeypatch.delenv(i18n.ENV_LANG)
    else:
        monkeypatch.setenv(i18n.ENV_LANG, variable)
    assert parse(argv).lang == expected
    assert i18n.get_language() == expected


@pytest.mark.parametrize(
    ("locale_name", "expected"),
    [("ru_RU.UTF-8", "ru"), ("en_US.UTF-8", "en"), ("de_DE.UTF-8", "en")],
)
def test_without_sources_the_locale_decides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, locale_name: str, expected: str
) -> None:
    """Без флага, переменной и ключа конфига язык берется из локали системы."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    use_locale(monkeypatch, locale_name)
    assert parse([]).lang is None
    assert cli._startup_language([]) == expected


def test_startup_language_skips_a_bad_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Предварительный разбор не завершает процесс: ошибку флага назовет основной."""
    monkeypatch.setenv(i18n.ENV_LANG, "en")
    assert cli._startup_language(["--mock", "--lang", "ru"]) == "ru"
    assert cli._startup_language(["--lang=ru"]) == "ru"
    assert cli._startup_language(["--lang", "de"]) == "en"
    assert cli._startup_language(["--lang"]) == "en"


@pytest.mark.parametrize(
    ("variable", "text", "part"),
    [
        ("de", "", "TERMISATIONS_LANG"),
        ("", '[ui]\nlang = "de"\n', "ui.lang"),
        ("", "[ui]\nlang = 5\n", "ui.lang"),
    ],
    ids=["variable", "config", "config-type"],
)
def test_bad_language_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    variable: str,
    text: str,
    part: str,
) -> None:
    """Недопустимый язык - отказ с кодом 2, как у других ошибок конфига."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, text)
    monkeypatch.setenv(i18n.ENV_LANG, variable)
    with pytest.raises(SystemExit) as stop:
        parse([])
    assert stop.value.code == 2
    error = capsys.readouterr().err
    assert part in error
    assert "en, ru" in error


def test_bad_language_is_reported_in_the_locale_language(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ошибка в переменной выводится на языке следующего источника - локали."""
    use_locale(monkeypatch, "en_US.UTF-8")
    monkeypatch.setenv(i18n.ENV_LANG, "klingon")
    with pytest.raises(SystemExit) as stop:
        cli.main(["--mock"])
    assert stop.value.code == 2
    assert "variable TERMISATIONS_LANG must be one of: en, ru, got 'klingon'" in (
        capsys.readouterr().err
    )


def test_mock_takes_the_language_from_the_root_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Эмулятор читает конфиг из корня каталога настроек, и [ui] lang действует там."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(i18n.ENV_LANG)
    root = tmp_path / "termisations" / "config.toml"
    root.parent.mkdir(parents=True)
    root.write_text('[ui]\nlang = "en"\n', encoding="utf-8")
    assert parse(["--mock"], profile="").lang == "en"
    assert i18n.get_language() == "en"


def test_help_follows_the_system_locale(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Справка выводится на языке локали, если язык не задан явно."""
    use_locale(monkeypatch, "en_US.UTF-8")
    with pytest.raises(SystemExit) as stop:
        cli.main(["--help"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert "Terminal XMPP client with protocol transparency." in out
    assert "sending without the interface:" in out
    assert "show this help message and exit" in out
    assert not re.search("[А-Яа-яЁё]", out)


def test_help_flag_beats_the_locale(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Флаг --lang действует и на справку, даже если стоит после --help."""
    use_locale(monkeypatch, "en_US.UTF-8")
    with pytest.raises(SystemExit):
        cli.main(["--help", "--lang", "ru"])
    out = capsys.readouterr().out
    assert "Терминальный XMPP-клиент с прозрачностью протокола." in out
    assert "показать эту справку и выйти" in out


def test_argument_errors_use_the_chosen_language(capsys: pytest.CaptureFixture[str]) -> None:
    """Ошибка значения флага выводится на языке из --lang."""
    with pytest.raises(SystemExit) as stop:
        cli.main(["--lang", "en", "--mock", "--port", "70000"])
    assert stop.value.code == 2
    assert "port out of range 1-65535: 70000" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["--mock", "--port", "70000"])
    assert "порт вне диапазона 1-65535: 70000" in capsys.readouterr().err
