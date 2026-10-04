"""Запуск без необязательных зависимостей.

Два независимых требования. Пакетная отправка из конвейера и cron не поднимает
интерфейс и не должна загружать Textual: импорт добавляет к каждому запуску
около 150 мс и 15 МБ памяти. Эмулятор не обращается к сети и обязан работать
без группы xmpp, а подключение к серверу без нее должно называть команду
установки вместо трассировки ImportError.

Проверки идут в отдельном интерпретаторе. В процессе pytest нужные модули уже
загружены через conftest и соседние тесты, и запрет импорта там ничего бы не
показал.
"""

import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

SRC: Final = Path(__file__).resolve().parents[1] / "src"

# None в sys.modules запрещает импорт: любой import textual.* или rich.*
# завершается ImportError. rich запрещен вместе с Textual: это его зависимость,
# и там, где нет Textual, обычно нет и rich.
NO_TUI: Final = 'sys.modules["textual"] = None\nsys.modules["rich"] = None\n'

# Группа xmpp целиком: aiohttp входит в нее явно, см. pyproject.toml.
NO_XMPP: Final = 'sys.modules["slixmpp"] = None\nsys.modules["aiohttp"] = None\n'

# Пароль не проверяется: до SASL дело не доходит.
PASSWORD: Final = "пароль не проверяется"


def run_python(prelude: str, body: str, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Выполнить сценарий в чистом интерпретаторе.

    Окружение берется у pytest: автоматические фикстуры уже увели каталоги XDG
    во временный каталог и убрали переменную профиля.
    """
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "PYTHONUTF8": "1",
        "TERMISATIONS_PASSWORD": PASSWORD,
    }
    return subprocess.run(
        [sys.executable, "-c", f"import sys\n{prelude}{body}", *argv],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def closed_port() -> int:
    """Порт, который точно никто не слушает: занять и сразу освободить."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_batch_mode_runs_without_textual() -> None:
    """Пакетный путь доходит до подключения и завершается без импорта Textual.

    Сервер не нужен: закрытый порт дает штатную ошибку подключения. Код 1 сам
    по себе ничего не доказывает, с тем же кодом процесс завершается и на
    ImportError. Поэтому проверяется текст ошибки подключения: он печатается
    только из ``_send_once``, то есть после создания сессии и попытки соединения.
    Таймаут пакетного режима сокращен: сервер недоступен, и ждать штатные 30 с
    незачем.
    """
    pytest.importorskip("slixmpp", reason="пакетный режим требует slixmpp")
    body = (
        "from termisations import cli\ncli._BATCH_TIMEOUT = 1.0\nsys.exit(cli.main(sys.argv[1:]))\n"
    )
    argv = [
        "--jid",
        "alice@example.org",
        "--server",
        "127.0.0.1",
        "--port",
        str(closed_port()),
        "--to",
        "bob@example.org",
        "--message",
        "текст",
    ]
    result = run_python(NO_TUI, body, argv)
    assert result.returncode == 1, result.stderr
    assert "не удалось подключиться" in result.stderr, result.stderr


def test_mock_runs_without_xmpp() -> None:
    """Эмулятор собирается и запускается без группы xmpp.

    Цикл Textual подменен пустым: проверяется путь ``main`` до запуска
    интерфейса, то есть разбор аргументов, эмулятор и сборка приложения.
    """
    body = (
        "from termisations import app, cli\n"
        "app.TermisationsApp.run = lambda self: None\n"
        "sys.exit(cli.main(sys.argv[1:]))\n"
    )
    result = run_python(NO_XMPP, body, ["--mock"])
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ["--jid", "alice@example.org"],
        ["--jid", "alice@example.org", "--to", "bob@example.org", "--message", "текст"],
    ],
    ids=["interface", "batch"],
)
def test_account_without_xmpp_names_the_group(argv: list[str]) -> None:
    """Подключение без группы xmpp - ошибка разбора с командой установки."""
    body = "from termisations import cli\nsys.exit(cli.main(sys.argv[1:]))\n"
    result = run_python(NO_XMPP, body, argv)
    assert result.returncode == 2, result.stderr
    assert "pip install 'termisations[xmpp]'" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
