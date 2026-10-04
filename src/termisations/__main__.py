"""Запуск пакета как модуля: ``python -m termisations``.

Обертка тонкая: вся работа с аргументами и сборка слоев живет в cli.main,
чтобы точка входа скрипта и запуск модулем вели себя одинаково.
"""

import sys

from termisations.cli import main

if __name__ == "__main__":
    sys.exit(main())
