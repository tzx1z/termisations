"""Пути по XDG Base Directory, каталоги профилей и права на них.

Данные разнесены по трем каталогам: настройки в ``XDG_CONFIG_HOME``, база и
ключевой материал в ``XDG_DATA_HOME``, кэш в ``XDG_CACHE_HOME``. Разделение не
косметическое: каталог кэша можно удалить в любой момент, и это не должно
задевать историю переписки и решения о доверии к устройствам.

Внутри каждого из трех каталогов данные разложены по профилям - подкаталогам с
именем bare JID. Без этого второй аккаунт на той же машине переиспользует историю,
кэш возможностей и ключи OMEMO первого. Профиль выбирается один раз при старте
(``use_profile``) и дальше виден всем, кто строит пути: кэш возможностей
открывается из глубины протокольного слоя, и протаскивать туда параметр дороже,
чем хранить выбор процесса.

Каталог данных без выбранного профиля недоступен: общего каталога истории нет,
и путь в обход профиля означал бы смешение учетных записей.
Настройки и кэш в этом случае берутся из корня: эмулятору профиль не нужен, а
переносу данных прежней раскладки корневые пути нужны сами по себе. Исключение
одно и названо явно - ``shared_data_file``: история ввода общая для всех
профилей, потому что набранная команда принадлежит человеку, а не учетной записи.

Права выставляются явно, а не полагаются на umask: в каталоге данных лежат ключи
OMEMO и история, и файл, доступный группе, - это утечка, о которой никто не
узнает. Каталоги 0700, файлы 0600.
"""

import os
import stat
from pathlib import Path
from typing import Final

from termisations.core.i18n import _

__all__ = [
    "CACHE_MODE",
    "DIR_MODE",
    "FILE_MODE",
    "cache_dir",
    "cache_file",
    "cache_root",
    "config_file",
    "config_home",
    "config_root",
    "current_profile",
    "data_dir",
    "data_file",
    "data_root",
    "ensure_dir",
    "profile_name",
    "secure_file",
    "shared_data_file",
    "use_profile",
]

APP: Final = "termisations"

# Каталоги: только владелец. Файлы: чтение и запись только владельцем.
DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600

# Кэш строгих прав не требует: там нет секретов, только аватары и ответы disco.
# Отдельная константа нужна, чтобы разница была видна в коде, а не подразумевалась.
CACHE_MODE: Final = 0o700

# Символы, недопустимые в имени каталога профиля. В JID их быть не может, но
# имя каталога строится из внешней строки, и проверка тут явная.
_FORBIDDEN: Final = frozenset("/\\\0")

# Выбранный профиль: нормализованный bare JID. Пустая строка означает, что
# профиля нет, - так работает только эмулятор.
_profile = ""


def profile_name(jid: str) -> str:
    """Имя каталога профиля по JID: bare-часть в нижнем регистре.

    ``ValueError`` с текстом для пользователя, если адрес не годится ни в
    учетную запись, ни в имя каталога.
    """
    bare = jid.split("/", 1)[0].strip().lower()
    local, separator, domain = bare.partition("@")
    if not separator or not local or not domain:
        raise ValueError(
            _("account address must look like user@server, got: {jid!r}").format(jid=jid)
        )
    if _FORBIDDEN.intersection(bare) or any(character.isspace() for character in bare):
        raise ValueError(
            _("address {jid!r} does not fit as a profile directory name").format(jid=jid)
        )
    if not bare.isprintable():
        raise ValueError(_("address {jid!r} contains non-printable characters").format(jid=jid))
    return bare


def use_profile(jid: str) -> str:
    """Выбрать профиль на весь процесс. Пустая строка снимает выбор.

    Возвращает имя каталога профиля. Вызывается один раз при старте, до чтения
    конфига: каталог профиля нужно знать раньше, чем появится что читать.
    """
    global _profile
    _profile = profile_name(jid) if jid else ""
    return _profile


def current_profile() -> str:
    """Имя каталога выбранного профиля. Пустая строка - профиль не выбран."""
    return _profile


def _home(variable: str, fallback: str) -> Path:
    """Каталог из переменной XDG или запасной путь в домашнем каталоге.

    Пустая переменная считается незаданной, как требует спецификация XDG:
    ``XDG_DATA_HOME=""`` в окружении встречается и не должен давать путь ``/``.
    """
    value = os.environ.get(variable, "").strip()
    if value:
        return Path(value)
    return Path.home() / fallback


def config_root() -> Path:
    """Корень настроек ``~/.config/termisations``: общий для всех профилей."""
    return _home("XDG_CONFIG_HOME", ".config") / APP


def data_root() -> Path:
    """Корень данных ``~/.local/share/termisations``."""
    return _home("XDG_DATA_HOME", ".local/share") / APP


def cache_root() -> Path:
    """Корень кэша ``~/.cache/termisations``."""
    return _home("XDG_CACHE_HOME", ".cache") / APP


def config_home() -> Path:
    """Каталог настроек профиля. Без профиля - корень настроек."""
    return config_root() / _profile if _profile else config_root()


def data_dir() -> Path:
    """Каталог данных профиля: база и ключи.

    Без выбранного профиля пути нет. История и ключи OMEMO принадлежат учетной
    записи, и запись мимо профиля означала бы ровно то смешение аккаунтов, ради
    которого профили и заведены.
    """
    if not _profile:
        raise RuntimeError(_("no profile selected: only an account has a data directory"))
    return data_root() / _profile


def cache_dir() -> Path:
    """Каталог кэша профиля: журнал, аватары, кэш disco. Без профиля - корень."""
    return cache_root() / _profile if _profile else cache_root()


def config_file(name: str = "config.toml") -> Path:
    """Путь файла настроек. Каталог не создается: чтения достаточно."""
    return config_home() / name


def data_file(name: str) -> Path:
    """Путь файла в каталоге данных вместе с созданием каталога с правами 0700."""
    return ensure_dir(data_dir(), DIR_MODE) / name


def shared_data_file(name: str) -> Path:
    """Путь файла в корне каталога данных: один на все профили.

    Нужен истории ввода: введенные команды к учетной записи не привязаны, и
    профильный каталог им не подходит. Все остальное в каталоге данных остается
    профильным, поэтому отдельная функция, а не послабление в ``data_dir``.
    """
    return ensure_dir(data_root(), DIR_MODE) / name


def cache_file(name: str) -> Path:
    """Путь файла в каталоге кэша вместе с созданием каталога."""
    return ensure_dir(cache_dir(), CACHE_MODE) / name


def ensure_dir(directory: Path, mode: int = DIR_MODE) -> Path:
    """Создать каталог и привести его права к нужным.

    ``mkdir`` применяет umask, поэтому права выставляются отдельным вызовом:
    иначе при umask 0022 каталог данных оказывается читаемым всей системе.
    Каталог приложения над профилем приводится к тем же правам: по списку
    профилей видно, какие учетные записи заведены на машине, и права 0700 на
    вложенном каталоге при доступном всем корне ничего не закрывают.
    """
    directory.mkdir(parents=True, exist_ok=True)
    targets = [directory]
    if directory.parent.name == APP:
        targets.append(directory.parent)
    for target in targets:
        if stat.S_IMODE(target.stat().st_mode) != mode:
            target.chmod(mode)
    return directory


def secure_file(path: Path) -> Path:
    """Привести права существующего файла к 0600.

    Вызывается после создания файла, а не вместо него: между ``open`` и ``chmod``
    файл существует с правами по umask, поэтому создавать его следует уже с
    ``os.open(..., mode=FILE_MODE)`` там, где это возможно. Здесь - страховка для
    файлов, созданных сторонним кодом, например базой SQLite.
    """
    if path.exists() and stat.S_IMODE(path.stat().st_mode) != FILE_MODE:
        path.chmod(FILE_MODE)
    return path
