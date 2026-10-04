"""Контракт сессии: то, что приложение ждет от источника событий.

Приложение не должно знать, откуда приходят строфы: из эмулятора или из
настоящего сокета. Обе реализации - ``mock.MockSession`` и
``protocol.session.SlixmppSession`` - соблюдают этот протокол, поэтому
``app.py`` и ``cli.py`` собираются одинаково для обоих режимов.

Протокол описан структурно (``typing.Protocol``), а не наследованием: у
эмулятора и у адаптера нет общей реализации, общее у них только это API.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from termisations.core.events import Command
from termisations.core.models import ClientState

__all__ = ["Session"]


@runtime_checkable
class Session(Protocol):
    """Источник событий и исполнитель команд.

    Сессия владеет состоянием клиента и публикует события в шину. Все, что
    приложение может ей сказать, проходит через ``handle_command``: прямых
    вызовов из интерфейса нет.
    """

    @property
    def state(self) -> ClientState:
        """Текущее состояние клиента: аккаунт, стадия, канал, метрики."""
        ...

    @property
    def stanzas_total(self) -> int:
        """Число строф, отданных в шину с момента создания сессии."""
        ...

    @property
    def stanzas_per_sec(self) -> float:
        """Фактическая частота потока за последнее окно измерения."""
        ...

    async def run(self) -> None:
        """Вести сессию до остановки. Выход из метода означает конец работы."""
        ...

    def stop(self) -> None:
        """Остановить сессию. Повторный вызов безопасен."""
        ...

    async def handle_command(self, command: Command) -> None:
        """Исполнить команду шины. Назначается через ``bus.set_command_handler``."""
        ...

    def describe(self) -> Sequence[str]:
        """Короткая сводка о сессии для команды /account и для тестов."""
        ...
