"""Мост «телефон → личность»: что он забирает из ответа и что с этим делает.

Мост существует затем, чтобы номер превратился в человека. Раньше он забирал
только ФИО и дату рождения — а поставщик может отдавать и ИНН, и это меняет
деньги: банкротство, статус ИП и арбитраж ищут ТОЛЬКО по ИНН, и без него бот
идёт за ним в ФНС отдельным платным обращением на каждого должника.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import respx
from httpx import Response

from app.config import AppMode, Settings
from app.domain.enums import ProviderStatus, SearchType
from app.domain.identity import SearchSubject
from app.providers.base import NO_CONTEXT
from app.providers.identity_bridge import InnBridgeProvider
from app.providers.phone_bridge import PhoneNameProvider, PhoneNameResult, build_phone_bridge
from app.providers.registry import build_inn_bridge

BASE = "https://bridge.example.test"


@pytest.fixture
def field_map(tmp_path: Path) -> Path:
    path = tmp_path / "map.json"
    path.write_text(
        '{"records_path": "results", "fields": {"fio": "fio", "birth_date": "bd",'
        ' "inn": "inn", "passport": "passport"}}',
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
        phone_bridge_enabled=True,
        phone_bridge_base_url=BASE,
        phone_bridge_path="/lookup/{phone}",
        phone_bridge_auth_style="none",
        phone_bridge_field_map=field_map,
        inn_bridge_enabled=True,
        newdb_api_key="fake",
        newdb_base_url="https://newdb.example.test",
        provider_max_retries=0,
        log_level="CRITICAL",
        _env_file=None,
    )


def subject(**over: object) -> SearchSubject:
    return SearchSubject(search_type=SearchType.PERSON.value, phone="+79991234567", **over)


def answer(**fields: str) -> Response:
    return Response(200, json={"results": [{"fio": "Иванов Иван Иванович", **fields}]})


def _phone(settings: Settings) -> PhoneNameProvider:
    """Мост под настройками теста. build_* отдаёт Optional — здесь он всегда есть."""
    bridge = build_phone_bridge(settings)
    assert bridge is not None
    return bridge


@respx.mock
async def test_the_bridge_carries_the_inn_when_the_source_gives_one(settings: Settings) -> None:
    """ИНН из ответа доезжает до субъекта — и это про деньги, а не про полноту.

    С готовым ИНН мост «паспорт → ИНН» не срабатывает вовсе: он проверяет
    ``is_needed``. Минус одно платное обращение с каждого должника, и охват
    всех, а не только тех, у кого паспорт есть в выгрузке.
    """
    respx.get(url__startswith=BASE).mock(
        return_value=answer(bd="15.03.1985", inn="500100732259", passport="45 05 123456")
    )

    result = await _phone(settings).fetch(subject(), NO_CONTEXT)
    assert isinstance(result, PhoneNameResult)

    assert result.status is ProviderStatus.SUCCESS
    assert result.inn == "500100732259"
    assert result.passport == "4505123456", "паспорт нормализуется, как введённый руками"


@respx.mock
async def test_a_ready_inn_switches_the_fns_bridge_off(settings: Settings) -> None:
    """Проверяется сама экономия, а не намерение сэкономить."""
    respx.get(url__startswith=BASE).mock(return_value=answer(bd="15.03.1985", inn="500100732259"))
    inn_bridge: InnBridgeProvider = build_inn_bridge(settings)

    result = await _phone(settings).fetch(subject(), NO_CONTEXT)
    assert isinstance(result, PhoneNameResult)
    enriched = subject().model_copy(
        update={"name": result.name, "birth_date": result.birth_date, "inn": result.inn}
    )

    assert not inn_bridge.is_needed(enriched), "обращение к ФНС не сэкономлено"
    assert inn_bridge.is_needed(enriched.model_copy(update={"inn": None})), (
        "без ИНН мост к ФНС обязан остаться нужным"
    )


@respx.mock
@pytest.mark.parametrize("junk", ["7712345678", "не указан", "—", "1234"])
async def test_a_malformed_inn_is_dropped_not_passed_on(settings: Settings, junk: str) -> None:
    """Кривой ИНН отбрасывается молча, и это не придирка к формату.

    По нему уйдут ПЛАТНЫЕ запросы в банкротство, ИП и арбитраж — и вернут либо
    чужие дела, либо пустоту, неотличимую от честного «ничего не найдено».
    Десять цифр здесь тоже мусор: это ИНН юрлица, у физлица их двенадцать.
    """
    respx.get(url__startswith=BASE).mock(return_value=answer(bd="15.03.1985", inn=junk))

    result = await _phone(settings).fetch(subject(), NO_CONTEXT)
    assert isinstance(result, PhoneNameResult)

    assert result.status is ProviderStatus.SUCCESS, "имя всё равно должно доехать"
    assert result.inn is None, f"мусорный ИНН «{junk}» просочился"


@respx.mock
async def test_the_source_never_overwrites_what_the_operator_typed(settings: Settings) -> None:
    """Названное оператором сильнее найденного мостом.

    Он смотрит в документ, мост — в чужую базу. Это правило уже действует для
    имени; ИНН и паспорт обязаны подчиняться ему же.
    """
    respx.get(url__startswith=BASE).mock(
        return_value=answer(bd="01.01.1990", inn="500100732259", passport="45 05 123456")
    )
    mine = subject(inn="770912345601", passport="1234567890")

    result = await _phone(settings).fetch(mine, NO_CONTEXT)
    assert isinstance(result, PhoneNameResult)
    update = {
        field: getattr(result, field)
        for field in ("inn", "passport")
        if getattr(result, field) is not None and getattr(mine, field) in (None, "")
    }

    assert update == {}, "мост переписал то, что ввёл оператор"


# ------------------------------------------------- ответ-свалка: поля врозь


@pytest.fixture
def dump_settings(settings: Settings, tmp_path: Path) -> Settings:
    """Настройки под поставщика, который отвечает свалкой находок по номеру.

    Карта названа его ключами — ``full_name``, ``birth_date``, ``inn``,
    ``passport``, ``snils``: именно их владелец подтвердил как верные на своём
    номере.
    """
    path = tmp_path / "dump.json"
    path.write_text(
        '{"records_path": "results", "fields": {"fio": "full_name",'
        ' "birth_date": "birth_date", "inn": "inn", "passport": "passport",'
        ' "passport_info": "passport_info", "snils": "snils"}}',
        encoding="utf-8",
    )
    return settings.model_copy(update={"phone_bridge_field_map": path})


#: Форма живого ответа depsearch, снятая с источника 08.09.2026 и обезличенная.
#: Существенно в ней ровно три вещи, и каждая ломала мост по-своему:
#: первое ``full_name`` — латиница (транслитерация из иностранной утечки),
#: настоящая личность лежит ниже; дата рождения и паспорт стоят в своей строке;
#: первый ``inn`` десятизначный, то есть юрлица.
SCATTERED = {
    "search_type": "phone",
    "phone_info": {"phone": "+79990000000", "operator": "…"},
    "results": [
        {"phone": "+79990000000", "data": "доставка"},
        {"full_name": "Ivanova Elena", "email": "e@example.test"},
        {
            "full_name": "Иванова Елена Петровна",
            "birth_date": "1984-06-25",
            "passport": "4510123456",
        },
        {"inn": "7701234567"},
        {"full_name": "Иванова Елена Петровна", "inn": "770123456789"},
    ],
}


@respx.mock
async def test_the_identity_is_gathered_across_the_whole_answer(
    dump_settings: Settings,
) -> None:
    """Поставщик отвечает свалкой находок, и личность в ней разложена по строкам.

    Раньше брался первый ряд с читаемым именем, а остальные поля доставались
    только из него. На живом ответе это давало имя без даты рождения — то есть
    ключ, по которому в выгрузке из двух тысяч человек поднимется однофамилец,
    и отчёт уедет про него.
    """
    respx.get(url__startswith=BASE).mock(return_value=Response(200, json=SCATTERED))
    bridge = build_phone_bridge(dump_settings)
    assert bridge is not None

    result = await bridge.fetch(
        SearchSubject(search_type=SearchType.PERSON.value, phone="+79990000000")
    )

    assert isinstance(result, PhoneNameResult)
    assert result.status is ProviderStatus.SUCCESS
    assert result.name is not None
    # Имя взято кириллическое, хотя латиница стояла раньше: транслитерацией в
    # русской выгрузке не найдётся никто, и бот сказал бы «не найден» про
    # человека, который в базе есть.
    assert result.name.last_name == "Иванова"
    assert result.birth_date is not None and result.birth_date.year == 1984
    assert result.passport == "4510123456"
    # Десятизначный ИНН принадлежит юрлицу; за ним ушли бы три платных запроса,
    # которые вернут чужие дела или пустоту. Взят двенадцатизначный, ниже.
    assert result.inn == "770123456789"
    # Мост записей в отчёт не приносит — он делает возможным вопрос, а не факт.
    assert not result.records


@respx.mock
async def test_a_translit_only_answer_is_still_better_than_silence(
    dump_settings: Settings,
) -> None:
    """Есть только транслитерация — отдаём её, а не молчим.

    В выгрузке по ней никто не найдётся, и бот честно попросит фамилию. Это
    лучше, чем «имя не определено» при ответившем источнике: разница между «не
    нашли» и «не спрашивали» — главное правило проекта.
    """
    respx.get(url__startswith=BASE).mock(
        return_value=Response(200, json={"results": [{"full_name": "Ivanova Elena"}]})
    )
    bridge = build_phone_bridge(dump_settings)
    assert bridge is not None

    result = await bridge.fetch(
        SearchSubject(search_type=SearchType.PERSON.value, phone="+79990000000")
    )

    assert isinstance(result, PhoneNameResult)
    assert result.status is ProviderStatus.SUCCESS
    assert result.name is not None and result.name.last_name == "Ivanova"


@respx.mock
async def test_a_passport_that_is_not_a_russian_one_is_dropped(
    dump_settings: Settings,
) -> None:
    """Загранпаспорт и иностранный к мосту ФНС не годятся — он ищет по паспорту РФ.

    Источник подписывает вид документа словами, и отличить их можно только по
    числу цифр. Пропустить негодный — купить пустой ответ ФНС за свои деньги.
    """
    respx.get(url__startswith=BASE).mock(
        return_value=Response(
            200,
            json={
                "results": [
                    {"full_name": "Иванова Елена Петровна"},
                    {"passport": "Загранпаспорт гражданина РФ 75 1234567"},
                    {"passport": "Паспорт гражданина РФ 4510 123456"},
                ]
            },
        )
    )
    bridge = build_phone_bridge(dump_settings)
    assert bridge is not None

    result = await bridge.fetch(
        SearchSubject(search_type=SearchType.PERSON.value, phone="+79990000000")
    )

    assert isinstance(result, PhoneNameResult)
    assert result.passport == "4510123456"


@respx.mock
async def test_a_snils_is_taken_only_when_its_checksum_agrees(
    dump_settings: Settings,
) -> None:
    """Одиннадцать цифр — ещё не СНИЛС, и разницу видит только контрольная сумма.

    В ответе поставщика одиннадцатизначные числа лежат в нескольких полях
    сразу: ИНН физлица — двенадцать знаков, юрлица — десять, а одиннадцать
    бывает и у внутреннего идентификатора чужой системы. Владелица уже приняла
    за свой ИНН одиннадцатизначное число из такого ответа — им оказался её же
    СНИЛС. Взять не то здесь значит вписать в заявление чужой идентификатор.
    """
    respx.get(url__startswith=BASE).mock(
        return_value=Response(
            200,
            json={
                "results": [
                    {"full_name": "Иванова Елена Петровна", "snils": "16011086812"},
                    {"snils": "160-110-868 11"},
                ]
            },
        )
    )
    bridge = build_phone_bridge(dump_settings)
    assert bridge is not None

    result = await bridge.fetch(
        SearchSubject(search_type=SearchType.PERSON.value, phone="+79990000000")
    )

    assert isinstance(result, PhoneNameResult)
    # Первое поле отвергнуто по контрольной сумме, взято второе — и оно
    # нормализовано до цифр, как паспорт.
    assert result.snils == "16011086811"


@respx.mock
async def test_a_snils_that_is_not_one_is_dropped(dump_settings: Settings) -> None:
    """Ни одного годного СНИЛСа — поле пустое, а не «почти правильное»."""
    respx.get(url__startswith=BASE).mock(
        return_value=Response(
            200,
            json={
                "results": [
                    {"full_name": "Иванова Елена Петровна"},
                    {"snils": "не указан"},
                    {"snils": "7604039395"},
                ]
            },
        )
    )
    bridge = build_phone_bridge(dump_settings)
    assert bridge is not None

    result = await bridge.fetch(
        SearchSubject(search_type=SearchType.PERSON.value, phone="+79990000000")
    )

    assert isinstance(result, PhoneNameResult)
    assert result.status is ProviderStatus.SUCCESS, "имя всё равно должно доехать"
    assert result.snils is None


# ------------------------------------------- якорь и родня: чужие в том же ответе


#: Форма живого ответа depsearch, снятая 08.09.2026 и обезличенная целиком:
#: номера и имена выдуманы, повторена только СТРУКТУРА, а она у поставщика для
#: любого номера одна и та же — это владелица и сформулировала как правило
#: («названия полей и где в каком блоке, для каждого номера это всё идентично»).
#:
#: Существенно в ней четыре вещи, и каждая ломала разбор по-своему:
#: первым идёт блок из иностранной утечки с одной латиницей;
#: личность лежит одним блоком — ФИО, дата рождения, паспорт и СНИЛС вместе;
#: рядом лежат ЧУЖИЕ люди — номером пользуются родственники и прежние владельцы;
#: дата выдачи паспорта отдельного поля не имеет и лежит текстом в ``passport_info``
#: у блоков с ДРУГИМИ документами тоже.
CROWDED = {
    "search_type": "phone",
    "results": [
        {"phone": "+79990000000", "data": "доставка"},
        {"full_name": "Ivanova Elena", "email": "e@example.test"},
        {
            "full_name": "Иванова Елена Петровна",
            "birth_date": "1984-06-25",
            "passport": "4510123456",
            "snils": "11223344595",
        },
        # Чужой человек с тем же номером телефона: другая фамилия, своя дата
        # рождения и свой паспорт. Ни одно его поле не имеет права уехать.
        {
            "full_name": "Петров Пётр Петрович",
            "birth_date": "1959-01-02",
            "passport": "4511654321",
            "inn": "770123456789",
        },
        # Блок без имени, но с тем же паспортом: это она, и ИНН здесь её.
        {"passport": "45 10 123456", "inn": "500100732259"},
        # Медицинская утечка: тот же паспорт и дата выдачи текстом.
        {"passport": "4510123456", "passport_info": "выдан ОВД, 29.01.2015"},
        # Загранпаспорт — другой документ, и дата выдачи у него своя.
        {"passport": "751234567", "passport_info": "выдан 10.10.2020"},
    ],
}


@respx.mock
async def test_the_identity_is_taken_from_its_own_block(dump_settings: Settings) -> None:
    """Личность собирается вокруг блока, где её признаки стоят вместе.

    Раньше якорем была первая запись с читаемым именем, а недостающее
    добиралось по всему ответу подряд. На такой свалке это давало личность,
    которой не существует: имя одного, дата рождения другого.
    """
    respx.get(url__startswith=BASE).mock(return_value=Response(200, json=CROWDED))
    bridge = build_phone_bridge(dump_settings)
    assert bridge is not None

    result = await bridge.fetch(
        SearchSubject(search_type=SearchType.PERSON.value, phone="+79990000000")
    )

    assert isinstance(result, PhoneNameResult)
    assert result.name is not None and result.name.last_name == "Иванова"
    assert result.birth_date is not None and result.birth_date.year == 1984
    assert result.passport == "4510123456"
    assert result.snils == "11223344595"
    # ИНН взят из блока без имени, но с ТЕМ ЖЕ паспортом: это проверяемое «тот
    # же человек», а не догадка по соседству.
    assert result.inn == "500100732259"


@respx.mock
async def test_a_stranger_sharing_the_phone_number_contributes_nothing(
    dump_settings: Settings,
) -> None:
    """Ни одно поле чужого человека не попадает в собранную личность.

    Это самая дорогая из возможных ошибок разбора и единственная, которую не
    видно по отчёту: он выглядел бы совершенно обычно. Номером телефона
    пользуются родственники и прежние владельцы номера — в живом ответе рядом с
    владелицей лежат ещё три человека.
    """
    respx.get(url__startswith=BASE).mock(return_value=Response(200, json=CROWDED))
    bridge = build_phone_bridge(dump_settings)
    assert bridge is not None

    result = await bridge.fetch(
        SearchSubject(search_type=SearchType.PERSON.value, phone="+79990000000")
    )

    assert isinstance(result, PhoneNameResult)
    assert result.passport != "4511654321", "уехал паспорт другого человека"
    assert result.inn != "770123456789", "уехал ИНН другого человека"
    assert result.birth_date is not None and result.birth_date.year != 1959


@respx.mock
async def test_the_issue_date_belongs_to_the_passport_it_was_found_with(
    dump_settings: Settings,
) -> None:
    """Дата выдачи берётся у ТОГО ЖЕ документа, а не первая найденная.

    Отдельного поля под неё поставщик не отдаёт: она лежит свободным текстом
    внутри ``passport_info``, и такие блоки есть у нескольких разных документов
    сразу. Приписать дату выдачи загранпаспорта к номеру внутреннего — ошибка,
    которую в заявлении заметит только суд.
    """
    respx.get(url__startswith=BASE).mock(return_value=Response(200, json=CROWDED))
    bridge = build_phone_bridge(dump_settings)
    assert bridge is not None

    result = await bridge.fetch(
        SearchSubject(search_type=SearchType.PERSON.value, phone="+79990000000")
    )

    assert isinstance(result, PhoneNameResult)
    assert result.passport == "4510123456"
    assert result.passport_issued is not None
    assert result.passport_issued.isoformat() == "2015-01-29"


@respx.mock
async def test_an_eleven_digit_inn_is_a_snils_and_is_refused(dump_settings: Settings) -> None:
    """Поставщик кладёт СНИЛС в поле ``inn`` — и на живом ответе это случилось.

    Владелица приняла такое число за свой ИНН, пока контрольная сумма не
    показала, что это её же СНИЛС. Пропустить его дальше — купить три платных
    ответа, которые вернут пустоту.
    """
    respx.get(url__startswith=BASE).mock(
        return_value=Response(
            200,
            json={
                "results": [
                    {"full_name": "Иванова Елена Петровна", "snils": "11223344595"},
                    {"inn": "11223344595"},
                ]
            },
        )
    )
    bridge = build_phone_bridge(dump_settings)
    assert bridge is not None

    result = await bridge.fetch(
        SearchSubject(search_type=SearchType.PERSON.value, phone="+79990000000")
    )

    assert isinstance(result, PhoneNameResult)
    assert result.status is ProviderStatus.SUCCESS
    assert result.inn is None, "одиннадцать цифр — это не ИНН"
