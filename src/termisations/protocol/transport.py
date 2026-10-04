"""Выбор способа подключения: резолв SRV и параметры канала.

Подключение идет по XEP-0368: сначала ищется ``_xmpps-client._tcp`` и прямой TLS
на найденном порту, затем ``_xmpp-client._tcp`` со STARTTLS, и только потом
запасной путь по A и AAAA на порт 5222. Порядок записей определяется
приоритетом и весом по RFC 2782.

Разбор параметров установленного канала (версия, шифр, привязка) лежит здесь же:
статус-бару нужна одна структура, а не разбор объекта сокета в трех местах.
"""

import random
import socket
import ssl
from dataclasses import dataclass
from typing import Final

from termisations.core.models import TlsInfo, Transport

__all__ = [
    "DEFAULT_DIRECT_TLS_PORT",
    "DEFAULT_STARTTLS_PORT",
    "Endpoint",
    "channel_binding_of",
    "resolve",
    "tls_info_of",
]

DEFAULT_STARTTLS_PORT: Final = 5222
DEFAULT_DIRECT_TLS_PORT: Final = 5223

_TLS_SERVICE: Final = "_xmpps-client._tcp"
_STARTTLS_SERVICE: Final = "_xmpp-client._tcp"

# Имя цели, которым сервер сообщает, что услуга не предоставляется (RFC 2782).
_NO_SERVICE: Final = "."


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Адрес подключения вместе со способом защиты канала."""

    host: str
    port: int
    transport: Transport
    source: str
    """Откуда взялся адрес: имя SRV-записи, "A/AAAA" или "аргумент запуска"."""

    @property
    def direct_tls(self) -> bool:
        """Канал шифруется сразу, без STARTTLS."""
        return self.transport is Transport.DIRECT_TLS

    def describe(self) -> str:
        """Строка для журнала подключения и для команды /tls."""
        return f"{self.host}:{self.port} ({self.source})"


def _sorted_records(records: list[tuple[int, int, int, str]]) -> list[tuple[int, int, int, str]]:
    """Упорядочить записи SRV по RFC 2782: приоритет по возрастанию, вес жребием.

    Вес разыгрывается внутри группы одного приоритета: запись с большим весом
    чаще оказывается первой, но и запись с весом ноль шанс получает. Это и есть
    балансировка, ради которой вес в записи существует.
    """
    ordered: list[tuple[int, int, int, str]] = []
    by_priority: dict[int, list[tuple[int, int, int, str]]] = {}
    for record in records:
        by_priority.setdefault(record[0], []).append(record)
    for priority in sorted(by_priority):
        group = by_priority[priority]
        while group:
            total = sum(item[1] for item in group)
            if total <= 0:
                # Все веса нулевые: порядок внутри группы произволен.
                ordered.extend(group)
                break
            pick = random.randint(0, total)
            running = 0
            for item in group:
                running += item[1]
                if running >= pick:
                    ordered.append(item)
                    group.remove(item)
                    break
            else:
                ordered.append(group.pop(0))
    return ordered


async def _query_srv(service: str, domain: str, timeout: float) -> list[tuple[int, int, int, str]]:
    """Запросить записи SRV. Пустой список означает, что услуги нет."""
    try:
        import aiodns
    except ImportError:  # pragma: no cover - aiodns приходит зависимостью slixmpp
        return []
    resolver = aiodns.DNSResolver(timeout=timeout, tries=1)
    try:
        answers = await resolver.query(f"{service}.{domain}", "SRV")
    except Exception:
        # Отсутствие записи - штатный случай, а не сбой: домен просто не
        # объявляет услугу. Разбирать коды ошибок резолвера смысла нет.
        return []
    records = [
        (int(item.priority), int(item.weight), int(item.port), str(item.host).rstrip("."))
        for item in answers
        if str(item.host).rstrip(".") not in ("", _NO_SERVICE)
    ]
    return _sorted_records(records)


async def resolve(domain: str, timeout: float = 5.0) -> list[Endpoint]:
    """Список адресов подключения в порядке предпочтения.

    Прямой TLS идет раньше STARTTLS: канал, зашифрованный с первого байта, не
    оставляет окна для понижения защиты на этапе согласования.
    """
    endpoints: list[Endpoint] = []
    for record in await _query_srv(_TLS_SERVICE, domain, timeout):
        endpoints.append(
            Endpoint(record[3], record[2], Transport.DIRECT_TLS, f"SRV {_TLS_SERVICE}")
        )
    for record in await _query_srv(_STARTTLS_SERVICE, domain, timeout):
        endpoints.append(
            Endpoint(record[3], record[2], Transport.STARTTLS, f"SRV {_STARTTLS_SERVICE}")
        )
    if not endpoints:
        # Запасной путь RFC 6120: имя домена и стандартный порт. Проверка A и
        # AAAA здесь же: если имя не разрешается, дальше идти незачем.
        try:
            await _resolve_host(domain)
        except OSError:
            return []
        endpoints.append(Endpoint(domain, DEFAULT_STARTTLS_PORT, Transport.STARTTLS, "A/AAAA"))
    return endpoints


async def _resolve_host(host: str) -> None:
    """Проверить, что имя разрешается в адрес. Бросает OSError, если нет."""
    loop = __import__("asyncio").get_running_loop()
    await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)


def channel_binding_of(sock: ssl.SSLObject | ssl.SSLSocket | None) -> str | None:
    """Тип привязки канала, доступный для SASL.

    Возвращает ``None``, когда привязки нет. Это не недоработка клиента:
    в стандартном модуле ssl реализован только ``tls-unique``, а он запрещен
    на TLS 1.3 (RFC 9266). Экспортер ``tls-exporter``, которого требует
    XEP-0440, в CPython не реализован, поэтому SCRAM-PLUS на TLS 1.3
    недоступен ни одному клиенту на стандартной библиотеке.
    """
    if sock is None:
        return None
    version = sock.version()
    if version == "TLSv1.3":
        return None
    try:
        binding = sock.get_channel_binding("tls-unique")
    except (ValueError, AttributeError):
        return None
    return "tls-unique" if binding else None


def tls_info_of(sock: ssl.SSLObject | ssl.SSLSocket | None, *, verified: bool) -> TlsInfo:
    """Параметры установленного канала для статус-бара и команды /tls."""
    if sock is None:
        return TlsInfo()
    cipher = sock.cipher()
    return TlsInfo(
        version=sock.version(),
        cipher=cipher[0] if cipher else None,
        channel_binding=channel_binding_of(sock),
        valid=verified,
    )
