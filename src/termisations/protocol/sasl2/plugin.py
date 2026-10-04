"""Плагин SASL2 и Bind 2 поверх точек расширения slixmpp.

Обработчик потоковой возможности регистрируется с порядком меньше 100 - того,
с которым идет штатный ``feature_mechanisms``. Так SASL2 получает ход первым,
когда сервер его объявил, и не мешает SASL1, когда не объявил.

Стрим после ``<success/>`` не перезапускается: XEP-0388 отменяет перезапуск,
и именно поэтому привязку ресурса можно сделать в том же круге. Следствие для
адаптера: события ``session_bind`` и ``session_start`` обязан поднять он сам,
иначе не запустится ничего ниже - ни XEP-0198, ни обработчики сессии.
"""

import base64
import logging
import uuid
from typing import Any, ClassVar, Final

from slixmpp import JID
from slixmpp.plugins.base import BasePlugin
from slixmpp.util import sasl
from slixmpp.util.stringprep_profiles import StringPrepError
from slixmpp.xmlstream import StanzaBase
from slixmpp.xmlstream.handler import Callback
from slixmpp.xmlstream.matcher import MatchXPath

from termisations.core.i18n import _
from termisations.protocol.sasl2 import stanza as sasl2

__all__ = ["SOFTWARE", "XEP_0388"]

_log = logging.getLogger(__name__)

# Название клиента в user-agent. Сервер показывает его в списке сессий.
SOFTWARE: Final = "termisations"

# Порядок обработчика потоковой возможности. Меньше 100, с которым идет
# feature_mechanisms: SASL2 обязан получить ход раньше SASL1.
FEATURE_ORDER: Final = 50

# Inline-действия, которые адаптер умеет разобрать в ответе.
KNOWN_INLINE: Final[tuple[str, ...]] = ("urn:xmpp:bind:0", "urn:xmpp:sm:3", "urn:xmpp:carbons:2")


class XEP_0388(BasePlugin):  # noqa: N801
    """SASL2 (XEP-0388) вместе с Bind 2 (XEP-0386)."""

    name = "xep_0388"
    # Метаданные плагина slixmpp, а не текст интерфейса: перевод не нужен.
    description = "XEP-0388: SASL2 and XEP-0386: Bind 2"
    dependencies: ClassVar[set[str]] = set()
    default_config: ClassVar[dict[str, Any]] = {
        # Устойчивый идентификатор установки. Задается снаружи: случайный на
        # каждый запуск добавляет запись в список сессий на сервере.
        "user_agent_id": "",
        # Метка ресурса для Bind 2. Сервер дописывает к ней свою часть.
        "resource_tag": SOFTWARE,
        # Аварийный выключатель на случай сервера, объявляющего SASL2 с
        # дефектами: без него дефект адаптера означает невозможность войти.
        "enabled": True,
    }

    @property
    def client(self) -> Any:
        """Клиент, к которому подключен плагин.

        Отдельное свойство нужно для типов: ``BasePlugin.xmpp`` объявлен как
        клиент или компонент, а SASL2 к компонентам отношения не имеет.
        """
        return self.xmpp

    def plugin_init(self) -> None:
        """Зарегистрировать строфы, обработчики и потоковую возможность."""
        sasl2.register_all()
        self.mech: Any = None
        self._inline_offered: tuple[str, ...] = ()
        if not str(self.config.get("user_agent_id") or ""):
            self.config["user_agent_id"] = str(uuid.uuid4())

        for element in (sasl2.Success, sasl2.Failure, sasl2.Challenge, sasl2.Continue):
            self.xmpp.register_stanza(element)
        self.xmpp.register_handler(
            Callback(
                "SASL2 Success",
                MatchXPath(sasl2.Success.tag_name()),
                self._handle_success,
                instream=True,
            )
        )
        self.xmpp.register_handler(
            Callback(
                "SASL2 Failure",
                MatchXPath(sasl2.Failure.tag_name()),
                self._handle_failure,
                instream=True,
            )
        )
        self.xmpp.register_handler(
            Callback(
                "SASL2 Challenge",
                MatchXPath(sasl2.Challenge.tag_name()),
                self._handle_challenge,
            )
        )
        self.xmpp.register_handler(
            Callback(
                "SASL2 Continue",
                MatchXPath(sasl2.Continue.tag_name()),
                self._handle_continue,
            )
        )
        self.client.register_feature(
            "sasl2", self._handle_feature, restart=False, order=FEATURE_ORDER
        )

    def plugin_end(self) -> None:
        """Снять обработчики."""
        for name in ("SASL2 Success", "SASL2 Failure", "SASL2 Challenge", "SASL2 Continue"):
            self.xmpp.remove_handler(name)

    # Потоковая возможность.

    def _handle_feature(self, features: StanzaBase) -> bool:
        """Начать обмен SASL2, если сервер его объявил.

        Возвращает ``False``: перезапуска потока по XEP-0388 нет, а дальнейшую
        обработку возможностей останавливает не этот флаг, а сам обмен - до
        ``<success/>`` сервер новых возможностей не шлет.
        """
        if not self.config.get("enabled", True) or "mechanisms" in self.client.features:
            return False
        offered = features["sasl2"]
        mechanisms = list(offered["mechanisms"])
        if not mechanisms:
            return False
        inline = offered["inline"]["features"] if offered.get_plugin("inline", check=True) else []
        self._inline_offered = tuple(inline)
        return self._send_authenticate(mechanisms)

    def _send_authenticate(self, mechanisms: list[str]) -> bool:
        """Выбрать механизм и отправить ``<authenticate/>``."""
        helper: Any = self.xmpp.plugin.get("feature_mechanisms", None)
        if helper is None:
            _log.error(_("SASL2: feature_mechanisms is not loaded, cannot choose a mechanism"))
            return False
        # Варианты -PLUS не заявляются: привязка канала недоступна в стандартном
        # ssl, и предлагать механизм, который нельзя выполнить, значит обещать
        # защиту, которой нет. Причина зафиксирована тестом про tls-exporter.
        usable = [name for name in mechanisms if not name.endswith("-PLUS")]
        try:
            self.mech = sasl.choose(
                usable, helper.sasl_callback, helper.security_callback, limit=helper.use_mechs
            )
        except (sasl.SASLNoAppropriateMechanism, StringPrepError):
            _log.error(_("SASL2: no suitable mechanism among %s"), ", ".join(usable))
            self.xmpp.event("failed_all_auth")
            return False

        request = sasl2.Authenticate(self.xmpp)
        request["mechanism"] = self.mech.name
        request["initial_response"] = _encode(self.mech.process())
        request["user_agent"]["id"] = str(self.config["user_agent_id"])
        request["user_agent"]["software"] = SOFTWARE
        if sasl2.BIND_NAMESPACE in self._inline_offered:
            request["bind2"]["tag"] = str(self.config["resource_tag"])
        request.send()
        return False

    # Обмен.

    def _handle_challenge(self, stanza_in: StanzaBase) -> None:
        """Ответить на вызов сервера."""
        if self.mech is None:
            return
        answer = sasl2.Response(self.xmpp)
        try:
            answer["value"] = _encode(self.mech.process(_decode(stanza_in["value"])))
        except (sasl.SASLFailed, sasl.SASLCancelled):
            _log.exception(_("SASL2: exchange aborted by the mechanism"))
            self.xmpp.disconnect()
            return
        answer.send()

    def _handle_success(self, stanza_in: StanzaBase) -> None:
        """Авторизация прошла: разобрать адрес и результаты inline-действий."""
        data = str(stanza_in["additional_data"] or "")
        if data and self.mech is not None:
            with _Quiet():
                self.mech.process(_decode(data))
        jid = str(stanza_in["jid"] or "")
        results = tuple(stanza_in["inline_results"])
        _log.debug(_("SASL2: success, address %s, inline %s"), jid, ", ".join(results) or _("no"))

        self.client.authenticated = True
        self.client.features.add("mechanisms")
        self.xmpp.event("auth_success", stanza_in)
        if jid:
            self._finish_bind(jid, results)

    def _finish_bind(self, jid: str, results: tuple[str, ...]) -> None:
        """Завершить привязку ресурса, выполненную inline по XEP-0386.

        Штатный ``feature_bind`` в этом круге не участвует, поэтому его работу
        приходится повторить: без ``session_bind`` и ``session_start`` не
        запустится ни XEP-0198, ни обработчики сессии.
        """
        self.client.boundjid = JID(jid)
        self.client.bound = True
        self.client.features.add("bind")
        self.client.session_bind_event.set()
        self.xmpp.event("session_bind", self.client.boundjid)
        for namespace in results:
            if namespace in KNOWN_INLINE:
                self.xmpp.event("sasl2_inline", namespace)
        self.client.sessionstarted = True
        self.xmpp.event("session_start")

    def _handle_failure(self, stanza_in: StanzaBase) -> None:
        """Сервер отклонил учетные данные."""
        condition = str(stanza_in["condition"] or "not-authorized")
        text = str(stanza_in["text"] or "")
        _log.error(_("SASL2: rejected %s %s"), condition, text)
        self.xmpp.event("failed_auth", stanza_in)
        self.xmpp.event("failed_all_auth")
        self.xmpp.disconnect()

    def _handle_continue(self, stanza_in: StanzaBase) -> None:
        """Сервер требует дополнительный шаг, которого адаптер не умеет.

        Отказ лучше молчания: продолжение обмена по XEP-0388 - это отдельная
        задача, и делать вид, что она выполнена, нельзя.
        """
        tasks = ", ".join(stanza_in["tasks"]) or _("none listed")
        _log.error(
            _("SASL2: server requires continuation (%s), the adapter does not support it"), tasks
        )
        self.xmpp.event("failed_auth", stanza_in)
        self.xmpp.disconnect()


class _Quiet:
    """Контекст, гасящий ошибку последнего шага механизма.

    Данные в ``<success/>`` механизм проверяет для взаимной аутентификации.
    Сбой здесь уже ничего не меняет: соединение установлено, и разрывать его
    из-за необязательной проверки хуже, чем записать в журнал.
    """

    def __enter__(self) -> None:
        """Ничего не подготавливает."""

    def __exit__(self, *exc: object) -> bool:
        """Поглотить исключение и записать его."""
        if exc[0] is not None:
            _log.warning(_("SASL2: final mechanism step did not verify: %s"), exc[1])
        return True


def _encode(value: bytes | None) -> str:
    """Полезная нагрузка механизма в base64. ``None`` дает пустую строку."""
    return "" if not value else base64.b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    """Полезная нагрузка из base64. Испорченное значение дает пустые байты."""
    try:
        return base64.b64decode(value or "")
    except ValueError:
        return b""
