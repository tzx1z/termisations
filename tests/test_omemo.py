"""OMEMO: хранилище ключевого материала и разбор отпечатков.

Сеть здесь не нужна. Обмен зашифрованными сообщениями проверяется живым
сценарием на поднятом сервере: воспроизводить в тесте X3DH и Double Ratchet
значит писать вторую реализацию протокола вместо проверки своей.
"""

import asyncio
import json
import socket
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

import pytest

from termisations.core import commands, i18n
from termisations.core.events import EventBus
from termisations.core.models import ConnectionStage, Conversation, Encryption, OmemoInfo
from termisations.core.storage import SCHEMA_VERSION, Storage

pytest.importorskip("omemo", reason="OMEMO требует необязательной группы omemo")

from omemo.storage import StorageException

from termisations.protocol import session as session_module
from termisations.protocol.account import Account
from termisations.protocol.omemo import (
    MANUAL_TRUST_HINT,
    OLDMEMO_NAMESPACE,
    TWOMEMO_NAMESPACE,
    WORKING_NAMESPACE,
    OmemoPlugin,
    fingerprint,
    trust_summary,
)
from termisations.protocol.omemo_storage import SqliteOmemoStorage
from termisations.protocol.session import SlixmppSession, _compact

ACCOUNT: Final = "alice@example.org"

# Отпечаток в том виде, в котором его печатает /omemo fingerprints.
GROUPED: Final = "64ed0aec bc928c91 262e7b68 ab05dce8 78432c9d baa450a0 88f0afbb 8c65e451"


@asynccontextmanager
async def omemo_storage() -> AsyncIterator[tuple[SqliteOmemoStorage, Storage, int]]:
    """Хранилище OMEMO поверх базы в памяти."""
    store = await Storage.open(":memory:")
    try:
        account = await store.account_id(ACCOUNT)
        yield SqliteOmemoStorage(store, account), store, account
    finally:
        await store.close()


async def test_missing_key_is_nothing_not_none() -> None:
    """Отсутствие ключа и значение null - разные вещи.

    Библиотека строит на этом различии логику первого запуска: ``Nothing``
    означает "устройства еще нет", а ``Just(None)`` - "значение равно null".
    """
    async with omemo_storage() as (storage, _store, _account):
        assert (await storage._load("/нет")).is_nothing
        await storage._store("/есть", None)
        loaded = await storage._load("/есть")
        assert loaded.is_just
        assert loaded.from_just() is None


@pytest.mark.parametrize(
    "value",
    [
        42,
        "строка",
        [1, 2, 3],
        {"вложено": {"список": [True, None, 1.5]}},
        {"байты-в-base64": "AQIDBA=="},
    ],
)
async def test_json_values_survive_round_trip(value: object) -> None:
    """Любое JSON-значение возвращается тем же: библиотека кладет туда все сразу."""
    async with omemo_storage() as (storage, _store, _account):
        await storage._store("/ключ", value)
        assert (await storage._load("/ключ")).from_just() == value


async def test_store_overwrites_and_delete_is_idempotent() -> None:
    """Повторная запись перекрывает значение, удаление отсутствующего не падает."""
    async with omemo_storage() as (storage, _store, _account):
        await storage._store("/ключ", "первое")
        await storage._store("/ключ", "второе")
        assert (await storage._load("/ключ")).from_just() == "второе"
        await storage._delete("/ключ")
        await storage._delete("/ключ")
        assert (await storage._load("/ключ")).is_nothing


async def test_accounts_do_not_share_keys() -> None:
    """Ключи одной учетной записи не видны другой.

    Ключи библиотеки адресом не префиксуются, и без разделения по учетной записи
    два аккаунта в одном файле затерли бы устройства друг друга.
    """
    async with omemo_storage() as (first, store, _account):
        second = SqliteOmemoStorage(store, await store.account_id("carol@example.org"))
        await first._store("/own_device_id", 1)
        await second._store("/own_device_id", 2)
        assert (await first._load("/own_device_id")).from_just() == 1
        assert (await second._load("/own_device_id")).from_just() == 2


async def test_broken_value_is_reported_as_storage_error() -> None:
    """Испорченное значение дает ошибку хранилища, а не падение разбора JSON."""
    async with omemo_storage() as (storage, store, account):
        await store.omemo_save(account, "/ключ", "{не json")
        with pytest.raises(StorageException, match="испорчен"):
            await storage._load("/ключ")


async def test_storage_error_follows_the_language() -> None:
    """Текст ошибки хранилища переводится в момент ошибки, а не при импорте."""
    async with omemo_storage() as (storage, store, account):
        await store.omemo_save(account, "/key", "{not json")
        i18n.set_language("en")
        with pytest.raises(StorageException, match=r"^OMEMO key /key is corrupted: "):
            await storage._load("/key")


def test_manual_trust_hint_is_translated_on_output() -> None:
    """Подсказка хранится по-английски и переводится там, где ее показывают."""
    assert MANUAL_TRUST_HINT.startswith("no trust decision made: ")
    assert i18n._(MANUAL_TRUST_HINT) == (
        "решение о доверии не принято: посмотрите /omemo fingerprints и пометьте "
        "устройства через /omemo trust <отпечаток>"
    )


async def test_write_reaches_disk_before_return(tmp_path: Path) -> None:
    """Запись завершена к возврату из метода: буферизация не допускается."""
    target = tmp_path / "history.db"
    store = await Storage.open(target)
    try:
        account = await store.account_id(ACCOUNT)
        await SqliteOmemoStorage(store, account)._store("/ikp/key", "ключ")
        with sqlite3.connect(target) as connection:
            row = connection.execute(
                "SELECT value FROM omemo_store WHERE key = ?", ("/ikp/key",)
            ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == "ключ"
    finally:
        await store.close()


async def test_rotate_clears_only_its_account() -> None:
    """/omemo rotate снимает ключевой материал одной учетной записи."""
    async with omemo_storage() as (first, store, _account):
        other_account = await store.account_id("carol@example.org")
        second = SqliteOmemoStorage(store, other_account)
        await first._store("/own_device_id", 1)
        await second._store("/own_device_id", 2)
        await store.omemo_clear(await store.account_id(ACCOUNT))
        assert (await first._load("/own_device_id")).is_nothing
        assert (await second._load("/own_device_id")).from_just() == 2


def test_schema_carries_the_omemo_table() -> None:
    """Таблица OMEMO приехала своей миграцией, а не пустой впрок."""
    assert SCHEMA_VERSION >= 2


def test_fingerprint_is_grouped_by_eight() -> None:
    """Отпечаток печатается восемью группами по восемь знаков.

    Ключ берется настоящий: библиотека переводит Ed25519 в Curve25519 и
    отбраковывает значения со слабыми свойствами, поэтому произвольные 32 байта
    сюда не подходят.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    groups = fingerprint(key).split()
    assert len(groups) == 8
    assert all(len(group) == 8 for group in groups)


def test_compact_ignores_spaces_and_case() -> None:
    """Пробелы и регистр в отпечатке не значимы: строку копируют из списка."""
    assert _compact(GROUPED) == _compact(GROUPED.upper().replace(" ", ""))


def test_trust_summary_counts_blind_trust_as_trust() -> None:
    """Слепое доверие считается доверием: сообщение таким устройствам уходит."""

    class Device:
        def __init__(self, level: str) -> None:
            self.trust_level_name = level

    devices = frozenset(
        {
            Device("TRUSTED"),
            Device("BLINDLY_TRUSTED"),
            Device("UNDECIDED"),
            Device("DISTRUSTED"),
        }
    )
    assert trust_summary(devices) == (2, 4)


def test_working_namespace_is_oldmemo() -> None:
    """Рабочее пространство имен - oldmemo.

    Библиотека публикует бандлы обоих, а шифрует и расшифровывает только
    oldmemo. Поддержка, которой нет, не заявляется.
    """
    assert TWOMEMO_NAMESPACE == "urn:xmpp:omemo:2"
    assert OLDMEMO_NAMESPACE == "eu.siacs.conversations.axolotl"
    assert WORKING_NAMESPACE == OLDMEMO_NAMESPACE


def test_grouped_fingerprint_fits_the_command() -> None:
    """Отпечаток из списка вводится в /omemo trust как есть, вместе с адресом.

    Восемь групп плюс адрес - это девять токенов, и предел аргументов команды должен
    их вмещать, иначе команда ответит ошибкой разбора на собственный вывод.
    """
    parsed = commands.parse(f"/omemo trust {GROUPED} bob@example.org")
    assert parsed is not None
    assert parsed.error is None
    assert parsed.handler_key == "crypto.omemo.trust"
    assert parsed.sub_args[-1] == "bob@example.org"


def test_conversation_carries_its_own_omemo_state() -> None:
    """Состояние OMEMO - поле беседы, а не клиента.

    Пока счетчик доверенных устройств жил в состоянии клиента, он относился к
    последней беседе, где выполнялась команда, а не к открытой.
    """
    plain = Conversation(jid="bob@example.org")
    secured = Conversation(
        jid="carol@example.org",
        encryption=Encryption.OMEMO,
        omemo=OmemoInfo(enabled=True, trusted_devices=2, total_devices=3),
    )
    assert plain.omemo == OmemoInfo()
    assert secured.omemo.trusted_devices == 2
    assert not hasattr(plain, "client_omemo")


def closed_port() -> int:
    """Порт, который точно никто не слушает: занять и сразу освободить."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_shutdown_without_bind_skips_session_manager(monkeypatch, wait_for) -> None:
    """Остановка без привязки ресурса не обращается к менеджеру сессий.

    Без привязки создание менеджера не запускалось. Вызов ``get_session_manager``
    при остановке запустил бы его на отключенном клиенте, и выход простоял бы
    весь предел ``OMEMO_SHUTDOWN_WAIT``: неудачная отправка из cron длилась на
    5 с дольше своего таймаута. Предел здесь увеличен, чтобы ожидание нельзя
    было спутать с быстрым выходом.
    """
    requested: list[bool] = []

    async def never_ready(self: OmemoPlugin) -> None:
        requested.append(True)
        await asyncio.Event().wait()

    monkeypatch.setattr(OmemoPlugin, "get_session_manager", never_ready)
    monkeypatch.setattr(session_module, "OMEMO_SHUTDOWN_WAIT", 30.0)
    monkeypatch.setenv("TERMISATIONS_PASSWORD", "пароль не проверяется")
    account = Account(
        jid=ACCOUNT,
        resource="omemo",
        host="127.0.0.1",
        port=closed_port(),
        direct_tls=False,
        tls_verify=False,
    )
    # База нужна обязательно: без нее плагин OMEMO не подключается вовсе.
    # Закрывает ее сама сессия при остановке.
    session = SlixmppSession(EventBus(), account, storage=await Storage.open(":memory:"))
    task = asyncio.create_task(session.run(), name="session")
    try:
        assert await wait_for(lambda: session.state.stage is ConnectionStage.ERROR, 3.0)
    finally:
        session.stop()
    await asyncio.wait_for(task, 2.0)
    assert not requested
