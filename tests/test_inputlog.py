"""История ввода: общий файл, отбор строк, лимит, права и два клиента разом.

Проверка идет на настоящем файле, а не в памяти: общий каталог, права 0600 и
одновременная запись двух клиентов - это ровно то, что в памяти не
воспроизводится.
"""

import sqlite3
import stat
from pathlib import Path
from typing import Final

import pytest

from termisations.core import i18n, paths
from termisations.core.inputlog import HISTORY_LIMIT, LOG_NAME, InputLog, is_command
from termisations.ui import prompt

ALICE: Final = "alice@example.org"
BOB: Final = "bob@simple.org"


def log_path(tmp_path: Path) -> Path:
    """Путь общего файла истории во временных каталогах теста."""
    return tmp_path / "xdg" / "xdg_data_home" / "termisations" / LOG_NAME


async def test_lines_survive_reopen(tmp_path: Path) -> None:
    """Записанная команда видна после повторного открытия файла."""
    target = tmp_path / "input.db"
    log = await InputLog.open(target)
    try:
        await log.append("/mam bob@example.org --limit 50")
        await log.append("/ping")
    finally:
        await log.close()

    again = await InputLog.open(target)
    try:
        assert again.lines == ("/mam bob@example.org --limit 50", "/ping")
    finally:
        await again.close()


async def test_only_commands_are_written(tmp_path: Path) -> None:
    """В общий файл идут команды, а тексты сообщений - нет.

    Файл один на все профили, и тело сообщения в нем означало бы переписку одной
    учетной записи в данных другой.
    """
    target = tmp_path / "input.db"
    log = await InputLog.open(target)
    try:
        await log.append("/roster")
        await log.append("привет, это текст сообщения")
        await log.append("//roster")
        await log.append("   ")
    finally:
        await log.close()

    again = await InputLog.open(target)
    try:
        assert again.lines == ("/roster",)
    finally:
        await again.close()


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("/help", True),
        ("  /omemo status  ", True),
        ("//help", False),
        ("обычный текст", False),
        ("", False),
        ("текст со /слэшем внутри", False),
    ],
)
def test_command_detection(line: str, expected: bool) -> None:
    """Командой считается строка с одиночным слэшем в начале."""
    assert is_command(line) is expected


async def test_repeat_in_a_row_is_not_stored(tmp_path: Path) -> None:
    """Повтор подряд не удваивается, а тот же текст после другого - пишется."""
    target = tmp_path / "input.db"
    log = await InputLog.open(target)
    try:
        await log.append("/roster")
        await log.append("/roster")
        await log.append("/ping")
        await log.append("/roster")
    finally:
        await log.close()

    again = await InputLog.open(target)
    try:
        assert again.lines == ("/roster", "/ping", "/roster")
    finally:
        await again.close()


async def test_limit_drops_oldest_lines(tmp_path: Path) -> None:
    """Сверх лимита в файле остаются последние строки, старые уходят."""
    target = tmp_path / "input.db"
    log = await InputLog.open(target, limit=5)
    try:
        for index in range(20):
            await log.append(f"/ping {index}")
    finally:
        await log.close()

    # Обрезка идет не на каждой вставке, поэтому лимит проверяется после
    # повторного открытия: оно обрезает файл само.
    again = await InputLog.open(target, limit=5)
    try:
        assert again.lines == tuple(f"/ping {index}" for index in range(15, 20))
    finally:
        await again.close()


def test_default_limit_is_shared_with_the_widget() -> None:
    """Лимит файла и лимит истории в памяти - одна и та же величина.

    Поиск Ctrl+R идет по памяти, и записи сверх ее лимита не нашлись бы ничем.
    """
    assert prompt.HISTORY_LIMIT == HISTORY_LIMIT


async def test_file_is_owner_only(tmp_path: Path) -> None:
    """Файл истории 0600, каталог над ним 0700: в командах есть адреса."""
    paths.use_profile(ALICE)
    log = await InputLog.open()
    try:
        await log.append("/chat bob@example.org")
    finally:
        await log.close()

    target = log_path(tmp_path)
    assert target.is_file(), "общий файл истории лежит в корне каталога данных"
    assert stat.S_IMODE(target.stat().st_mode) == paths.FILE_MODE
    assert stat.S_IMODE(target.parent.stat().st_mode) == paths.DIR_MODE


async def test_history_is_shared_between_profiles(tmp_path: Path) -> None:
    """Команда, набранная в одном профиле, видна в другом: файл общий."""
    paths.use_profile(ALICE)
    first = await InputLog.open()
    try:
        await first.append("/join room@conference.example.org")
    finally:
        await first.close()

    paths.use_profile(BOB)
    second = await InputLog.open()
    try:
        assert second.lines == ("/join room@conference.example.org",)
    finally:
        await second.close()

    assert log_path(tmp_path).is_file()
    # Профильных копий не появилось: файл ровно один.
    profiles = tmp_path / "xdg" / "xdg_data_home" / "termisations"
    assert not list(profiles.glob(f"*/{LOG_NAME}"))


async def test_read_only_mode_writes_nothing(tmp_path: Path) -> None:
    """Эмулятор историю читает, но не пишет и файла не создает."""
    target = tmp_path / "input.db"
    writer = await InputLog.open(target)
    try:
        await writer.append("/features")
    finally:
        await writer.close()

    reader = await InputLog.open(target, writable=False)
    try:
        assert reader.writable is False
        assert reader.lines == ("/features",)
        await reader.append("/ping")
    finally:
        await reader.close()

    again = await InputLog.open(target)
    try:
        assert again.lines == ("/features",), "эмулятор дописал строку в общий файл"
    finally:
        await again.close()


async def test_read_only_mode_does_not_create_the_file(tmp_path: Path) -> None:
    """Без файла эмулятор получает пустую историю и ничего не создает."""
    target = tmp_path / "input.db"
    log = await InputLog.open(target, writable=False)
    try:
        assert log.lines == ()
        await log.append("/ping")
    finally:
        await log.close()
    assert not target.exists(), "эмулятор создал файл истории"


@pytest.mark.parametrize("writable", [True, False])
async def test_broken_file_is_refused_with_a_reason(tmp_path: Path, writable: bool) -> None:
    """Испорченный файл называет причину отказа, а не отдает пустую историю.

    Молча пустая история выглядит как потерянные записи, и человек не узнает, что
    файл поврежден. Решение, что делать с отказом, принимает ``cli``: он печатает
    причину и запускает клиент без истории ввода.
    """
    target = tmp_path / "input.db"
    target.write_bytes("это не база sqlite".encode())

    with pytest.raises(sqlite3.DatabaseError):
        await InputLog.open(target, writable=writable)


async def test_file_without_our_table_gives_empty_history(tmp_path: Path) -> None:
    """Чужая, но исправная база читается как пустая история.

    Так выглядит файл, который сосед по машине открыл раньше нас и еще не
    заполнил: схемы в нем нет, а отказывать из-за этого не в чем.
    """
    target = tmp_path / "input.db"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")

    log = await InputLog.open(target, writable=False)
    try:
        assert log.lines == ()
    finally:
        await log.close()


async def test_two_clients_write_the_same_file(tmp_path: Path) -> None:
    """Два клиента разных профилей пишут в один файл, и записи не теряются."""
    target = tmp_path / "input.db"
    first = await InputLog.open(target)
    second = await InputLog.open(target)
    try:
        await first.append("/roster")
        await second.append("/features")
        await first.append("/ping")
    finally:
        await first.close()
        await second.close()

    again = await InputLog.open(target)
    try:
        assert set(again.lines) == {"/roster", "/features", "/ping"}
    finally:
        await again.close()


async def test_closed_log_refuses_work(tmp_path: Path) -> None:
    """После закрытия история не пишется: повторное закрытие безопасно."""
    target = tmp_path / "input.db"
    log = await InputLog.open(target)
    await log.close()
    await log.close()
    await log.append("/ping")

    again = await InputLog.open(target)
    try:
        assert again.lines == ()
    finally:
        await again.close()


async def test_newer_schema_is_refused_in_both_languages(tmp_path: Path) -> None:
    """Файл более новой версии программы не открывается, и причина названа."""
    target = tmp_path / "input.db"
    log = await InputLog.open(target)
    await log.close()
    with sqlite3.connect(target) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="история ввода новее программы"):
        await InputLog.open(target)
    i18n.set_language("en")
    with pytest.raises(RuntimeError, match="the input history is newer than the program"):
        await InputLog.open(target)
