"""Протокольные сценарии на настоящем сервере.

Здесь то, что нельзя проверить без второй стороны и без сервера: обмен
зашифрованным сообщением, возобновление потока после обрыва, копии с другого
устройства, постраничный архив, комнаты.

Проверки собраны крупными: одна связка вопросов на одно подключение. Дробить
дальше значит платить за поднятие сессии по разу на каждое утверждение, а
поднятие - самая дорогая часть живого теста.

Подготовка сервера: см. README.md, раздел "Live tests". Без взаимных подписок
OMEMO не забирает списки устройств из PEP, и половина набора завершится с
ошибкой.
"""

import asyncio
from pathlib import Path
from typing import Final

import aiohttp
import pytest

from termisations.core.events import (
    ConnectionStageChanged,
    OperationProgress,
    SendText,
    SetChatState,
)
from termisations.core.i18n import _
from termisations.core.models import ConnectionStage, Encryption
from termisations.core.router import RETRACTED_BODY
from termisations.protocol.session import CONNECT_DEADLINE

pytestmark = pytest.mark.live

pytest.importorskip("slixmpp", reason="живые тесты требуют slixmpp")

from live_harness import (  # noqa: E402
    DOMAIN,
    PEER,
    ROOM,
    USER,
    Live,
    live_session,
    needs_two_accounts,
    omemo_store,
    server_is_up,
    wait_for,
)

if not server_is_up():  # pragma: no cover - зависит от окружения
    pytest.skip(
        "XMPP-сервер не поднят, см. README.md, раздел Live tests",
        allow_module_level=True,
    )

PEER_JID: Final = f"{PEER}@{DOMAIN}"
OWN_JID: Final = f"{USER}@{DOMAIN}"


async def wait_omemo(live: Live, limit: float = 25.0) -> bool:
    """Дождаться публикации бандла OMEMO."""
    return await wait_for(lambda: live.session._omemo_ready, limit)


# История и хранилище.


async def test_history_survives_restart(tmp_path: Path) -> None:
    """Отправленное в одном запуске поднимается из базы в следующем.

    Ради этого хранилище и заведено: без него переписка живет в памяти процесса
    и пропадает вместе с ним.
    """
    database = tmp_path / "history.db"
    text = "сообщение до перезапуска"
    async with live_session("hist-1", storage=database) as live:
        await live.run(f"/chat {PEER_JID}")
        await live.session.handle_command(SendText(PEER_JID, text))
        assert await wait_for(lambda: any(item.body == text for item in live.messages()))

    async with live_session("hist-2", storage=database) as live:
        assert await wait_for(lambda: any(item.body == text for item in live.messages()))


# Команды.


async def test_subscription_commands_reach_the_server() -> None:
    """/sub и /unsub работают.

    Раньше они молча не работали вовсе: сетевая сессия сверялась с ключами
    ``roster.sub`` и ``roster.unsub``, а реестр объявляет ``roster.subscribe`` и
    ``roster.unsubscribe``, и обе команды падали в ветку отказа.

    Адрес берется посторонний, а не адрес собеседника: ``/unsub`` снимает
    подписку на сервере по-настоящему, и тест, снявший ее с ``bob``, ломает
    половину набора - без взаимной подписки не доходит присутствие, а без него
    не работает ни кэш возможностей, ни выбор устройств OMEMO.
    """
    outsider = f"carol@{DOMAIN}"
    async with live_session("subs") as live:
        mark = live.mark()
        await live.run(f"/sub {outsider}")
        await live.run(f"/unsub {outsider}")
        answers = " ".join(live.feedback(mark))
    assert "подписка запрошена" in answers
    assert "подписка отменена" in answers
    assert "не поддержана" not in answers


async def test_unsupported_commands_name_what_is_missing() -> None:
    """Команда, которой в сетевом режиме нет, называет причину.

    Общий текст "команда не поддержана" не отличает незаконченную работу от
    опечатки в имени команды.
    """
    async with live_session("unsup") as live:
        mark = live.mark()
        await live.run("/ox status")
        answers = live.feedback(mark)
    # Не последний ответ, а любой: рядом идет автовход по закладкам XEP-0402, и
    # его сообщение приходит когда угодно относительно ответа команды.
    assert any("XEP-0373" in answer for answer in answers), answers


async def test_blocking_commands_work() -> None:
    """/block и /unblock доходят до сервера по XEP-0191."""
    async with live_session("block") as live:
        mark = live.mark()
        await live.run(f"/block carol@{DOMAIN}")
        await live.run(f"/unblock carol@{DOMAIN}")
        answers = " ".join(live.feedback(mark))
    assert "заблокирован" in answers
    assert "разблокирован" in answers


async def test_sasl1_fallback_is_named() -> None:
    """Сервер без SASL2 пускает по SASL1, и клиент говорит, какой путь отработал.

    Адаптер SASL2 на этом сервере проверить не на чем: модулей ``mod_sasl2`` и
    ``mod_bind2`` в сборке нет. Проверяется то, что он не сломал запасной путь.
    """
    async with live_session("sasl1") as live:
        detail = live.session._sasl_detail()
    assert detail.startswith("SASL1 + bind")
    assert "SCRAM" in detail


# Возобновление потока и копии.


async def test_stream_resumes_after_a_network_drop() -> None:
    """После обрыва сети поток возобновляется, а не начинается заново.

    Обрыв делается через ``transport.abort``: штатный ``disconnect`` закрывает
    поток по-хорошему, и сервер тогда завершает сессию, делая возобновление
    невозможным по протоколу.
    """
    async with live_session("resume") as live:
        assert live.session.state.sm.enabled
        client = live.session._client
        assert client is not None
        client.transport.abort()
        assert await wait_for(lambda: live.session.state.sm.resumed, 30.0)
        # Возобновление - это не только ответ сервера: следом идет догон архива
        # и проверка возможностей. На удаленном сервере это занимает заметно
        # больше времени, чем на контейнере рядом.
        assert await live.ready(45.0), (
            f"стадия {live.session.state.stage.value}, уведомления: {live.notices()[-3:]}"
        )
        # Сторож рукопожатия снимается возобновлением: событие session_start при
        # нем не приходит, и взведенный на переподключение сторож через свои
        # двадцать секунд обрывал уже работающее соединение.
        await asyncio.sleep(CONNECT_DEADLINE + 2.0)
        assert live.session.state.stage is ConnectionStage.READY, (
            "сторож оборвал возобновленный поток"
        )
    assert live.session.state.metrics.reconnects >= 1
    assert any("возобновлен" in text for text in live.notices())


async def test_carbon_reaches_the_second_device() -> None:
    """Сообщение собеседника видно на втором своем устройстве.

    Без явного включения XEP-0280 сервер копии не шлет, и переписка с телефона в
    ленте не появляется вовсе. Выглядит это как потерянные сообщения.
    """
    text = "проверка копий"
    async with (
        live_session("desk") as desk,
        live_session("phone") as phone,
        live_session("peer", user=PEER) as peer,
    ):
        await peer.run(f"/chat {OWN_JID}")
        mark = phone.mark()
        await peer.session.handle_command(SendText(OWN_JID, text))
        assert await wait_for(lambda: any(item.body == text for item in phone.messages(mark)))
        assert any(item.body == text for item in desk.messages())


async def test_note_to_self_reaches_each_resource_once() -> None:
    """Заметка самому себе доходит до второго устройства ровно один раз.

    Сообщение на собственный адрес сервер доставляет каждому ресурсу дважды:
    как адресату и копией по XEP-0280, потому что отправитель - тот же аккаунт.
    Клиент, который не связывает копию с оригиналом, показывает два сообщения;
    именно так это и выглядело в Dino. Подсказка no-copy по XEP-0334 оставляет
    одну прямую доставку.
    """
    text = "заметка самому себе"
    async with live_session("self-a") as sender, live_session("self-b") as watcher:
        await sender.run(f"/chat {OWN_JID}")
        mark = watcher.mark()
        await sender.session.handle_command(SendText(OWN_JID, text))
        assert await wait_for(lambda: bool(watcher.stanzas(text, mark)))
        await asyncio.sleep(2.0)
        copies = watcher.stanzas(text, mark)
    assert len(copies) == 1, f"копий пришло {len(copies)}"
    assert "urn:xmpp:carbons:2" not in copies[0], "копия пришла вдобавок к прямой доставке"


# Расширения, меняющие чужое сообщение.


async def test_reaction_reaches_the_other_side() -> None:
    """Реакция доходит до собеседника и садится на его сообщение.

    Строфа реакции приходит без тела, поэтому проверка на пустое тело не должна
    отбрасывать ее до разбора расширений.
    """
    text = "сообщение под реакцию"
    async with live_session("react-a") as sender, live_session("react-b") as watcher:
        await sender.run(f"/chat {PEER_JID}")
        mark = watcher.mark()
        await sender.session.handle_command(SendText(PEER_JID, text))
        assert await wait_for(lambda: any(item.body == text for item in watcher.messages(mark)))
        target = next(item for item in watcher.messages(mark) if item.body == text)

        await watcher.run(f"/chat {OWN_JID}")
        second = sender.mark()
        await watcher.run(f"/react {target.message_id} \N{THUMBS UP SIGN}", settle=3.0)
        assert await wait_for(
            lambda: any(
                item.xep == "0444" for message in sender.updated(second) for item in message.xeps
            )
        )
        changed = [
            message
            for message in sender.updated(second)
            if any(item.xep == "0444" for item in message.xeps)
        ]
    mark_detail = next(item for item in changed[-1].xeps if item.xep == "0444")
    assert mark_detail.detail["emoji"] == "\N{THUMBS UP SIGN}"
    assert changed[-1].body == text, "реакция не должна менять текст сообщения"


async def test_retraction_leaves_the_client_side_consistent() -> None:
    """Отзыв уходит строфой и меняет свое сообщение в ленте.

    Доставка до второй стороны здесь не проверяется: сервер строфу отзыва
    другим ресурсам не пересылает, хотя объявляет ``urn:xmpp:message-retract:1``
    в своих возможностях. Реакция по XEP-0444 при тех же условиях доходит,
    поэтому дело не в отсутствии тела у строфы. Проверяется то, что зависит от
    клиента: строфа собрана и отправлена, запись в ленте изменена.
    """
    text = "сообщение под отзыв"
    async with live_session("retract") as live:
        await live.run(f"/chat {PEER_JID}")
        mark = live.mark()
        await live.session.handle_command(SendText(PEER_JID, text))
        assert await wait_for(lambda: any(item.body == text for item in live.messages(mark)))
        sent = next(item for item in live.messages(mark) if item.body == text)

        second = live.mark()
        await live.run(f"/retract {sent.message_id}", settle=3.0)
        stanzas = live.stanzas("urn:xmpp:message-retract:1", second)
        changed = [
            message for message in live.updated(second) if message.message_id == sent.message_id
        ]
        answers = " ".join(live.feedback(second))
    assert stanzas, "строфа отзыва не ушла в поток"
    assert sent.message_id in stanzas[0], "в строфе нет идентификатора цели"
    assert "отозвано" in answers
    assert changed and changed[-1].body == _(RETRACTED_BODY)


async def test_reply_carries_the_original_id() -> None:
    """Ответ уходит с указанием исходного сообщения и виден меткой."""
    text = "исходная реплика"
    async with live_session("reply-a") as sender, live_session("reply-b") as watcher:
        await sender.run(f"/chat {PEER_JID}")
        mark = watcher.mark()
        await sender.session.handle_command(SendText(PEER_JID, text))
        assert await wait_for(lambda: any(item.body == text for item in watcher.messages(mark)))
        target = next(item for item in watcher.messages(mark) if item.body == text)

        await watcher.run(f"/chat {OWN_JID}")
        second = sender.mark()
        await watcher.run(f"/reply {target.message_id} и вот ответ", settle=3.0)
        assert await wait_for(
            lambda: any(item.body == "и вот ответ" for item in sender.messages(second))
        )
        answer = next(item for item in sender.messages(second) if item.body == "и вот ответ")
    assert any(item.xep == "0461" for item in answer.xeps), "у ответа нет метки XEP-0461"


# Архив и путь строфы.


async def test_archive_is_paged_and_the_cursor_advances(tmp_path: Path) -> None:
    """Повторный запрос архива не тянет его заново: курсор сдвинулся.

    Без курсора на диске постраничный MAM превращается в повторную выкачку
    архива при каждом запуске.
    """
    async with live_session("mam", storage=tmp_path / "mam.db") as live:
        await live.run(f"/chat {PEER_JID}")
        await live.session.handle_command(SendText(PEER_JID, "строка для архива"))
        await asyncio.sleep(1.0)
        first = live.mark()
        await live.run(f"/mam {PEER_JID}", settle=3.0)
        second = live.mark()
        await live.run(f"/mam {PEER_JID}", settle=3.0)
        initial = " ".join(live.feedback(first)[:1])
        repeated = " ".join(live.feedback(second)[:1])
    assert "получено сообщений" in initial
    assert "получено сообщений 0" in repeated


async def test_trace_shows_the_path_of_a_stanza() -> None:
    """Путь строфы собирается из отметок разных расширений.

    Смысл команды в том, чтобы отличить "не ушло" от "ушло, но не прочитано":
    отсутствие отметки само по себе диагноз.
    """
    async with live_session("trace") as live, live_session("trace-peer", user=PEER) as peer:
        await peer.run(f"/chat {OWN_JID}")
        await live.run(f"/chat {PEER_JID}")
        mark = live.mark()
        await live.session.handle_command(SendText(PEER_JID, "сообщение для пути"))
        assert await wait_for(lambda: bool(live.messages(mark)))
        message_id = live.messages(mark)[0].message_id
        # Квитанция и подтверждение приходят разными строфами и не мгновенно:
        # путь собирается по мере их прихода.
        assert await wait_for(lambda: live.messages(mark)[0].message_id == message_id, 2.0)
        await asyncio.sleep(2.0)
        await live.run(f"/trace {message_id}")
        rows = live.rows("путь строфы")
    assert rows.get("origin-id") == message_id
    assert any(name.startswith("XEP-0184") for name in rows)
    assert rows.get("состояние") in ("received", "displayed", "acked")


# Расширения сообщений.


async def test_caps_second_call_comes_from_the_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Повтор /caps при том же хэше не порождает исходящего запроса disco.

    Ради этого кэш и существует: на десятке контактов круг disco на каждого при
    каждом подключении - заметный служебный трафик. Каталог кэша подменяется:
    иначе тест зависел бы от того, что осталось от прошлых запусков.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # Порядок важен: своя сессия поднимается первой, иначе присутствие
    # собеседника уйдет до того, как мы подключимся, и хэша мы не увидим вовсе.
    async with live_session("caps-self") as live, live_session("caps-peer", user=PEER):
        # Хэш возможностей приходит присутствием собеседника, а оно доходит не
        # мгновенно. Пока хэша нет, класть в кэш нечего, и каждый вызов идет в
        # сеть - это верное поведение, а не промах теста.
        target = f"{PEER_JID}/caps-peer"
        known = False
        for _ in range(10):
            await live.run(f"/caps {target}", settle=1.5)
            known = "не объявил" not in live.rows("caps").get("строка проверки", "не объявил")
            if known:
                break
        assert known, "сервер не объявил строку проверки собеседника"
        first = live.rows("caps")
        mark = live.mark()
        await live.run(f"/caps {target}", settle=1.5)
        second = live.rows("caps", mark)
        sources = live.xeps("0115", mark)
    assert first.get("источник") == "запрос disco"
    assert second.get("источник") == "кэш"
    assert sources == ["cache-hit:local"]


async def test_chat_state_and_read_marker_reach_the_peer() -> None:
    """Состояние набора уходит собеседнику, прочтение помечается маркером.

    Оба расширения видны только со стороны собеседника: свое состояние набора в
    своей же ленте показывать нечего.
    """
    needs_two_accounts()
    async with live_session("states") as live, live_session("states-peer", user=PEER) as peer:
        await live.run(f"/chat {PEER_JID}")
        await peer.run(f"/chat {OWN_JID}")
        mark = peer.mark()
        await live.session.handle_command(SetChatState(PEER_JID, "composing"))
        assert await wait_for(lambda: "composing:in" in peer.xeps("0085", mark))

        marker_mark = live.mark()
        await peer.session.handle_command(SendText(OWN_JID, "прочти меня"))
        assert await wait_for(lambda: "displayed:out" in live.xeps("0333", marker_mark))


async def test_action_message_is_marked() -> None:
    """Строка на "/me " помечается действием по XEP-0245, а не идет текстом."""
    async with live_session("action") as live, live_session("action-peer", user=PEER) as peer:
        await live.run(f"/chat {PEER_JID}")
        mark = live.mark()
        await peer.session.handle_command(SendText(OWN_JID, "/me машет рукой"))
        assert await wait_for(lambda: bool(live.messages(mark)))
        marks = [f"{item.xep}/{item.action}" for item in live.messages(mark)[0].xeps]
    assert "0245/me" in marks


# Шифрование.


async def test_omemo_round_trip() -> None:
    """Сообщение шифруется, доходит и расшифровывается.

    Ключевой материал лежит на диске: на временном хранилище каждый запуск создает
    новое устройство, и собеседник видит нового непроверенного участника беседы.
    """
    needs_two_accounts()
    text = "секретное сообщение"
    async with (
        live_session("omemo-a", storage=omemo_store(USER)) as live,
        live_session("omemo-b", user=PEER, storage=omemo_store(PEER)) as peer,
    ):
        assert await wait_omemo(live)
        assert await wait_omemo(peer)
        await live.run(f"/chat {PEER_JID}")
        await peer.run(f"/chat {OWN_JID}")
        await live.run(f"/omemo enable {PEER_JID}", settle=1.5)
        mark = peer.mark()
        await live.session.handle_command(SendText(PEER_JID, text))
        assert await wait_for(lambda: any(item.body == text for item in peer.messages(mark)), 20.0)
        received = next(item for item in peer.messages(mark) if item.body == text)
    assert received.encryption is Encryption.OMEMO


async def test_encrypted_note_to_self_is_not_decrypted_again() -> None:
    """Свое зашифрованное сообщение не пытается расшифроваться на возврате.

    Ключи в строфе лежат для устройств собеседника и для других своих
    устройств, но не для того, которое отправило. Своя строфа, вернувшаяся
    прямой доставкой, расшифроваться не может, и попытка давала в ленте
    "OMEMO: сообщение не расшифровано" на собственную же реплику.
    """
    text = "шифрованная заметка себе"
    async with live_session("self-omemo", storage=omemo_store(USER)) as live:
        assert await wait_omemo(live)
        await live.run(f"/chat {OWN_JID}")
        await live.run(f"/omemo enable {OWN_JID}", settle=2.0)
        mark = live.mark()
        await live.session.handle_command(SendText(OWN_JID, text))
        await asyncio.sleep(4.0)
        mine = [item for item in live.messages(mark) if item.body == text]
        answers = live.feedback(mark)
    blocked = [item for item in answers if "не зашифровано" in item]
    if blocked:
        # Шифрование не собралось из-за состояния учетной записи на сервере:
        # накопленные устройства ждут решения о доверии. Проверять на этом
        # нечего, но и зеленым тест считать нельзя.
        pytest.skip(f"шифрование недоступно: {blocked[-1]}")
    assert len(mine) == 1, f"записей в ленте {len(mine)}"
    assert mine[0].encryption is Encryption.OMEMO
    assert not [item for item in answers if "не расшифровано" in item], answers


async def test_omemo_trust_is_shown_and_changed() -> None:
    """Отпечатки видны, доверие снимается и возвращается тем же отпечатком.

    Отпечаток печатается группами по восемь знаков, и команда обязана принимать
    его ровно в этом виде: копируют его из собственного вывода.
    """
    async with (
        live_session("trust-a", storage=omemo_store(USER)) as live,
        live_session("trust-b", user=PEER, storage=omemo_store(PEER)) as peer,
    ):
        assert await wait_omemo(live)
        assert await wait_omemo(peer)
        await live.run(f"/chat {PEER_JID}")
        await live.run(f"/omemo enable {PEER_JID}", settle=1.5)
        await live.session.handle_command(SendText(PEER_JID, "первое сообщение"))
        await asyncio.sleep(3.0)
        await live.run(f"/omemo fingerprints {PEER_JID}", settle=1.5)
        rows = live.rows("отпечатки")
        devices = [value.split("  ")[0] for key, value in rows.items() if key.startswith("устройс")]
        assert devices, "у собеседника нет ни одного устройства"

        mark = live.mark()
        await live.run(f"/omemo distrust {devices[0]} {PEER_JID}", settle=1.5)
        after_distrust = " ".join(live.feedback(mark))
        mark = live.mark()
        await live.run(f"/omemo trust {devices[0]} {PEER_JID}", settle=1.5)
        after_trust = " ".join(live.feedback(mark))
    assert "недоверенный" in after_distrust
    assert "доверенный" in after_trust


# Комнаты.


async def test_muc_round_trip() -> None:
    """Вход, состав, тема, смена ника, сообщение и выход.

    Одним тестом: все это одна связка, и разнести ее на шесть тестов значит шесть
    раз войти в комнату ради одного утверждения каждый.
    """
    async with live_session("muc-a") as live, live_session("muc-b", user=PEER) as peer:
        await live.run(f"/join {ROOM} --nick alice", settle=2.0)
        await peer.run(f"/join {ROOM} --nick bob", settle=2.0)
        assert await wait_for(lambda: live.session._occupants.get(ROOM, ()) == ("alice", "bob"))

        await live.run("/topic релизы и инциденты", settle=1.0)
        room = live.session._conversations[ROOM]
        assert room.topic == "релизы и инциденты"
        assert room.is_muc

        mark = peer.mark()
        hello = "привет комнате"

        def heard() -> list[str]:
            return [item.sender for item in peer.messages(mark) if item.body == hello]

        await live.session.handle_command(SendText(ROOM, hello))
        assert await wait_for(lambda: bool(heard()))
        assert heard() == ["alice"], "реплика комнаты идет один раз и от ника, а не от адреса"

        await live.run("/nick alice2", settle=2.0)
        assert await wait_for(lambda: "alice2" in peer.session._occupants.get(ROOM, ()))

        mark = live.mark()
        await live.run("/leave", settle=1.0)
    assert any("закрыта" in text for text in live.feedback(mark))


# Загрузка файлов.


async def test_upload_reaches_the_storage(tmp_path: Path) -> None:
    """Слот, PUT и ссылка: файл действительно оказывается в хранилище.

    Раньше проверялась только половина: HTTP-порт тестового контейнера наружу
    не был опубликован, и PUT проверять было не на чем. На сервере со службой
    загрузки проверяется весь путь, включая доступность файла по ссылке.
    """
    payload = tmp_path / "obrazec.bin"
    body = bytes(range(256)) * 800
    payload.write_bytes(body)
    async with live_session("upload") as live:
        await live.run(f"/chat {PEER_JID}")
        mark = live.mark()
        await live.run(f"/upload {payload}", settle=8.0)
        requested = live.xeps("0363", mark)
        answers = " ".join(live.feedback(mark))
        progress = [event for event in live.events[mark:] if isinstance(event, OperationProgress)]
    assert "slot-requested:local" in requested
    assert progress, "доля выполненного не публиковалась"
    if "uploaded:local" not in requested:
        # Слот выдан, а PUT не прошел: на сервере тестового контейнера HTTP-порт
        # службы загрузки наружу не опубликован. Своя часть работы проверена,
        # чужой недоступный порт - не повод считать клиент сломанным.
        pytest.skip(f"HTTP-часть загрузки недоступна: {answers}")
    assert progress[-1].finished, "итог загрузки не объявлен"
    url = answers.split("файл загружен: ", 1)[1].split()[0]
    async with aiohttp.ClientSession() as http, http.get(url) as response:
        assert response.status == 200
        assert await response.read() == body


async def test_upload_progress_is_not_a_connection_stage(tmp_path: Path) -> None:
    """Загрузка не притворяется стадией получения архива.

    Раньше доля выполненного ехала на стадии ``FETCHING_MAM``, и при отправке
    файла в статус-баре стояло "получение архива".
    """
    payload = tmp_path / "malyj.bin"
    payload.write_bytes(b"\x01" * 5000)
    async with live_session("upload-stage") as live:
        await live.run(f"/chat {PEER_JID}")
        mark = live.mark()
        await live.run(f"/upload {payload}", settle=8.0)
        stages = [
            event
            for event in live.events[mark:]
            if isinstance(event, ConnectionStageChanged)
            and event.stage is ConnectionStage.FETCHING_MAM
        ]
    assert not stages, "загрузка публиковалась стадией архива"


async def test_session_stays_ready_after_all_of_it() -> None:
    """После всех сценариев сессия по-прежнему поднимается и доходит до готовности."""
    async with live_session("sanity") as live:
        assert live.session.state.stage is ConnectionStage.READY
