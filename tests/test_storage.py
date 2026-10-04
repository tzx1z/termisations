"""Хранилище истории и пути по XDG.

База проверяется на настоящем файле, а не только в памяти: режим WAL, права и
повторное открытие - это ровно то, что в памяти не воспроизводится.
"""

import asyncio
import sqlite3
import stat
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from termisations.core import i18n, paths
from termisations.core.models import (
    Conversation,
    DeliveryState,
    Direction,
    Encryption,
    Message,
)
from termisations.core.storage import MEMORY, SCHEMA_VERSION, Storage, StoredMessage

ACCOUNT: Final = "alice@example.org"
PEER: Final = "bob@example.org"


def make_message(index: int, *, body: str = "", conversation: str = PEER) -> Message:
    """Сообщение с предсказуемым идентификатором и временем."""
    return Message(
        message_id=f"m-{index}",
        conversation=conversation,
        sender=PEER,
        body=body or f"сообщение {index}",
        ts=1_700_000_000.0 + index,
        direction=Direction.IN,
    )


@asynccontextmanager
async def storage(path: Path | str = MEMORY) -> AsyncIterator[tuple[Storage, int]]:
    """Открытое хранилище вместе с идентификатором учетной записи."""
    store = await Storage.open(path)
    try:
        yield store, await store.account_id(ACCOUNT)
    finally:
        await store.close()


async def test_migration_sets_schema_version(tmp_path: Path) -> None:
    """Миграции применяются с нуля и отмечают версию схемы."""
    target = tmp_path / "history.db"
    async with storage(target):
        pass
    with sqlite3.connect(target) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"accounts", "roster", "conversations", "messages"} <= tables


async def test_reopen_applies_nothing_and_keeps_data(tmp_path: Path) -> None:
    """Повторное открытие не теряет сообщений и не запускает миграции заново."""
    target = tmp_path / "history.db"
    async with storage(target) as (store, account):
        assert await store.save_message(account, StoredMessage(make_message(1)))
    async with storage(target) as (store, account):
        loaded = await store.load_messages(account, PEER, 10)
    assert [item.message_id for item in loaded] == ["m-1"]


async def test_wal_mode_is_enabled(tmp_path: Path) -> None:
    """Режим журнала WAL: читатели не блокируют писателя."""
    target = tmp_path / "history.db"
    async with storage(target):
        pass
    with sqlite3.connect(target) as connection:
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"


async def test_database_file_is_owner_only(tmp_path: Path) -> None:
    """Файл базы доступен только владельцу: в нем переписка."""
    target = tmp_path / "history.db"
    async with storage(target):
        pass
    assert stat.S_IMODE(target.stat().st_mode) == paths.FILE_MODE


async def test_duplicate_stanza_id_is_skipped() -> None:
    """Повтор по stanza-id не создает второй строки.

    Это и есть защита от дублей между живой доставкой, Carbons и архивом.
    """
    async with storage() as (store, account):
        first = StoredMessage(make_message(1), stanza_id="s-1")
        second = StoredMessage(make_message(2), stanza_id="s-1")
        assert await store.save_message(account, first) is True
        assert await store.save_message(account, second) is False
        assert len(await store.load_messages(account, PEER, 10)) == 1


async def test_duplicate_message_id_is_skipped() -> None:
    """Повтор по идентификатору сообщения в той же беседе тоже отсекается."""
    async with storage() as (store, account):
        assert await store.save_message(account, StoredMessage(make_message(1))) is True
        assert await store.save_message(account, StoredMessage(make_message(1))) is False


async def test_empty_stanza_id_does_not_collide() -> None:
    """Пустой stanza-id не считается совпадением: до XEP-0359 его просто нет."""
    async with storage() as (store, account):
        assert await store.save_message(account, StoredMessage(make_message(1))) is True
        assert await store.save_message(account, StoredMessage(make_message(2))) is True
        assert len(await store.load_messages(account, PEER, 10)) == 2


async def test_messages_load_oldest_first_within_limit() -> None:
    """Выборка берет последние сообщения, а отдает их в порядке чтения."""
    async with storage() as (store, account):
        for index in range(5):
            await store.save_message(account, StoredMessage(make_message(index)))
        loaded = await store.load_messages(account, PEER, 3)
    assert [item.message_id for item in loaded] == ["m-2", "m-3", "m-4"]


async def test_message_fields_survive_round_trip() -> None:
    """Направление, шифрование, состояние доставки и признак правки сохраняются."""
    async with storage() as (store, account):
        original = Message(
            message_id="m-42",
            conversation=PEER,
            sender=ACCOUNT,
            body="исправленный текст",
            ts=1_700_000_500.0,
            direction=Direction.OUT,
            encryption=Encryption.OMEMO,
            state=DeliveryState.DISPLAYED,
            corrected=True,
        )
        await store.save_message(account, StoredMessage(original, stanza_id="s-42"))
        loaded = (await store.load_messages(account, PEER, 1))[0]
    assert loaded.direction is Direction.OUT
    assert loaded.encryption is Encryption.OMEMO
    assert loaded.state is DeliveryState.DISPLAYED
    assert loaded.corrected is True
    assert loaded.body == "исправленный текст"


async def test_update_message_changes_body_and_state() -> None:
    """Корректировка и смена состояния доставки доходят до базы."""
    async with storage() as (store, account):
        await store.save_message(account, StoredMessage(make_message(1)))
        fixed = make_message(1, body="поправлено")
        await store.update_message(account, fixed.with_state(DeliveryState.DISPLAYED))
        loaded = (await store.load_messages(account, PEER, 1))[0]
    assert loaded.body == "поправлено"
    assert loaded.state is DeliveryState.DISPLAYED


async def test_conversations_are_sorted_by_last_message() -> None:
    """Список бесед идет от свежих к старым."""
    async with storage() as (store, account):
        await store.save_message(account, StoredMessage(make_message(1, conversation="a@x")))
        await store.save_message(account, StoredMessage(make_message(9, conversation="b@x")))
        assert await store.conversations(account) == ["b@x", "a@x"]


async def test_accounts_are_isolated() -> None:
    """Сообщения одной учетной записи не видны другой."""
    async with storage() as (store, account):
        other = await store.account_id("carol@example.org")
        await store.save_message(account, StoredMessage(make_message(1)))
        assert await store.load_messages(other, PEER, 10) == []


async def test_account_id_is_stable() -> None:
    """Повторное обращение к учетной записи дает тот же идентификатор."""
    async with storage() as (store, account):
        assert await store.account_id(ACCOUNT) == account


async def test_roster_is_replaced_as_a_snapshot() -> None:
    """Контакт-лист переписывается целиком: удаленный на другом устройстве уходит."""
    async with storage() as (store, account):
        await store.save_roster(account, [(PEER, "Боб", "both", ["работа"]), ("c@x", "", "to", [])])
        await store.save_roster(account, [(PEER, "Боб", "both", ["работа", "друзья"])])
        loaded = await store.load_roster(account)
    assert loaded == [(PEER, "Боб", "both", ["работа", "друзья"])]


async def test_closed_storage_refuses_work() -> None:
    """Обращение к закрытому хранилищу дает понятную ошибку, а не зависание."""
    store = await Storage.open(MEMORY)
    await store.close()
    await store.close()
    with pytest.raises(RuntimeError, match="закрыто"):
        await store.account_id(ACCOUNT)


async def test_newer_schema_is_refused(tmp_path: Path) -> None:
    """База, записанная более новой версией программы, не открывается молча."""
    target = tmp_path / "history.db"
    async with storage(target):
        pass
    with sqlite3.connect(target) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    with pytest.raises(RuntimeError, match="новее программы"):
        await Storage.open(target)
    i18n.set_language("en")
    with pytest.raises(RuntimeError, match="newer than the program: schema version"):
        await Storage.open(target)


@pytest.mark.slow
async def test_bulk_write_does_not_block_the_event_loop(tmp_path: Path) -> None:
    """Запись пачки сообщений не останавливает цикл событий.

    Порог взят с запасом: проверяется отсутствие блокировки, а не скорость диска.
    Прием тот же, что в tests/test_perf.py: измеряется задержка тика таймера.
    """
    delays: list[float] = []

    async def ticker() -> None:
        while True:
            started = time.monotonic()
            await asyncio.sleep(0.01)
            delays.append(time.monotonic() - started - 0.01)

    async with storage(tmp_path / "history.db") as (store, account):
        watcher = asyncio.create_task(ticker())
        for index in range(2000):
            await store.save_message(account, StoredMessage(make_message(index)))
        watcher.cancel()
    assert delays, "таймер не успел тикнуть ни разу"
    assert max(delays) < 0.5


def test_xdg_paths_follow_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Пути берутся из переменных XDG, а пустая переменная считается незаданной."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    paths.use_profile(ACCOUNT)
    assert paths.config_file() == tmp_path / "cfg" / "termisations" / ACCOUNT / "config.toml"
    assert paths.data_dir() == tmp_path / "data" / "termisations" / ACCOUNT
    assert paths.cache_dir() == tmp_path / "home" / ".cache" / "termisations" / ACCOUNT


def test_profile_name_is_normalized() -> None:
    """Имя каталога - bare JID в нижнем регистре: ресурс и регистр не разводят данные."""
    assert paths.use_profile("Alice@Example.ORG/phone") == ACCOUNT
    assert paths.current_profile() == ACCOUNT


@pytest.mark.parametrize("jid", ["", "без собаки", "@example.org", "alice@", "ali ce@srv"])
def test_unfit_profile_name_is_refused(jid: str) -> None:
    """Адрес, не годящийся в имя каталога, отклоняется явной проверкой."""
    with pytest.raises(ValueError, match="адрес"):
        paths.profile_name(jid)


def test_data_directory_needs_a_profile() -> None:
    """Без профиля каталога данных нет: история принадлежит учетной записи."""
    with pytest.raises(RuntimeError, match="профиль"):
        paths.data_dir()


def test_data_directory_is_owner_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Каталоги профиля и приложения создаются с правами 0700 независимо от umask."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    paths.use_profile(ACCOUNT)
    created = paths.data_file("keys.bin").parent
    assert created == tmp_path / "data" / "termisations" / ACCOUNT
    assert stat.S_IMODE(created.stat().st_mode) == paths.DIR_MODE
    assert stat.S_IMODE(created.parent.stat().st_mode) == paths.DIR_MODE


def test_existing_directory_mode_is_tightened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Слишком свободные права существующего каталога приводятся к 0700."""
    root = tmp_path / "data"
    target = root / "termisations" / ACCOUNT
    target.mkdir(parents=True)
    target.chmod(0o755)
    target.parent.chmod(0o755)
    monkeypatch.setenv("XDG_DATA_HOME", str(root))
    paths.use_profile(ACCOUNT)
    paths.data_file("keys.bin")
    assert stat.S_IMODE(target.stat().st_mode) == paths.DIR_MODE
    assert stat.S_IMODE(target.parent.stat().st_mode) == paths.DIR_MODE


async def test_profiles_do_not_share_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Две учетные записи не видят переписку друг друга: у каждой своя база."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    paths.use_profile(ACCOUNT)
    first = await Storage.open()
    try:
        account = await first.account_id(ACCOUNT)
        await first.save_message(account, StoredMessage(make_message(1)))
    finally:
        await first.close()

    paths.use_profile("tom@simple.org")
    second = await Storage.open()
    try:
        account = await second.account_id("tom@simple.org")
        assert await second.load_messages(account, PEER, 10) == []
    finally:
        await second.close()

    database = tmp_path / "data" / "termisations"
    assert (database / ACCOUNT / "history.db").is_file()
    assert (database / "tom@simple.org" / "history.db").is_file()


async def test_conversation_survives_reopen(tmp_path: Path) -> None:
    """Беседа возвращается с названием, темой и счетчиком непрочитанного.

    Таблица бесед существовала с первой миграции и не использовалась: список
    выводился из сообщений, поэтому после перезапуска от беседы оставался один
    адрес.
    """
    target = tmp_path / "history.db"
    room = Conversation(
        jid="devops@conference.example.org",
        title="devops",
        is_muc=True,
        unread=3,
        encryption=Encryption.OMEMO,
        topic="релиз в пятницу",
    )
    async with storage(target) as (store, account):
        await store.save_conversation(account, room)
    async with storage(target) as (store, account):
        assert await store.load_conversations(account) == [room]


async def test_conversation_is_replaced_not_duplicated() -> None:
    """Повторная запись беседы обновляет ее, а не добавляет вторую строку."""
    async with storage() as (store, account):
        item = Conversation(jid=PEER, title="Боб", unread=1)
        await store.save_conversation(account, item)
        await store.save_conversation(account, replace(item, unread=0, topic="новая тема"))
        loaded = await store.load_conversations(account)
    assert len(loaded) == 1
    assert loaded[0].unread == 0
    assert loaded[0].topic == "новая тема"


async def test_forgotten_conversation_keeps_its_messages() -> None:
    """Закрытая беседа исчезает из списка, а ее сообщения остаются."""
    async with storage() as (store, account):
        await store.save_conversation(account, Conversation(jid=PEER, title="Боб"))
        await store.save_message(account, StoredMessage(make_message(1)))
        await store.forget_conversation(account, PEER)
        assert await store.load_conversations(account) == []
        assert len(await store.load_messages(account, PEER, 10)) == 1


async def test_unread_column_is_added_to_an_existing_base(tmp_path: Path) -> None:
    """Миграция 4 применяется поверх базы, созданной прошлой версией."""
    target = tmp_path / "history.db"
    async with storage(target) as (store, account):
        await store.save_message(account, StoredMessage(make_message(1)))
    # Откат к версии 3: колонки unread еще нет, данные есть.
    with sqlite3.connect(target) as connection:
        connection.execute("ALTER TABLE conversations DROP COLUMN unread")
        connection.execute("PRAGMA user_version = 3")
    async with storage(target) as (store, account):
        await store.save_conversation(account, Conversation(jid=PEER, title="Боб", unread=2))
        loaded = await store.load_conversations(account)
    assert [item.unread for item in loaded] == [2]
    assert len(await _messages_of(target)) == 1


async def _messages_of(target: Path) -> list[sqlite3.Row]:
    """Сообщения в базе напрямую, без открытия хранилища."""
    with sqlite3.connect(target) as connection:
        return list(connection.execute("SELECT id FROM messages").fetchall())
