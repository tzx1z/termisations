"""OMEMO (XEP-0384) поверх slixmpp-omemo.

Плагин библиотеки абстрактный: наследник обязан дать хранилище, политику доверия
и обработчик случая, когда автоматического решения о доверии не хватает. Все три
вещи здесь.

Про два пространства имен. Клиенту нужны обе версии - ``urn:xmpp:omemo:2`` и
``eu.siacs.conversations.axolotl``. slixmpp-omemo 2.2.0 загружает оба бэкенда и
публикует оба бандла, но работает только со вторым: ветка подготовки открытого
текста для omemo:2 в библиотеке закомментирована, а расшифровка входящего
omemo:2 после успешного разбора поднимает ``NotImplementedError``. Клиент этого
не скрывает: ``/omemo status`` называет рабочее пространство имен, а входящее
сообщение в неподдержанном виде дает понятный отказ, а не пустую строку в ленте.

Политика доверия - BTBV: устройства, о которых решения еще не было, получают
слепое доверие, и клиент об этом сообщает. Так делает Conversations, и это
единственный вариант, при котором первое сообщение уходит без ручного
подтверждения. Как только по учетной записи есть хоть одно ручное решение,
слепое доверие для нее выключается, и новое устройство требует ``/omemo trust``.
"""

from collections.abc import Callable
from typing import Final

import oldmemo.oldmemo
import twomemo.twomemo
from omemo.session_manager import SessionManager, TrustDecisionFailed
from omemo.storage import Storage as OmemoStorage
from omemo.types import DeviceInformation
from slixmpp import JID
from slixmpp_omemo import XEP_0384, TrustLevel

from termisations.core.i18n import N_, _

__all__ = [
    "OLDMEMO_NAMESPACE",
    "TWOMEMO_NAMESPACE",
    "WORKING_NAMESPACE",
    "OmemoPlugin",
    "Report",
    "fingerprint",
    "trust_summary",
]

TWOMEMO_NAMESPACE: Final = twomemo.twomemo.NAMESPACE
OLDMEMO_NAMESPACE: Final = oldmemo.oldmemo.NAMESPACE

# Пространство имен, в котором библиотека действительно шифрует и расшифровывает.
WORKING_NAMESPACE: Final = OLDMEMO_NAMESPACE

# Ключ конфигурации плагина, через который передается хранилище. Плагин создает
# slixmpp, конструктор у него свой, и передать зависимость можно только так.
STORAGE_KEY: Final = "termisations_storage"

# Ключ конфигурации с обработчиком слепого доверия.
REPORT_KEY: Final = "termisations_report"

# Текст отказа, когда слепого доверия не хватает. Он попадает пользователю, и в
# нем должно быть сказано, что делать дальше. Переводится в месте вывода.
MANUAL_TRUST_HINT: Final = N_(
    "no trust decision made: check /omemo fingerprints and mark "
    "devices with /omemo trust <fingerprint>"
)

# Тип обработчика событий плагина: он публикует их в шину.
type Report = Callable[[str, dict[str, str]], None]


def fingerprint(identity_key: bytes) -> str:
    """Отпечаток ключа группами по восемь знаков.

    Формат тот же, что показывает Conversations: строку из списка
    ``/omemo fingerprints`` пользователь копирует как есть.
    """
    return " ".join(SessionManager.format_identity_key(identity_key))


def trust_summary(devices: frozenset[DeviceInformation]) -> tuple[int, int]:
    """Число доверенных устройств и общее число. Слепое доверие считается доверием."""
    trusted = sum(
        1
        for device in devices
        if device.trust_level_name in (TrustLevel.TRUSTED.value, TrustLevel.BLINDLY_TRUSTED.value)
    )
    return trusted, len(devices)


class OmemoPlugin(XEP_0384):
    """Плагин OMEMO, привязанный к хранилищу клиента."""

    name = "xep_0384"
    description = "XEP-0384: OMEMO"

    manager_requested = False
    """Создание менеджера сессий запущено. Выставляется при привязке ресурса."""

    def session_bind(self, jid: JID) -> None:
        """Отметить запуск создания менеджера сессий.

        Базовый класс запускает создание по привязке ресурса, а остановка сессии
        по этому признаку решает, ждать ли менеджер. Без привязки ждать нечего:
        вызов ``get_session_manager`` сам запустил бы создание на отключенном
        клиенте и простоял бы весь предел ожидания.
        """
        self.manager_requested = True
        super().session_bind(jid)

    @property
    def storage(self) -> OmemoStorage:
        """Хранилище ключевого материала. Передается конфигурацией плагина."""
        value = self.config.get(STORAGE_KEY)
        if not isinstance(value, OmemoStorage):
            message = _("OMEMO plugin is built without storage")
            raise RuntimeError(message)
        return value

    @property
    def _btbv_enabled(self) -> bool:
        """Слепое доверие до проверки включено.

        Без него первое же сообщение упирается в ручное подтверждение отпечатков,
        а подтвердить их в момент отправки некому: запрос доверия выполняется
        внутри шифрования и заблокировал бы его до ответа пользователя.
        """
        return True

    async def _devices_blindly_trusted(
        self, blindly_trusted: frozenset[DeviceInformation], identifier: str | None
    ) -> None:
        """Сообщить, каким устройствам доверие выдано автоматически.

        Молчать здесь нельзя: слепое доверие - это компромисс, и пользователь
        обязан узнать, что оно применено, чтобы сверить отпечатки позже.
        """
        del identifier
        self._report(
            "blind-trust",
            {
                "devices": str(len(blindly_trusted)),
                "peers": ", ".join(sorted({item.bare_jid for item in blindly_trusted})),
            },
        )

    async def _prompt_manual_trust(
        self, manually_trusted: frozenset[DeviceInformation], identifier: str | None
    ) -> None:
        """Слепого доверия не хватило: решение принимает пользователь.

        Ждать ввода здесь нельзя - вызов происходит внутри шифрования и держал бы
        отправку. Поэтому отказ с подсказкой, а не диалог.
        """
        del identifier
        peers = ", ".join(sorted({item.bare_jid for item in manually_trusted}))
        self._report("manual-trust", {"devices": str(len(manually_trusted)), "peers": peers})
        raise TrustDecisionFailed(
            _("{hint}. Awaiting decision: {peers}").format(hint=_(MANUAL_TRUST_HINT), peers=peers)
        )

    def _report(self, action: str, detail: dict[str, str]) -> None:
        """Отдать событие наружу, если обработчик задан."""
        handler = self.config.get(REPORT_KEY)
        if callable(handler):
            handler(action, detail)
