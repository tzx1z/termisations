"""Перенос данных прежней раскладки в каталог профиля и пример конфига.

Проверяется главное свойство переноса: он не трогает чужое. Файлы в корне
каталогов остались с тех пор, когда конфиг и база были одни на пользователя
системы, и уехать они должны ровно к той учетной записи, которой принадлежат.

База здесь создается настоящая, через ``Storage``: владелец определяется по
таблице учетных записей, и подделывать схему в тесте значило бы проверять
собственную выдумку.

Пример конфига проверяется на то же свойство: он не занимает место настоящего
конфига и ничего не меняет, пока его не правили.
"""

import os
import re
import stat
import tomllib
from pathlib import Path
from typing import Final, NoReturn

import pytest

from termisations import cli
from termisations.core import i18n, paths, profile
from termisations.core.models import Direction, Message
from termisations.core.storage import Storage, StoredMessage
from termisations.protocol.account import Account

ALICE: Final = "alice@example.org"
TOM: Final = "tom@simple.org"

CONFIG: Final = '[account]\njid = "alice@example.org"\n\n[ui]\nlayout = "debug"\n'


@pytest.fixture(autouse=True)
def xdg_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Каталоги XDG в отдельном временном корне."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def legacy_config(text: str = CONFIG) -> Path:
    """Положить конфиг в корень каталога настроек, как было до профилей."""
    target = paths.config_root() / profile.CONFIG_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


async def legacy_database(*accounts: str) -> Path:
    """Создать базу в корне каталога данных с перечисленными учетными записями."""
    target = paths.data_root() / "history.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    store = await Storage.open(target)
    try:
        for jid in accounts:
            await store.account_id(jid)
    finally:
        await store.close()
    return target


async def test_config_and_database_move_into_the_profile() -> None:
    """Первый запуск профиля забирает конфиг и базу из корня каталогов."""
    config = legacy_config()
    database = await legacy_database(ALICE)
    paths.use_profile(ALICE)

    assert profile.migrate_legacy() == ("config.toml", "history.db")
    assert not config.exists()
    assert not database.exists()
    assert (paths.config_home() / "config.toml").read_text(encoding="utf-8") == CONFIG
    assert (paths.data_dir() / "history.db").is_file()


async def test_moved_files_keep_owner_only_rights() -> None:
    """Перенесенные файлы получают права 0600, а каталоги профиля - 0700."""
    legacy_config()
    (paths.config_root() / profile.CONFIG_NAME).chmod(0o644)
    await legacy_database(ALICE)
    paths.use_profile(ALICE)

    profile.migrate_legacy()
    for moved in (paths.config_home() / "config.toml", paths.data_dir() / "history.db"):
        assert stat.S_IMODE(moved.stat().st_mode) == paths.FILE_MODE
        assert stat.S_IMODE(moved.parent.stat().st_mode) == paths.DIR_MODE


async def test_data_of_another_account_stays_in_place() -> None:
    """Данные чужой учетной записи остаются ждать своего запуска."""
    config = legacy_config()
    database = await legacy_database(ALICE)
    paths.use_profile(TOM)

    assert profile.migrate_legacy() == ()
    assert config.is_file()
    assert database.is_file()
    assert not (paths.config_home() / "config.toml").exists()


async def test_owner_is_taken_from_the_database_without_config() -> None:
    """Без jid в конфиге владельца называет единственная запись в базе."""
    legacy_config('[ui]\nlayout = "focus"\n')
    await legacy_database(ALICE)
    paths.use_profile(TOM)

    assert profile.migrate_legacy() == ()

    paths.use_profile(ALICE)
    assert profile.migrate_legacy() == ("config.toml", "history.db")


async def test_database_of_several_accounts_goes_to_the_first_profile() -> None:
    """Однозначного владельца нет: база достается тому профилю, что запустился."""
    await legacy_database(ALICE, TOM)
    paths.use_profile(TOM)

    assert profile.migrate_legacy() == ("history.db",)
    assert (paths.data_dir() / "history.db").is_file()


async def test_write_ahead_log_moves_with_the_database() -> None:
    """Спутники WAL едут вместе с базой: в них последние транзакции."""
    database = await legacy_database(ALICE)
    journal = database.with_name("history.db-wal")
    journal.write_bytes(b"\x00")
    paths.use_profile(ALICE)

    profile.migrate_legacy()
    assert not journal.exists()
    assert (paths.data_dir() / "history.db-wal").is_file()


async def test_existing_profile_files_are_not_replaced() -> None:
    """Перенос идет один раз: файл, уже лежащий в профиле, не затирается."""
    legacy_config()
    paths.use_profile(ALICE)
    paths.ensure_dir(paths.config_home())
    own = paths.config_home() / "config.toml"
    own.write_text('[ui]\nlayout = "focus"\n', encoding="utf-8")

    assert profile.migrate_legacy() == ()
    assert own.read_text(encoding="utf-8") == '[ui]\nlayout = "focus"\n'
    assert (paths.config_root() / profile.CONFIG_NAME).is_file()


async def test_clean_machine_has_nothing_to_move() -> None:
    """Без прежних файлов перенос ничего не делает и каталогов не создает."""
    paths.use_profile(ALICE)
    assert profile.migrate_legacy() == ()
    assert not paths.config_home().exists()


async def test_unreadable_legacy_database_does_not_stop_the_launch() -> None:
    """Испорченная прежняя база не мешает: владельца просто не удалось узнать."""
    legacy_config('[ui]\nlayout = "focus"\n')
    database = paths.data_root() / "history.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"not a database")
    paths.use_profile(ALICE)

    assert profile.migrate_legacy() == ("config.toml", "history.db")


async def test_migration_needs_a_profile() -> None:
    """Без выбранного профиля переносить некуда, и это ошибка программы."""
    with pytest.raises(RuntimeError, match="профиль"):
        profile.migrate_legacy()


async def test_moved_database_keeps_the_history() -> None:
    """Перенос сохраняет переписку: база открывается из профиля и отдает сообщения."""
    message = Message(
        message_id="m-1",
        conversation=TOM,
        sender=TOM,
        body="сообщение до переноса",
        ts=1_700_000_000.0,
        direction=Direction.IN,
    )
    database = paths.data_root() / "history.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    store = await Storage.open(database)
    try:
        await store.save_message(await store.account_id(ALICE), StoredMessage(message))
    finally:
        await store.close()

    paths.use_profile(ALICE)
    profile.migrate_legacy()

    store = await Storage.open()
    try:
        loaded = await store.load_messages(await store.account_id(ALICE), TOM, 10)
    finally:
        await store.close()
    assert [item.body for item in loaded] == ["сообщение до переноса"]


# Запуск с --message без --to отказывает разбором аргументов уже после выбора
# профиля, переноса и чтения конфига: этого достаточно, чтобы проверить
# подготовку профиля без сети и без интерфейса.
STOP_AFTER_PROFILE: Final = ["--jid", ALICE, "--message", "текст"]

# Ресурс для проверок примера: генерация ресурса проверяется отдельно.
RESOURCE: Final = "termisations.0123abcd"

# Вид ресурса по умолчанию: постоянная часть и восемь шестнадцатеричных знаков.
RESOURCE_PATTERN: Final = re.compile(r"termisations\.[0-9a-f]{8}")


def uncommented(text: str) -> str:
    """Пример конфига со всеми включенными настройками."""
    return re.sub(r"^# (\w+ = .+)$", r"\1", text, flags=re.MULTILINE)


def test_example_config_is_created_in_the_profile() -> None:
    """Пример ложится в каталог настроек профиля с правами только владельца."""
    paths.use_profile(ALICE)
    created = profile.write_example_config(RESOURCE)
    assert created is not None
    assert created == paths.config_file()
    assert created.is_file()
    assert stat.S_IMODE(created.stat().st_mode) == paths.FILE_MODE
    text = created.read_text(encoding="utf-8")
    assert profile._tilde(paths.data_dir()) in text
    assert profile._tilde(paths.cache_dir()) in text
    assert profile._tilde(paths.data_dir() / profile.RESOURCE_NAME) in text
    assert f'# password_command = "secret-tool lookup jid {ALICE} type xmpp"' in text
    assert f"pass show xmpp/{ALICE}" in text


def test_example_shows_home_paths_with_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Пути под домашним каталогом записаны через ~, как их пишут руками."""
    monkeypatch.setenv("HOME", str(tmp_path))
    paths.use_profile(ALICE)
    created = profile.write_example_config(RESOURCE)
    assert created is not None
    text = created.read_text(encoding="utf-8")
    assert f"#   ~/data/termisations/{ALICE}\n" in text
    assert str(tmp_path) not in text


def test_example_config_changes_nothing() -> None:
    """Пока пример не правили, в нем только пустые секции."""
    paths.use_profile(ALICE)
    created = profile.write_example_config(RESOURCE)
    assert created is not None
    with created.open("rb") as handle:
        data = tomllib.load(handle)
    assert data == {"account": {}, "tls": {}, "ui": {}, "log": {}}


def test_example_values_are_the_client_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Раскомментированный пример проходит проверку конфига и повторяет умолчания.

    Пример - это документация, и расхождение с кодом в нем незаметно: человек
    раскомментирует строку в уверенности, что ничего не меняет. Поэтому все
    настройки включаются разом и проходят тот же разбор, что и настоящий конфиг.

    У языка умолчания нет: его выбирает локаль системы. Пример показывает язык,
    на котором написан, и включенная строка сохраняет этот выбор. Переменная
    языка убирается, иначе она перекрыла бы ключ конфига и тот не проверялся бы.
    """
    monkeypatch.delenv(i18n.ENV_LANG)
    paths.use_profile(ALICE)
    resource = profile.profile_resource()
    created = profile.write_example_config(resource)
    assert created is not None
    created.write_text(uncommented(created.read_text(encoding="utf-8")), encoding="utf-8")
    with created.open("rb") as handle:
        data = tomllib.load(handle)
    assert {section: set(values) for section, values in data.items()} == {
        "account": {"resource", "server", "port", "password_command", "sasl2"},
        "tls": {"direct", "verify"},
        "ui": {"layout", "xml_buffer", "lang"},
        "log": {"level"},
    }

    parser = cli.build_parser()
    args = parser.parse_args(["--jid", ALICE])
    cli._apply_config(parser, args)
    defaults = Account(jid=ALICE)
    assert args.resource == resource
    assert args.sasl2 is defaults.sasl2
    assert args.tls_verify is defaults.tls_verify
    assert args.layout == cli._DEFAULT_LAYOUT
    assert args.xml_buffer == cli._DEFAULT_XML_BUFFER
    assert args.log_level == cli._DEFAULT_LOG_LEVEL
    assert args.lang == data["ui"]["lang"] == "ru"


def test_example_is_written_in_the_current_language() -> None:
    """Пример пишется на языке запуска: пояснения и значение lang на английском."""
    i18n.set_language("en")
    paths.use_profile(ALICE)
    created = profile.write_example_config(RESOURCE)
    assert created is not None
    text = created.read_text(encoding="utf-8")
    assert text.startswith("# termisations profile config.\n#\n# The file was created")
    assert "# Emulator settings (--mock, section [mock]) are read from another file:\n" in text
    assert "The /lang command changes the language in a running client\n" in text
    assert '# lang = "en"\n' in text
    assert f'# password_command = "secret-tool lookup jid {ALICE} type xmpp"' in text
    assert not re.search("[А-Яа-яЁё]", text)
    with created.open("rb") as handle:
        assert tomllib.load(handle) == {"account": {}, "tls": {}, "ui": {}, "log": {}}


def test_existing_config_is_not_replaced_by_the_example() -> None:
    """Конфиг пользователя остается как есть."""
    paths.use_profile(ALICE)
    target = paths.ensure_dir(paths.config_home()) / profile.CONFIG_NAME
    target.write_text(CONFIG, encoding="utf-8")
    assert profile.write_example_config(RESOURCE) is None
    assert target.read_text(encoding="utf-8") == CONFIG


def test_interrupted_write_leaves_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Оборванная запись не оставляет недописанный конфиг.

    Оборванная строка TOML остановила бы следующий запуск ошибкой разбора, а
    новый пример поверх существующего файла уже не создается.
    """
    paths.use_profile(ALICE)

    def disk_full(descriptor: int, *args: object, **kwargs: object) -> NoReturn:
        os.close(descriptor)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(profile.os, "fdopen", disk_full)
    with pytest.raises(OSError, match="No space"):
        profile.write_example_config(RESOURCE)
    assert not paths.config_file().exists()


def test_example_needs_a_profile() -> None:
    """Без выбранного профиля класть пример некуда, и это ошибка программы."""
    with pytest.raises(RuntimeError, match="профиль"):
        profile.write_example_config(RESOURCE)


def test_profile_launch_creates_the_example(capsys: pytest.CaptureFixture[str]) -> None:
    """Запуск профиля без конфига создает пример и называет его путь."""
    with pytest.raises(SystemExit):
        cli.main(STOP_AFTER_PROFILE)
    target = paths.config_root() / ALICE / profile.CONFIG_NAME
    assert target.is_file()
    assert f"создан пример конфига: {target}" in capsys.readouterr().err
    resource = (paths.data_root() / ALICE / profile.RESOURCE_NAME).read_text(encoding="utf-8")
    assert f'# resource = "{resource.strip()}"' in target.read_text(encoding="utf-8")


def test_example_does_not_take_the_place_of_legacy_config() -> None:
    """Конфиг прежней раскладки переносится в профиль, а пример не создается.

    Порядок важен: пример, созданный до переноса, занял бы место конфига, и
    перенос пропустил бы его как уже существующий.
    """
    legacy_config()
    with pytest.raises(SystemExit):
        cli.main(STOP_AFTER_PROFILE)
    target = paths.config_root() / ALICE / profile.CONFIG_NAME
    assert target.read_text(encoding="utf-8") == CONFIG


def test_explicit_config_skips_the_example(tmp_path: Path) -> None:
    """С явным --config настройки лежат в другом месте, и пример не нужен."""
    shared = tmp_path / "shared.toml"
    shared.write_text('[ui]\nlayout = "debug"\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main([*STOP_AFTER_PROFILE, "--config", str(shared)])
    assert not (paths.config_root() / ALICE / profile.CONFIG_NAME).exists()


def test_failed_example_does_not_stop_the_launch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Отказ записи примера печатается, а запуск идет дальше.

    Признак того, что запуск продолжился, - отказ из-за отсутствия --to: эта
    проверка идет уже после подготовки профиля.
    """

    def read_only(resource: str) -> NoReturn:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(profile, "write_example_config", read_only)
    with pytest.raises(SystemExit):
        cli.main(STOP_AFTER_PROFILE)
    err = capsys.readouterr().err
    assert f"пример конфига для профиля {ALICE} не создан" in err
    assert "--to" in err


def test_profile_resource_is_created_once() -> None:
    """Ресурс создается при первом обращении, хранится в профиле и не меняется."""
    paths.use_profile(ALICE)
    first = profile.profile_resource()
    assert RESOURCE_PATTERN.fullmatch(first)
    stored = paths.data_dir() / profile.RESOURCE_NAME
    assert stored.read_text(encoding="utf-8") == first + "\n"
    assert stat.S_IMODE(stored.stat().st_mode) == paths.FILE_MODE
    assert profile.profile_resource() == first


def test_each_profile_has_its_own_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    """У каждого профиля свой хвост, и возврат к профилю возвращает его ресурс."""
    tails = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(profile.secrets, "token_hex", lambda _size: next(tails))
    paths.use_profile(ALICE)
    alice = profile.profile_resource()
    paths.use_profile(TOM)
    tom = profile.profile_resource()
    paths.use_profile(ALICE)
    assert (alice, tom, profile.profile_resource()) == (
        "termisations.aaaaaaaa",
        "termisations.bbbbbbbb",
        "termisations.aaaaaaaa",
    )


@pytest.mark.parametrize(
    "content",
    [b"", b"\n", b"bad\x01value\n", b"\xff\xfe", b"a" * 1024],
    ids=["empty", "newline", "control", "not-utf8", "too-long"],
)
def test_damaged_resource_file_is_replaced(content: bytes) -> None:
    """Пустой или поврежденный файл заменяется новым ресурсом.

    После сбоя питания файл может остаться пустым, и без замены профиль остался
    бы без ресурса при каждом следующем запуске.
    """
    paths.use_profile(ALICE)
    stored = paths.data_file(profile.RESOURCE_NAME)
    stored.write_bytes(content)
    resource = profile.profile_resource()
    assert RESOURCE_PATTERN.fullmatch(resource)
    assert stored.read_text(encoding="utf-8") == resource + "\n"


def test_resource_of_a_parallel_launch_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если файл успел создать параллельный запуск, берется его значение."""
    paths.use_profile(ALICE)
    create = profile._create_exclusive

    def raced(path: Path, text: str) -> bool:
        path.write_text("termisations.feedbeef\n", encoding="utf-8")
        return create(path, text)

    monkeypatch.setattr(profile, "_create_exclusive", raced)
    assert profile.profile_resource() == "termisations.feedbeef"


def test_resource_needs_a_profile() -> None:
    """Без выбранного профиля хранить ресурс негде, и это ошибка программы."""
    with pytest.raises(RuntimeError, match="профиль"):
        profile.profile_resource()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [([], RESOURCE), (["--resource", "phone"], "phone")],
    ids=["profile", "flag"],
)
def test_flag_overrides_the_profile_resource(argv: list[str], expected: str) -> None:
    """Ресурс профиля действует, пока ресурс не задан флагом или конфигом."""
    parser = cli.build_parser()
    args = parser.parse_args(["--jid", ALICE, *argv])
    account = cli._account_from(args, RESOURCE)
    assert account is not None
    assert account.resource == expected


def test_unsaved_resource_does_not_stop_the_launch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Отказ записи ресурса печатается, а запуск идет с новым ресурсом."""

    def read_only() -> NoReturn:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(profile, "profile_resource", read_only)
    resource = cli._default_resource(ALICE)
    assert RESOURCE_PATTERN.fullmatch(resource)
    err = capsys.readouterr().err
    assert f"ресурс профиля {ALICE} не сохранен, на этот запуск {resource}" in err
