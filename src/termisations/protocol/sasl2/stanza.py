"""Строфы SASL2 (XEP-0388) и Bind 2 (XEP-0386).

Разметка взята из самих расширений, а не из чужой реализации. Разбор и сборка
проверяются тестами на образцах из текста XEP: сервера с поддержкой SASL2 под
рукой может не оказаться, а форма строфы от этого не меняется.
"""

from typing import Any, Final

from slixmpp.stanza import StreamFeatures
from slixmpp.xmlstream import ElementBase, StanzaBase, register_stanza_plugin

__all__ = [
    "BIND_NAMESPACE",
    "SASL2_NAMESPACE",
    "Authenticate",
    "Authentication",
    "Bind2",
    "Bound",
    "Challenge",
    "Continue",
    "Failure",
    "Inline",
    "Response",
    "Success",
    "UserAgent",
    "register_all",
]

SASL2_NAMESPACE: Final = "urn:xmpp:sasl:2"
BIND_NAMESPACE: Final = "urn:xmpp:bind:0"


class Authentication(ElementBase):
    """Потоковая возможность: список механизмов и блок inline."""

    name = "authentication"
    namespace = SASL2_NAMESPACE
    plugin_attrib = "sasl2"
    interfaces = {"mechanisms"}

    def get_mechanisms(self) -> list[str]:
        """Механизмы, предложенные сервером, в порядке объявления."""
        return [
            (element.text or "").strip()
            for element in self.xml.findall(f"{{{SASL2_NAMESPACE}}}mechanism")
            if (element.text or "").strip()
        ]


class Inline(ElementBase):
    """Блок inline: что сервер готов сделать в одном круге с авторизацией."""

    name = "inline"
    namespace = SASL2_NAMESPACE
    plugin_attrib = "inline"
    interfaces = {"features"}

    def get_features(self) -> list[str]:
        """Пространства имен предложенных inline-действий."""
        return [_namespace_of(child.tag) for child in self.xml]


class UserAgent(ElementBase):
    """Описание клиента: устойчивый идентификатор установки и название.

    Идентификатор обязан быть устойчивым между запусками: по нему сервер
    отличает устройства в списке сессий. Случайное значение на каждый запуск
    добавляет в этот список новую запись.
    """

    name = "user-agent"
    namespace = SASL2_NAMESPACE
    plugin_attrib = "user_agent"
    interfaces = {"id", "software", "device"}
    sub_interfaces = {"software", "device"}


class Bind2(ElementBase):
    """Запрос привязки ресурса в одном круге с авторизацией по XEP-0386."""

    name = "bind"
    namespace = BIND_NAMESPACE
    plugin_attrib = "bind2"
    interfaces = {"tag"}
    sub_interfaces = {"tag"}


class Authenticate(StanzaBase):
    """Начало обмена: механизм, первый ответ и inline-запросы."""

    name = "authenticate"
    namespace = SASL2_NAMESPACE
    plugin_attrib = "authenticate"
    interfaces = {"mechanism", "initial_response"}

    def set_initial_response(self, value: str) -> None:
        """Первый ответ механизма в base64. Пустое значение элемент не создает."""
        self._del_sub(f"{{{SASL2_NAMESPACE}}}initial-response")
        if value:
            self._set_sub_text(f"{{{SASL2_NAMESPACE}}}initial-response", value)

    def get_initial_response(self) -> str:
        """Первый ответ механизма в base64."""
        return str(self._get_sub_text(f"{{{SASL2_NAMESPACE}}}initial-response", ""))


class Response(StanzaBase):
    """Очередной ответ клиента в обмене SASL."""

    name = "response"
    namespace = SASL2_NAMESPACE
    plugin_attrib = "sasl2_response"
    interfaces = {"value"}

    def get_value(self) -> str:
        """Полезная нагрузка в base64."""
        return (self.xml.text or "").strip()

    def set_value(self, value: str) -> None:
        """Полезная нагрузка в base64."""
        self.xml.text = value


class Challenge(StanzaBase):
    """Вызов сервера в обмене SASL."""

    name = "challenge"
    namespace = SASL2_NAMESPACE
    plugin_attrib = "sasl2_challenge"
    interfaces = {"value"}

    def get_value(self) -> str:
        """Полезная нагрузка в base64."""
        return (self.xml.text or "").strip()

    def set_value(self, value: str) -> None:
        """Полезная нагрузка в base64."""
        self.xml.text = value


class Bound(ElementBase):
    """Подтверждение привязки ресурса внутри success."""

    name = "bound"
    namespace = BIND_NAMESPACE
    plugin_attrib = "bound"
    interfaces = set()


class Success(StanzaBase):
    """Успешное завершение: назначенный адрес и результаты inline-действий."""

    name = "success"
    namespace = SASL2_NAMESPACE
    plugin_attrib = "sasl2_success"
    interfaces = {"jid", "additional_data", "inline_results"}

    def get_jid(self) -> str:
        """Адрес, назначенный сервером."""
        return str(self._get_sub_text(f"{{{SASL2_NAMESPACE}}}authorization-identifier", ""))

    def get_additional_data(self) -> str:
        """Последние данные механизма в base64, если они есть."""
        return str(self._get_sub_text(f"{{{SASL2_NAMESPACE}}}additional-data", ""))

    def get_inline_results(self) -> list[str]:
        """Пространства имен inline-действий, которые сервер выполнил."""
        skip = {
            f"{{{SASL2_NAMESPACE}}}authorization-identifier",
            f"{{{SASL2_NAMESPACE}}}additional-data",
        }
        return [_namespace_of(child.tag) for child in self.xml if child.tag not in skip]


class Failure(StanzaBase):
    """Отказ авторизации: условие и пояснение."""

    name = "failure"
    namespace = SASL2_NAMESPACE
    plugin_attrib = "sasl2_failure"
    interfaces = {"condition", "text"}

    def get_condition(self) -> str:
        """Условие отказа по RFC 6120. Пустая строка, если сервер его не дал."""
        for child in self.xml:
            tag = str(child.tag)
            if tag.endswith("}text"):
                continue
            return tag.rsplit("}", 1)[-1]
        return ""

    def get_text(self) -> str:
        """Пояснение отказа, если сервер его дал."""
        return str(self._get_sub_text(f"{{{SASL2_NAMESPACE}}}text", ""))


class Continue(StanzaBase):
    """Сервер требует дополнительный шаг, которого адаптер не умеет.

    Отказ здесь лучше молчания: продолжение обмена по XEP-0388 предполагает
    вторую задачу (например, подтверждение через второй фактор), и делать вид,
    что она выполнена, нельзя.
    """

    name = "continue"
    namespace = SASL2_NAMESPACE
    plugin_attrib = "sasl2_continue"
    interfaces = {"tasks"}

    def get_tasks(self) -> list[str]:
        """Задачи, которые предлагает сервер."""
        tasks = self.xml.find(f"{{{SASL2_NAMESPACE}}}tasks")
        if tasks is None:
            return []
        return [(item.text or "").strip() for item in tasks if (item.text or "").strip()]


def _namespace_of(tag: Any) -> str:
    """Пространство имен из полного имени элемента."""
    text = str(tag)
    return text[1:].split("}", 1)[0] if text.startswith("{") else ""


def register_all() -> None:
    """Связать строфы между собой. Вызывается при инициализации плагина."""
    register_stanza_plugin(StreamFeatures, Authentication)
    register_stanza_plugin(Authentication, Inline)
    register_stanza_plugin(Authenticate, UserAgent)
    register_stanza_plugin(Authenticate, Bind2)
    register_stanza_plugin(Success, Bound)
