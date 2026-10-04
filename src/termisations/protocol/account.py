"""Параметры учетной записи и получение пароля.

Пароль в конфигурации открытым текстом не хранится. Источники перебираются в
порядке предпочтения: внешняя команда (``password_command``), системный keyring,
переменная окружения, запрос при старте. Каждый источник называет себя: клиент
диагностический, и ответ на вопрос "почему не пустило" должен читаться с экрана,
а не выясняться перебором.

Keyring подключается необязательной зависимостью и импортируется внутри функции:
на машине без сессии D-Bus он не работает, и обязательный импорт превращал бы
отсутствие графической сессии в отказ запуска.
"""

import asyncio
import os
import shlex
from dataclasses import dataclass
from typing import Final

from termisations.core.i18n import _

__all__ = ["Account", "PasswordError", "Secret", "keyring_service", "resolve_password"]

_ENV_PASSWORD: Final = "TERMISATIONS_PASSWORD"

# Имя службы в системном keyring. Учетная запись внутри службы - полный JID.
KEYRING_SERVICE: Final = "termisations"


@dataclass(frozen=True, slots=True)
class Secret:
    """Пароль вместе с названием источника, из которого он взят."""

    value: str
    source: str

    def __repr__(self) -> str:
        """Представление без значения: пароль не должен попасть в журнал."""
        return f"Secret(source={self.source!r})"


# Предел ожидания внешней команды: она может спросить пароль от хранилища,
# но висеть вечно не должна, иначе клиент не стартует и не объясняет почему.
_COMMAND_TIMEOUT: Final = 30.0


class PasswordError(RuntimeError):
    """Пароль получить не удалось. Текст пригоден для показа пользователю."""


@dataclass(frozen=True, slots=True)
class Account:
    """Учетная запись и параметры подключения к ее серверу."""

    jid: str
    resource: str = "termisations"
    """Ресурс. Профильный хвост к нему дописывает ``cli``, здесь только основа."""
    host: str = ""
    """Адрес сервера в обход SRV. Пустая строка означает обычный резолв."""

    port: int = 0
    """Порт в обход SRV. Ноль означает выбор по записи SRV."""

    direct_tls: bool | None = None
    """Явный способ защиты канала. ``None`` означает выбор по записи SRV."""

    tls_verify: bool = True
    """Проверять цепочку сертификатов. Выключается только для своего сервера."""

    password_command: str = ""
    """Внешняя команда, печатающая пароль в стандартный вывод."""

    sasl2: bool = True
    """Пробовать SASL2 и Bind 2, если сервер их объявил.

    Аварийный выключатель на случай сервера, объявляющего SASL2 с дефектами: без
    него дефект адаптера означает невозможность войти вообще.
    """

    @property
    def domain(self) -> str:
        """Домен учетной записи: часть JID после собаки."""
        _local, _sep, domain = self.jid.partition("@")
        return domain

    @property
    def username(self) -> str:
        """Локальная часть JID."""
        local, _sep, _domain = self.jid.partition("@")
        return local

    @property
    def full_jid(self) -> str:
        """JID вместе с ресурсом."""
        return f"{self.jid}/{self.resource}" if self.resource else self.jid

    def validate(self) -> str:
        """Проверить параметры. Пустая строка означает, что все в порядке."""
        if "@" not in self.jid or not self.username or not self.domain:
            return _("account address must look like user@server, got: {jid!r}").format(
                jid=self.jid
            )
        if self.port and not 1 <= self.port <= 65535:
            return _("port out of range 1-65535: {number}").format(number=self.port)
        return ""


def keyring_service() -> str:
    """Имя службы в системном keyring. Вынесено ради одной точки правки."""
    return KEYRING_SERVICE


def _from_keyring(account: Account) -> str:
    """Пароль из системного keyring. Пустая строка означает, что его там нет.

    Любая ошибка keyring считается отсутствием пароля: на машине без D-Bus
    библиотека поднимает исключение, и это не повод не пускать пользователя к
    остальным источникам.
    """
    try:
        import keyring
    except ImportError:
        return ""
    try:
        value = keyring.get_password(KEYRING_SERVICE, account.full_jid)
    except Exception:
        return ""
    return value or ""


async def resolve_password(account: Account) -> Secret:
    """Получить пароль учетной записи вместе с названием источника.

    Порядок: внешняя команда, системный keyring, переменная окружения. Запрос с
    терминала здесь не делается: его выполняет ``cli`` до старта приложения, пока
    терминалом не владеет Textual.
    """
    if account.password_command:
        return Secret(await _run_password_command(account.password_command), "password_command")
    from_keyring = _from_keyring(account)
    if from_keyring:
        return Secret(from_keyring, _("system keyring"))
    from_env = os.environ.get(_ENV_PASSWORD)
    if from_env:
        return Secret(from_env, _("environment variable {name}").format(name=_ENV_PASSWORD))
    raise PasswordError(
        _(
            "password is not set: specify password_command in the configuration, put it "
            "into the system keyring (service {service}, entry {entry}) "
            "or set the environment variable {variable}"
        ).format(service=KEYRING_SERVICE, entry=account.full_jid, variable=_ENV_PASSWORD)
    )


async def _run_password_command(command: str) -> str:
    """Выполнить внешнюю команду и забрать первую строку ее вывода."""
    try:
        argv = shlex.split(command)
    except ValueError as error:
        raise PasswordError(
            _("cannot parse password_command: {error}").format(error=error)
        ) from error
    if not argv:
        raise PasswordError(_("password_command is empty"))
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise PasswordError(
            _("cannot start password_command: {error}").format(error=error)
        ) from error
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), _COMMAND_TIMEOUT)
    except TimeoutError:
        process.kill()
        raise PasswordError(
            _("password_command did not respond within {timeout:.0f} s").format(
                timeout=_COMMAND_TIMEOUT
            )
        ) from None
    if process.returncode:
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        reason = detail[0] if detail else _("exit code {code}").format(code=process.returncode)
        raise PasswordError(_("password_command failed: {reason}").format(reason=reason))
    # Берется первая строка: менеджеры паролей печатают пароль первой строкой,
    # а следом метаданные записи.
    password = stdout.decode("utf-8", "replace").splitlines()
    if not password or not password[0]:
        raise PasswordError(_("password_command printed nothing"))
    return password[0]
