"""Мост «ФИО + дата рождения → паспорт»: кого он признаёт своим.

Этот мост опаснее двух соседних, и разница не в сложности кода, а в цене
ошибки. Поставщик ищет по строке и на распространённое имя отвечает десятками
разных людей: живой ответ 09.09.2026 на «Иванов Иван Иванович 1985» — 254
записи, на «Иванов Иван Иванович 15.03.1985» — 43.

Взяли чужой паспорт — по нему ушёл платный запрос в ФНС, пришёл чужой ИНН, по
чужому ИНН проверились банкротство, ИП и арбитраж, и в отчёте про вашего
должника оказалась чужая жизнь. Ни одна строка отчёта при этом не выглядит
подозрительно. Поэтому здесь проверяется не «работает ли», а «кого он
отвергает».
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.config import AppMode, Settings
from app.domain.enums import MissingInput, ProviderStatus, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import ProviderResult
from app.providers.name_bridge import PassportByNameResult, build_name_bridge

BASE = "https://names.example.test"
OURS = PersonName(last_name="Иванов", first_name="Иван", middle_name="Иванович")
BIRTH = date(1985, 3, 15)
OUR_PASSPORT = "4510123456"
STRANGER_PASSPORT = "4511654321"


@pytest.fixture
def field_map(tmp_path: Path) -> Path:
    path = tmp_path / "fio.json"
    path.write_text(
        '{"records_path": "results", "fields": {"fio": "fio", "birth_date": "dob",'
        ' "inn": "inn", "passport": "passport", "passport_info": "passport_info",'
        ' "snils": "snils"}}',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def settings(field_map: Path) -> Settings:
    return Settings(
        app_mode=AppMode.LIVE,
        app_name="t",
        telegram_bot_token="t",
        allowed_telegram_user_ids="*",
        owner_telegram_user_ids="111",
        database_url="sqlite+aiosqlite:///:memory:",
        name_bridge_enabled=True,
        name_bridge_base_url=BASE,
        name_bridge_path="/quest={query}&token=secret",
        name_bridge_auth_style="none",
        name_bridge_field_map=field_map,
        provider_max_retries=0,
        log_level="CRITICAL",
        _env_file=None,
    )


def subject(**over: object) -> SearchSubject:
    base: dict[str, object] = {
        "search_type": SearchType.PERSON.value,
        "name": OURS,
        "birth_date": BIRTH,
    }
    return SearchSubject(**{**base, **over})


def answer(*rows: dict[str, object]) -> Response:
    return Response(200, json={"search_type": "fio", "results": list(rows)})


async def fetch(settings: Settings, subj: SearchSubject) -> PassportByNameResult:
    """Ответ моста. Отказ по нехватке данных сюда не ходит — см. ``refuse``."""
    bridge = build_name_bridge(settings)
    assert bridge is not None
    result = await bridge.fetch(subj)
    assert isinstance(result, PassportByNameResult)
    return result


async def refuse(settings: Settings, subj: SearchSubject) -> ProviderResult:
    """Отказ до обращения: ``insufficient_query`` отдаёт базовый результат."""
    bridge = build_name_bridge(settings)
    assert bridge is not None
    return await bridge.fetch(subj)


# ---------------------------------------------------------------- отбор


@respx.mock
async def test_only_the_row_with_our_exact_birth_date_is_ours(settings: Settings) -> None:
    """Точная дата рождения — единственный признак, отделяющий однофамильцев.

    В ответе три Иванова Ивана Ивановича: годом раньше, годом позже и наш. У
    каждого свой паспорт, и все три записи выглядят одинаково добротно.
    """
    respx.get(url__startswith=BASE).mock(
        return_value=answer(
            {"fio": "Иванов Иван Иванович", "dob": "15.03.1984", "passport": STRANGER_PASSPORT},
            {"fio": "Иванов Иван Иванович", "dob": "15.03.1985", "passport": OUR_PASSPORT},
            {"fio": "Иванов Иван Иванович", "dob": "15.03.1986", "passport": "4512999999"},
        )
    )

    result = await fetch(settings, subject())

    assert result.status is ProviderStatus.SUCCESS
    assert result.passport == OUR_PASSPORT, "взят паспорт человека с другой датой рождения"


@respx.mock
async def test_a_different_surname_on_the_same_date_is_not_ours(settings: Settings) -> None:
    """Дата рождения не уникальна — фамилия обязана совпасть тоже.

    В один день рождаются тысячи людей, и в выдачу по строке попадают записи,
    подошедшие по другим полям.
    """
    respx.get(url__startswith=BASE).mock(
        return_value=answer(
            {"fio": "Петров Пётр Петрович", "dob": "15.03.1985", "passport": STRANGER_PASSPORT},
        )
    )

    result = await fetch(settings, subject())

    assert result.status is ProviderStatus.NO_RESULTS
    assert result.passport is None, "уехал паспорт однодневки с другой фамилией"


@respx.mock
async def test_a_row_without_a_readable_date_is_never_ours(settings: Settings) -> None:
    """Нет даты — нет и права считаться нашим. Молчание не признак совпадения."""
    respx.get(url__startswith=BASE).mock(
        return_value=answer(
            {"fio": "Иванов Иван Иванович", "passport": STRANGER_PASSPORT},
            {"fio": "Иванов Иван Иванович", "dob": "не указана", "passport": "4513111111"},
        )
    )

    result = await fetch(settings, subject())

    assert result.status is ProviderStatus.NO_RESULTS
    assert result.passport is None


@respx.mock
async def test_both_date_formats_of_the_source_are_understood(settings: Settings) -> None:
    """Поставщик шлёт и ``15.03.1985``, и ``1985-03-15`` — в одном ответе.

    Снято с живого: из 43 записей 34 в первом формате и 3 во втором. Понимать
    надо оба, иначе часть своих же записей отсеется как чужие.
    """
    respx.get(url__startswith=BASE).mock(
        return_value=answer(
            {"fio": "Иванов Иван Иванович", "dob": "1985-03-15", "passport": OUR_PASSPORT}
        )
    )

    result = await fetch(settings, subject())

    assert result.passport == OUR_PASSPORT


# ---------------------------------------------------------------- что берём


@respx.mock
async def test_the_issue_date_comes_from_the_same_document(settings: Settings) -> None:
    """Дата выдачи — у того же номера, а не первая попавшаяся.

    Отдельного поля под неё поставщик не отдаёт: она лежит текстом внутри
    ``passport_info``, и такие блоки есть у нескольких разных документов.
    """
    respx.get(url__startswith=BASE).mock(
        return_value=answer(
            {
                "fio": "Иванов Иван Иванович",
                "dob": "15.03.1985",
                "passport": "751234567",
                "passport_info": "выдан 10.10.2020",
            },
            {
                "fio": "Иванов Иван Иванович",
                "dob": "15.03.1985",
                "passport": OUR_PASSPORT,
                "passport_info": "выдан ОВД, 29.01.2015",
            },
        )
    )

    result = await fetch(settings, subject())

    assert result.passport == OUR_PASSPORT, "загранпаспорт на девять цифр принят за паспорт РФ"
    assert result.passport_issued is not None
    assert result.passport_issued.isoformat() == "2015-01-29"


@respx.mock
async def test_an_eleven_digit_inn_is_refused_here_too(settings: Settings) -> None:
    """Поставщик кладёт СНИЛС в поле ``inn`` — тот же трюк, что в поиске по номеру."""
    respx.get(url__startswith=BASE).mock(
        return_value=answer(
            {
                "fio": "Иванов Иван Иванович",
                "dob": "15.03.1985",
                "passport": OUR_PASSPORT,
                "inn": "16011086811",
                "snils": "16011086811",
            }
        )
    )

    result = await fetch(settings, subject())

    assert result.inn is None, "одиннадцать цифр — это не ИНН"
    assert result.snils == "16011086811"


# ---------------------------------------------------------------- когда не звать


def test_the_bridge_is_not_needed_when_the_passport_is_known(settings: Settings) -> None:
    """С паспортом на руках покупать его второй раз незачем."""
    bridge = build_name_bridge(settings)
    assert bridge is not None

    assert not bridge.is_needed(subject(passport=OUR_PASSPORT))


def test_the_bridge_is_not_needed_when_the_inn_is_already_there(settings: Settings) -> None:
    """Паспорт нужен ради ИНН. Есть ИНН — вся цепочка лишняя.

    Это минус два платных обращения с каждого такого должника: и этот мост, и
    мост ФНС следом.
    """
    bridge = build_name_bridge(settings)
    assert bridge is not None

    assert not bridge.is_needed(subject(inn="770123456789"))
    # А десятизначный ИНН принадлежит юрлицу, и три источника его отвергнут —
    # значит мост по-прежнему нужен.
    assert bridge.is_needed(subject(inn="7701234567"))


@respx.mock
async def test_without_a_birth_date_the_bridge_spends_nothing(settings: Settings) -> None:
    """По одному имени мост не идёт — и не тратит ни одного обращения.

    Без даты поставщик отвечает сотнями однофамильцев, из которых выбрать
    нужного нельзя ничем. Платить за такой ответ не за что.
    """
    calls = respx.get(url__startswith=BASE).mock(return_value=answer())

    result = await refuse(settings, subject(birth_date=None))

    assert result.status is ProviderStatus.ERROR
    assert MissingInput.BIRTH_DATE.value in result.missing_input
    assert calls.call_count == 0, "мост сходил к поставщику без даты рождения"
