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
from app.utils.address import has_premises, pick_address

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


# ------------------------------- какой адрес брать, когда их несколько


FEST = "обл Тестовая, г Тестов, б-р Первый,17,151"
ODD = "г Москва, ул Одиночная, 5, 12"


def test_the_address_confirmed_by_several_blocks_wins_over_the_first() -> None:
    """Частота сильнее порядка выдачи, и это довод, а не вкус.

    Правило было «первый адрес с квартирой», то есть опиралось на порядок,
    который задаёт поставщик, а не жизнь должника. Владелец предложил лучшее:
    «он в ответе встречается чаще всего». Блоки собраны из НЕЗАВИСИМЫХ утечек, и
    адрес, повторившийся в нескольких, подтверждён несколькими сразу; одиночный
    не подтверждён ничем.
    """
    assert pick_address([ODD, FEST, FEST]) == FEST


def test_the_same_place_written_differently_counts_once() -> None:
    """Пробелы вокруг запятых различают не адреса, а тех, кто их записывал.

    Без приведения «…Первый,17,151» и «…Первый, 17, 151» считались бы разными
    адресами, и частота — главный здесь довод — считалась бы неверно.
    """
    spaced = FEST.replace(",17,151", ", 17, 151")

    assert pick_address([ODD, FEST, spaced]) == FEST


def test_order_still_decides_a_tie() -> None:
    """Равная частота — берём встреченный раньше: другого довода нет."""
    assert pick_address([ODD, FEST]) == ODD


def test_a_flat_beats_frequency() -> None:
    """Адрес с квартирой сильнее частого адреса до дома — и это про деньги.

    Частота говорит о подтверждённости, а квартира — о том, примет ли Росреестр
    запрос вообще. Второе решается раньше.
    """
    house_only = "г Москва, ул Первая, 2"

    assert pick_address([house_only, house_only, FEST]) == FEST


def test_every_address_of_a_block_takes_part_not_just_the_first() -> None:
    """В блоке бывает и прописка, и фактический — участвуют оба.

    Раньше из строки брался первый непустой ключ из шести, и второй адрес того
    же блока не участвовал ни в выборе, ни в подсчёте частоты.
    """
    rows = [
        {"address": ODD, "address_reg": FEST},
        {"address": FEST},
    ]

    assert pick_by_phone(rows) == FEST
    assert pick_by_name(rows) == FEST


def test_nothing_usable_gives_nothing() -> None:
    """«Москва» и прочерк адресом не являются."""
    assert pick_address(["Москва", "—", ""]) is None
