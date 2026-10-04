"""Язык интерфейса и перевод строк.

Исходный язык строк в коде - английский: строка сама служит ключом перевода.
Переводы лежат в ``termisations/locale/<язык>/*.toml``, по файлу на модуль. Ключ -
английская строка, значение - перевод. У строки с числом значение - список форм
множественного числа в порядке правила языка: для русского это формы для 1, 2 и 5.

Язык - состояние процесса. Его выбирает ``cli.main`` при запуске и меняет команда
``/lang``. Поэтому строка переводится в момент показа, а не при импорте: константы
модулей помечаются ``N_`` и проходят через ``_`` там, где их выводят.

Строка без перевода показывается по-английски. Полноту каталога и совпадение
подстановок проверяет tests/test_i18n.py.
"""

import functools
import logging
import os
import tomllib
from collections.abc import Callable, Mapping
from importlib import resources
from types import MappingProxyType
from typing import Final

__all__ = [
    "DEFAULT_LANGUAGE",
    "ENV_LANG",
    "LANGUAGES",
    "N_",
    "_",
    "detect_language",
    "get_language",
    "ngettext",
    "normalize_language",
    "set_language",
]

type Catalog = Mapping[str, str | tuple[str, ...]]

LANGUAGES: Final[tuple[str, ...]] = ("en", "ru")
"""Поддерживаемые языки. Первый - исходный, каталога у него нет."""

DEFAULT_LANGUAGE: Final = "en"
"""Язык, если локаль системы не задана или не поддерживается."""

ENV_LANG: Final = "TERMISATIONS_LANG"
"""Переменная окружения с явным выбором языка."""

# Переменные локали в порядке, в котором их читает gettext. LANGUAGE - список
# предпочтений через двоеточие, остальные - одно имя локали вида ru_RU.UTF-8.
_PREFERENCE_VARIABLE: Final = "LANGUAGE"
_LOCALE_VARIABLES: Final[tuple[str, ...]] = ("LC_ALL", "LC_MESSAGES", "LANG")

# Локали без языка. При них gettext не читает LANGUAGE, и перевода нет.
_NEUTRAL_LOCALES: Final[frozenset[str]] = frozenset({"c", "posix"})


def _plural_en(count: int) -> int:
    """Форма английского числа: 0 - одно, 1 - остальное."""
    return 0 if count == 1 else 1


def _plural_ru(count: int) -> int:
    """Форма русского числа: 0 - "1 строфа", 1 - "2 строфы", 2 - "5 строф"."""
    tail, tail100 = count % 10, count % 100
    if tail == 1 and tail100 != 11:
        return 0
    if 2 <= tail <= 4 and not 12 <= tail100 <= 14:
        return 1
    return 2


_PLURAL_RULES: Final[Mapping[str, Callable[[int], int]]] = MappingProxyType(
    {"en": _plural_en, "ru": _plural_ru}
)

_log = logging.getLogger(__name__)

_language: str = DEFAULT_LANGUAGE
_catalog: Catalog = MappingProxyType({})


def _(message: str) -> str:
    """Перевести строку на текущий язык. Строка без перевода возвращается как есть."""
    translated = _catalog.get(message)
    return translated if isinstance(translated, str) else message


def N_(message: str) -> str:  # noqa: N802 - имя по соглашению gettext
    """Пометить строку для каталога без перевода.

    Нужна константам уровня модуля: при импорте язык еще не выбран, и перевод
    делается позже вызовом ``_`` в месте показа.
    """
    return message


def ngettext(singular: str, plural: str, count: int) -> str:
    """Выбрать форму строки по числу и перевести ее.

    Ключ каталога - форма единственного числа. Подстановку числа делает
    вызывающий код: ``ngettext("{count} stanza", "{count} stanzas", n).format(count=n)``.
    """
    forms = _catalog.get(singular)
    if isinstance(forms, tuple):
        index = _PLURAL_RULES[_language](count)
        if index < len(forms):
            return forms[index]
    return singular if count == 1 else plural


def get_language() -> str:
    """Текущий язык интерфейса."""
    return _language


def set_language(language: str) -> None:
    """Сменить язык интерфейса.

    Неподдерживаемый язык отклоняется ``ValueError``: выбор приходит из флага,
    конфига или команды, и ошибку там нужно показать, а не заменить молча.
    """
    global _language, _catalog
    if language not in LANGUAGES:
        supported = ", ".join(LANGUAGES)
        raise ValueError(f"unsupported language {language!r}, expected one of: {supported}")
    _catalog = _load_catalog(language)
    _language = language


def normalize_language(value: str) -> str | None:
    """Код поддерживаемого языка из имени локали или ``None``.

    Понимает ``ru``, ``RU``, ``ru_RU.UTF-8``, ``ru-RU``, ``en_US@euro``.
    """
    code = value.strip().replace("-", "_").split(".", 1)[0].split("@", 1)[0]
    code = code.split("_", 1)[0].lower()
    return code if code in LANGUAGES else None


def detect_language(environ: Mapping[str, str] | None = None) -> str:
    """Язык по локали системы.

    Порядок как у gettext: сначала список LANGUAGE, затем LC_ALL, LC_MESSAGES и
    LANG. Берется первый поддерживаемый язык. Локаль C и POSIX означает отказ от
    перевода, и тогда LANGUAGE не читается.
    """
    env = os.environ if environ is None else environ
    locale_name = next((env[name] for name in _LOCALE_VARIABLES if env.get(name)), "")
    if locale_name.split(".", 1)[0].lower() in _NEUTRAL_LOCALES:
        return DEFAULT_LANGUAGE
    preferences = [item for item in env.get(_PREFERENCE_VARIABLE, "").split(":") if item]
    for candidate in (*preferences, locale_name):
        language = normalize_language(candidate)
        if language is not None:
            return language
    return DEFAULT_LANGUAGE


@functools.cache
def _load_catalog(language: str) -> Catalog:
    """Собрать каталог языка из файлов пакета.

    Поврежденный файл пропускается с записью в журнал: интерфейс с частью строк
    по-английски лучше, чем клиент, который не запускается. Целостность каталога
    проверяют тесты, так что в выпуск такой файл попасть не должен.
    """
    if language == LANGUAGES[0]:
        return MappingProxyType({})
    folder = resources.files("termisations") / "locale" / language
    if not folder.is_dir():
        _log.warning("translation catalog for %s is missing", language)
        return MappingProxyType({})

    merged: dict[str, str | tuple[str, ...]] = {}
    for entry in sorted(folder.iterdir(), key=lambda item: item.name):
        if not entry.name.endswith(".toml"):
            continue
        try:
            data = tomllib.loads(entry.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            _log.warning("translation file %s skipped: %s", entry.name, exc)
            continue
        for key, value in data.items():
            if isinstance(value, str):
                merged[key] = value
            elif isinstance(value, list) and all(isinstance(form, str) for form in value):
                merged[key] = tuple(value)
    return MappingProxyType(merged)
