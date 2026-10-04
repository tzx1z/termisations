"""Точка входа termisations.

Модуль собирает слои в рабочее приложение: читает аргументы и конфиг, создает шину,
сессию-источник событий и Textual-приложение. Никакой логики протокола здесь нет.

Логи пишутся в файл, а не в stderr. Причина: Textual владеет терминалом, и любая
строка, напечатанная мимо него, портит отрисовку экрана.
"""

import argparse
import asyncio
import getpass
import logging
import os
import sqlite3
import sys
import tomllib
from collections.abc import Callable, Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from termisations import __version__
from termisations.core import i18n, paths, profile
from termisations.core.events import (
    CommandFeedback,
    EventBus,
    MessageAdded,
    Notice,
    SendText,
)
from termisations.core.i18n import N_, _
from termisations.core.inputlog import InputLog
from termisations.core.models import ConnectionStage, DeliveryState, Message
from termisations.core.session import Session
from termisations.core.storage import Storage
from termisations.layouts import LAYOUT_MODES
from termisations.mock import MockSession
from termisations.protocol.account import Account, PasswordError, Secret, resolve_password

# Сетевая сессия импортируется в _load_transport: slixmpp и aiohttp лежат в
# необязательной группе xmpp, и без нее обязан запускаться хотя бы эмулятор.
if TYPE_CHECKING:
    from termisations.protocol.session import SlixmppSession

__all__ = ["main"]

_DEFAULT_RATE: Final = 8.0

# Сколько ждать подключения и отправки в пакетном режиме. Больше минуты держать
# процесс в конвейере нельзя: он молча израсходует таймаут вызывающего скрипта.
_BATCH_TIMEOUT: Final = 30.0
"""Частота мока по умолчанию: поток читается глазами, а не нагружает панель."""

_MAX_RATE: Final = 5000.0
"""Верхняя граница частоты. Целевая нагрузка - 500 строф в секунду."""

_DEFAULT_XML_BUFFER: Final = 2000
"""Размер кольцевого буфера строф по умолчанию."""

_MAX_XML_BUFFER: Final = 1_000_000
"""Верхняя граница буфера: выше начинается расход памяти без пользы."""

_LOG_LEVELS: Final[tuple[str, ...]] = ("debug", "info", "warning", "error", "critical")

_DEFAULT_LAYOUT: Final = "split"
"""Раскладка при запуске, если ее не задали флагом или конфигом."""

_DEFAULT_LOG_LEVEL: Final = "warning"
"""Уровень журнала, если его не задали флагом или конфигом."""


def _positive_float(value: str) -> float:
    """Разобрать положительное дробное число для --mock-rate."""
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            _("a number is expected, got {value!r}").format(value=value)
        ) from None
    if number <= 0:
        raise argparse.ArgumentTypeError(
            _("the rate must be greater than zero, got {number:g}").format(number=number)
        )
    if number > _MAX_RATE:
        raise argparse.ArgumentTypeError(
            _("a rate above {limit:g} stanzas per second is not supported, got {number:g}").format(
                limit=_MAX_RATE, number=number
            )
        )
    return number


def _positive_int(value: str) -> int:
    """Разобрать положительное целое для --xml-buffer."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            _("an integer is expected, got {value!r}").format(value=value)
        ) from None
    if number <= 0:
        raise argparse.ArgumentTypeError(
            _("the buffer size must be greater than zero, got {number}").format(number=number)
        )
    if number > _MAX_XML_BUFFER:
        raise argparse.ArgumentTypeError(
            _("a buffer size above {limit} is not supported, got {number}").format(
                limit=_MAX_XML_BUFFER, number=number
            )
        )
    return number


def _port(value: str) -> int:
    """Номер порта. Значение вне диапазона отклоняется с внятным текстом."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            _("a number is expected, got {value!r}").format(value=value)
        ) from None
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError(
            _("port out of range 1-65535: {number}").format(number=number)
        )
    return number


def _scenario_choices() -> tuple[str, ...]:
    """Список имен сценариев. Единственный источник - сам мок."""
    return tuple(sorted(MockSession.SCENARIOS))


# Текст после списка флагов, по абзацам: так каждый абзац - отдельная запись
# каталога перевода. Переносы строк расставлены вручную, поэтому разбор идет с
# RawDescriptionHelpFormatter: обычный форматтер склеил бы примеры в абзац.
# Флаги в примерах сверяет с разборщиком tests/test_config.py.
_EPILOG: Final[tuple[str, ...]] = (
    N_(
        "examples:\n"
        "  termisations --mock\n"
        "      emulated stanza stream, without network or an account\n"
        "  termisations --jid alice@example.org\n"
        "      connect to the server with the interface\n"
        '  termisations --jid alice@example.org --to bob@example.org --message "text"\n'
        "      send a message without the interface, for scripts and cron\n"
        "  df -h | termisations --jid alice@example.org --to bob@example.org --stdin\n"
        "      send the output of a command"
    ),
    N_(
        "profile files (default paths, the XDG_* variables change them):\n"
        "  ~/.config/termisations/<jid>/config.toml\n"
        "      settings; on the first launch an example with explanations is created\n"
        "  ~/.local/share/termisations/<jid>/\n"
        "      message history, OMEMO keys and the profile resource\n"
        "  ~/.cache/termisations/<jid>/client.log\n"
        "      log"
    ),
    N_(
        "exit codes of sending without the interface:\n"
        "  0 - sent, 1 - not sent, 2 - error in the arguments or the config"
    ),
)


def build_parser() -> argparse.ArgumentParser:
    """Собрать разборщик аргументов командной строки.

    Тексты справки переводятся при сборке, поэтому язык выбирается раньше:
    ``main`` зовет ``_startup_language`` до этой функции.
    """
    parser = argparse.ArgumentParser(
        prog="termisations",
        description=_("Terminal XMPP client with protocol transparency."),
        epilog="\n\n".join(_(part) for part in _EPILOG),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # Флаг справки добавляется вручную: текст стандартного флага приходит из
        # stdlib и на язык интерфейса не переводится.
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help=_("show this help message and exit"))
    account = parser.add_argument_group(_("account"))
    account.add_argument(
        "--jid",
        default=None,
        metavar="USER@SERVER",
        help=_(
            "account address; it also selects the profile - the directory with the "
            "config, history and keys. Without the flag the {env} variable is used"
        ).format(env=profile.ENV_JID),
    )
    account.add_argument(
        "--resource",
        default=None,
        metavar=_("NAME"),
        help=_(
            "resource; defaults to {prefix}.<tail>, the tail is random "
            "and is created once per profile"
        ).format(prefix=profile.RESOURCE_PREFIX),
    )
    account.add_argument(
        "--server",
        default=None,
        metavar=_("HOST"),
        help=_("server address, bypassing SRV resolution"),
    )
    account.add_argument(
        "--port",
        type=_port,
        default=None,
        metavar="N",
        help=_("port, bypassing SRV resolution"),
    )
    account.add_argument(
        "--direct-tls",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=_("direct TLS per XEP-0368; by default the method is chosen by the SRV record"),
    )
    account.add_argument(
        "--tls-verify",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=_("certificate chain verification; turn it off only for your own server"),
    )
    account.add_argument(
        "--password-command",
        default=None,
        metavar=_("COMMAND"),
        help=_(
            "external command that prints the password; without it the password is taken "
            "from the system keyring, then from the TERMISATIONS_PASSWORD variable, "
            "otherwise it is asked for at startup"
        ),
    )

    parser.add_argument(
        "--mock",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=_("emulated stanza stream; it needs neither an account nor a profile"),
    )
    parser.add_argument(
        "--mock-rate",
        "--rate",
        dest="rate",
        type=_positive_float,
        default=None,
        metavar="N",
        help=_("stanzas per second, default {rate:g}").format(rate=_DEFAULT_RATE),
    )
    parser.add_argument(
        "--mock-scenario",
        "--scenario",
        dest="scenario",
        choices=_scenario_choices(),
        default=None,
        help=_("scenario of the emulated stream"),
    )
    parser.add_argument(
        "--xml-buffer",
        dest="xml_buffer",
        type=_positive_int,
        default=None,
        metavar="N",
        help=_("size of the stanza ring buffer, default {size}").format(size=_DEFAULT_XML_BUFFER),
    )
    parser.add_argument(
        "--layout",
        choices=LAYOUT_MODES,
        default=None,
        help=_("layout at startup, default {layout}").format(layout=_DEFAULT_LAYOUT),
    )
    parser.add_argument(
        "--lang",
        choices=i18n.LANGUAGES,
        default=None,
        help=_(
            "interface language; by default taken from the {env} variable, "
            "the [ui] lang config key or the system locale"
        ).format(env=i18n.ENV_LANG),
    )
    parser.add_argument(
        "--no-xml",
        action="store_true",
        help=_("start in focus mode, without the RAW XML panel"),
    )
    parser.add_argument(
        "--sasl2",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=_("try SASL2 and Bind 2 if the server announced them (default yes)"),
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help=_("do not open the database: no message history and no input history on disk"),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help=_("TOML configuration file instead of the profile config"),
    )
    parser.add_argument(
        "--log-level",
        dest="log_level",
        choices=_LOG_LEVELS,
        default=None,
        help=_("log level, default {level}").format(level=_DEFAULT_LOG_LEVEL),
    )
    batch = parser.add_argument_group(_("sending without the interface"))
    batch.add_argument(
        "--to",
        default=None,
        metavar="JID",
        help=_("recipient of the message in batch mode"),
    )
    batch.add_argument(
        "--message",
        default=None,
        metavar="TEXT",
        help=_("send the text and exit, the interface does not start"),
    )
    batch.add_argument(
        "--stdin",
        action="store_true",
        help=_("take the message text from standard input"),
    )
    batch.add_argument(
        "--file",
        type=Path,
        default=None,
        metavar="PATH",
        help=_("send a file via XEP-0363 and exit"),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"termisations {__version__}",
        help=_("show program's version number and exit"),
    )
    return parser


def _startup_language(argv: Sequence[str] | None) -> str:
    """Язык справки и ошибок разбора: выбирается до сборки разборщика.

    Конфиг лежит в профиле, а профиль известен только после разбора, поэтому
    здесь действуют флаг ``--lang``, переменная окружения и локаль системы.
    Флаг достается отдельным разборщиком, который знает только его и не
    завершает процесс на ошибке. Недопустимое значение пропускается: основной
    разбор или ``_apply_language`` назовут его уже на языке следующего источника.
    """
    early = argparse.ArgumentParser(add_help=False, allow_abbrev=False, exit_on_error=False)
    early.add_argument("--lang")
    try:
        flag = early.parse_known_args(argv)[0].lang
    except argparse.ArgumentError:
        flag = None
    if flag in i18n.LANGUAGES:
        return str(flag)
    variable = i18n.normalize_language(os.environ.get(i18n.ENV_LANG, ""))
    return variable or i18n.detect_language()


def _select_profile(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """Выбрать профиль учетной записи. Пустая строка означает режим эмулятора.

    Источники JID по убыванию важности: флаг ``--jid``, переменная окружения.
    Конфиг источником быть не может: каталог профиля нужно знать до того, как
    появится что читать, поэтому запуск без обоих способов - отказ разбора
    аргументов, а не молчаливый выбор умолчания.

    Переменная окружения при ``--mock`` игнорируется: она задается в профиле
    оболочки на весь сеанс, и эмулятор из-за нее запускаться перестать не должен.

    Учетная запись приводится к имени профиля: bare JID в нижнем регистре.
    Регистр локальной части и домена сервер и так не различает, а ресурс задается
    отдельным флагом ``--resource`` - иначе ``--jid alice@srv/phone`` дал бы
    полный адрес с двумя ресурсами.
    """
    if args.mock and args.jid:
        parser.error(
            _("--mock and --jid are incompatible: choose the emulator or a server connection")
        )
    if args.mock:
        return ""
    jid = str(args.jid or os.environ.get(profile.ENV_JID, "")).strip()
    if not jid:
        parser.error(
            _(
                "no account given: set --jid USER@SERVER or the {env} variable; "
                "the emulated stream starts with the --mock flag"
            ).format(env=profile.ENV_JID)
        )
    try:
        name = paths.use_profile(jid)
    except ValueError as error:
        parser.error(str(error))
    args.jid = name
    return name


def _migrate_profile(jid: str) -> None:
    """Перенести данные прежней раскладки в профиль при первом запуске.

    Отказ переноса запуск не останавливает: файлы остаются в корне и попадут в
    профиль при следующей попытке, а клиент тем временем работает. Сообщение
    идет в поток ошибок, потому что журнал на этот момент еще не настроен: его
    уровень берется из конфига, который сейчас и переносится.
    """
    try:
        moved = profile.migrate_legacy()
    except OSError as error:
        print(
            _("data of the previous layout not moved into profile {jid}: {error}").format(
                jid=jid, error=error
            ),
            file=sys.stderr,
        )
        return
    if moved:
        print(
            _("moved into profile {jid}: {names}").format(jid=jid, names=", ".join(moved)),
            file=sys.stderr,
        )


def _default_resource(jid: str) -> str:
    """Ресурс профиля по умолчанию.

    Отказ записи запуск не останавливает: ресурс на этот запуск берется новый,
    и клиент подключается. Сообщение идет в поток ошибок по той же причине, что
    и у переноса: журнал еще не настроен.
    """
    try:
        return profile.profile_resource()
    except OSError as error:
        resource = profile.new_resource()
        print(
            _(
                "resource of profile {jid} not saved, using {resource} for this launch: {error}"
            ).format(jid=jid, resource=resource, error=error),
            file=sys.stderr,
        )
        return resource


def _write_example_config(jid: str, resource: str) -> None:
    """Положить в профиль пример конфига, если конфига там нет.

    Отказ запуск не останавливает: пример нужен для удобства, без него клиент
    работает на умолчаниях. Сообщение идет в поток ошибок по той же причине,
    что и у переноса: журнал еще не настроен.
    """
    try:
        created = profile.write_example_config(resource)
    except OSError as error:
        print(
            _("example config for profile {jid} not created: {error}").format(jid=jid, error=error),
            file=sys.stderr,
        )
        return
    if created is not None:
        print(_("example config created: {path}").format(path=created), file=sys.stderr)


def _load_config(path: Path) -> dict[str, Any]:
    """Прочитать TOML-конфиг. Ошибки превращаются в ValueError с понятным текстом."""
    try:
        with path.open("rb") as handle:
            data: dict[str, Any] = tomllib.load(handle)
    except OSError as error:
        raise ValueError(
            _("config {path} cannot be read: {error}").format(path=path, error=error)
        ) from error
    except tomllib.TOMLDecodeError as error:
        raise ValueError(
            _("config {path} has a parse error: {error}").format(path=path, error=error)
        ) from error
    return data


def _config_value(config: dict[str, Any], section: str, key: str) -> Any:
    """Достать значение из секции конфига. Отсутствие секции не ошибка."""
    block = config.get(section)
    if not isinstance(block, dict):
        return None
    return block.get(key)


def _config_path(args: argparse.Namespace) -> Path | None:
    """Файл настроек: заданный флагом или путь профиля, если он существует.

    Отсутствие файла по умолчанию - штатный случай: клиент должен запускаться на
    чистой машине. Отсутствие файла, заданного флагом, - ошибка: пользователь
    назвал конкретный путь и вправе узнать, что его там нет. Явный ``--config``
    перекрывает путь профиля: один конфиг на несколько учетных записей - законный
    способ не дублировать общие настройки.
    """
    if args.config is not None:
        return Path(args.config)
    default = paths.config_file()
    return default if default.is_file() else None


def _apply_config(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Подставить значения из конфига там, где флаг не задан явно.

    Приоритет обычный: флаг командной строки важнее конфига, конфиг важнее умолчания.
    Секции: [account] (jid, resource, server, port, password_command),
    [tls] (direct, verify), [mock] (rate, scenario), [ui] (layout, xml_buffer, lang),
    [log] (level). Эмулятор профиля не имеет и читает конфиг из корня каталога
    настроек: там действуют те же секции.

    Язык разбирается первым и сразу включается: остальные ошибки конфига
    выводятся уже на нем. Переменная окружения языка проверяется и без файла
    конфига, поэтому отсутствие файла не прерывает разбор.
    """
    path = _config_path(args)
    config = _load_config(path) if path is not None else {}
    _apply_language(parser, args, config)
    _apply_account(parser, args, config)

    if args.rate is None:
        raw = _config_value(config, "mock", "rate")
        if raw is not None:
            if not isinstance(raw, int | float) or isinstance(raw, bool):
                parser.error(_("mock.rate in the config must be a number"))
            args.rate = _positive_float(str(raw))

    if args.scenario is None:
        raw = _config_value(config, "mock", "scenario")
        if raw is not None:
            if not isinstance(raw, str) or raw not in _scenario_choices():
                parser.error(_one_of("mock.scenario", _scenario_choices()))
            args.scenario = raw

    if args.xml_buffer is None:
        raw = _config_value(config, "ui", "xml_buffer")
        if raw is not None:
            if not isinstance(raw, int) or isinstance(raw, bool):
                parser.error(_("ui.xml_buffer in the config must be an integer"))
            args.xml_buffer = _positive_int(str(raw))

    if args.layout is None:
        raw = _config_value(config, "ui", "layout")
        if raw is not None:
            if not isinstance(raw, str) or raw not in LAYOUT_MODES:
                parser.error(_one_of("ui.layout", LAYOUT_MODES))
            args.layout = raw

    if args.log_level is None:
        raw = _config_value(config, "log", "level")
        if raw is not None:
            if not isinstance(raw, str) or raw.lower() not in _LOG_LEVELS:
                parser.error(_one_of("log.level", _LOG_LEVELS))
            args.log_level = raw.lower()


def _one_of(name: str, choices: Sequence[str]) -> str:
    """Текст отказа для ключа конфига с перечнем допустимых значений."""
    return _("{name} in the config must be one of: {choices}").format(
        name=name, choices=", ".join(choices)
    )


def _apply_language(
    parser: argparse.ArgumentParser, args: argparse.Namespace, config: dict[str, Any]
) -> None:
    """Язык интерфейса: флаг, затем переменная окружения, затем ``[ui] lang``.

    Переменная важнее конфига: ее задают на сеанс оболочки или на один запуск,
    а конфиг - на профиль. Если не задан ни один источник, ``args.lang`` остается
    None, и действует язык локали системы, выбранный в ``_startup_language``.

    Значение приводится через ``normalize_language``, поэтому годится и имя
    локали вида ru_RU.UTF-8: переменную часто копируют из LANG. Недопустимое
    значение - отказ, как и у остальных ключей конфига: молча заменить его
    локалью значило бы скрыть опечатку.
    """
    if args.lang is None:
        variable = os.environ.get(i18n.ENV_LANG, "").strip()
        if variable:
            args.lang = i18n.normalize_language(variable)
            if args.lang is None:
                parser.error(
                    _("variable {name} must be one of: {choices}, got {value!r}").format(
                        name=i18n.ENV_LANG, choices=", ".join(i18n.LANGUAGES), value=variable
                    )
                )
        else:
            raw = _config_value(config, "ui", "lang")
            if raw is not None:
                args.lang = i18n.normalize_language(raw) if isinstance(raw, str) else None
                if args.lang is None:
                    parser.error(_one_of("ui.lang", i18n.LANGUAGES))
    if args.lang is not None:
        i18n.set_language(args.lang)


def _apply_account(
    parser: argparse.ArgumentParser, args: argparse.Namespace, config: dict[str, Any]
) -> None:
    """Секции [account] и [tls]: параметры подключения из конфига."""
    _check_config_jid(parser, _config_value(config, "account", "jid"))

    for section, key, target in (
        ("account", "resource", "resource"),
        ("account", "server", "server"),
        ("account", "password_command", "password_command"),
    ):
        if getattr(args, target, None):
            continue
        raw = _config_value(config, section, key)
        if raw is not None:
            if not isinstance(raw, str):
                parser.error(
                    _("{name} in the config must be a string").format(name=f"{section}.{key}")
                )
            setattr(args, target, raw)

    if not args.port:
        raw = _config_value(config, "account", "port")
        if raw is not None:
            if not isinstance(raw, int) or isinstance(raw, bool) or not 1 <= raw <= 65535:
                parser.error(_("account.port in the config must be a number from 1 to 65535"))
            args.port = raw

    if getattr(args, "sasl2", None) is None:
        raw = _config_value(config, "account", "sasl2")
        if raw is not None:
            if not isinstance(raw, bool):
                parser.error(
                    _("{name} in the config must be true or false").format(name="account.sasl2")
                )
            args.sasl2 = raw

    for key, target in (("direct", "direct_tls"), ("verify", "tls_verify")):
        if getattr(args, target) is not None:
            continue
        raw = _config_value(config, "tls", key)
        if raw is not None:
            if not isinstance(raw, bool):
                parser.error(
                    _("{name} in the config must be true or false").format(name=f"tls.{key}")
                )
            setattr(args, target, raw)


def _check_config_jid(parser: argparse.ArgumentParser, raw: Any) -> None:
    """Сверить ``account.jid`` из конфига с выбранным профилем.

    В профильном конфиге ключ избыточен: учетную запись задает каталог. Источником
    он не служит, но расхождение почти всегда означает, что человек правит не тот
    файл, поэтому это отказ, а не молчаливое предпочтение одного из адресов.
    Без профиля сверять не с чем: эмулятору учетная запись не нужна.
    """
    if raw is None or not paths.current_profile():
        return
    if not isinstance(raw, str) or "@" not in raw:
        parser.error(_("account.jid in the config must look like user@server"))
    try:
        name = paths.profile_name(raw)
    except ValueError as error:
        parser.error(_("account.jid in the config: {error}").format(error=error))
    if name != paths.current_profile():
        parser.error(
            _(
                "account.jid in the config ({jid}) does not match profile {profile}: "
                "check that you are editing the right file"
            ).format(jid=raw, profile=paths.current_profile())
        )


def _log_path() -> Path:
    """Путь файла журнала: каталог кэша профиля, а без профиля - корень кэша.

    Журнал профильный, а не общий: разбирают по нему подключение конкретной
    учетной записи, и чужие строки в нем только мешают.
    """
    return paths.cache_dir() / "client.log"


_LOGGERS: Final[tuple[str, ...]] = ("termisations", "slixmpp", "asyncio", "omemo")
"""Логгеры, которые пишутся в файл журнала.

``omemo`` здесь потому, что библиотека предупреждает о состоянии списка
устройств в PEP на уровне warning. Без перехвата эти строки уходят в stderr
через lastResort-обработчик Python: в интерактивном режиме они ложатся поверх
интерфейса, а в пакетном - засоряют вывод конвейера."""

# Предел размера файла журнала и число сохраняемых предыдущих файлов.
LOG_MAX_BYTES: Final = 1_048_576
LOG_BACKUPS: Final = 5


def configure_logging(level: str) -> Path | None:
    """Направить журнал в файл. Возвращает путь или None, если файл недоступен.

    В stderr писать нельзя: терминалом владеет Textual. При недоступном файле
    журнал уходит в никуда, а приложение все равно запускается.
    """
    numeric = getattr(logging, level.upper(), logging.WARNING)
    try:
        path = paths.cache_file("client.log")
        # Ротация: журнал отладки растет быстро, а на диске он не единственный
        # потребитель места. Пять файлов по мегабайту хватает, чтобы разобрать
        # последнее подключение, и не хватает, чтобы заполнить раздел.
        handler: logging.Handler = RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
        paths.secure_file(path)
    except OSError:
        logging.getLogger(_LOGGERS[0]).addHandler(logging.NullHandler())
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s"))
    # Журнал собирает и свои сообщения, и сообщения slixmpp. Библиотека пишет
    # ошибку подключения через log.error и продолжает работу: без этого хендлера
    # сорванное соединение выглядит как бесконечное рукопожатие без причины.
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(numeric)
        logger.propagate = False
        logger.handlers.clear()
        logger.addHandler(handler)
    return path


def _account_from(args: argparse.Namespace, default_resource: str) -> Account | None:
    """Собрать учетную запись из аргументов. None означает режим эмулятора.

    ``default_resource`` - ресурс профиля: он действует, если ресурс не задан ни
    флагом, ни конфигом.
    """
    if not args.jid:
        return None
    return Account(
        jid=str(args.jid),
        resource=str(args.resource or default_resource),
        host=str(args.server or ""),
        port=int(args.port or 0),
        direct_tls=args.direct_tls,
        tls_verify=True if args.tls_verify is None else bool(args.tls_verify),
        password_command=str(args.password_command or ""),
        sasl2=True if args.sasl2 is None else bool(args.sasl2),
    )


def _open_storage(disabled: bool) -> Storage | None:
    """Открыть базу истории профиля до старта приложения.

    Путь базы берется из выбранного профиля: у каждой учетной записи своя
    история и свои ключи OMEMO. Открытие идет в собственном цикле событий: цикла
    приложения еще нет, а поток хранилища и соединение переживают смену цикла -
    исполнитель к циклу не привязан. Недоступная база не должна мешать переписке,
    поэтому отказ только печатается: клиент запускается без истории.

    Эмулятор истории не пишет вовсе: ``--mock`` не должен оставлять следов в
    настоящей базе.
    """
    if disabled:
        return None
    try:
        return asyncio.run(Storage.open())
    except (OSError, sqlite3.Error, RuntimeError) as error:
        print(
            _("history unavailable, working without it: {error}").format(error=error),
            file=sys.stderr,
        )
        return None


def _open_input_log(disabled: bool, writable: bool) -> InputLog | None:
    """Открыть общую историю ввода до старта приложения.

    Файл один на все профили: набранная команда принадлежит человеку, а не
    учетной записи. Открытие идет в собственном цикле событий по той же причине,
    что и у базы переписки: цикла приложения еще нет, а поток истории и
    соединение переживают смену цикла.

    ``writable=False`` отдает эмулятору историю только на чтение: листать и
    искать по ней он может, а следов на диске не оставляет. Недоступный или
    испорченный файл переписке не мешает, поэтому отказ только печатается:
    клиент запускается без истории ввода.
    """
    if disabled:
        return None
    try:
        return asyncio.run(InputLog.open(writable=writable))
    except (OSError, sqlite3.Error, RuntimeError) as error:
        print(
            _("input history unavailable, working without it: {error}").format(error=error),
            file=sys.stderr,
        )
        return None


def _obtain_password(account: Account) -> Secret:
    """Пароль до старта приложения.

    Порядок неинтерактивных источников задает ``resolve_password``. Если ни один
    не сработал и терминал интерактивный, пароль спрашивается здесь: после
    ``app.run()`` терминалом владеет Textual, и обычный ввод в нем невозможен.
    Собственный цикл событий нужен потому, что внешняя команда - подпроцесс, а
    цикла приложения на этот момент еще нет.
    """
    try:
        return asyncio.run(resolve_password(account))
    except PasswordError:
        if not sys.stdin.isatty():
            raise
    value = getpass.getpass(_("password for {jid}: ").format(jid=account.full_jid))
    if not value:
        message = _("password not entered")
        raise PasswordError(message)
    return Secret(value, _("prompt at startup"))


def _load_transport(parser: argparse.ArgumentParser) -> "type[SlixmppSession]":
    """Загрузить сетевую сессию или назвать группу, которой не хватает.

    Трассировка ImportError пользователю ничего не скажет, поэтому вместо нее
    печатается команда установки. Вызывается до запроса пароля: вводить пароль
    ради клиента, который все равно не подключится, незачем.
    """
    try:
        from termisations.protocol.session import SlixmppSession
    except ImportError as error:
        parser.error(
            _(
                "connecting to a server needs the xmpp extra ({error}), "
                "install it: pip install 'termisations[xmpp]'"
            ).format(error=error)
        )
    return SlixmppSession


async def _send_once(
    transport: "type[SlixmppSession]",
    account: Account,
    secret: Secret | None,
    storage: Storage | None,
    target: str,
    text: str,
    attachment: Path | None,
    timeout: float,
) -> int:
    """Подключиться, отправить сообщение или файл и выйти.

    Интерфейс здесь не поднимается: режим нужен ровно для конвейера и cron, где
    терминала нет вовсе. База та же, что у интерактивного клиента: в ней лежат
    ключи OMEMO и сохраненные беседы, поэтому шифрование в пакетном режиме
    получается таким же, каким пользователь оставил его в этой беседе. С базой
    в памяти каждый запуск публиковал бы в PEP новое устройство.
    """
    if storage is None:
        # Без базы нет ни ключей, ни признака шифрования беседы: сообщение
        # уйдет открытым текстом. Молчать об этом нельзя - пользователь мог
        # включить OMEMO в интерактивном клиенте и ждать того же здесь.
        print(
            _("history disabled: OMEMO unavailable, the text will be sent in the clear"),
            file=sys.stderr,
        )
    bus = EventBus(logging.getLogger("termisations.bus"))
    problems: list[str] = []
    bus.subscribe(Notice, lambda event: problems.append(event.text))
    refusals: list[str] = []

    def note_refusal(event: CommandFeedback) -> None:
        if not event.ok:
            refusals.append(event.text)

    bus.subscribe(CommandFeedback, note_refusal)
    session = transport(bus, account, secret, storage)
    sent: list[Message] = []
    bus.subscribe(MessageAdded, lambda event: sent.append(event.message))
    tasks = [
        asyncio.create_task(bus.run(), name="bus"),
        asyncio.create_task(session.run(), name="session"),
    ]
    try:
        if not await _wait_until(lambda: session.state.stage is ConnectionStage.READY, timeout):
            print(
                _("could not connect within {timeout:.0f} s: {problems}").format(
                    timeout=timeout, problems="; ".join(problems[-2:])
                ),
                file=sys.stderr,
            )
            return 1
        # Беседа открывается до отправки: от нее зависит и шифрование, и то,
        # куда уйдет вложение - upload берет адресата из активной беседы.
        session.open_conversation(target)
        if not await _await_omemo(session, target, timeout):
            print(_("OMEMO keys not ready, the conversation is encrypted"), file=sys.stderr)
            return 1
        if attachment is not None:
            await session.upload(str(attachment))
        if text:
            await session.handle_command(SendText(target, text))
        return await _confirm_sent(sent, refusals, text, attachment, timeout)
    finally:
        session.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _await_omemo(session: "SlixmppSession", target: str, timeout: float) -> bool:
    """Дождаться готовности ключей, если беседа зашифрована.

    Для открытой беседы ждать нечего: незашифрованное сообщение уходит сразу.
    Зашифрованному нужен опубликованный бандл, иначе отправка отказывает с
    "ключи OMEMO еще не готовы".
    """
    if not session._omemo_wanted(target):
        return True
    return await _wait_until(lambda: session._omemo_ready, timeout)


async def _confirm_sent(
    sent: list[Message],
    refusals: list[str],
    text: str,
    attachment: Path | None,
    timeout: float,
) -> int:
    """Дождаться, пока отправленное появится в ленте и будет подтверждено."""

    def delivered() -> bool:
        if text and not any(item.body == text for item in sent):
            return False
        return not (attachment is not None and not sent)

    if not await _wait_until(delivered, timeout):
        reason = refusals[-1] if refusals else _("no response from the server")
        print(_("not sent: {reason}").format(reason=reason), file=sys.stderr)
        return 1
    if refusals:
        print(_("not sent: {reason}").format(reason=refusals[-1]), file=sys.stderr)
        return 1
    # Подтверждение потоком приходит после отправки, и без паузы процесс
    # закрывает сокет раньше, чем сервер успевает принять строфу.
    await _wait_until(lambda: any(item.state is DeliveryState.ACKED for item in sent), 5.0)
    return 0


async def _wait_until(condition: Callable[[], bool], timeout: float) -> bool:
    """Дождаться условия опросом. Ложь означает, что время вышло."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return condition()


def _batch_text(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str | None:
    """Текст пакетного сообщения. ``None`` означает обычный запуск с интерфейсом."""
    if args.stdin and args.message is not None:
        parser.error(_("--stdin and --message exclude each other"))
    if args.stdin:
        text = sys.stdin.read().strip()
        if not text:
            parser.error(_("standard input is empty"))
        return text
    message: str | None = args.message
    return message


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. Возвращает код завершения процесса."""
    # Язык выбирается до сборки разборщика: справка и ошибки разбора аргументов
    # выводятся уже на нем. Язык из конфига включает _apply_config.
    i18n.set_language(_startup_language(argv))
    parser = build_parser()
    args = parser.parse_args(argv)

    # Профиль выбирается до конфига: конфиг лежит внутри профиля, и пути к нему
    # без выбранной учетной записи нет. Перенос данных прежней раскладки идет
    # там же, иначе первый запуск профиля прочитал бы конфиг уже после переноса.
    jid = _select_profile(parser, args)
    default_resource = ""
    if jid:
        _migrate_profile(jid)
        # Ресурс создается вместе с профилем, как у Conversations и Dino, даже
        # если конфиг задает свой: тогда при удалении ключа из конфига ресурс
        # профиля остается тем же, а не появляется новый.
        default_resource = _default_resource(jid)
        # Пример создается после переноса: иначе он занял бы место конфига
        # прежней раскладки, и перенос пропустил бы его как уже существующий.
        # С явным --config настройки лежат в другом месте, и пример в профиле
        # только сбивал бы с толку.
        if args.config is None:
            _write_example_config(jid, default_resource)

    try:
        _apply_config(parser, args)
    except ValueError as error:
        parser.error(str(error))

    live = _account_from(args, default_resource)
    if live is not None:
        problem = live.validate()
        if problem:
            parser.error(problem)

    if args.no_xml:
        if args.layout is not None and args.layout != "focus":
            parser.error(
                _("--no-xml is incompatible with --layout {layout}").format(layout=args.layout)
            )
        args.layout = "focus"

    rate: float = args.rate if args.rate is not None else _DEFAULT_RATE
    # Имя сценария передается как есть: подмена наружу выдавала не то имя,
    # которое ввел пользователь, а сессия во всех сообщениях называла себя иначе.
    scenario: str = args.scenario if args.scenario is not None else MockSession.SCENARIOS[0]
    layout: str = args.layout if args.layout is not None else _DEFAULT_LAYOUT
    log_level: str = args.log_level if args.log_level is not None else _DEFAULT_LOG_LEVEL

    configure_logging(log_level)

    batch_text = _batch_text(args, parser)
    batch_file: Path | None = args.file
    if batch_text is not None or batch_file is not None:
        if live is None:
            parser.error(
                _(
                    "sending without the interface requires --jid: "
                    "the emulator has nowhere to send it"
                )
            )
        target = args.to or ""
        if not target:
            parser.error(_("no recipient given: --to <jid>"))
        if batch_file is not None and not batch_file.is_file():
            parser.error(_("file unavailable: {path}").format(path=batch_file))
        transport = _load_transport(parser)
        try:
            batch_secret = _obtain_password(live)
        except PasswordError as error:
            parser.error(str(error))
        # База та же, что у интерактивного клиента с этим --jid: профиль один и
        # тот же, а в базе ключи OMEMO и сохраненные беседы. Без нее пакетный
        # режим не смог бы ни зашифровать сообщение, ни узнать, что эта беседа
        # зашифрована.
        batch_storage = _open_storage(args.no_history)
        return asyncio.run(
            _send_once(
                transport,
                live,
                batch_secret,
                batch_storage,
                target,
                batch_text or "",
                batch_file,
                _BATCH_TIMEOUT,
            )
        )

    # Textual импортируется только здесь, после ветки пакетного режима: конвейеру
    # и cron он не нужен, а импорт добавляет к каждому запуску около 150 мс и
    # 15 МБ памяти. Импорт стоит до запроса пароля, чтобы сломанная установка
    # интерфейса обнаружилась раньше, чем пользователь введет пароль.
    from termisations.app import TermisationsApp

    bus = EventBus(logging.getLogger("termisations.bus"))
    session: Session
    if live is not None:
        transport = _load_transport(parser)
        try:
            secret = _obtain_password(live)
        except PasswordError as error:
            parser.error(str(error))
        session = transport(bus, live, secret, _open_storage(args.no_history))
    else:
        session = MockSession(bus, rate=rate, scenario=scenario)
    # История ввода общая для всех профилей и от учетной записи не зависит.
    # Эмулятор получает ее только на чтение: его команды в файл не попадают.
    input_log = _open_input_log(args.no_history, live is not None)
    app = TermisationsApp(
        bus, session, layout=layout, xml_buffer=args.xml_buffer, input_log=input_log
    )

    try:
        app.run()
    except KeyboardInterrupt:
        # Ctrl+C внутри приложения перехвачен и спрашивает подтверждение.
        # Сюда прерывание доходит только снаружи цикла событий.
        return 130
    finally:
        if input_log is not None:
            # Закрытие идет в своем цикле: цикл приложения уже остановлен, а
            # поток истории держит соединение и без закрытия пережил бы процесс.
            asyncio.run(input_log.close())
    return 0
