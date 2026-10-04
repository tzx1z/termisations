"""Профиль учетной записи: переменная окружения и перенос данных прежней раскладки.

До появления профилей клиент держал один конфиг и одну базу на пользователя
системы. Эти файлы остались у всех, кто ставил клиент раньше, поэтому при первом
запуске профиля они переносятся в его каталог: молча начинать с пустой истории
хуже, чем перенести, а требовать ручной команды - значит оставить рабочий клиент
без переписки до тех пор, пока человек не прочитает README.

Переносятся только конфиг и база: кэш возможностей и журнал восстанавливаются
сами, и таскать их за учетной записью незачем.

Перенос не трогает чужие данные. Владелец прежнего набора определяется по
``[account] jid`` из конфига, а если его там нет - по единственной учетной записи
в базе. Если владелец известен и это не текущий профиль, файлы остаются на месте
и дожидаются своего запуска.

Если конфига в профиле нет, при запуске в нем создается пример: все настройки
закомментированы и описаны. Новый пользователь сразу видит, что и где
настраивается, а поведение клиента от примера не меняется.

Ресурс по умолчанию тоже профильный: ``termisations.<хвост>``, где хвост
случайный, создается один раз и хранится в каталоге данных профиля. Так делают
Conversations и Dino: одинаковый ресурс на двух устройствах одной учетной записи
заставляет сервер закрыть первую сессию при входе второй.
"""

import os
import secrets
import sqlite3
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from termisations.core import i18n, paths
from termisations.core.i18n import N_, _
from termisations.core.storage import DB_NAME

__all__ = [
    "CONFIG_NAME",
    "ENV_JID",
    "EXAMPLE_CONFIG",
    "RESOURCE_NAME",
    "RESOURCE_PREFIX",
    "migrate_legacy",
    "new_resource",
    "profile_resource",
    "write_example_config",
]

# Переменная окружения с JID профиля. Нужна там, где флаг не проставить:
# systemd-юнит, cron, обертка в ~/.bashrc.
ENV_JID: Final = "TERMISATIONS_JID"

CONFIG_NAME: Final = "config.toml"

# Файл с ресурсом профиля в каталоге данных. Не в конфиге: конфиг принадлежит
# пользователю, и клиент его не переписывает. Не в базе: с --no-history базы
# нет, и ресурс менялся бы при каждом запуске. Не в кэше: кэш удаляют.
RESOURCE_NAME: Final = "resource"

# Постоянная часть ресурса по умолчанию: по ней видно, какой клиент подключен.
RESOURCE_PREFIX: Final = "termisations"

# Байт случайности в хвосте ресурса. Четыре байта дают восемь шестнадцатеричных
# знаков: совпадение у двух устройств одной учетной записи практически исключено,
# а ресурс остается коротким в списке сессий и в адресах строф.
_RESOURCE_BYTES: Final = 4

# Предел длины ресурса по RFC 7622 в байтах UTF-8. Значение из файла длиннее
# предела считается поврежденным.
_RESOURCE_MAX_BYTES: Final = 1023

# Спутники базы в режиме WAL. После штатного закрытия их нет, но после убитого
# процесса в журнале остаются последние транзакции, и перенос одного файла базы
# потерял бы их.
_DB_SIDECARS: Final[tuple[str, ...]] = ("-wal", "-shm")

# Пример конфига профиля. Заголовки секций рабочие, настройки закомментированы:
# пустая секция ничего не меняет, а включить настройку - значит убрать "# " в
# одной строке. Значения настроек, у которых есть умолчание, совпадают с ним,
# это проверяет tests/test_profile.py. Секции [mock] здесь нет: эмулятор работает
# без профиля и читает конфиг из корня каталога настроек.
#
# Пояснения лежат отдельно, в _EXAMPLE_NOTES, по абзацу на поле note_*: они
# переводятся на язык запуска, а строки настроек остаются в шаблоне как есть,
# это синтаксис TOML, а не текст. У языка умолчания нет, поэтому пример
# показывает язык, на котором он написан: включенная строка ничего не меняет.
EXAMPLE_CONFIG: Final = """\
{note_intro}
#
{note_priority}
#
{note_files}
#
{note_mock}

[account]
{note_resource}
# resource = "{resource}"

{note_server}
# server = "xmpp.example.org"
# port = 5222

{note_password}
#
{note_keyring}
# password_command = "secret-tool lookup jid {jid} type xmpp"
#
{note_pass}

{note_sasl2}
# sasl2 = true

[tls]
{note_direct}
# direct = true

{note_verify}
# verify = true

[ui]
{note_layout}
# layout = "split"

{note_xml_buffer}
# xml_buffer = 2000

{note_lang}
# lang = "{lang}"

[log]
{note_level}
# level = "warning"
"""

# Абзацы пояснений к примеру. Префикс комментария "# " добавляет _comment, а
# поля путей, ресурса и JID подставляются в переведенный текст.
_EXAMPLE_NOTES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "note_intro": N_(
            "termisations profile config.\n"
            "\n"
            "The file was created on the first launch of the profile as an example.\n"
            "All settings are commented out, and the client runs with the default\n"
            'values. To change a setting, remove the "# " at the start of the line and\n'
            "edit the value."
        ),
        "note_priority": N_(
            "Priority: a command line flag beats the config, and the config beats the\n"
            "default. The profile is selected by the --jid flag or the TERMISATIONS_JID\n"
            "variable, so the account address is not in this file: the directory name\n"
            "sets it."
        ),
        "note_files": N_(
            "Profile files:\n"
            "  {config}\n"
            "      this config\n"
            "  {data}\n"
            "      message history, OMEMO keys and the profile resource\n"
            "  {cache}\n"
            "      client.log and the capabilities cache, safe to delete at any time"
        ),
        "note_mock": N_(
            "Emulator settings (--mock, section [mock]) are read from another file:\n{shared}"
        ),
        "note_resource": N_(
            "Resource: the server uses it to tell this client from your other devices.\n"
            "The default is termisations.<random tail>: the tail was created once for\n"
            "this profile and is stored in {resource_file}"
        ),
        "note_server": N_(
            "Server address and port, bypassing the SRV record. Needed if the domain\n"
            "has no SRV record or the server is reachable at another address."
        ),
        "note_password": N_(
            "A command that prints the password to standard output. Without it the\n"
            "password is looked up in the system keyring (service termisations), then in\n"
            "the TERMISATIONS_PASSWORD variable, otherwise it is asked for at startup.\n"
            'All methods are described in the README, section "Password".'
        ),
        "note_keyring": N_(
            "Fedora keyring (GNOME Keyring, KWallet), the password is stored with\n"
            "  secret-tool store --label='XMPP {jid}' jid {jid} type xmpp"
        ),
        "note_pass": N_(
            "The pass password manager: store the password with pass insert xmpp/{jid}\n"
            'and set password_command to "pass show xmpp/{jid}".'
        ),
        "note_sasl2": N_(
            "SASL2 and Bind 2, if the server announced them. false switches to regular\n"
            "SASL: a fallback for a server with a broken SASL2 implementation."
        ),
        "note_direct": N_(
            "Direct TLS per XEP-0368 instead of STARTTLS. If the key is not set, the\n"
            "method is chosen by the SRV record."
        ),
        "note_verify": N_(
            "Server certificate verification. Turn it off only for your own server\n"
            "with a self-signed certificate."
        ),
        "note_layout": N_(
            "Layout at startup: focus - the conversation only, split - the conversation\n"
            "and the RAW XML panel, debug - the RAW XML panel full screen. Ctrl+D\n"
            "switches it in the client."
        ),
        "note_xml_buffer": N_("How many recent stanzas the RAW XML panel keeps."),
        "note_lang": N_(
            "Interface language: en or ru. By default it is taken from the system\n"
            "locale. The --lang flag and the TERMISATIONS_LANG variable take precedence\n"
            "over this key. The /lang command changes the language in a running client\n"
            "until it exits and does not write the choice to this file."
        ),
        "note_level": N_("Log level of client.log: debug, info, warning, error, critical."),
    }
)


def migrate_legacy() -> tuple[str, ...]:
    """Перенести конфиг и базу из корня каталогов в выбранный профиль.

    Возвращает имена перенесенных файлов; пустой кортеж означает, что переносить
    было нечего. Файл, который в профиле уже есть, не трогается: перенос идет
    один раз, при первом запуске профиля.

    Ошибки файловых операций наружу не глушатся: решение, что делать с отказом
    переноса, принимает вызывающий слой.
    """
    name = paths.current_profile()
    if not name:
        raise RuntimeError(_("no profile selected: there is nowhere to move the data"))

    config = paths.config_root() / CONFIG_NAME
    database = paths.data_root() / DB_NAME
    if not config.is_file() and not database.is_file():
        return ()

    owner = _legacy_owner(config, database)
    if owner and owner != name:
        return ()

    moved: list[str] = []
    if _move(config, paths.config_home() / CONFIG_NAME):
        moved.append(config.name)
    if _move(database, paths.data_dir() / DB_NAME):
        moved.append(database.name)
        for suffix in _DB_SIDECARS:
            _move(database.with_name(DB_NAME + suffix), paths.data_dir() / (DB_NAME + suffix))
    return tuple(moved)


def new_resource() -> str:
    """Новый ресурс по умолчанию: постоянная часть и случайный хвост."""
    return f"{RESOURCE_PREFIX}.{secrets.token_hex(_RESOURCE_BYTES)}"


def profile_resource() -> str:
    """Ресурс профиля по умолчанию. При первом вызове создается и сохраняется.

    Пустой или поврежденный файл заменяется новым значением: после сбоя питания
    файл может остаться пустым, и без замены профиль остался бы без ресурса
    навсегда. Если файл одновременно создал параллельный запуск, берется его
    значение: у одного профиля ресурс один.

    Ошибки файловых операций наружу не глушатся: решение, чем заменить ресурс,
    принимает вызывающий слой.
    """
    if not paths.current_profile():
        raise RuntimeError(_("no profile selected: the resource is stored only in a profile"))
    path = paths.data_file(RESOURCE_NAME)
    stored = _read_resource(path)
    if stored:
        return stored
    if stored is not None:
        path.unlink(missing_ok=True)
    resource = new_resource()
    if _create_exclusive(path, resource + "\n"):
        return resource
    return _read_resource(path) or resource


def write_example_config(resource: str) -> Path | None:
    """Создать пример конфига в каталоге профиля, если конфига там еще нет.

    ``resource`` - ресурс профиля по умолчанию: пример показывает его как есть,
    и раскомментированная строка ничего не меняет. Пояснения пишутся на текущем
    языке интерфейса. Возвращает путь созданного файла; None означает, что
    конфиг уже есть и не перезаписывается.

    Ошибки файловых операций наружу не глушатся, как и в ``migrate_legacy``.
    """
    name = paths.current_profile()
    if not name:
        raise RuntimeError(_("no profile selected: there is nowhere to put the example config"))
    target = paths.config_home() / CONFIG_NAME
    if target.exists():
        return None
    values = {
        "config": _tilde(target),
        "data": _tilde(paths.data_dir()),
        "cache": _tilde(paths.cache_dir()),
        "shared": _tilde(paths.config_root() / CONFIG_NAME),
        "resource": resource,
        "resource_file": _tilde(paths.data_dir() / RESOURCE_NAME),
        "jid": name,
        "lang": i18n.get_language(),
    }
    notes = {field: _comment(_(note).format(**values)) for field, note in _EXAMPLE_NOTES.items()}
    text = EXAMPLE_CONFIG.format(**values, **notes)
    paths.ensure_dir(target.parent)
    return target if _create_exclusive(target, text) else None


def _create_exclusive(path: Path, text: str) -> bool:
    """Создать файл с текстом, если его еще нет. Ложь означает, что файл уже есть.

    Файл создается сразу с правами 0600 и флагом O_EXCL: появившийся между
    проверкой и записью файл не перезаписывается. Недописанный файл удаляется:
    оборванное содержимое следующий запуск принял бы за настоящее.
    """
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, paths.FILE_MODE)
    except FileExistsError:
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return True


def _read_resource(path: Path) -> str | None:
    """Сохраненный ресурс. None - файла нет, пустая строка - значение не годится.

    Два случая различаются: удалять можно только поврежденный файл.
    Отсутствующий мог как раз сейчас создать параллельный запуск.
    """
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        return ""
    if not value.isprintable() or len(value.encode()) > _RESOURCE_MAX_BYTES:
        return ""
    return value


def _comment(text: str) -> str:
    """Абзац пояснения в виде комментария TOML: "# " в начале каждой строки."""
    return "\n".join(f"# {line}".rstrip() for line in text.split("\n"))


def _tilde(path: Path) -> str:
    """Путь для показа человеку: домашний каталог сокращен до ``~``."""
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


def _legacy_owner(config: Path, database: Path) -> str:
    """Чей это набор данных. Пустая строка означает, что определить не удалось.

    Сначала ``[account] jid`` прежнего конфига: он был основным способом задать
    учетную запись. Потом единственная запись в базе: если их там несколько,
    однозначного владельца нет.
    """
    jid = _config_jid(config) or _single_account(database)
    if not jid:
        return ""
    try:
        return paths.profile_name(jid)
    except ValueError:
        return ""


def _config_jid(config: Path) -> str:
    """``[account] jid`` прежнего конфига. Нечитаемый файл - не повод падать."""
    try:
        with config.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    account = data.get("account")
    if not isinstance(account, dict):
        return ""
    jid = account.get("jid")
    return jid if isinstance(jid, str) else ""


def _single_account(database: Path) -> str:
    """Учетная запись прежней базы, если она там одна.

    База открывается только на чтение: иначе сам факт проверки создал бы рядом
    журнал WAL в каталоге, из которого мы собираемся переносить файлы.
    """
    if not database.is_file():
        return ""
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error):
        return ""
    try:
        rows = connection.execute("SELECT jid FROM accounts LIMIT 2").fetchall()
    except sqlite3.Error:
        return ""
    finally:
        connection.close()
    return str(rows[0][0]) if len(rows) == 1 else ""


def _move(source: Path, target: Path) -> bool:
    """Перенести файл в профиль. Ложь означает, что переносить было нечего."""
    if not source.is_file() or target.exists():
        return False
    paths.ensure_dir(target.parent)
    source.rename(target)
    paths.secure_file(target)
    return True
