"""Подстановка значений в путь источника.

Половина API принимает значение в пути, а не параметром, и адаптер это обещал:
в документации моста «ФИО по телефону» прямо написано, что номер подставляется
в ``{phone}``. Не подставлялся: путь уезжал в запрос как есть, httpx экранировал
скобки, и на сервер уходило ``/lookup/%7Bphone%7D?phone=%2B7…``. Ответ — 404, и
ни одной подсказки: источник настроен, ключ верный, а имя «не определилось».
"""

from __future__ import annotations

import pytest

from app.providers.base import ProviderUnavailableError
from app.providers.vendor_http import _fill_path


def test_a_placeholder_is_replaced_and_not_repeated_in_the_query() -> None:
    """Подставленное уходит в путь и исчезает из параметров.

    Дублировать нельзя: номер ушёл бы дважды — в пути и параметром, — и вендор,
    разбирающий строку запроса строго, ответил бы ошибкой на верно настроенный
    мост.
    """
    path, query = _fill_path("/lookup/{phone}", {"phone": "+79991234567"})

    assert path == "/lookup/%2B79991234567"
    assert query == {}


def test_the_value_is_escaped_whole() -> None:
    """Значение не должно разъехаться на несколько сегментов пути.

    Плюс в телефоне обязан стать ``%2B``, а не потеряться; косая черта в любом
    значении — ``%2F``, а не новый сегмент, ведущий неизвестно куда.
    """
    path, _ = _fill_path("/x/{v}", {"v": "a/b +c"})

    assert path == "/x/a%2Fb%20%2Bc"


def test_parameters_without_a_placeholder_still_go_to_the_query() -> None:
    """Путь берёт только то, что в нём названо; остальное — параметрами."""
    path, query = _fill_path("/lookup/{phone}", {"phone": "+7999", "limit": 10})

    assert path == "/lookup/%2B7999"
    assert query == {"limit": 10}


def test_a_path_without_placeholders_is_untouched() -> None:
    """Обычный путь ведёт себя как прежде: значения уходят параметрами."""
    path, query = _fill_path("/search", {"phone": "+7999"})

    assert path == "/search"
    assert query == {"phone": "+7999"}


@pytest.mark.parametrize("params", [{}, {"phone": None}])
def test_a_placeholder_with_nothing_to_fill_says_so(params: dict[str, object]) -> None:
    """Молчаливая подстановка пустой строки дала бы тот же необъяснимый 404.

    Ошибка настройки обязана называться вслух: иначе владелец видит «имя по
    номеру не определено» и ищет причину в источнике, а она в одной строке
    его же ``.env``.
    """
    with pytest.raises(ProviderUnavailableError) as excinfo:
        _fill_path("/lookup/{phone}", params)

    assert "phone" in str(excinfo.value)
