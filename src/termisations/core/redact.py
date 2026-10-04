"""Маскирование секретов в сыром XMPP-потоке.

Модуль безопасности. Правило по умолчанию одно: в лог не попадает ничего, чем можно
воспользоваться повторно - учетные данные SASL, материал ключей OMEMO, подписанные
ссылки и пароли комнат.

Реализация - однопроходный сканер разметки, а не набор независимых регулярных
выражений по всей строке. Причина в том, что правило вида "элемент, его тело,
закрывающий тег" в одном шаблоне обходится тривиально: другим регистром имени,
префиксом пространства имен, секцией CDATA, комментарием внутри тела, незакрытым
вложенным элементом. Сканер идет по разметке слева направо, поэтому все эти формы
для него одинаковы.

Свойства реализации:

* стоимость линейна по длине строфы. Поиск закрывающего тега идет вперед и никогда
  не возвращается назад, шаблонов с вложенными квантификаторами нет, поэтому
  катастрофического отката не возникает даже на строфе, которую собрала удаленная
  сторона;
* имена элементов и атрибутов сравниваются без учета регистра и без префикса
  пространства имен;
* правила не привязаны к пространству имен. Проверка "namespace встретился в той же
  строке" - это не проверка, ее снимает любая строфа без объявления, поэтому
  маскирование включается по имени элемента. Цена решения: элемент с именем
  ``key`` или ``response`` из постороннего XEP тоже будет усечен. Для отладочной
  панели это дешевле утечки;
* комментарии и секции CDATA не снимают маскирование: содержимое элемента берется
  целиком, вместе с ними;
* закрывающий тег ищется с учетом вложенности. Если его нет, содержимым считается
  весь остаток строки: отказ идет в безопасную сторону;
* поврежденный тег не проглатывает остаток строфы. Символ "<" внутри значения
  атрибута стандарт запрещает, поэтому он считается началом следующего элемента,
  и разбор продолжается с него.

Функция redact не бросает исключений ни при каких входных данных, включая обрезанный
и поврежденный XML. При внутреннем сбое возвращается заглушка, а не исходный текст.
"""

import re
from typing import Final

from termisations.core.i18n import N_, _

__all__ = [
    "OMEMO_NAMESPACES",
    "SASL_NAMESPACES",
    "UNSAFE_WARNING",
    "redact",
    "redaction_summary",
]

SASL_NAMESPACES: Final[tuple[str, ...]] = (
    "urn:ietf:params:xml:ns:xmpp-sasl",
    "urn:xmpp:sasl:2",
)
"""Пространства имен SASL первой и второй версии."""

OMEMO_NAMESPACES: Final[tuple[str, ...]] = (
    "eu.siacs.conversations.axolotl",
    "urn:xmpp:omemo:2",
)
"""Пространства имен OMEMO: вариант siacs и стандартизованный вариант."""

UNSAFE_WARNING: Final = N_(
    "Unsafe mode disables masking of the raw stream. SASL credentials, OMEMO key "
    "material and signed links will get into the log. Enable it only on a test "
    "account."
)
"""Текст предупреждения, который показывается при включении /xml --unsafe.

Только помечен для каталога: при импорте язык еще не выбран, и переводит его
через ``_()`` тот, кто выводит предупреждение."""

# Длина, до которой усекаются тела ключей OMEMO.
_OMEMO_KEEP: Final = 16
_ELLIPSIS: Final = "…"
_REDACTED: Final = "[redacted]"
_SIGNED: Final = " [signed]"
_REDACTION_FAILED: Final = "[redaction failed, payload hidden]"

# Перечни элементов и атрибутов.

# Тела элементов SASL, включая элементы SASL2: в SASL2 нагрузку механизма несут
# initial-response и additional-data, без них правило имело бы дыру в самом
# частом случае - механизм PLAIN, где в initial-response лежит открытый пароль.
_SASL_ELEMENTS: Final[frozenset[str]] = frozenset(
    {"auth", "response", "challenge", "initial-response", "additional-data"}
)

# Элемент success в SASL1 несет подпись сервера текстом, а в SASL2 это контейнер
# с additional-data и идентификатором учетной записи. Текст маскируется, разбор
# контейнера отдается общему проходу: имя учетной записи секретом не является и
# нужно при отладке.
_SASL_CONTAINER: Final = "success"

# Тела ключей и полезной нагрузки OMEMO.
_OMEMO_ELEMENTS: Final[frozenset[str]] = frozenset({"key", "payload"})

# Бандл ключей в PEP: вместо тел показывается количество ключей.
_BUNDLE_ELEMENT: Final = "bundle"

# Пароль комнаты при входе в MUC и пароль при регистрации по XEP-0077.
_PASSWORD_ELEMENTS: Final[frozenset[str]] = frozenset({"password", "passwd"})

# Ссылки: тело url в OOB, uri в источниках XEP-0447.
_URL_ELEMENTS: Final[frozenset[str]] = frozenset({"url", "uri"})

# Заголовки запроса в слоте XEP-0363 и в OOB.
_HEADER_ELEMENT: Final = "header"

_RULE_ELEMENTS: Final[frozenset[str]] = frozenset(
    _SASL_ELEMENTS
    | _OMEMO_ELEMENTS
    | _PASSWORD_ELEMENTS
    | _URL_ELEMENTS
    | {_SASL_CONTAINER, _BUNDLE_ELEMENT, _HEADER_ELEMENT}
)

# Атрибуты, значение которых заменяется целиком.
_SECRET_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "access-token",
        "credential",
        "credentials",
        "signature",
        "sig",
        "authorization",
    }
)

# Атрибуты со ссылками: put и get слота XEP-0363, target в url-data,
# uri в источниках XEP-0447, обычные url, src и href.
_URL_ATTRS: Final[frozenset[str]] = frozenset(
    {"url", "uri", "put", "get", "target", "src", "href", "location"}
)

# Заголовки, которые заведомо не несут учетных данных. Список работает как
# разрешающий: неизвестное имя заголовка маскируется. Перечисление запрещенных
# имен здесь не годится - его обходит любой нестандартный заголовок.
_SAFE_HEADER_NAMES: Final[frozenset[str]] = frozenset(
    {
        "expires",
        "content-type",
        "content-length",
        "content-disposition",
        "urgency",
        "priority",
        "date",
        "keywords",
        "store",
        "distribute",
        "in-reply-to",
    }
)

# Шаблоны.

# Имя элемента сразу после "<". Префикс пространства имен допускается.
_TAG_NAME_RE: Final = re.compile(r"[A-Za-z_][\w.-]*(?::[A-Za-z_][\w.-]*)?")

# Атрибут тега. Значение берется в кавычках любого вида или без них.
_ATTR_RE: Final = re.compile(
    r"""(?P<name>[^\s=<>"'/]+)\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s"'<>]*))"""
)

# Имя заголовка. Отрицательный просмотр назад отсекает filename и подобные атрибуты.
_HEADER_NAME_RE: Final = re.compile(
    r"""(?<![\w.:-])name\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s"'<>]+))""",
    re.IGNORECASE,
)

# Элементы, которые считаются ключами при подсчете содержимого бандла.
_BUNDLE_KEY_RE: Final = re.compile(
    r"<(?:[\w.-]+:)?(?:preKeyPublic|signedPreKeyPublic|key|spk|ik|pk)(?![\w.-])",
    re.IGNORECASE,
)

# Граница тега: кавычка открывает значение атрибута, ">" закрывает тег.
_QUOTE_OR_GT_RE: Final = re.compile(r"[\"'>]")

# Ссылка в текстовом узле.
_TEXT_URL_RE: Final = re.compile(r"[A-Za-z][\w+.-]*://[^\s<>\"']+")

# Признак подписанной ссылки: имя параметра запроса намекает на подпись, токен
# или срок действия. Обычная ссылка с параметрами страницы при этом остается
# читаемой целиком - в теле сообщения это пользовательский текст, а не протокол.
_SIGNED_PARAM_RE: Final = re.compile(
    r"[?&;][^=&;]*(?:sig|token|auth|key|secret|cred|hmac|policy|expire|nonce|password|access)",
    re.IGNORECASE,
)

# Скомпилированные шаблоны поиска закрывающего тега. Ключи - имена из _RULE_ELEMENTS,
# то есть перечень закрыт и кэш не растет от входных данных.
_CLOSE_PATTERNS: Final[dict[str, re.Pattern[str]]] = {}


def _local_name(name: str) -> str:
    """Локальное имя элемента или атрибута: без префикса, в нижнем регистре."""
    return name.rpartition(":")[2].lower()


def _close_pattern(local: str) -> re.Pattern[str]:
    """Шаблон поиска границ элемента: комментарий, CDATA, открывающий и закрывающий тег."""
    pattern = _CLOSE_PATTERNS.get(local)
    if pattern is None:
        pattern = re.compile(
            r"<!--|<!\[CDATA\[|<(?P<close>/?)(?:[\w.-]+:)?" + re.escape(local) + r"(?![\w.-])",
            re.IGNORECASE,
        )
        _CLOSE_PATTERNS[local] = pattern
    return pattern


def _tag_end(xml: str, start: int) -> tuple[int, int]:
    """Границы тега: позиция ">" и позиция обрыва.

    Значения атрибутов пропускаются целиком, поэтому ">" внутри кавычек тег не
    обрывает. Поиск идет регулярным выражением по кавычкам и ">": число шагов
    равно числу атрибутов, посимвольного обхода нет.

    Возвращается пара. ``(индекс, -1)`` - нормальный тег. ``(-1, индекс)`` - тег
    поврежден: внутри значения атрибута встретился "<", который стандарт там
    запрещает, значит это начало другого элемента, и разбор должен продолжиться
    с него, а не проглотить остаток строфы вместе с секретами. ``(-1, -1)`` -
    тега нет вовсе, дальше разбирать нечего.
    """
    pos = start
    while True:
        match = _QUOTE_OR_GT_RE.search(xml, pos)
        if match is None:
            return -1, -1
        char = match.group()
        if char == ">":
            return match.start(), -1
        closing = xml.find(char, match.end())
        if closing < 0:
            # Незакрытая кавычка: тег поврежден, обрываемся на ней.
            return -1, match.start()
        broken = xml.find("<", match.end(), closing)
        if broken >= 0:
            return -1, broken
        pos = closing + 1


def _content_end(xml: str, start: int, local: str) -> tuple[int, int]:
    """Границы содержимого элемента: конец содержимого и позиция после закрывающего тега.

    Вложенные одноименные элементы учитываются, комментарии и секции CDATA
    пропускаются целиком: закрывающий тег внутри них не настоящий. Если закрывающего
    тега нет, содержимым считается весь остаток строки.
    """
    pattern = _close_pattern(local)
    length = len(xml)
    depth = 1
    pos = start
    while pos < length:
        match = pattern.search(xml, pos)
        if match is None:
            return length, length
        token = match.group()
        if token == "<!--":
            skip = xml.find("-->", match.end())
            pos = length if skip < 0 else skip + 3
            continue
        if token == "<![CDATA[":
            skip = xml.find("]]>", match.end())
            pos = length if skip < 0 else skip + 3
            continue
        gt, broken = _tag_end(xml, match.end())
        if gt < 0:
            if broken < 0:
                return length, length
            # Поврежденный тег внутри содержимого: продолжаем с места обрыва.
            pos = broken + 1
            continue
        if match.group("close"):
            depth -= 1
            if depth == 0:
                return match.start(), gt + 1
        elif xml[gt - 1] != "/":
            depth += 1
        pos = gt + 1
    return length, length


def _strip_query(url: str) -> str:
    """Срезать query string и фрагмент, оставив схему, хост и путь."""
    base, has_fragment, _ = url.partition("#")
    base, has_query, _ = base.partition("?")
    return f"{base}{_SIGNED}" if has_query or has_fragment else base


def _mask_text_url(match: re.Match[str]) -> str:
    """Срезать подпись из ссылки в текстовом узле."""
    url = match.group()
    query = url.partition("?")[2]
    if query and _SIGNED_PARAM_RE.search(f"?{query}"):
        return _strip_query(url)
    return url


def _mask_text(text: str) -> str:
    """Обработать текстовый узел: подписанные ссылки теряют query string."""
    if "://" not in text:
        return text
    return _TEXT_URL_RE.sub(_mask_text_url, text)


def _attr_value(match: re.Match[str]) -> tuple[str, str]:
    """Кавычка и значение атрибута. Для значения без кавычек подставляется одинарная."""
    value = match.group("dq")
    if value is not None:
        return '"', value
    value = match.group("sq")
    if value is not None:
        return "'", value
    return "'", match.group("bare")


def _mask_attr(match: re.Match[str]) -> str:
    """Заменить значение чувствительного атрибута или срезать подпись из ссылки."""
    name = match.group("name")
    local = _local_name(name)
    quote, value = _attr_value(match)
    if not value:
        return match.group(0)
    if local in _SECRET_ATTRS:
        return f"{name}={quote}{_REDACTED}{quote}"
    if local in _URL_ATTRS and ("?" in value or "#" in value):
        return f"{name}={quote}{_strip_query(value)}{quote}"
    return match.group(0)


def _mask_attrs(attrs: str) -> str:
    """Обработать атрибуты тега."""
    if "=" not in attrs:
        return attrs
    return _ATTR_RE.sub(_mask_attr, attrs)


def _sasl_marker(content: str) -> str | None:
    """Отметка о длине нагрузки SASL. Пустое тело не трогается."""
    if not content.strip():
        return None
    return f"[SASL payload: {len(content.encode('utf-8', 'replace'))} bytes, redacted]"


def _header_is_sensitive(attrs: str) -> bool:
    """Несет ли заголовок учетные данные.

    Элемент header без атрибута name - это заголовок OMEMO с номером устройства,
    а не заголовок HTTP: он не маскируется, разбор уходит внутрь, к ключам.
    """
    match = _HEADER_NAME_RE.search(attrs)
    if match is None:
        return False
    quote_free = match.group("dq") or match.group("sq") or match.group("bare") or ""
    return quote_free.strip().lower() not in _SAFE_HEADER_NAMES


def _mask_content(local: str, attrs: str, content: str) -> str | None:
    """Замена содержимого элемента.

    ``None`` означает, что содержимое надо разобрать обычным проходом: так
    обрабатываются контейнеры, у которых секрет лежит не в них самих, а во
    вложенных элементах.
    """
    if local in _SASL_ELEMENTS:
        return _sasl_marker(content)
    if local == _SASL_CONTAINER:
        return None if "<" in content else _sasl_marker(content)
    if local in _OMEMO_ELEMENTS:
        if "<" in content:
            # Разметка внутри ключа - это либо CDATA, либо попытка обхода. Усечение
            # оборвало бы ее посередине и дало бы неразбираемую строку, поэтому тело
            # заменяется отметкой целиком.
            return f"[{local}: {len(content.encode('utf-8', 'replace'))} bytes, redacted]"
        if len(content) <= _OMEMO_KEEP:
            return None
        return f"{content[:_OMEMO_KEEP]}{_ELLIPSIS}"
    if local == _BUNDLE_ELEMENT:
        if not content.strip():
            return None
        return f"[bundle: {len(_BUNDLE_KEY_RE.findall(content))} keys, redacted]"
    if local in _PASSWORD_ELEMENTS:
        return _REDACTED if content.strip() else None
    if local in _URL_ELEMENTS:
        if "<" in content or ("?" not in content and "#" not in content):
            return None
        return _strip_query(content.strip())
    if local == _HEADER_ELEMENT:
        if not _header_is_sensitive(attrs) or not content.strip():
            return None
        return _REDACTED
    return None


def _apply_rules(xml: str) -> str:
    """Пройти строфу сканером разметки и применить правила маскирования."""
    out: list[str] = []
    append = out.append
    length = len(xml)
    pos = 0

    while pos < length:
        lt = xml.find("<", pos)
        if lt < 0:
            append(_mask_text(xml[pos:]))
            break
        if lt > pos:
            append(_mask_text(xml[pos:lt]))

        if xml.startswith("<!--", lt):
            stop = xml.find("-->", lt + 4)
            if stop < 0:
                append(xml[lt:])
                break
            append(xml[lt : stop + 3])
            pos = stop + 3
            continue

        if xml.startswith("<![CDATA[", lt):
            stop = xml.find("]]>", lt + 9)
            if stop < 0:
                append(f"<![CDATA[{_mask_text(xml[lt + 9 :])}")
                break
            append(f"<![CDATA[{_mask_text(xml[lt + 9 : stop])}]]>")
            pos = stop + 3
            continue

        gt, broken = _tag_end(xml, lt + 1)
        if gt < 0:
            if broken < 0:
                # Тега нет вовсе: разбирать нечего, остаток идет как есть.
                append(xml[lt:])
                break
            # Тег поврежден. Текст до места обрыва выводится как есть, а разбор
            # продолжается с него: за обрывом может стоять элемент с секретом.
            append(xml[lt:broken])
            pos = broken
            continue

        name_match = _TAG_NAME_RE.match(xml, lt + 1)
        if name_match is None:
            # Закрывающий тег, декларация или инструкция обработки.
            append(xml[lt : gt + 1])
            pos = gt + 1
            continue

        name = name_match.group()
        attrs = xml[name_match.end() : gt]
        self_closing = attrs.endswith("/")
        if self_closing:
            attrs = attrs[:-1]
        tail = "/" if self_closing else ""
        start_tag = f"<{name}{_mask_attrs(attrs)}{tail}>"
        local = _local_name(name)
        if self_closing or local not in _RULE_ELEMENTS:
            append(start_tag)
            pos = gt + 1
            continue

        content_end, after = _content_end(xml, gt + 1, local)
        replacement = _mask_content(local, attrs, xml[gt + 1 : content_end])
        if replacement is None:
            append(start_tag)
            pos = gt + 1
            continue
        append(start_tag)
        append(replacement)
        # Закрывающий тег переносится как есть. Если его не было, он дописывается:
        # содержимое все равно заменено, а строфа остается разбираемой.
        append(xml[content_end:after] if after > content_end else f"</{name}>")
        pos = after

    return "".join(out)


def redact(xml: str, unsafe: bool = False) -> str:
    """Замаскировать секреты в строфе.

    При ``unsafe=True`` строка возвращается без изменений: это режим, который
    включается отдельной командой с подтверждением.

    Функция не бросает исключений. Если внутреннее правило завершилось ошибкой,
    возвращается заглушка, а не исходный текст.
    """
    if unsafe:
        return xml
    try:
        return _apply_rules(xml)
    except Exception:
        # Отказ маскирования не должен ронять поток и не должен показывать исходник.
        return _REDACTION_FAILED


def redaction_summary() -> tuple[str, ...]:
    """Перечень активных правил маскирования.

    Показывается командой /help и предупреждением при включении режима unsafe.
    """
    return (
        _(
            "SASL: bodies of auth, response, challenge, initial-response, additional-data "
            "and success are replaced with a length mark"
        ),
        _("OMEMO: bodies of key and payload are truncated to 16 characters"),
        _("PEP bundle: the number of keys is shown instead of their bodies"),
        _(
            "XEP-0363 and OOB: the signed query string is cut from links in the put, get, "
            "url, uri, target attributes"
        ),
        _("Signed links in message text lose the query string"),
        _(
            "MUC: the room password is replaced completely, as are the password, token, "
            "signature and similar attributes"
        ),
        _(
            "Request headers in the slot and in OOB are replaced completely, except "
            "known safe ones like Content-Type"
        ),
        _(
            "Rules match the element name regardless of case and prefix, the content "
            "is taken together with CDATA and comments"
        ),
    )
