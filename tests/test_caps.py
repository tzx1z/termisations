"""Кэш возможностей по XEP-0115 и расширения сообщений.

Кэш проверяется на настоящем файле: его смысл в том, что он переживает запуск, а
база в памяти этого не показывает.
"""

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

import pytest

from termisations.core import i18n, paths
from termisations.protocol.caps import CAPS_FILE, CapsCache
from termisations.protocol.session import CHAT_STATES, ME_PREFIX, _feature_title, _is_action
from termisations.xeps import NAMESPACE_XEPS

PEER: Final = "bob@example.org/phone"
VER: Final = "QgayPKawpkPSDYmwT/WM94uAlu0="
FEATURES: Final = (
    "http://jabber.org/protocol/disco#info",
    "urn:xmpp:receipts",
    "urn:xmpp:chat-markers:0",
)
IDENTITIES: Final = ("client/pc",)


@asynccontextmanager
async def cache(path: Path | str = ":memory:") -> AsyncIterator[CapsCache]:
    """Открытый кэш возможностей."""
    store = await CapsCache.open(path)
    try:
        yield store
    finally:
        await store.close()


async def test_entry_round_trip() -> None:
    """Возможности возвращаются теми же, какими были положены."""
    async with cache() as store:
        await store.put(VER, FEATURES, IDENTITIES)
        entry = await store.get(VER)
    assert entry is not None
    assert entry.features == FEATURES
    assert entry.identities == IDENTITIES
    assert entry.verstring == VER


async def test_miss_returns_nothing() -> None:
    """Неизвестный хэш дает промах, а не пустую запись."""
    async with cache() as store:
        assert await store.get("нет такого") is None
        assert await store.verstring_of(PEER) == ""


async def test_binding_jid_to_verstring() -> None:
    """Адрес связывается с хэшем: по нему и определяется попадание в кэш."""
    async with cache() as store:
        await store.put(VER, FEATURES, IDENTITIES)
        await store.bind(PEER, VER)
        assert await store.verstring_of(PEER) == VER


async def test_changed_hash_replaces_the_binding() -> None:
    """Смена хэша означает смену возможностей: связь перезаписывается.

    Старый набор в кэше остается: он может быть общим с другими контактами, и
    удалять его из-за одного собеседника неверно.
    """
    async with cache() as store:
        await store.put(VER, FEATURES, IDENTITIES)
        await store.bind(PEER, VER)
        await store.put("новый", ("urn:xmpp:ping",), IDENTITIES)
        await store.bind(PEER, "новый")
        assert await store.verstring_of(PEER) == "новый"
        assert await store.get(VER) is not None


async def test_forget_sends_the_next_request_to_the_network() -> None:
    """Забытая связь означает промах: следующий запрос пойдет в сеть."""
    async with cache() as store:
        await store.bind(PEER, VER)
        await store.forget(PEER)
        assert await store.verstring_of(PEER) == ""


async def test_cache_survives_reopen(tmp_path: Path) -> None:
    """Кэш переживает перезапуск: ради этого он и лежит на диске."""
    target = tmp_path / "caps.db"
    async with cache(target) as store:
        await store.put(VER, FEATURES, IDENTITIES)
        await store.bind(PEER, VER)
    async with cache(target) as store:
        assert await store.verstring_of(PEER) == VER
        entry = await store.get(VER)
    assert entry is not None
    assert entry.features == FEATURES


async def test_cache_lives_in_the_profile_cache_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Файл кэша лежит в каталоге кэша профиля, а не рядом с перепиской.

    Каталог кэша можно удалить в любой момент, и это не должно задевать историю
    и решения о доверии к устройствам. Профиль здесь тоже важен: возможности
    собеседника кэшируются по строке проверки, но список собеседников у каждой
    учетной записи свой.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    paths.use_profile("alice@example.org")
    store = await CapsCache.open()
    await store.close()
    assert (tmp_path / "termisations" / "alice@example.org" / CAPS_FILE).exists()
    assert paths.cache_dir() == tmp_path / "termisations" / "alice@example.org"


async def test_closed_cache_refuses_work() -> None:
    """Обращение к закрытому кэшу дает понятную ошибку."""
    store = await CapsCache.open(":memory:")
    await store.close()
    await store.close()
    with pytest.raises(RuntimeError, match="закрыт"):
        await store.get(VER)
    i18n.set_language("en")
    with pytest.raises(RuntimeError, match=r"^capabilities cache is closed$"):
        await store.get(VER)


async def test_schema_is_created_once(tmp_path: Path) -> None:
    """Повторное открытие не пересоздает таблицы и не теряет данные."""
    target = tmp_path / "caps.db"
    async with cache(target) as store:
        await store.put(VER, FEATURES, IDENTITIES)
    async with cache(target):
        pass
    with sqlite3.connect(target) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM caps").fetchone()
    assert rows[0] == 1


# Расширения сообщений.


@pytest.mark.parametrize("state", CHAT_STATES)
def test_chat_states_cover_the_extension(state: str) -> None:
    """Набор состояний тот же, что задает XEP-0085."""
    assert state in ("active", "composing", "paused", "inactive", "gone")


def test_action_prefix_is_recognised() -> None:
    """Строка на "/me " - это действие от третьего лица по XEP-0245."""
    assert _is_action(f"{ME_PREFIX}машет рукой") is True
    assert _is_action("/method вызов") is False
    assert _is_action("обычный текст") is False


def test_feature_title_names_the_extension() -> None:
    """Строка возможности подписывается номером расширения из справочника."""
    assert _feature_title("urn:xmpp:receipts") == "XEP-0184"
    assert _feature_title("urn:example:unknown") == "возможность"


def test_namespace_table_is_shared() -> None:
    """Таблица пространств имен одна на эмулятор и на сетевую сессию."""
    assert NAMESPACE_XEPS["urn:xmpp:carbons:2"] == "0280"
    assert NAMESPACE_XEPS["http://jabber.org/protocol/chatstates"] == "0085"
    from termisations import mock

    assert mock._NAMESPACE_XEPS is NAMESPACE_XEPS
