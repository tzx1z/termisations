"""Язык интерфейса: выбор языка, перевод, целостность каталога.

Каталог проверяется по исходникам, а не по списку в тесте. Каждая строка,
переданная литералом в ``_``, ``N_`` или ``ngettext``, должна иметь русский
перевод с теми же подстановками, а каждая запись каталога - строку в коде.
Последняя проверка ищет кириллицу в строках кода: она означает текст, который
прошел мимо каталога и не переключается на английский.
"""

import ast
import re
import string
import tomllib
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from termisations.core import i18n
from termisations.core.i18n import N_, _, ngettext

_PACKAGE: Final = Path(__file__).resolve().parent.parent / "src" / "termisations"
_CATALOG_DIR: Final = _PACKAGE / "locale" / "ru"
_CYRILLIC: Final = re.compile("[А-Яа-яЁё]")

# Имена функций перевода. Вызов с литералом дает ключ каталога.
_MARKERS: Final[frozenset[str]] = frozenset({"_", "N_", "ngettext"})

# Кириллица, которая остается в коде законно. Это не текст интерфейса, а данные:
# раскладка ЙЦУКЕН для сочетаний клавиш, русские синонимы ответов, которые ввод
# принимает на любом языке, и комментарии внутри SQL и CSS.
_ALLOWED_WORDS: Final[frozenset[str]] = frozenset({"да", "нет"})
_KEY_NAME: Final = re.compile(r"^(?:(?:ctrl|alt|shift)\+)*[А-Яа-яЁё]$")
_IGNORED_ASSIGNMENTS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("app.py", "_CYRILLIC_TWINS"),
        ("core/storage.py", "_MIGRATIONS"),
        ("ui/prompt.py", "PromptInput.DEFAULT_CSS"),
    }
)

_PERCENT: Final = re.compile(r"%(?:\([A-Za-z_]+\))?[-#0 +]*\d*(?:\.\d+)?[sdirfgxX%]")


def _sources() -> Iterator[tuple[str, ast.Module]]:
    """Модули пакета: относительный путь и дерево разбора."""
    for path in sorted(_PACKAGE.rglob("*.py")):
        yield path.relative_to(_PACKAGE).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _marker_name(call: ast.Call) -> str | None:
    """Имя функции перевода в вызове или None."""
    func = call.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name if name in _MARKERS else None


def _messages() -> dict[str, list[str]]:
    """Ключи каталога из кода: строка и места, где она встречается."""
    found: dict[str, list[str]] = {}
    for rel, tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _marker_name(node) is None or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.setdefault(first.value, []).append(f"{rel}:{node.lineno}")
    return found


def _catalog_files() -> dict[str, dict[str, object]]:
    """Файлы русского каталога по отдельности: нужны для поиска расхождений."""
    return {
        path.name: tomllib.loads(path.read_text(encoding="utf-8"))
        for path in sorted(_CATALOG_DIR.glob("*.toml"))
    }


def _placeholders(text: str) -> tuple[Counter[str], list[str]]:
    """Подстановки строки: поля str.format без учета порядка и %-поля по порядку."""
    fields: Counter[str] = Counter()
    try:
        for _literal, field, spec, conversion in string.Formatter().parse(text):
            if field is not None:
                fields[f"{{{field}!{conversion}:{spec}}}"] += 1
    except ValueError:
        fields = Counter(char for char in text if char in "{}")
    return fields, _PERCENT.findall(text)


def _docstring_ids(tree: ast.Module) -> set[int]:
    """Строки документации модуля, классов, функций и атрибутов."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for statement in body:
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                ids.add(id(statement.value))
    return ids


def _ignored_ids(rel: str, tree: ast.Module) -> set[int]:
    """Строки внутри присваиваний из _IGNORED_ASSIGNMENTS."""
    ids: set[int] = set()
    scopes: list[tuple[str, list[ast.stmt]]] = [("", tree.body)]
    scopes += [(f"{n.name}.", n.body) for n in tree.body if isinstance(n, ast.ClassDef)]
    for prefix, body in scopes:
        for statement in body:
            if isinstance(statement, ast.Assign):
                targets, value = statement.targets, statement.value
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                targets, value = [statement.target], statement.value
            else:
                continue
            names = {f"{prefix}{t.id}" for t in targets if isinstance(t, ast.Name)}
            if any((rel, name) in _IGNORED_ASSIGNMENTS for name in names):
                ids.update(id(sub) for sub in ast.walk(value))
    return ids


# Выбор языка.


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ru", "ru"),
        ("RU", "ru"),
        ("ru_RU.UTF-8", "ru"),
        ("ru-RU", "ru"),
        ("en_US@euro", "en"),
        (" en ", "en"),
        ("de_DE.UTF-8", None),
        ("", None),
    ],
)
def test_normalize_language(value: str, expected: str | None) -> None:
    assert i18n.normalize_language(value) == expected


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, "en"),
        ({"LANG": "ru_RU.UTF-8"}, "ru"),
        ({"LANG": "en_US.UTF-8"}, "en"),
        ({"LANG": "de_DE.UTF-8"}, "en"),
        ({"LANG": "en_US.UTF-8", "LC_MESSAGES": "ru_RU.UTF-8"}, "ru"),
        ({"LC_ALL": "en_US.UTF-8", "LC_MESSAGES": "ru_RU.UTF-8"}, "en"),
        ({"LANG": "en_US.UTF-8", "LANGUAGE": "de:ru"}, "ru"),
        ({"LANG": "C", "LANGUAGE": "ru"}, "en"),
        ({"LC_ALL": "POSIX", "LANG": "ru_RU.UTF-8"}, "en"),
    ],
)
def test_detect_language(environ: dict[str, str], expected: str) -> None:
    assert i18n.detect_language(environ) == expected


def test_set_language_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="de"):
        i18n.set_language("de")
    assert i18n.get_language() == "ru"


def test_english_returns_source() -> None:
    message = next(iter(_messages()))
    i18n.set_language("en")
    assert i18n.get_language() == "en"
    assert _(message) == message


def test_unknown_message_passes_through() -> None:
    assert _("no such message in the catalog") == "no such message in the catalog"
    assert N_("marker only") == "marker only"


@pytest.mark.parametrize(
    ("count", "form"),
    [(1, 0), (21, 0), (2, 1), (4, 1), (22, 1), (5, 2), (11, 2), (12, 2), (14, 2), (0, 2), (111, 2)],
)
def test_russian_plural_rule(count: int, form: int) -> None:
    assert i18n._PLURAL_RULES["ru"](count) == form


def test_ngettext_falls_back_to_english_rule() -> None:
    one, many = "{count} unknown thing", "{count} unknown things"
    assert ngettext(one, many, 1) == one
    assert ngettext(one, many, 3) == many


def test_catalog_loads_plural_forms() -> None:
    plurals = [
        (key, value)
        for data in _catalog_files().values()
        for key, value in data.items()
        if isinstance(value, list)
    ]
    for key, forms in plurals:
        assert ngettext(key, key, 1) == forms[0]
        assert ngettext(key, key, 3) == forms[1]
        assert ngettext(key, key, 5) == forms[2]


# Целостность каталога.


def test_catalog_files_parse() -> None:
    files = _catalog_files()
    assert files, "в locale/ru нет ни одного файла перевода"
    for name, data in files.items():
        for key, value in data.items():
            ok = isinstance(value, str) or (
                isinstance(value, list)
                and len(value) == 3
                and all(isinstance(f, str) for f in value)
            )
            assert ok, f"{name}: {key!r} - ожидается строка или список из трех форм"
            assert value, f"{name}: {key!r} - пустой перевод"


def test_catalog_has_no_conflicts() -> None:
    seen: dict[str, tuple[str, object]] = {}
    conflicts = []
    for name, data in _catalog_files().items():
        for key, value in data.items():
            if key in seen and seen[key][1] != value:
                conflicts.append(f"{key!r}: {seen[key][0]} и {name}")
            seen.setdefault(key, (name, value))
    assert not conflicts, "разные переводы одной строки:\n" + "\n".join(conflicts)


def test_every_message_is_translated() -> None:
    catalog = {key for data in _catalog_files().values() for key in data}
    missing = [
        f"{places[0]}: {message!r}"
        for message, places in _messages().items()
        if message not in catalog
    ]
    assert not missing, "строки без перевода:\n" + "\n".join(missing)


def test_catalog_has_no_stale_entries() -> None:
    messages = _messages()
    stale = [
        f"{name}: {key!r}"
        for name, data in _catalog_files().items()
        for key in data
        if key not in messages
    ]
    assert not stale, "записи каталога без строки в коде:\n" + "\n".join(stale)


def test_placeholders_match() -> None:
    mismatched = []
    for name, data in _catalog_files().items():
        for key, value in data.items():
            forms = value if isinstance(value, list) else [value]
            expected = _placeholders(key)
            # Русская форма для 1 служит и для 21, поэтому число есть во всех формах.
            for form in forms:
                assert isinstance(form, str)
                if _placeholders(form) != expected:
                    mismatched.append(f"{name}: {key!r} -> {form!r}")
    assert not mismatched, "подстановки не совпадают:\n" + "\n".join(mismatched)


def test_messages_are_english() -> None:
    russian = [f"{places[0]}: {m!r}" for m, places in _messages().items() if _CYRILLIC.search(m)]
    assert not russian, "ключ каталога должен быть английским:\n" + "\n".join(russian)


def test_marker_arguments_are_literals_or_names() -> None:
    """``_(f"...")`` и ``_("..." + x)`` ищут в каталоге строку, которой там нет."""
    wrong = []
    for rel, tree in _sources():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _marker_name(node)
                and node.args
                and isinstance(node.args[0], ast.JoinedStr | ast.BinOp)
            ):
                wrong.append(f"{rel}:{node.lineno}")
    assert not wrong, "перевод собранной строки:\n" + "\n".join(wrong)


def test_no_untranslated_text_in_code() -> None:
    leftovers = []
    for rel, tree in _sources():
        skipped = _docstring_ids(tree) | _ignored_ids(rel, tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value
            if id(node) in skipped or not _CYRILLIC.search(text):
                continue
            if text in _ALLOWED_WORDS or _KEY_NAME.match(text):
                continue
            leftovers.append(f"{rel}:{node.lineno}: {text[:60]!r}")
    assert not leftovers, "русский текст мимо каталога:\n" + "\n".join(leftovers)
