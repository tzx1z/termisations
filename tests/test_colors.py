"""Цвет собеседника по XEP-0392.

Смысл расширения в том, что у всех клиентов один участник получает один цвет.
Держится это на одной формуле, поэтому она здесь и проверяется: угол считается
независимо, прямо по описанию раздела 5.4, и сверяется с реализацией. Сверка с
таблицей контрольных значений из текста расширения не делается - таблицы под
рукой нет, а приводить ее по памяти значит проверять память, а не код.
"""

import hashlib
import math
from typing import Final

import pytest

from termisations.core.colors import LUMA, color_of, hue_of, rgb_of

# Имена для проверок. Латиница, адрес, эмодзи и кириллица: разбор идет по
# байтам UTF-8, и однобайтовые имена ничего не говорят о многобайтовых.
NAMES: Final[tuple[str, ...]] = (
    "Romeo",
    "juliet@capulet.lit",
    "😺",
    "council",
    "Марья",
)


def expected_angle(name: str) -> float:
    """Угол по описанию раздела 5.4, посчитанный независимо от реализации."""
    digest = hashlib.sha1(name.encode("utf-8"), usedforsecurity=False).digest()
    return (digest[0] + digest[1] * 256) / 65536.0 * 2 * math.pi


@pytest.mark.parametrize("name", NAMES)
def test_angle_follows_the_formula(name: str) -> None:
    """Угол берется из первых двух байт SHA-1 как little-endian.

    Любое другое прочтение хэша даст другой цвет, и совпадение с чужими
    клиентами - единственный смысл расширения - пропадет.
    """
    assert hue_of(name) == pytest.approx(expected_angle(name), abs=1e-12)


def test_angle_covers_the_whole_circle() -> None:
    """Угол лежит в полном обороте: из хэша он отображается целиком."""
    for name in NAMES:
        assert 0.0 <= hue_of(name) <= 2 * math.pi


def test_same_name_gives_same_color() -> None:
    """Один участник всегда получает один цвет: на этом держится расширение."""
    assert color_of("Romeo") == color_of("Romeo")


def test_different_names_differ() -> None:
    """Разные участники получают разные цвета."""
    assert len({color_of(name) for name in NAMES}) == len(NAMES)


def test_color_is_a_hex_triple() -> None:
    """Цвет отдается в виде, который принимает терминал."""
    value = color_of("Romeo")
    assert value.startswith("#")
    assert len(value) == 7
    assert int(value[1:], 16) >= 0


def test_empty_name_is_grey_not_an_error() -> None:
    """Пустое имя дает серый: исключение заставило бы проверять имя при отрисовке."""
    assert color_of("") == "#808080"


@pytest.mark.parametrize("name", NAMES)
def test_components_stay_in_range(name: str) -> None:
    """Составляющие цвета не выходят за границы даже при отсечении."""
    assert all(0 <= item <= 255 for item in rgb_of(name))


def test_luma_is_constant() -> None:
    """Яркость постоянна: цвет обязан читаться и на светлом фоне, и на темном.

    Из хэша она не берется - иначе часть участников оказалась бы нечитаемой в
    одной из тем.
    """
    assert pytest.approx(0.732) == LUMA
