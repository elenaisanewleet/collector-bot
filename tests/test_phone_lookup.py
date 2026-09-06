"""Поиск должника по телефону.

Проверяемое свойство: номер, попавший в базу из выгрузки, находится в любой
записи, в какой его наберёт оператор. Это не удобство ввода, а главный сценарий
из ТЗ — «ввёл ФИО + номер телефона». Выгрузка хранит номер в одном виде
(«+7 (926) 324-86-00»), оператор набирает в другом («89263248600»), и совпасть
они обязаны.

Поломка здесь особенно тиха: ненайденный должник неотличим от отсутствующего в
выгрузке. Источник отвечает «отработал, совпадений нет», ИНН из 1С не
подтягивается, остальные реестры отказываются искать без него — и виноватой
выглядит выгрузка заказчика.
"""

from __future__ import annotations

import pytest

from app.container import Container
from app.db.repository import phone_hash

EXPORT = "\n".join(
    [
        "ФИО,Телефон,Дата_рождения,ИНН,Долг",
        "Александрова Анна Максимовна,+7 (926) 324-86-00,25.06.1984,699246149770,13792",
    ]
)

# Формы одного и того же номера, каждая из которых встречается живьём: как его
# печатает 1С, как набирают с восьмёрки, как хранит база, как диктуют без кода.
SAME_NUMBER = [
    "+7 (926) 324-86-00",
    "89263248600",
    "+79263248600",
    "79263248600",
    "9263248600",
    "8 926 324 86 00",
]


def test_hash_is_the_same_for_every_way_of_writing_one_number() -> None:
    """Хеш обязан сходиться по построению, а не по договорённости.

    Раньше нормализацию делала сторона записи, а сторона поиска — нет: хеш
    считался от того, что набрал оператор. Совпадение случалось только при
    посимвольном равенстве.
    """
    hashes = {phone_hash(form) for form in SAME_NUMBER}
    assert len(hashes) == 1, f"одна и та же цифра дала разные хеши: {hashes}"


def test_number_that_is_not_russian_is_not_folded_into_a_neighbour() -> None:
    """Лучше не найти, чем найти чужого.

    Ненормализуемый номер хешируется как есть: подгонять его под российский
    формат значило бы склеить двух разных людей в одну запись.
    """
    assert phone_hash("+1 202 555 0143") != phone_hash("+7 202 555 0143")
    assert phone_hash("") is None
    assert phone_hash(None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", SAME_NUMBER)
async def test_debtor_is_found_however_the_operator_types_the_number(
    container: Container, typed: str
) -> None:
    """Сквозная проверка: из выгрузки в базу и обратно поиском."""
    await container.import_service.import_text(EXPORT)

    found = await container.registry.internal.find_by_phone(typed)

    assert len(found) == 1, f"«{typed}» не нашёл должника, записанного как +79263248600"
    assert found[0].full_name == "Александрова Анна Максимовна"
    assert found[0].inn == "699246149770"


@pytest.mark.asyncio
async def test_phone_search_pulls_the_inn_the_registries_need(container: Container) -> None:
    """Ради этого поиск по телефону и существует.

    Телефона нет как поля ни в одном реестре: он ключ к базе заказчика, а не к
    ФССП. Смысл совпадения — вытащить из 1С ИНН и дату рождения, без которых
    банкротство, ИП и арбитраж отвечают «недостаточно данных».
    """
    from app.domain.enums import SearchType
    from app.domain.identity import SearchSubject

    await container.import_service.import_text(EXPORT)

    report = await container.search_service.search(
        SearchSubject(search_type=SearchType.PERSON, phone="89263248600"),
        telegram_user_id=1,
    )

    assert report.internal_records, "по телефону из 1С ничего не подтянулось"
    assert report.internal_records[0].inn == "699246149770"
