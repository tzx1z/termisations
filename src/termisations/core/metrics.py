"""Метрики процесса и соединения.

Модуль не зависит ни от Textual, ни от внешних пакетов. psutil сознательно не
используется: из всего пакета нужен один показатель - резидентная память, а
взамен он требует сборки бинарного расширения на целевой машине и превращает
установку клиента в невоспроизводимую. Резидентная память читается напрямую из
procfs, задержка и частота считаются на стандартных структурах.

Форматирование вынесено в тонкие обертки над ``core.models``: единая точка
правды на весь проект, иначе статус-бар и команда /stats разойдутся в записи
одних и тех же величин.
"""

import math
import os
import resource
import sys
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Final

from termisations.core import models
from termisations.core.i18n import _

__all__ = [
    "LatencyTracker",
    "ProcessUptime",
    "RateCounter",
    "format_bytes",
    "format_latency",
    "read_rss_bytes",
]

# Вторая колонка /proc/self/statm - число резидентных страниц процесса.
_STATM_PATH: Final = Path("/proc/self/statm")

# Индекс колонки с резидентными страницами и минимальное число колонок в файле.
_STATM_RSS_COLUMN: Final = 1


def _page_size() -> int:
    """Размер страницы памяти в байтах. 4096 - безопасное значение по умолчанию."""
    try:
        return os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return 4096


_PAGE_SIZE: Final = _page_size()

# Единицы ru_maxrss различаются по системам: на Linux это килобайты, на macOS
# байты (getrusage(2)). Проверка вынесена в константу: прямое сравнение
# sys.platform внутри функции mypy считает недостижимой веткой.
_MAXRSS_IN_BYTES: Final = sys.platform == "darwin"


def read_rss_bytes() -> int | None:
    """Резидентная память процесса в байтах или None, если измерить нечем.

    Основной источник - /proc/self/statm: одна короткая строка, разбор дешевле,
    чем построчный поиск VmRSS в /proc/self/status. Если procfs недоступен,
    берется ru_maxrss из resource.getrusage. Это пиковое, а не текущее значение,
    поэтому источник вторичный, но других переносимых нет.
    """
    try:
        columns = _STATM_PATH.read_text(encoding="ascii").split()
        if len(columns) > _STATM_RSS_COLUMN:
            return int(columns[_STATM_RSS_COLUMN]) * _PAGE_SIZE
    except (OSError, ValueError):
        # Файла нет или содержимое не разбирается - переходим к запасному источнику.
        pass
    try:
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError):
        return None
    if max_rss <= 0:
        return None
    return max_rss if _MAXRSS_IN_BYTES else max_rss * 1024


class LatencyTracker:
    """Скользящее среднее задержки по последним N замерам.

    Размер окна по умолчанию 5: latency усредняется по пяти последним пингам
    XEP-0199.
    """

    __slots__ = ("_values",)

    def __init__(self, window: int = 5) -> None:
        if window < 1:
            raise ValueError(_("window size must be at least 1"))
        self._values: deque[float] = deque(maxlen=window)

    @property
    def window(self) -> int:
        """Размер окна усреднения."""
        # maxlen задан в конструкторе, поэтому None здесь невозможен.
        return self._values.maxlen or 0

    def add(self, ms: float) -> None:
        """Добавить замер в миллисекундах.

        Отрицательные значения, NaN и бесконечность отбрасываются: испорченный
        замер не должен портить среднее.
        """
        if not math.isfinite(ms) or ms < 0:
            return
        self._values.append(float(ms))

    def average(self) -> float | None:
        """Среднее по окну. None, пока нет ни одного замера."""
        if not self._values:
            return None
        return math.fsum(self._values) / len(self._values)

    def last(self) -> float | None:
        """Последний замер. None, пока нет ни одного."""
        return self._values[-1] if self._values else None

    def clear(self) -> None:
        """Сбросить накопленные замеры, например при переподключении."""
        self._values.clear()

    def __len__(self) -> int:
        """Число замеров в окне."""
        return len(self._values)


class RateCounter:
    """Частота событий в секунду по скользящему окну.

    Вызывается на горячем пути (до 500 строф в секунду), поэтому ``tick`` только
    добавляет запись и подрезает хвост окна, без вычислений.
    """

    __slots__ = ("_clock", "_events", "_total", "_window_s")

    def __init__(
        self,
        window_s: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if window_s <= 0:
            raise ValueError(_("window must be positive"))
        self._window_s = float(window_s)
        # Часы вынесены в параметр, чтобы тест не зависел от реального времени.
        self._clock = clock
        self._events: deque[tuple[float, int]] = deque()
        self._total = 0

    def tick(self, n: int = 1) -> None:
        """Учесть n событий. Неположительное n игнорируется."""
        if n <= 0:
            return
        now = self._clock()
        self._events.append((now, n))
        self._total += n
        self._trim(now)

    def per_second(self) -> float:
        """Частота событий в секунду по текущему окну."""
        now = self._clock()
        self._trim(now)
        if not self._events:
            return 0.0
        return sum(count for _, count in self._events) / self._window_s

    def total(self) -> int:
        """Всего событий с момента создания или последнего сброса."""
        return self._total

    def reset(self) -> None:
        """Обнулить окно и общий счетчик."""
        self._events.clear()
        self._total = 0

    def _trim(self, now: float) -> None:
        """Выбросить события, вышедшие за окно."""
        edge = now - self._window_s
        events = self._events
        while events and events[0][0] <= edge:
            events.popleft()


class ProcessUptime:
    """Время работы процесса.

    Отсчет ведется от создания объекта: он создается при старте приложения.
    Читать время старта процесса из procfs ради этого не нужно, разница
    составляет миллисекунды инициализации интерпретатора.
    """

    __slots__ = ("_clock", "_started")

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()

    def seconds(self) -> float:
        """Время работы в секундах."""
        return max(0.0, self._clock() - self._started)

    def reset(self) -> None:
        """Начать отсчет заново, например после переподключения."""
        self._started = self._clock()

    def formatted(self) -> str:
        """Время работы в компактном виде: 42s, 5m03s, 2h05m."""
        return models.humanize_duration(self.seconds())


def format_bytes(value: int | None) -> str:
    """Размер в компактном виде: 61M, 512K, 940B. None отдает "n/a"."""
    return models.humanize_bytes(value)


def format_latency(latency_ms: float | None) -> str:
    """Задержка в миллисекундах: 42ms. None отдает "n/a"."""
    return models.format_latency(latency_ms)
