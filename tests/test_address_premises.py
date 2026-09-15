"""Доходит ли адрес до квартиры — один вопрос, один ответ, три потребителя.

Правило существует из-за денег: Росреестр по адресу до дома отвечает ошибкой, а
вызов всё равно оплачен. Поэтому адрес без квартиры в ЕГРН не отправляется, а
мосты предпочитают тот адрес, который до квартиры доходит.

Жило оно тремя копиями — в мосте по телефону, в мосте по ФИО и в провайдере
ЕГРН, — и копии обошлись дорого ровно так, как должны были: расширь одну, и
мосты выбирают адрес с квартирой, а ЕГРН его всё равно отвергает.
"""

from __future__ import annotations

import pytest

from app.domain.enums import SearchType
from app.domain.identity import SearchSubject
from app.providers.name_bridge import _pick_address as pick_by_name
from app.providers.phone_bridge import _pick_address as pick_by_phone
from app.providers.property import _query_for
from app.utils.address import has_premises

#: Оба адреса — живые, из ответов поставщика по двум разным регионам, и оба
#: заканчиваются домом и квартирой без слова «кв». Улицы и числа изменены:
#: живые адреса — персональные данные и в репозиторий не едут.
NUMERIC_TAIL = (
    "г Москва, проезд Тестовый,8,139",
    "обл Тестовая, г Тестов, б-р Первый,17,151",
)
WITH_WORD = (
    "г Тестов, б-р Первый, д 17, кв 151",
    "обл Тестовая, г Тестов, ул Первая, помещ. 5",
)
HOUSE_ONLY = (
    "обл Тестовая, г Тестов, ул Первая, 17",
    "г Москва, ул Первая, 8",
    # Индекс перед городом числом не считается: он не в конце.
    "123456, г Москва, ул Первая, 8",
)


@pytest.mark.parametrize("address", NUMERIC_TAIL + WITH_WORD)
def test_an_address_that_reaches_the_flat_is_accepted(address: str) -> None:
    """Обе формы равноправны: со словом «кв» и числовым хвостом.

    Вторая добавлена по живым данным: владелец назвал её частой и подтвердил
    двумя адресами из разных регионов. До этого «…,8,139» отвергалось, и ЕГРН
    не спрашивался по должникам, у которых полный адрес есть.
    """
    assert has_premises(address)


@pytest.mark.parametrize("address", HOUSE_ONLY)
def test_an_address_that_stops_at_the_house_is_refused(address: str) -> None:
    """Одинокое число в конце — дом без квартиры, и это тот самый случай.

    Расширение формы не имеет права съесть исходное правило: за адрес до дома
    Росреестр берёт деньги и отвечает ошибкой.
    """
    assert not has_premises(address)


def test_two_bare_numbers_without_a_street_are_not_an_address() -> None:
    """«8,139,2» — обрывок, и угадывать в нём квартиру не из чего."""
    assert not has_premises("8,139")
    assert not has_premises("г Москва, ул Первая, 8, 139, 2")


@pytest.mark.parametrize("address", NUMERIC_TAIL)
def test_all_three_users_agree_on_the_same_address(address: str) -> None:
    """Мосты выбирают тот адрес, который ЕГРН согласится спросить.

    Это и есть смысл общего правила. Разойдись они — мост отдал бы в карточку
    адрес с квартирой, а провайдер отказался бы его спрашивать, и раздел
    писал бы «нужен адрес с квартирой» под адресом с квартирой.
    """
    rows = [{"address": "г Тестов, ул Вторая, 3"}, {"address": address}]

    assert pick_by_phone(rows) == address
    assert pick_by_name(rows) == address
    subject = SearchSubject(search_type=SearchType.PERSON.value, address=address)
    assert _query_for(subject) == {"country": "ru", "address": address}
