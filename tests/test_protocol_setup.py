"""Учетная запись, пароль и выбор адреса подключения.

Сеть здесь не нужна: проверяются разбор параметров, порядок предпочтения адресов
и поведение при недоступном пароле.
"""

import importlib
import ssl
from pathlib import Path
from typing import cast

import pytest

from termisations.core import i18n
from termisations.core.models import TlsInfo, Transport
from termisations.protocol import account as account_module
from termisations.protocol.account import Account, PasswordError, resolve_password
from termisations.protocol.transport import (
    Endpoint,
    _sorted_records,
    channel_binding_of,
    tls_info_of,
)


def test_account_parts() -> None:
    """JID разбирается на локальную часть, домен и полный адрес с ресурсом."""
    account = Account(jid="alice@example.org", resource="term")
    assert account.username == "alice"
    assert account.domain == "example.org"
    assert account.full_jid == "alice@example.org/term"


@pytest.mark.parametrize(
    ("jid", "port", "problem"),
    [
        ("alice@example.org", 0, ""),
        ("alice", 0, "user@server"),
        ("@example.org", 0, "user@server"),
        ("alice@", 0, "user@server"),
        ("alice@example.org", 70000, "диапазона"),
    ],
)
def test_account_validation(jid: str, port: int, problem: str) -> None:
    """Неверные параметры отклоняются с объяснением, а не молча."""
    result = Account(jid=jid, port=port).validate()
    assert problem in result


async def test_password_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пароль берется из переменной окружения, когда команда не задана."""
    monkeypatch.setenv("TERMISATIONS_PASSWORD", "из-окружения")
    monkeypatch.setattr(account_module, "_from_keyring", lambda _account: "")
    secret = await resolve_password(Account(jid="a@b"))
    assert secret.value == "из-окружения"
    assert "TERMISATIONS_PASSWORD" in secret.source


async def test_password_command_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Внешняя команда важнее и keyring, и переменной окружения."""
    monkeypatch.setenv("TERMISATIONS_PASSWORD", "из-окружения")
    monkeypatch.setattr(account_module, "_from_keyring", lambda _account: "из-keyring")
    account = Account(jid="a@b", password_command="printf 'секрет\\nметаданные\\n'")
    secret = await resolve_password(account)
    assert secret.value == "секрет"
    assert secret.source == "password_command"


async def test_keyring_wins_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Системный keyring важнее переменной окружения.

    Переменная окружения видна в списке процессов и в дампе памяти, keyring - нет.
    """
    monkeypatch.setenv("TERMISATIONS_PASSWORD", "из-окружения")
    monkeypatch.setattr(account_module, "_from_keyring", lambda _account: "из-keyring")
    secret = await resolve_password(Account(jid="a@b"))
    assert secret.value == "из-keyring"
    assert "keyring" in secret.source


async def test_secret_repr_hides_the_value() -> None:
    """Пароль не должен попасть в журнал через представление объекта."""
    assert "тайна" not in repr(account_module.Secret("тайна", "тест"))


async def test_password_missing_explains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отсутствие пароля объясняется текстом, а не ошибкой авторизации на сервере."""
    monkeypatch.delenv("TERMISATIONS_PASSWORD", raising=False)
    monkeypatch.setattr(account_module, "_from_keyring", lambda _account: "")
    with pytest.raises(PasswordError) as error:
        await resolve_password(Account(jid="a@b"))
    assert "TERMISATIONS_PASSWORD" in str(error.value)


async def test_password_missing_explains_in_english(monkeypatch: pytest.MonkeyPatch) -> None:
    """На английском интерфейсе объяснение и проверка параметров тоже английские."""
    monkeypatch.delenv("TERMISATIONS_PASSWORD", raising=False)
    monkeypatch.setattr(account_module, "_from_keyring", lambda _account: "")
    i18n.set_language("en")
    with pytest.raises(PasswordError) as error:
        await resolve_password(Account(jid="a@b"))
    assert str(error.value).startswith("password is not set: specify password_command")
    assert "(service termisations, entry a@b/termisations)" in str(error.value)
    problem = Account(jid="alice@example.org", port=70000).validate()
    assert problem == "port out of range 1-65535: 70000"


async def test_password_command_failure_is_reported(tmp_path: Path) -> None:
    """Ошибка внешней команды доходит до пользователя вместе с причиной."""
    script = tmp_path / "fail.sh"
    script.write_text("#!/bin/sh\necho 'хранилище закрыто' >&2\nexit 3\n")
    script.chmod(0o700)
    account = Account(jid="a@b", password_command=str(script))
    with pytest.raises(PasswordError) as error:
        await resolve_password(account)
    assert "хранилище закрыто" in str(error.value)


async def test_password_command_not_found() -> None:
    """Несуществующая команда не роняет клиент."""
    account = Account(jid="a@b", password_command="/nonexistent/termisations-password")
    with pytest.raises(PasswordError):
        await resolve_password(account)


def test_srv_priority_order() -> None:
    """Записи меньшего приоритета идут раньше, вес играет роль внутри группы."""
    records = [(20, 0, 5222, "backup"), (10, 10, 5222, "main"), (10, 0, 5222, "spare")]
    ordered = _sorted_records(records)
    assert ordered[-1][3] == "backup"
    assert {item[3] for item in ordered[:2]} == {"main", "spare"}


def test_endpoint_describes_source() -> None:
    """В журнале подключения видно, откуда взялся адрес."""
    endpoint = Endpoint("srv.example.org", 5223, Transport.DIRECT_TLS, "SRV _xmpps-client._tcp")
    assert endpoint.direct_tls
    assert "srv.example.org:5223" in endpoint.describe()
    assert "SRV" in endpoint.describe()


# Подмена подставляется вместо ssl.SSLObject: функции берут у сокета только
# version, cipher и get_channel_binding, а поднимать настоящий TLS ради трех
# методов незачем.
class _FakeSocket:
    """Подмена сокета TLS: настоящее соединение для проверки не нужно."""

    def __init__(self, version: str, binding: bytes | None) -> None:
        self._version = version
        self._binding = binding

    def version(self) -> str:
        """Версия протокола."""
        return self._version

    def cipher(self) -> tuple[str, str, int]:
        """Согласованный шифр."""
        return ("TLS_AES_256_GCM_SHA384", self._version, 256)

    def get_channel_binding(self, kind: str) -> bytes | None:
        """Данные привязки канала."""
        if kind != "tls-unique":
            raise ValueError(kind)
        return self._binding


def _as_socket(fake: _FakeSocket) -> ssl.SSLObject:
    """Выдать подмену за сокет: проверяемые функции обращаются только к трем методам."""
    return cast(ssl.SSLObject, fake)


def test_channel_binding_absent_on_tls13() -> None:
    """На TLS 1.3 привязки нет: tls-unique запрещен, tls-exporter в Python нет.

    Это ограничение платформы, а не недоработка клиента, и поле пустое.
    """
    assert channel_binding_of(_as_socket(_FakeSocket("TLSv1.3", b"x"))) is None


def test_channel_binding_present_on_tls12() -> None:
    """На TLS 1.2 привязка доступна и попадает в сведения о канале."""
    assert channel_binding_of(_as_socket(_FakeSocket("TLSv1.2", b"x"))) == "tls-unique"


def test_tls_info_without_socket() -> None:
    """Без соединения сведения о канале пусты, а не выдуманы."""
    assert tls_info_of(None, verified=True) == TlsInfo()


def test_tls_info_reports_verification() -> None:
    """Отключенная проверка цепочки видна в сведениях о канале."""
    info = tls_info_of(_as_socket(_FakeSocket("TLSv1.3", None)), verified=False)
    assert info.version == "TLSv1.3"
    assert info.cipher == "TLS_AES_256_GCM_SHA384"
    assert info.valid is False


def test_every_plugin_imports() -> None:
    """Каждый плагин из списка сессии импортируется в этом окружении.

    Сессия регистрирует плагины списком, а ``register_plugin`` пробрасывает
    ошибку импорта наружу. Значит плагин с необъявленной зависимостью ломает не
    свою команду, а подключение целиком. Так и было с XEP-0363: он импортирует
    aiohttp на уровне модуля, slixmpp объявляет его только в extra
    "xep-0363", а в окружении пакет оказался случайно, из группы dev.
    """
    pytest.importorskip("slixmpp")
    from termisations.protocol.session import PLUGINS

    for name in PLUGINS:
        importlib.import_module(f"slixmpp.plugins.{name}")


def test_real_ssl_module_lacks_tls_exporter() -> None:
    """Причина пустой привязки зафиксирована тестом, а не только комментарием.

    Если в CPython появится tls-exporter, тест не пройдет и напомнит, что
    SCRAM-PLUS и XEP-0440 стали достижимы.
    """
    assert "tls-exporter" not in ssl.CHANNEL_BINDING_TYPES
