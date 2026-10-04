"""Метаданные проекта в pyproject.toml, пакете и файле AppStream.

Лицензия, адреса, версия и автор записаны в трех местах и расходятся незаметно:
ссылка на чужой репозиторий или забытый релиз в metainfo видны только в центре
приложений. Тест сверяет все три источника между собой.
"""

import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Final
from xml.etree import ElementTree

import pytest

import termisations

ROOT: Final = Path(__file__).resolve().parents[1]
METAINFO: Final = ROOT / "data" / "dev.inkov.termisations.metainfo.xml"

# Типы ссылок AppStream и соответствующие им ключи [project.urls].
URL_KEYS: Final = {
    "homepage": "Homepage",
    "vcs-browser": "Repository",
    "bugtracker": "Issues",
    "help": "Discussions",
}

# Атрибут xml:lang в представлении ElementTree.
XML_LANG: Final = "{http://www.w3.org/XML/1998/namespace}lang"


def load_pyproject() -> dict[str, Any]:
    """Содержимое pyproject.toml."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def load_metainfo() -> ElementTree.Element:
    """Корневой элемент metainfo."""
    return ElementTree.parse(METAINFO).getroot()


def test_metainfo_matches_the_project() -> None:
    """Лицензия, адреса, команда и версия в metainfo совпадают с pyproject.toml.

    Первый релиз в metainfo обязан совпадать с версией проекта: так новая версия
    не выйдет без записи о релизе.
    """
    project = load_pyproject()["project"]
    component = load_metainfo()
    assert component.get("type") == "console-application"
    assert component.findtext("project_license") == project["license"]
    assert project["license"] == termisations.__license__
    urls = {url.get("type"): url.text for url in component.findall("url")}
    assert {URL_KEYS[kind]: value for kind, value in urls.items()} == project["urls"]
    assert component.findtext("provides/binary") in project["scripts"]
    release = component.find("releases/release")
    assert release is not None
    assert release.get("version") == project["version"] == termisations.__version__


def test_author_is_the_same_everywhere() -> None:
    """Автор и контакт одинаковы в pyproject.toml, пакете и metainfo."""
    project = load_pyproject()["project"]
    assert project["maintainers"] == project["authors"]
    author = project["authors"][0]
    assert termisations.__author__ == f"{author['name']} <{author['email']}>"

    component = load_metainfo()
    developer = component.find("developer")
    assert developer is not None
    component_id = component.findtext("id") or ""
    assert component_id.startswith(f"{developer.get('id')}.")
    names = [item.text for item in developer.findall("name") if XML_LANG not in item.attrib]
    assert names == [author["name"]]
    assert component.findtext("developer_name") == author["name"]
    assert component.findtext("update_contact") == author["email"]


def test_metainfo_goes_into_the_source_archive() -> None:
    """Каталог data попадает в sdist: оттуда metainfo берут сборщики пакетов."""
    sdist = load_pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "data" in sdist["include"]


@pytest.mark.skipif(shutil.which("appstreamcli") is None, reason="нет appstreamcli")
def test_metainfo_passes_appstream_validation() -> None:
    """Файл проходит проверку appstreamcli в строгом режиме.

    Сеть не используется: доступность ссылок проверяется при выпуске, а не на
    каждом прогоне тестов.
    """
    result = subprocess.run(
        ["appstreamcli", "validate", "--no-net", "--pedantic", str(METAINFO)],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
