"""Статус-бар: две строки состояния клиента внизу экрана.

Поля статус-бара: JID и присутствие, шифрование канала, шифрование беседы, latency,
транспорт, состояние потокового менеджмента, память процесса, спиннер активной
операции.

Два правила реализации, на которые опирается остальной код:

* метрики собираются в таймере раз в секунду, после чего виджет обновляется один
  раз; в ``render`` нет ни чтения procfs, ни других обращений к системе;
* строка собирается объектами ``rich.text.Text`` с явными стилями, а не
  markup-разметкой. JID, текст присутствия и детализация стадии приходят извне
  и могут содержать квадратные скобки, которые markup принял бы за теги.
"""

import time
from dataclasses import dataclass, replace
from typing import ClassVar, Final

from rich.cells import cell_len
from rich.text import Text
from textual.app import RenderResult
from textual.widget import Widget

from termisations.core.events import (
    ActiveConversationChanged,
    ConnectionStageChanged,
    ConversationsUpdated,
    EventBus,
    OperationProgress,
    StateUpdated,
    Subscription,
    UnsafeModeChanged,
)
from termisations.core.metrics import format_bytes, format_latency, read_rss_bytes
from termisations.core.models import (
    NOT_AVAILABLE,
    PRESENCE_MARKS,
    ClientState,
    ConnectionStage,
    Conversation,
    Encryption,
    OmemoInfo,
    stage_label,
    transport_label,
)

__all__ = ["METRICS_INTERVAL", "SPINNER_FRAMES", "SPINNER_INTERVAL", "STAGE_HOLD", "StatusBar"]

# Кадры брайлевского спиннера.
SPINNER_FRAMES: Final = "⣾⣽⣻⢿⡿⣟⣯⣷"

# Спиннер перерисовывается не чаще 12 кадров в секунду.
SPINNER_INTERVAL: Final = 1.0 / 12.0

# Метрики собираются с частотой 1 Гц.
METRICS_INTERVAL: Final = 1.0

# Сколько держится подпись завершенной стадии. Результат и длительность нужно
# успеть прочитать, но через полминуты после подключения они уже не текущие.
STAGE_HOLD: Final = 20.0

# Стадии, на которых спиннера нет. ERROR входит в их число, потому что ошибка это
# конечное состояние, а не выполняемая операция: крутить на ней анимацию бессмысленно.
_STATIC_STAGES: Final = frozenset(
    {ConnectionStage.READY, ConnectionStage.OFFLINE, ConnectionStage.ERROR}
)

# Порядок снятия полей при нехватке ширины. Память к диагностике соединения отношения
# не имеет, шифронабор длиннее всех остальных полей вместе взятых, транспорт
# дублируется версией TLS. JID, шифрование беседы, latency и маркер UNSAFE в списке
# отсутствуют и не снимаются никогда: когда снимать больше нечего, усекается JID,
# см. _shrink.
DROP_ORDER: Final = ("rss", "cipher", "transport", "binding", "sm", "tls", "stage")

# Разделитель полей. Ширина в две позиции.
_SEPARATOR: Final = "  "

# Ширина, которая берется, пока раскладка не назначила виджету размер.
_FALLBACK_WIDTH: Final = 80

# Минимум, ниже которого сокращаемое поле не усекается: от адреса должно
# оставаться хоть что-то узнаваемое.
_MIN_SHRINK_WIDTH: Final = 12

# Границы окраски latency в миллисекундах: до 100 мс нормально, до 300 мс
# заметно, дальше плохо.
_LATENCY_GOOD_MS: Final = 100.0
_LATENCY_FAIR_MS: Final = 300.0


@dataclass(frozen=True, slots=True)
class _Field:
    """Поле статус-бара.

    ``key`` - ключ приоритета усечения из ``DROP_ORDER``. Пустой ключ означает,
    что поле не снимается ни при какой ширине. ``shrink`` помечает поле, которое
    при нехватке ширины усекается с многоточием вместо снятия целиком.
    """

    content: Text
    key: str = ""
    shrink: bool = False

    @property
    def width(self) -> int:
        """Ширина поля в знакоместах терминала."""
        return cell_len(self.content.plain)


def _text_field(value: str, style: str = "", key: str = "") -> _Field:
    """Поле из одной строки с одним стилем."""
    return _Field(Text(value, style=style or "", no_wrap=True, end=""), key=key)


def _line_width(fields: list[_Field]) -> int:
    """Ширина строки из полей вместе с разделителями."""
    if not fields:
        return 0
    return sum(item.width for item in fields) + len(_SEPARATOR) * (len(fields) - 1)


def _fit(fields: list[_Field], width: int) -> list[_Field]:
    """Снять поля по приоритету, пока строка не влезет в ширину.

    Снимается именно поле целиком, а не хвост строки многоточием: при нехватке
    места пропадает наименее важная метрика, а оставшиеся читаются полностью.

    Порядок ``DROP_ORDER`` общий на обе строки, но применяется к той строке,
    которая не помещается. Снимать память со второй строки, когда не помещается
    первая, бессмысленно: место от этого не появится.
    """
    visible = fields
    for key in DROP_ORDER:
        if _line_width(visible) <= width:
            return visible
        visible = [item for item in visible if item.key != key]
    return visible


def _shrink(fields: list[_Field], width: int) -> list[_Field]:
    """Усечь сокращаемое поле, когда снимать больше нечего.

    Обрезка идет по JID, а не по концу строки. В конце строки стоят шифрование
    беседы и маркер UNSAFE, и срезать их значит скрыть признак отключенного
    маскирования ровно тогда, когда он нужнее всего. Свой адрес пользователь
    знает и так, поэтому усекается именно он, и обязательно с многоточием.
    """
    excess = _line_width(fields) - width
    if excess <= 0:
        return fields
    result = list(fields)
    for index, item in enumerate(result):
        if not item.shrink or excess <= 0:
            continue
        keep = max(item.width - excess, _MIN_SHRINK_WIDTH)
        if keep >= item.width:
            continue
        content = item.content.copy()
        content.truncate(keep, overflow="ellipsis")
        result[index] = replace(item, content=content)
        excess -= item.width - keep
    return result


def _render_line(fields: list[_Field], width: int) -> Text:
    """Собрать строку статус-бара, уложившись в ширину."""
    line = Text(no_wrap=True, overflow="ellipsis", end="")
    for index, item in enumerate(_shrink(_fit(fields, width), width)):
        if index:
            line.append(_SEPARATOR)
        line.append_text(item.content)
    # Страховка для совсем узкого терминала: даже усеченные обязательные поля
    # могут не влезть. Обрезка с многоточием, а не молча: иначе усеченное
    # значение выглядит как настоящее.
    line.truncate(width, overflow="ellipsis")
    return line


class StatusBar(Widget):
    """Строка состояния клиента.

    Виджет ничего не вычисляет: состояние приходит готовым в ``ClientState``.
    Единственная метрика, которую он снимает сам, - резидентная память, и то
    только если сессия не проставила ее в состоянии.
    """

    DEFAULT_CSS: ClassVar[str] = """
    StatusBar {
        height: 2;
        width: 1fr;
        padding: 0 1;
        background: $panel;
        color: $foreground;
        overflow: hidden hidden;
    }
    StatusBar.-unsafe {
        background: $error 20%;
    }
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._bus = bus
        self._subs = Subscription()
        self._state = ClientState()
        self._stage = ConnectionStage.OFFLINE
        self._stage_detail = ""
        # Стадия считается выполняемой, пока не пришло событие с длительностью.
        self._stage_running = False
        self._spinner_index = 0
        self._unsafe = False
        # Последний собственный замер памяти. None означает, что измерить нечем.
        self._rss_bytes: int | None = None
        # Когда завершилась показанная стадия. По нему подпись гаснет.
        self._stage_done_at: float | None = None
        # Длинная операция: загрузка файла, обход архива. Пока она идет, место
        # стадии занимает она - иначе о ходе операции нигде не видно.
        self._operation = ""
        self._operation_detail = ""
        self._operation_done_at: float | None = None
        # Активная беседа и ее список: шифрование показывается по беседе, а не по
        # глобальному флагу OMEMO. Зеленый OMEMO на открытой plain-беседе - прямая
        # дезинформация о защищенности канала.
        self._conversations: tuple[Conversation, ...] = ()
        self._active: str | None = None

    # Жизненный цикл.

    def on_mount(self) -> None:
        """Подписаться на шину и завести таймеры метрик и спиннера."""
        self._subs.add(self._bus.subscribe(StateUpdated, self._on_state))
        self._subs.add(self._bus.subscribe(ConnectionStageChanged, self._on_stage))
        self._subs.add(self._bus.subscribe(OperationProgress, self._on_progress))
        self._subs.add(self._bus.subscribe(UnsafeModeChanged, self._on_unsafe))
        self._subs.add(self._bus.subscribe(ConversationsUpdated, self._on_conversations))
        self._subs.add(self._bus.subscribe(ActiveConversationChanged, self._on_active))
        # Первый замер сразу, иначе первую секунду память показывалась бы как n/a.
        self._collect_metrics()
        self.set_interval(METRICS_INTERVAL, self._collect_metrics)
        self.set_interval(SPINNER_INTERVAL, self._advance_spinner)

    def on_unmount(self) -> None:
        """Снять подписки, чтобы шина не держала ссылку на снятый виджет."""
        self._subs.close()

    # Публичный интерфейс.

    def update_state(self, state: ClientState) -> None:
        """Принять новый агрегат состояния. Публикуется не чаще 1 раза в секунду."""
        self._state = state
        # Пока идет стадия, подпись берется из событий стадий: они точнее,
        # состояние обновляется реже.
        if not self._stage_running and self._stage_done_at is None:
            self._stage = state.stage
            self._stage_detail = ""
        self._set_unsafe(state.unsafe_xml)
        self.refresh()

    def set_stage(self, stage: ConnectionStage, detail: str) -> None:
        """Задать текущую стадию и ее детализацию."""
        self._stage = stage
        self._stage_detail = detail
        self._stage_running = stage not in _STATIC_STAGES
        self._stage_done_at = None
        if not self._stage_running:
            self._spinner_index = 0
        self.refresh()

    # Обработчики событий.

    def _on_state(self, event: StateUpdated) -> None:
        self.update_state(event.state)

    def _on_stage(self, event: ConnectionStageChanged) -> None:
        if event.duration_ms is None:
            self.set_stage(event.stage, event.detail)
            return
        # Стадия завершилась: спиннер гаснет, подпись с результатом и длительностью
        # держится до следующей стадии. Без этого область активности мигает:
        # между стадиями она была пустой.
        self._stage = event.stage
        detail = event.detail
        duration = f"{event.duration_ms:.0f}ms"
        self._stage_detail = f"{duration} {detail}" if detail else duration
        self._stage_running = False
        self._stage_done_at = time.monotonic()
        self.refresh()

    def _on_progress(self, event: OperationProgress) -> None:
        """Доля выполненного длинной операции.

        Стадиями подключения такие операции не являются: загрузка файла шла под
        подписью "получение архива", потому что ехала на чужой стадии.
        """
        if event.finished:
            self._operation = ""
            self._operation_detail = event.detail
            self._operation_done_at = time.monotonic()
        else:
            share = f" {round(event.done * 100 / event.total)}%" if event.total else ""
            self._operation = f"{event.operation}{share}"
            self._operation_detail = event.detail
            self._operation_done_at = None
        self.refresh()

    def _on_unsafe(self, event: UnsafeModeChanged) -> None:
        self._set_unsafe(event.enabled)
        self.refresh()

    def _on_conversations(self, event: ConversationsUpdated) -> None:
        self._conversations = event.items
        self.refresh()

    def _on_active(self, event: ActiveConversationChanged) -> None:
        self._active = event.jid
        self.refresh()

    # Таймеры.

    def _collect_metrics(self) -> None:
        """Снять метрики вне цикла рендера и обновить виджет один раз."""
        self._rss_bytes = read_rss_bytes()
        self._expire_stage()
        self.refresh()

    def _expire_stage(self) -> None:
        """Погасить подпись завершенной стадии, когда она перестала быть текущей."""
        done_at = self._stage_done_at
        if done_at is None or time.monotonic() - done_at < STAGE_HOLD:
            return
        self._stage_done_at = None
        self._stage = self._state.stage
        self._stage_detail = ""

    def _advance_spinner(self) -> None:
        """Прокрутить кадр спиннера. Без активной стадии перерисовки нет."""
        if not self._spinner_active:
            return
        self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)
        self.refresh()

    # Отрисовка.

    def render(self) -> RenderResult:
        """Собрать две строки статус-бара под текущую ширину."""
        width = self.size.width or _FALLBACK_WIDTH
        text = Text(no_wrap=True, overflow="crop", end="")
        text.append_text(_render_line(self._identity_fields(), width))
        text.append("\n")
        text.append_text(_render_line(self._metric_fields(), width))
        return text

    @property
    def _spinner_active(self) -> bool:
        """Идет ли операция, для которой нужна анимация."""
        return self._stage_running and self._stage not in _STATIC_STAGES

    def _identity_fields(self) -> list[_Field]:
        """Первая строка: кто мы, чем зашифрованы беседа и канал.

        Маркер UNSAFE стоит сразу после присутствия, а не в конце строки: на узком
        терминале конец строки обрезается первым, а признак отключенного
        маскирования нужен всегда. JID пользователь и так знает.
        """
        fields = [self._jid_field()]
        if self._unsafe:
            # Пока маскирование выключено, горит маркер.
            fields.append(_text_field("UNSAFE", "bold white on red"))
        fields.append(self._encryption_field())
        fields.extend(self._tls_fields())
        return fields

    def _metric_fields(self) -> list[_Field]:
        """Вторая строка: измеряемые величины и текущая операция."""
        return [
            self._latency_field(),
            _text_field(transport_label(self._state.transport), "cyan", key="transport"),
            self._sm_field(),
            self._memory_field(),
            *self._activity_fields(),
        ]

    def _jid_field(self) -> _Field:
        """JID с маркером присутствия. Не снимается ни при какой ширине."""
        mark, mark_style = PRESENCE_MARKS.get(self._state.presence_show, ("○", "dim"))
        content = Text(no_wrap=True, end="")
        content.append(f"{mark} ", style=mark_style)
        content.append(self._state.jid or NOT_AVAILABLE, style="bold")
        return _Field(content, shrink=True)

    def _encryption_field(self) -> _Field:
        """Шифрование активной беседы и число доверенных устройств.

        И тип, и счетчик берутся из одной беседы: открытая беседа может быть
        незашифрованной при включенном OMEMO в другой, а два источника давали
        число устройств от последней беседы, где выполнялась команда.
        """
        encryption = self._conversation_encryption()
        if encryption is Encryption.PLAIN:
            return _Field(Text("plain", style="yellow", no_wrap=True, end=""))
        name = encryption.value.upper()
        omemo = self._active_omemo()
        if encryption is not Encryption.OMEMO or not omemo.enabled:
            return _text_field(name, "green")
        total = omemo.total_devices or omemo.trusted_devices
        if omemo.trusted_devices and omemo.trusted_devices == total:
            return _text_field(f"{name}✓ {omemo.trusted_devices} dev", "green")
        if omemo.trusted_devices:
            return _text_field(f"{name}! {omemo.trusted_devices}/{total} dev", "yellow")
        return _text_field(f"{name}✗ 0/{total} dev", "red")

    def _active_conversation(self) -> Conversation | None:
        """Активная беседа или None, пока она не выбрана."""
        for conversation in self._conversations:
            if conversation.jid == self._active:
                return conversation
        return None

    def _conversation_encryption(self) -> Encryption:
        """Шифрование активной беседы. Без беседы - шифрования нет."""
        conversation = self._active_conversation()
        return conversation.encryption if conversation is not None else Encryption.PLAIN

    def _active_omemo(self) -> OmemoInfo:
        """Состояние OMEMO активной беседы."""
        conversation = self._active_conversation()
        return conversation.omemo if conversation is not None else OmemoInfo()

    def _tls_fields(self) -> list[_Field]:
        """Версия TLS, шифронабор и тип привязки к каналу."""
        tls = self._state.tls
        if tls.version is None:
            return [_text_field(f"TLS {NOT_AVAILABLE}", "dim", key="tls")]
        version = _text_field(tls.version, "green" if tls.valid else "yellow", key="tls")
        cipher = _text_field(tls.cipher or NOT_AVAILABLE, "cyan", key="cipher")
        binding = _text_field(tls.channel_binding or NOT_AVAILABLE, "cyan", key="binding")
        return [version, cipher, binding]

    def _latency_field(self) -> _Field:
        """Скользящее среднее задержки. Не снимается ни при какой ширине."""
        latency = self._state.metrics.latency_ms
        if latency is None:
            style = "dim"
        elif latency <= _LATENCY_GOOD_MS:
            style = "green"
        elif latency <= _LATENCY_FAIR_MS:
            style = "yellow"
        else:
            style = "red"
        return _text_field(format_latency(latency), style)

    def _sm_field(self) -> _Field:
        """Потоковый менеджмент: факт возобновления и счетчики строфов.

        Величины разнородные, поэтому у каждой своя подпись: ack - очередь
        неподтвержденных исходящих, h - накопительное число обработанных
        входящих. Две стрелки без подписей читались как одна и та же величина.
        """
        sm = self._state.sm
        if not sm.enabled:
            return _text_field(f"SM {NOT_AVAILABLE}", "dim", key="sm")
        resumed = "↻" if sm.resumed else ""
        style = "yellow" if sm.outbound_unacked else "green"
        value = f"SM{resumed} ack{sm.outbound_unacked} h{sm.inbound_handled}"
        return _text_field(value, style, key="sm")

    def _memory_field(self) -> _Field:
        """Резидентная память. Значение сессии приоритетнее собственного замера."""
        rss = self._state.metrics.rss_bytes
        if rss is None:
            rss = self._rss_bytes
        return _text_field(f"rss {format_bytes(rss)}", "dim", key="rss")

    def _activity_fields(self) -> list[_Field]:
        """Спиннер и подпись текущей стадии.

        Завершенная стадия оставляет подпись с результатом и длительностью до
        начала следующей: сами стадии проходят за десятки миллисекунд, и без
        этого подпись прочитать невозможно.
        """
        operation = self._operation_field()
        if operation is not None:
            return operation
        label = stage_label(self._stage)
        if self._stage_detail:
            label = f"{label} {self._stage_detail}"
        if self._spinner_active:
            frame = SPINNER_FRAMES[self._spinner_index]
            return [_text_field(frame, "cyan"), _text_field(label, "dim", key="stage")]
        if self._stage is ConnectionStage.ERROR:
            return [_text_field(label, "bold red", key="stage")]
        if self._stage is ConnectionStage.OFFLINE and not self._stage_detail:
            return []
        return [_text_field(label, "dim", key="stage")]

    def _operation_field(self) -> list[_Field] | None:
        """Область активности, занятая длинной операцией.

        ``None`` означает, что операции нет и место занимает стадия. Итог
        держится столько же, сколько итог стадии: без этого подпись "загружено"
        исчезала бы быстрее, чем ее успевали прочитать.
        """
        done_at = self._operation_done_at
        if done_at is not None and time.monotonic() - done_at > STAGE_HOLD:
            self._operation_done_at = None
            self._operation_detail = ""
        if self._operation:
            label = f"{self._operation} {self._operation_detail}".strip()
            frame = SPINNER_FRAMES[self._spinner_index]
            return [_text_field(frame, "cyan"), _text_field(label, "dim", key="stage")]
        if self._operation_done_at is not None and self._operation_detail:
            return [_text_field(self._operation_detail, "dim", key="stage")]
        return None

    # Служебное.

    def _set_unsafe(self, enabled: bool) -> None:
        """Запомнить режим unsafe и повесить класс, на который опирается тема."""
        self._unsafe = enabled
        self.set_class(enabled, "-unsafe")
