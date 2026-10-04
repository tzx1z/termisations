"""Режимы раскладки интерфейса.

Перечень вынесен из ``app.py`` в модуль без зависимостей. Его читает разбор
аргументов в ``cli.py``, и пакетный режим не должен загружать Textual ради
проверки значения ``--layout``.
"""

from typing import Final

__all__ = ["LAYOUT_MODES"]

LAYOUT_MODES: Final[tuple[str, ...]] = ("focus", "split", "debug")
"""Порядок обхода раскладок по Ctrl+D."""
