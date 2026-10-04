"""termisations - терминальный XMPP-клиент с упором на прозрачность протокола.

Пакет не импортирует подмодули на верхнем уровне: слои собираются в точке входа
(``termisations.cli``), а тесты подключают только то, что проверяют.
Это исключает циклические импорты между core, ui и protocol.

Copyright (C) 2026 Evgeny Inkov <me@inkov.dev>, XMPP: tzx1z@inkov.dev

Программа свободная: ее можно распространять и изменять на условиях GNU Affero
General Public License версии 3, опубликованной Free Software Foundation.
Программа распространяется в надежде на пользу, но БЕЗ КАКИХ-ЛИБО ГАРАНТИЙ,
включая подразумеваемые гарантии товарного состояния и пригодности для
определенной цели. Полный текст лицензии лежит в файле LICENSE и доступен на
https://www.gnu.org/licenses/agpl-3.0.html
"""

__all__ = ["__author__", "__license__", "__version__"]

__version__ = "0.1.1"
__author__ = "Evgeny Inkov <me@inkov.dev>"
__license__ = "AGPL-3.0-only"
