"""Доходит ли адрес до квартиры — один вопрос, один ответ, три потребителя.

Правило существует из-за денег: Росреестр по адресу до дома отвечает ошибкой, а
вызов всё равно оплачен. Поэтому адрес без квартиры в ЕГРН не отправляется, а
мосты предпочитают тот адрес, который до квартиры доходит.

Жило оно тремя копиями — в мосте по телефону, в мосте по ФИО и в провайдере
ЕГРН, — и копии обошлись дорого ровно так, как должны были: расширь одну, и
мосты выбирают адрес с квартирой, а ЕГРН его всё равно отвергает.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.enums import SearchType
from app.domain.identity import SearchSubject
from app.providers.name_bridge import _pick_address as pick_by_name
from app.providers.phone_bridge import _address_candidates
from app.providers.property import _query_for
from app.utils.address import MAX_OPTIONS, address_options, has_premises, pick_address


def pick_by_phone(rows: list[Any], anchor: Any = None) -> str | None:
    """Выбор адреса мостом по телефону — через тот же путь, что и в проде.

    Собственной функции у моста больше нет: она была бы второй копией правила,
    а копии в этом проекте уже стоили трёх неверных выборов адреса. Тест
    складывает кандидатов тем же ``_address_candidates`` и выбирает тем же
    ``pick_address``, что и разбор.
    """
    front, rest = _address_candidates(rows, anchor)
    return pick_address(rest, preferred=front)


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

    # Опорного блока нет — значит решают квартира и частота, как и раньше.
    assert pick_by_phone(rows, None) == address
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
    же блока не участвовал ни в выборе, ни в подсчёте частоты. Проверяется на
    блоке, у которого первый ключ адресом НЕ является: если бы участвовал
    только он, не нашлось бы ничего.
    """
    rows = [
        {"address": "Москва", "address_reg": FEST},
        {"address": FEST},
    ]

    assert pick_by_phone(rows, None) == FEST
    assert pick_by_name(rows) == FEST


def test_the_anchor_block_decides_the_address_not_the_crowd() -> None:
    """Адрес опорного блока побеждает адрес, повторившийся чаще.

    Регрессия, найденная владельцем на собственном номере: бот подставил ей
    чужую квартиру по другому проспекту, а её собственный адрес лежал в опорном
    блоке. Частота сама по себе — довод настоящий, но применили её ко ВСЕЙ
    родне, а родню разбор определяет правилом «не противоречит»: блок с одним
    адресом и без единого идентификатора не противоречит никому и попадает в
    родню, ничего про себя не доказав. Несколько таких перевешивают
    единственный блок, про который известно, что он о нашем человеке.

    Так адрес встаёт в один ряд с остальными полями: паспорт, СНИЛС, ИНН и дату
    рождения разбор берёт первым годным начиная с якоря, и голосованием
    решался ровно один адрес.
    """
    anchor = {"address": FEST}
    strangers = [{"address": ODD}, {"address": ODD}, {"address": ODD}]

    assert pick_by_phone([anchor, *strangers], anchor) == FEST
    # И то же самое на уровне общего правила, без моста.
    assert pick_address([ODD, ODD, ODD], preferred=[FEST]) == FEST


def test_the_anchor_house_beats_a_strangers_flat() -> None:
    """Дом нашего человека сильнее чужой квартиры — и это про деньги.

    Внутри группы адрес с квартирой сильнее адреса до дома: только такой примет
    Росреестр. Но между группами это предпочтение не действует. Дом якоря даёт
    бесплатный и честный отказ «нужен адрес с квартирой», а чужая квартира —
    оплаченный ответ про чужое имущество, и именно он выглядит как находка.
    """
    house = "г Москва, ул Первая, 8"

    assert pick_address([FEST, FEST], preferred=[house]) == house


def test_frequency_still_decides_when_the_anchor_is_silent() -> None:
    """Опорный блок без адреса — и довод владельца работает в полную силу."""
    assert pick_address([ODD, FEST, FEST], preferred=[]) == FEST
    assert pick_address([ODD, FEST, FEST], preferred=["Москва", "—"]) == FEST


def test_nothing_usable_gives_nothing() -> None:
    """«Москва» и прочерк адресом не являются."""
    assert pick_address(["Москва", "—", ""]) is None


# ------------------------------- когда выбрать кодом нельзя


def test_the_candidates_are_offered_with_the_default_first() -> None:
    """Все адреса с квартирой, и подставленный по умолчанию — первым.

    Заведено после четырёх неудачных правил подряд. Живой ответ по одному
    номеру: восемь кандидатов, и опорный блок — тот, из которого взяты паспорт,
    СНИЛС и дата рождения, — несёт адрес, по которому должник не живёт.
    Признака, по которому машина отличила бы верный, в ответе нет.

    Порядок здесь не ранжирование, а вежливость: согласиться с умолчанием
    должно быть одним взглядом, а не поиском среди восьми.
    """
    options = address_options([ODD, ODD, FEST], preferred=[FEST])

    assert options[0] == FEST, "умолчание обязано стоять первым"
    assert set(options) == {FEST, ODD}
    assert len(options) == 2, "один и тот же адрес не предлагается дважды"


def test_every_candidate_is_offered_with_the_usable_ones_first() -> None:
    """Предлагаются ВСЕ адреса, а годные для ЕГРН — выше.

    Сначала список отбирал только доходящие до квартиры, с доводом «ЕГРН
    остальные не примет». Довод оказался неверным дважды. Кнопка выбора при
    одном годном кандидате не появлялась вовсе — владелец видел неверный адрес
    и не мог его сменить: «по Олегу конкретный и выбрать не даёт». А оператор,
    узнавший свой адрес в списке, дописывает квартиру сам — не увидев его, не
    может и этого.
    """
    house_only = "г Москва, ул Первая, 2"

    assert address_options([house_only]) == [house_only]
    # Годный для ЕГРН — первым, но дом из списка не выброшен.
    assert address_options([house_only, FEST]) == [FEST, house_only]


def test_more_than_eight_candidates_are_cut() -> None:
    """Восемь — предел экрана, и умолчание остаётся в списке при любом обрезе."""
    many = [f"г Москва, ул Тестовая {index}, 5, 12" for index in range(20)]

    options = address_options(many, preferred=[FEST])

    assert len(options) == MAX_OPTIONS
    assert options[0] == FEST
