"""Путь строфы: что с ней произошло от отправки до прочтения.

Команда ``/trace`` отвечает на один вопрос: где сообщение. Ответ собирается из
четырех независимых источников, и в этом вся сложность - каждый может не прийти,
и отсутствие отметки само по себе диагноз:

* ``origin-id`` по XEP-0359 ставит отправитель. Он есть всегда;
* ``stanza-id`` ставит сервер. Его отсутствие означает, что архив сообщение не
  записал, и искать его в MAM бесполезно;
* подтверждение XEP-0198 говорит, что строфа принята сервером. Без него
  сообщение не ушло дальше сокета;
* receipt XEP-0184 и маркер XEP-0333 приходят от собеседника. Их отсутствие
  разделяет "не дошло до устройства" и "дошло, но не прочитано".

Запись пути живет в ядре, а не в протокольном слое: ее наполняют и эмулятор, и
сетевая сессия, а показывает одна и та же команда.
"""

import time
from dataclasses import dataclass, field, replace
from typing import Final

from termisations.core.i18n import _
from termisations.core.models import DeliveryState

__all__ = ["MAX_TRACES", "Trace", "TraceStep", "TraceStore"]

# Сколько путей держится в памяти. Команда смотрит недавние сообщения, а не всю
# историю: путь собирается из событий сессии, и хранить его вечно незачем.
MAX_TRACES: Final = 500


@dataclass(frozen=True, slots=True)
class TraceStep:
    """Одна отметка пути: что произошло, когда и по какому расширению."""

    xep: str
    action: str
    ts: float
    detail: str = ""
    peer: str = ""


@dataclass(frozen=True, slots=True)
class Trace:
    """Путь одной строфы."""

    message_id: str
    conversation: str
    origin_id: str = ""
    stanza_id: str = ""
    steps: tuple[TraceStep, ...] = field(default_factory=tuple)

    def with_step(self, step: TraceStep) -> "Trace":
        """Новая запись с добавленной отметкой.

        Повтор по паре расширение-действие от того же участника не добавляется:
        сервер шлет подтверждение XEP-0198 пачкой, и одно и то же событие иначе
        занимало бы весь вывод команды.
        """
        if any(
            item.xep == step.xep and item.action == step.action and item.peer == step.peer
            for item in self.steps
        ):
            return self
        return replace(self, steps=(*self.steps, step))

    @property
    def state(self) -> DeliveryState:
        """Состояние доставки, выведенное из отметок пути."""
        # Имена действий те же, что публикует сессия в XepEvent: путь и метки в
        # ленте обязаны говорить об одном и том же событии одинаково.
        actions = {(item.xep, item.action) for item in self.steps}
        if any(xep == "0333" for xep, _ in actions):
            return DeliveryState.DISPLAYED
        if any(xep == "0184" for xep, _ in actions):
            return DeliveryState.RECEIVED
        if ("0198", "acked") in actions:
            return DeliveryState.ACKED
        if ("0359", "origin-id") in actions:
            return DeliveryState.SENT
        return DeliveryState.PENDING

    def rows(self) -> tuple[tuple[str, str], ...]:
        """Строки таблицы для команды /trace.

        Пустые поля не скрываются: отсутствие ``stanza-id`` - это ответ на
        вопрос, а не нечего показать.
        """
        started = self.steps[0].ts if self.steps else 0.0
        rows: list[tuple[str, str]] = [
            (_("conversation"), self.conversation),
            ("origin-id", self.origin_id or _("not set")),
            (
                "stanza-id",
                self.stanza_id
                or _("not assigned by the server, the message is not in the archive"),
            ),
        ]
        for step in self.steps:
            offset = f"+{(step.ts - started) * 1000:.0f}ms" if started else ""
            detail = " ".join(part for part in (step.detail, step.peer, offset) if part)
            rows.append((f"XEP-{step.xep} {step.action}", detail))
        rows.append((_("state"), self.state.value))
        return tuple(rows)


class TraceStore:
    """Пути последних строф. Кольцо по числу записей, не по времени."""

    def __init__(self, limit: int = MAX_TRACES) -> None:
        """Создать хранилище путей."""
        self._limit = max(1, limit)
        self._traces: dict[str, Trace] = {}
        self._by_stanza: dict[str, str] = {}

    def start(self, message_id: str, conversation: str, origin_id: str = "") -> Trace:
        """Начать путь строфы. Повторный вызов возвращает существующую запись."""
        trace = self._traces.get(message_id)
        if trace is None:
            trace = Trace(message_id=message_id, conversation=conversation, origin_id=origin_id)
            self._traces[message_id] = trace
            self._trim()
        return trace

    def note(
        self,
        message_id: str,
        xep: str,
        action: str,
        *,
        detail: str = "",
        peer: str = "",
        ts: float | None = None,
    ) -> Trace | None:
        """Добавить отметку к пути. ``None``, если пути с таким идентификатором нет."""
        trace = self._traces.get(message_id)
        if trace is None:
            return None
        step = TraceStep(xep=xep, action=action, ts=ts or time.time(), detail=detail, peer=peer)
        updated = trace.with_step(step)
        self._traces[message_id] = updated
        return updated

    def set_stanza_id(self, message_id: str, stanza_id: str) -> None:
        """Запомнить идентификатор, присвоенный сервером."""
        trace = self._traces.get(message_id)
        if trace is None or not stanza_id:
            return
        self._traces[message_id] = replace(trace, stanza_id=stanza_id)
        self._by_stanza[stanza_id] = message_id

    def find(self, identifier: str) -> Trace | None:
        """Путь по идентификатору сообщения, origin-id или stanza-id.

        Пользователь копирует идентификатор из панели лога и не обязан знать, чей
        он: поиск идет по всем трем.
        """
        key = identifier.strip()
        if not key:
            return None
        direct = self._traces.get(key)
        if direct is not None:
            return direct
        by_stanza = self._by_stanza.get(key)
        if by_stanza is not None:
            return self._traces.get(by_stanza)
        for trace in self._traces.values():
            if trace.origin_id == key or trace.stanza_id == key:
                return trace
        return None

    def __len__(self) -> int:
        """Число записей в хранилище."""
        return len(self._traces)

    def _trim(self) -> None:
        """Снять самые старые записи сверх предела."""
        while len(self._traces) > self._limit:
            oldest = next(iter(self._traces))
            trace = self._traces.pop(oldest)
            self._by_stanza.pop(trace.stanza_id, None)
