"""Методы NewDB поверх общего конверта.

Каждый тест здесь проверяет один и тот же инвариант с новой стороны: метод,
схему строк которого деплой не описал, не опрашивается и не выдаёт «ничего не
найдено», а любой сбой на описанном методе становится честным статусом, а не
пустым результатом.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import FedresursBackend, FNSBackend, Settings
from app.domain.enums import (
    BankruptcyStatus,
    BusinessRole,
    CourtCaseRole,
    PledgeStatus,
    ProviderStatus,
)
from app.domain.identity import SearchSubject, VehicleDescriptor
from app.domain.models import BankruptcyRecord, BusinessRelation, CourtCase, PledgeRecord
from app.providers.court import NewDBArbitrationProvider
from app.providers.fedresurs import NewDBBankruptcyProvider
from app.providers.fns import NewDBBusinessProvider
from app.providers.mapping import FieldMapError
from app.providers.newdb import NewDBFieldMaps
from app.providers.pledge import NewDBPledgeProvider

BASE_URL = "https://api.example.test"
NEWDB_URL = f"{BASE_URL}/v2"

# Placeholder row keys, matching config/field_maps/example_newdb.json. They are
# what a deployment writes down after reading its own contract; the point of the
# tests is that the adapter reads whatever the map says, not that these names
# are the real ones.
FIELD_MAP: dict[str, Any] = {
    "_comment": ["ignored"],
    "bankrot_person": {
        "fields": {
            "debtor_name": "Debtor",
            "inn": "INN",
            "case_number": "CaseNumber",
            "procedure": "Procedure",
            "status": "ProcedureStatus",
            "started_at": "ProcedureStartDate",
            "completed_at": "ProcedureEndDate",
        }
    },
    "egrul_ip": {
        "fields": {
            "inn": "INN",
            "ogrn": "OGRNIP",
            "name": "Name",
            "role": "Role",
            "status": "Status",
        },
        "value_maps": {"role": {"индивидуальный предприниматель": "ип"}},
    },
    "arbitr_person": {
        "fields": {
            "case_number": "CaseNumber",
            "court_name": "Court",
            "amount": "ClaimAmount",
            "filed_at": "FilingDate",
            "participant_name": "Participant",
            "inn": "INN",
            "role": "ParticipantRole",
            "status": "CaseStatus",
        }
    },
    "pledge_person": {
        "fields": {
            "registration_number": "NotificationNumber",
            "registered_at": "RegistrationDate",
            "terminated_at": "ExclusionDate",
            "pledgor_name": "Pledgor",
            "pledgor_birth_date": "PledgorBirthDate",
            "pledgee_name": "Pledgee",
            "subject": "PledgeSubject",
            "vin": "VIN",
        }
    },
    "pledge_vin": {
        "fields": {
            "registration_number": "NotificationNumber",
            "registered_at": "RegistrationDate",
            "pledgor_name": "Pledgor",
            "pledgee_name": "Pledgee",
            "subject": "PledgeSubject",
            "vin": "VIN",
        }
    },
}


# Пути из архивной документации, теми же именами, что в поставляемой карте.
# Здесь они нужны ради формы ответа: у половины методов строки лежат не в
# data[], а во вложенном массиве внутри строки data[].
DOCUMENTED_MAP: dict[str, Any] = {
    "bankrot_person": {
        "records_path": "bankruptcy",
        "row_fields": {
            "debtor_name": "commmon.name_or_fio",
            "inn": "commmon.inn",
        },
        "fields": {
            "case_number": "case_number",
            "status": "status",
            "source_url": "case_url",
        },
    },
    "arbitr_person": {
        "fields": {
            "case_number": "case_number",
            "status": "status",
            "participants_defendants": "participants.defendants",
            "participants_plaintiffs": "participants.plaintiffs",
        },
        "options": {"participant_name_key": "name"},
    },
    "pledge_person": {
        "records_path": "fnp",
        "fields": {
            "registration_number": "reference_number",
            "registered_at": "json_extra.registrationTime",
            "pledgor_name": "pledgor",
            "status": "message_type",
        },
        "value_maps": {"status": {"возникновение залога": "действует"}},
    },
}


def deployment(
    live_settings: Settings, tmp_path: Path, field_map: Mapping[str, Any]
) -> tuple[Settings, NewDBFieldMaps]:
    """A deployment whose contract is described by ``field_map``."""
    path = tmp_path / "documented.json"
    path.write_text(json.dumps(field_map, ensure_ascii=False), encoding="utf-8")
    settings = live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": BASE_URL,
            "newdb_method_path": "/v2",
            "newdb_field_map": path,
            "provider_max_retries": 0,
            "provider_retry_backoff_seconds": 0.0,
        }
    )
    return settings, NewDBFieldMaps.load(path)


def envelope(
    method: str,
    *,
    state: str = "complete",
    data: Sequence[Mapping[str, Any]] | None = None,
    include_results: bool = True,
    errors_info: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The NewDB async envelope, in the shape the ФССП adapter already reads."""
    payload: dict[str, Any] = {
        "params": {"method": method, "country": "ru"},
        "requestId": "00000000-0000-4000-8000-000000000001",
        "state": state,
    }
    if errors_info is not None:
        payload["errors_info"] = list(errors_info)
    if include_results and state == "complete":
        payload["results"] = {
            method: {"result": {"status": 200, "data": list(data) if data is not None else []}}
        }
    return payload


@pytest.fixture
def field_map_path(tmp_path: Path) -> Path:
    path = tmp_path / "newdb.json"
    path.write_text(json.dumps(FIELD_MAP, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def newdb_settings(live_settings: Settings, field_map_path: Path) -> Settings:
    return live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": BASE_URL,
            "newdb_method_path": "/v2",
            "newdb_poll_attempts": 2,
            "newdb_poll_interval_seconds": 0.01,
            "newdb_field_map": field_map_path,
            "provider_max_retries": 0,
            "provider_retry_backoff_seconds": 0.0,
            "fedresurs_backend": FedresursBackend.NEWDB,
            "fns_provider": FNSBackend.NEWDB,
        }
    )


@pytest.fixture
def maps(field_map_path: Path) -> NewDBFieldMaps:
    return NewDBFieldMaps.load(field_map_path)


@pytest.fixture
def inn_subject(person_subject: SearchSubject) -> SearchSubject:
    return person_subject.model_copy(update={"inn": "770912345601"})


# ---------------------------------------------------------------- field maps


def test_field_map_reads_every_described_method(maps: NewDBFieldMaps) -> None:
    assert maps.methods == {
        "bankrot_person",
        "egrul_ip",
        "arbitr_person",
        "pledge_person",
        "pledge_vin",
    }
    # Comment keys are documentation, not methods.
    assert "_comment" not in maps.methods


def test_no_field_map_means_no_methods() -> None:
    assert NewDBFieldMaps.load(None).methods == frozenset()


def test_field_map_rejects_an_entry_without_fields(tmp_path: Path) -> None:
    """An empty map would turn every row into an all-None record — a source that
    always answers "ничего не известно" while looking connected."""
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"bankrot_person": {"fields": {}}}), encoding="utf-8")

    with pytest.raises(FieldMapError):
        NewDBFieldMaps.load(path)


def test_field_map_rejects_a_non_object_entry(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"bankrot_person": "fssp_person"}), encoding="utf-8")

    with pytest.raises(FieldMapError):
        NewDBFieldMaps.load(path)


def test_field_map_rejects_row_fields_without_a_nested_array(tmp_path: Path) -> None:
    """Без вложенного массива строка ответа и есть запись.

    ``row_fields`` описывают контейнер, в котором записи лежат; там, где
    контейнера нет, они молча делали бы то же, что ``fields``, и файл
    утверждал бы про ответ то, чего в нём нет.
    """
    path = tmp_path / "broken.json"
    path.write_text(
        json.dumps(
            {"bankrot_person": {"fields": {"case_number": "n"}, "row_fields": {"inn": "i"}}}
        ),
        encoding="utf-8",
    )

    with pytest.raises(FieldMapError):
        NewDBFieldMaps.load(path)


def _method_map_with(entry: Mapping[str, Any], tmp_path: Path) -> Any:
    path = tmp_path / "one.json"
    path.write_text(json.dumps({"probe": entry}, ensure_ascii=False), encoding="utf-8")
    return NewDBFieldMaps.load(path).require("probe")


def test_a_missed_path_in_a_record_does_not_erase_the_container_value(tmp_path: Path) -> None:
    """Запись перекрывает контейнер значением, а не промахом.

    ``FieldMap.apply`` пишет каждый ключ, включая ``None``, поэтому простое
    объединение «контейнер, сверху запись» позволяло непопавшему пути записи
    затереть то, что контейнер знал. Ловушка стоит ровно там, где ею захотят
    воспользоваться: очевидное применение ``row_fields`` — «возьми из
    контейнера, если в записи нет».
    """
    method_map = _method_map_with(
        {
            "records_path": "cases",
            "row_fields": {"status": "common.status"},
            "fields": {"status": "status", "case_number": "number"},
        },
        tmp_path,
    )

    mapped = method_map.apply(
        [{"common": {"status": "из контейнера"}, "cases": [{"number": "А40-1/2025"}]}]
    )

    assert mapped.records == [{"status": "из контейнера", "case_number": "А40-1/2025"}]


def test_a_value_in_the_record_still_wins_over_the_container(tmp_path: Path) -> None:
    method_map = _method_map_with(
        {
            "records_path": "cases",
            "row_fields": {"status": "common.status"},
            "fields": {"status": "status"},
        },
        tmp_path,
    )

    mapped = method_map.apply(
        [{"common": {"status": "из контейнера"}, "cases": [{"status": "из записи"}]}]
    )

    assert mapped.records == [{"status": "из записи"}]


def test_the_container_is_read_even_when_its_array_is_empty(tmp_path: Path) -> None:
    """«Нашёл тринадцать, отдал ноль» обязано отличаться от «ничего нет».

    ФНП отвечает ``"fnp": []`` рядом с непустым ``fnp_urls``. Пока контейнер
    читался только при непустом вложенном массиве, такой ответ был неотличим от
    пустого реестра: ноль записей, ноль неразобранного, «залогов нет».
    """
    method_map = _method_map_with(
        {
            "records_path": "fnp",
            "row_fields": {"notice_urls": "fnp_urls"},
            "fields": {"registration_number": "reference_number"},
        },
        tmp_path,
    )

    mapped = method_map.apply([{"fnp": [], "fnp_urls": ["https://example.test/n/1"]}])

    assert mapped.records == []
    assert mapped.unreadable == 0
    assert mapped.containers == [{"notice_urls": ["https://example.test/n/1"]}]


@pytest.mark.parametrize(
    "array",
    [
        # Реестр, приславший уведомления строками, а не объектами.
        ["УВ-1", "УВ-2"],
        # Смесь: одно уведомление разобрано, второе — нет.
        [{"reference_number": "2025-012-1"}, "УВ-2"],
        [None, None],
    ],
)
def test_items_that_are_not_records_are_counted_as_losses(tmp_path: Path, array: Any) -> None:
    """Потеря ВНУТРИ массива — тоже потеря, и раньше её никто не считал.

    Проверялся только тип самого массива: список — значит ответ прочитан.
    ``{"fnp": ["УВ-1", "УВ-2"]}`` давало ноль записей и ноль неразобранного,
    источник отвечал «залогов не найдено», а скоринг платил за это плюс. Два
    найденных уведомления исчезали молча — это ровно инверсия, ради которой всё
    остальное в этом файле и написано.
    """
    method_map = _method_map_with(
        {
            "records_path": "fnp",
            "fields": {"registration_number": "reference_number"},
        },
        tmp_path,
    )

    mapped = method_map.apply([{"fnp": array}])

    assert mapped.unreadable == sum(1 for item in array if not isinstance(item, dict))


def test_a_container_whose_own_fields_all_missed_is_a_loss(tmp_path: Path) -> None:
    """Записи есть, а чьи они — не прочитано.

    ``row_fields`` держат личность записей. Стоит вендору переименовать блок
    (живьём он называется ``commmon``, с тремя «m»), как все пути промахнутся, а
    дела останутся — и код проставит на них идентификатор запроса, то есть
    в ответе с двумя субъектами отдаст дела второго первому. Молча.
    """
    method_map = _method_map_with(
        {
            "records_path": "bankruptcy",
            "row_fields": {"debtor_name": "commmon.name_or_fio", "inn": "commmon.inn"},
            "fields": {"case_number": "case_number"},
        },
        tmp_path,
    )

    mapped = method_map.apply(
        [{"common": {"inn": "770912345601"}, "bankruptcy": [{"case_number": "А73-1/2017"}]}]
    )

    assert mapped.records == []
    assert mapped.unreadable == 1


def test_an_empty_container_beside_an_empty_array_is_still_an_answer(tmp_path: Path) -> None:
    """Живой пустой ответ ``bankrot_person`` — это ``commmon: {}`` и ``bankruptcy: []``.

    Приписывать там нечего и некому, поэтому нечитаемым такой контейнер не
    считается: иначе каждый должник без банкротства получал бы «не проверено».
    """
    method_map = _method_map_with(
        {
            "records_path": "bankruptcy",
            "row_fields": {"debtor_name": "commmon.name_or_fio"},
            "fields": {"case_number": "case_number"},
        },
        tmp_path,
    )

    mapped = method_map.apply([{"commmon": {}, "bankruptcy": []}])

    assert mapped.records == []
    assert mapped.unreadable == 0


def test_an_empty_string_in_a_record_does_not_erase_the_container_value(tmp_path: Path) -> None:
    """Пустая строка — не значение, а её отсутствие.

    Живой ``bankrot_person`` присылает ``commmon.address: ""``; такое же поле в
    записи затирало бы прочитанное значение контейнера ничем.
    """
    method_map = _method_map_with(
        {
            "records_path": "cases",
            "row_fields": {"debtor_name": "commmon.name_or_fio"},
            "fields": {"debtor_name": "debtor", "case_number": "number"},
        },
        tmp_path,
    )

    mapped = method_map.apply(
        [
            {
                "commmon": {"name_or_fio": "Тестов Андрей Сергеевич"},
                "cases": [{"number": "А40-1/2025", "debtor": ""}],
            }
        ]
    )

    assert mapped.records == [
        {"debtor_name": "Тестов Андрей Сергеевич", "case_number": "А40-1/2025"}
    ]


def test_field_map_rejects_non_object_options(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text(
        json.dumps({"arbitr_person": {"fields": {"case_number": "n"}, "options": "name"}}),
        encoding="utf-8",
    )

    with pytest.raises(FieldMapError):
        NewDBFieldMaps.load(path)


def test_field_map_rejects_unreadable_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(FieldMapError):
        NewDBFieldMaps.load(path)


# ------------------------------------------------------- вложенные строки


@respx.mock
async def test_records_path_unwraps_the_array_inside_a_row(
    live_settings: Settings, tmp_path: Path, inn_subject: SearchSubject
) -> None:
    """Строка data[] у половины методов — контейнер, а не запись.

    ``bankrot_person`` отвечает одним объектом-персоной, дела которого лежат в
    её массиве ``bankruptcy``. Без разворачивания два дела превратились бы в
    одну запись без единого заполненного поля.
    """
    settings, maps = deployment(live_settings, tmp_path, DOCUMENTED_MAP)
    person = {
        "bankruptcy": [
            {"case_number": "А73-7992/2017", "status": "Производство по делу завершено"},
            {"case_number": "А73-1/2019", "status": "Введена процедура"},
        ],
        "commmon": {"name_or_fio": "Иванов Иван Иванович", "inn": "770912345601"},
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("bankrot_person", data=[person]))
    )

    result = await NewDBBankruptcyProvider(settings, maps).fetch(inn_subject)

    assert result.status is ProviderStatus.SUCCESS
    cases = [record for record in result.records if isinstance(record, BankruptcyRecord)]
    assert [record.case_number for record in cases] == ["А73-7992/2017", "А73-1/2019"]


@respx.mock
async def test_an_empty_nested_array_is_an_answer_not_a_schema_error(
    live_settings: Settings, tmp_path: Path, inn_subject: SearchSubject
) -> None:
    """``"bankruptcy": []`` — источник ответил: дел нет."""
    settings, maps = deployment(live_settings, tmp_path, DOCUMENTED_MAP)
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(
            200,
            json=envelope("bankrot_person", data=[{"bankruptcy": [], "commmon": {}}]),
        )
    )

    result = await NewDBBankruptcyProvider(settings, maps).fetch(inn_subject)

    assert result.status is ProviderStatus.NO_RESULTS
    assert result.status.is_answered


@respx.mock
async def test_rows_the_map_cannot_read_are_never_an_empty_register(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    """Ответ есть, карта в нём ничего не нашла — это про карту, а не про долги.

    Ровно тот случай, ради которого схемы вынесены в файл: пути в нём взяты из
    документации, а не из живого ответа, и первый же расход контракта не должен
    выглядеть как чистый реестр.
    """
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(
            200, json=envelope("bankrot_person", data=[{"совершенно": "другое"}])
        )
    )

    result = await NewDBBankruptcyProvider(newdb_settings, maps).fetch(inn_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "unexpected_schema"
    assert not result.status.is_answered


@respx.mock
async def test_a_row_whose_nested_array_is_missing_is_not_an_empty_register(
    live_settings: Settings, tmp_path: Path, inn_subject: SearchSubject
) -> None:
    settings, maps = deployment(live_settings, tmp_path, DOCUMENTED_MAP)
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(
            200, json=envelope("bankrot_person", data=[{"cases": [{"case_number": "А73-1/2019"}]}])
        )
    )

    result = await NewDBBankruptcyProvider(settings, maps).fetch(inn_subject)

    assert result.error_code == "unexpected_schema"


@pytest.mark.parametrize("instead_of_an_array", [None, 0, "нет данных", "", {"": ""}])
@respx.mock
async def test_a_nested_array_that_is_not_an_array_is_not_an_empty_register(
    live_settings: Settings,
    tmp_path: Path,
    inn_subject: SearchSubject,
    instead_of_an_array: Any,
) -> None:
    """``"bankruptcy": []`` — ответ. ``"bankruptcy": "нет данных"`` — не ответ.

    Пропажу раньше ловило только ``None``, а всё остальное — скаляр, строка,
    ноль — доходило до извлечения записей, там превращалось в пустой список и
    выходило из источника как «проверено, ничего нет». Показать «в реестре
    чисто» на строке, которую никто не смог прочитать, — это ровно та подмена,
    против которой написан весь этот механизм.
    """
    settings, maps = deployment(live_settings, tmp_path, DOCUMENTED_MAP)
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(
            200, json=envelope("bankrot_person", data=[{"bankruptcy": instead_of_an_array}])
        )
    )

    result = await NewDBBankruptcyProvider(settings, maps).fetch(inn_subject)

    assert result.error_code == "unexpected_schema"
    assert not result.status.is_answered


@respx.mock
async def test_one_unreadable_record_fails_the_call_even_if_another_parsed(
    live_settings: Settings, tmp_path: Path, person_subject: SearchSubject
) -> None:
    """Из двух уведомлений разобралось одно — показать одно нельзя.

    Раньше «не разобрано» побеждало только когда не разобралось НИЧЕГО, а
    частичная потеря уходила в лог. Отчёту нечем сказать «одно уведомление
    выпало», поэтому короткий список неотличим от полного: у должника с двумя
    залогами оператор увидел бы один и никакого предупреждения. Источник целиком
    становится «не проверено» — потеря видимая и честная, в отличие от тихой.
    """
    settings, maps = deployment(live_settings, tmp_path, DOCUMENTED_MAP)
    container = {
        "fnp": [
            {
                "reference_number": "2025-012-232030-634",
                "message_type": "Возникновение залога",
                "pledgor": "Тестов Андрей Сергеевич",
            },
            # Та же ветка, но строка другой формы: ни одного знакомого ключа.
            {"notificationNumber": "2025-012-232031-100", "debtor": "Тестов А.С."},
        ]
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("pledge_person", data=[container]))
    )

    result = await NewDBPledgeProvider(settings, maps).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "unexpected_schema"
    assert result.records == []
    assert "1 из 2" in (result.error_message or "")


@respx.mock
async def test_notices_that_arrived_as_strings_are_not_an_empty_register(
    live_settings: Settings, tmp_path: Path, person_subject: SearchSubject
) -> None:
    """Тот же счёт потерь до самого статуса источника.

    Массив на месте, тип у него правильный, а внутри — не уведомления, а
    строки. Раньше провайдер отвечал ``NO_RESULTS``: «в реестре залогов ничего
    не найдено», плюс к взыскиваемости — при двух найденных уведомлениях.
    """
    settings, maps = deployment(live_settings, tmp_path, DOCUMENTED_MAP)
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(
            200,
            json=envelope("pledge_person", data=[{"fnp": ["УВ-1", "УВ-2"]}]),
        )
    )

    result = await NewDBPledgeProvider(settings, maps).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "unexpected_schema"
    assert "2 из 2" in (result.error_message or "")


# ---------------------------------------------------------------- not configured


async def test_unmapped_method_is_not_configured(
    live_settings: Settings, person_subject: SearchSubject
) -> None:
    """A key without a row map is not an integration.

    This is the case the whole design turns on: NewDB would happily answer, and
    the adapter would have nothing to read the answer with.
    """
    settings = live_settings.model_copy(
        update={"newdb_api_key": "test-key", "newdb_base_url": BASE_URL}
    )
    provider = NewDBBankruptcyProvider(settings, NewDBFieldMaps())

    result = await provider.fetch(person_subject)

    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert not result.status.is_answered
    assert result.records == []


async def test_mapped_method_without_a_key_is_not_configured(
    live_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    result = await NewDBBankruptcyProvider(live_settings, maps).fetch(person_subject)
    assert result.status is ProviderStatus.NOT_CONFIGURED


async def test_pledge_provider_needs_at_least_one_mapped_method(
    newdb_settings: Settings, person_subject: SearchSubject
) -> None:
    provider = NewDBPledgeProvider(newdb_settings, NewDBFieldMaps())
    assert provider.mapped_methods == ()
    result = await provider.fetch(person_subject)
    assert result.status is ProviderStatus.NOT_CONFIGURED


# ---------------------------------------------------------------- банкротство


@respx.mock
async def test_bankruptcy_maps_rows_through_the_field_map(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    row = {
        "Debtor": "Тестов Андрей Сергеевич",
        "INN": "770912345601",
        "CaseNumber": "А40-118472/2026",
        "Procedure": "Реализация имущества гражданина",
        "ProcedureStatus": "Введена",
        "ProcedureStartDate": "2026-02-17",
    }
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("bankrot_person", data=[row]))
    )

    result = await NewDBBankruptcyProvider(newdb_settings, maps).fetch(inn_subject)

    assert result.status is ProviderStatus.SUCCESS
    record = result.records[0]
    assert isinstance(record, BankruptcyRecord)
    assert record.case_number == "А40-118472/2026"
    assert record.status is BankruptcyStatus.ACTIVE
    assert record.started_at == date(2026, 2, 17)

    body = json.loads(route.calls[0].request.content)
    assert body["params"]["method"] == "bankrot_person"
    # ``innfiz``, not ``inn``: the latter is the ten-digit legal-entity field.
    assert body["params"]["innfiz"] == "770912345601"
    assert "inn" not in body["params"]
    assert "lastname" not in body["params"]


@respx.mock
async def test_bankruptcy_reports_an_empty_register_as_no_results(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("bankrot_person", data=[]))
    )

    result = await NewDBBankruptcyProvider(newdb_settings, maps).fetch(inn_subject)

    # The source did answer, so this one genuinely means "checked, nothing there".
    assert result.status is ProviderStatus.NO_RESULTS
    assert result.status.is_answered


@respx.mock
async def test_bankruptcy_failed_state_is_never_an_empty_register(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    """A rejected key arrives as HTTP 200 + state=failed on every method."""
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(
            200,
            json=envelope(
                "bankrot_person",
                state="failed",
                errors_info=[{"error": "Проверьте баланс и токен доступа (X-API-KEY)"}],
            ),
        )
    )

    result = await NewDBBankruptcyProvider(newdb_settings, maps).fetch(inn_subject)

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "unauthorized"
    assert not result.status.is_answered


@respx.mock
async def test_bankruptcy_missing_result_section_is_a_schema_error(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("bankrot_person", include_results=False))
    )

    result = await NewDBBankruptcyProvider(newdb_settings, maps).fetch(inn_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "unexpected_schema"


@respx.mock
async def test_bankruptcy_record_carries_the_inn_it_was_searched_by(
    live_settings: Settings, tmp_path: Path, inn_subject: SearchSubject
) -> None:
    """Строка-дело не содержит должника — иначе дело досталось бы «никому».

    ФИО и ИНН лежат в соседнем блоке ответа, куда плоская карта не дотягивается.
    Запись без единого идентификатора матчер оценил бы как слабое совпадение, и
    настоящее банкротство исчезло бы из отчёта как чужое.

    Контейнер здесь прочитан — ФИО из него приехало, — а ИНН в нём не оказалось:
    именно этот пробел и закрывает ИНН запроса. Контейнер, из которого не
    прочиталось НИЧЕГО, — другой случай, и он честно ломает разбор: см.
    ``test_a_container_without_identity_is_reported_unchecked_not_assumed``.
    """
    settings, maps = deployment(live_settings, tmp_path, DOCUMENTED_MAP)
    row = {
        "commmon": {"name_or_fio": "Тестов Андрей Сергеевич"},
        "bankruptcy": [
            {
                "case_number": "А73-7992/2017",
                "status": "Производство по делу завершено",
                "case_url": "/legalcases/7975d0c7",
            }
        ],
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("bankrot_person", data=[row]))
    )

    result = await NewDBBankruptcyProvider(settings, maps).fetch(inn_subject)

    record = result.records[0]
    assert isinstance(record, BankruptcyRecord)
    assert record.inn == "770912345601"
    # Относительный путь Федресурса — не ссылка; в отчёт он идёт с хостом.
    assert record.source_url == "https://fedresurs.ru/legalcases/7975d0c7"


@respx.mock
async def test_a_legal_entity_inn_is_never_sent_as_innfiz(
    newdb_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    """Десятизначный ИНН — это ИНН юрлица, и ``innfiz`` его отвергает.

    Отправить его значило бы купить отказ вместо честного «искать нечем».
    """
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("bankrot_person", data=[]))
    )
    subject = person_subject.model_copy(update={"inn": "7709123456"})

    result = await NewDBBankruptcyProvider(newdb_settings, maps).fetch(subject)

    assert result.error_code == "insufficient_query"
    assert not route.calls


@respx.mock
async def test_bankruptcy_without_inn_is_not_queried(
    newdb_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    """ФИО с датой рождения метод не принимает — спрашивать нечем.

    Живой эндпоинт отвечает на person-блок ``Отсутствует обязательный параметр:
    innfiz``. Отправить запрос всё равно значило бы получить отказ и показать
    его как чистый реестр — ровно та подмена, против которой написан проект.
    """
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("bankrot_person", data=[]))
    )

    result = await NewDBBankruptcyProvider(newdb_settings, maps).fetch(person_subject)

    assert result.error_code == "insufficient_query"
    assert not result.status.is_answered
    assert not route.calls  # платный вызов не потрачен на заведомый отказ


# ---------------------------------------------------------------- ИП


@respx.mock
async def test_sole_proprietor_status_is_mapped(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    row = {
        "INN": "770912345601",
        "OGRNIP": "316774600000000",
        "Name": "ИП Тестов Андрей Сергеевич",
        "Role": "Индивидуальный предприниматель",
        "Status": "Действует",
    }
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("egrul_ip", data=[row]))
    )

    result = await NewDBBusinessProvider(newdb_settings, maps).fetch(inn_subject)

    assert result.status is ProviderStatus.SUCCESS
    record = result.records[0]
    assert isinstance(record, BusinessRelation)
    assert record.role is BusinessRole.SOLE_PROPRIETOR
    assert record.is_active_sole_proprietor
    assert json.loads(route.calls[0].request.content)["params"]["innfiz"] == "770912345601"


async def test_sole_proprietor_needs_an_identifier(
    newdb_settings: Settings, maps: NewDBFieldMaps
) -> None:
    subject = SearchSubject(search_type="contract", contract_number="EV-1")

    result = await NewDBBusinessProvider(newdb_settings, maps).fetch(subject)

    assert result.error_code == "insufficient_query"


@respx.mock
async def test_sole_proprietor_is_not_searched_by_name(
    newdb_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    """ФИО с датой рождения здесь было запасным путём — метод его не принимает."""
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("egrul_ip", data=[]))
    )

    result = await NewDBBusinessProvider(newdb_settings, maps).fetch(person_subject)

    assert result.error_code == "insufficient_query"
    assert not result.status.is_answered
    assert not route.calls


# ---------------------------------------------------------------- арбитраж


async def test_arbitration_without_inn_is_not_searched_by_name(
    newdb_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    """Поиск по одному ФИО вернул бы чужие дела — и мы бы сняли должника с иска."""
    result = await NewDBArbitrationProvider(newdb_settings, maps).fetch(person_subject)

    assert result.error_code == "insufficient_query"
    assert not result.status.is_answered


@respx.mock
async def test_arbitration_reads_role_and_amount(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    row = {
        "CaseNumber": "А40-227414/2026",
        "Court": "Арбитражный суд города Москвы",
        "ClaimAmount": "1 180 400,00",
        "FilingDate": "2026-05-20",
        "Participant": "Тестов Андрей Сергеевич",
        "INN": "770912345601",
        "ParticipantRole": "Ответчик",
        "CaseStatus": "Рассмотрение по существу",
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("arbitr_person", data=[row]))
    )

    result = await NewDBArbitrationProvider(newdb_settings, maps).fetch(inn_subject)

    record = result.records[0]
    assert isinstance(record, CourtCase)
    assert record.role is CourtCaseRole.DEFENDANT
    assert record.amount == Decimal("1180400.00")
    assert record.is_active
    assert record.is_against_debtor


@respx.mock
async def test_arbitration_skips_a_row_without_a_case_number(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(
            200, json=envelope("arbitr_person", data=[{"Court": "Арбитражный суд"}])
        )
    )

    result = await NewDBArbitrationProvider(newdb_settings, maps).fetch(inn_subject)

    assert result.status is ProviderStatus.NO_RESULTS


@respx.mock
async def test_arbitration_role_comes_from_the_participant_lists(
    live_settings: Settings, tmp_path: Path, inn_subject: SearchSubject
) -> None:
    """У КАД нет поля роли — есть списки истцов и ответчиков.

    Роль решает, попадёт ли дело в «иски к должнику», то есть в конкурентов за
    его имущество. Пока она не определялась, пустой список исков к должнику был
    неотличим от «исков к нему нет» — и приносил в скоринг плюс.
    """
    settings, maps = deployment(live_settings, tmp_path, DOCUMENTED_MAP)
    row = {
        "case_number": "А57-10442/2025",
        "status": "Рассматривается в первой инстанции",
        "participants": {
            "plaintiffs": [{"name": "ПАО Сбербанк"}],
            "defendants": [{"name": "Тестов Андрей Сергеевич"}],
        },
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("arbitr_person", data=[row]))
    )

    result = await NewDBArbitrationProvider(settings, maps).fetch(inn_subject)

    record = result.records[0]
    assert isinstance(record, CourtCase)
    assert record.role is CourtCaseRole.DEFENDANT
    assert record.is_against_debtor
    # Дело найдено по ИНН должника — этот ИНН и стоит на записи.
    assert record.inn == "770912345601"


@respx.mock
async def test_arbitration_role_survives_an_abbreviated_participant(
    live_settings: Settings, tmp_path: Path, inn_subject: SearchSubject
) -> None:
    """КАД сокращает участников, и роль не должна на этом теряться.

    В примере документации третьи лица записаны как «Бычков Д.Ю.» — точному
    равенству строк такое имя не равно ничему. Промах давал роль OTHER, дело
    выпадало из исков к должнику, и скоринг начислял плюс «исков не найдено»,
    печатая при этом само дело в отчёте выше.
    """
    settings, maps = deployment(live_settings, tmp_path, DOCUMENTED_MAP)
    row = {
        "case_number": "А57-10442/2025",
        "status": "Рассматривается в первой инстанции",
        "participants": {
            "plaintiffs": [{"name": "ПАО Сбербанк"}],
            "defendants": [{"name": "ИП Тестов А.С."}],
        },
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("arbitr_person", data=[row]))
    )

    result = await NewDBArbitrationProvider(settings, maps).fetch(inn_subject)

    record = result.records[0]
    assert isinstance(record, CourtCase)
    assert record.role is CourtCaseRole.DEFENDANT
    assert record.is_against_debtor


@respx.mock
async def test_the_key_holding_a_participant_name_comes_from_the_map(
    live_settings: Settings, tmp_path: Path, inn_subject: SearchSubject
) -> None:
    """Путь до списка участников живёт в карте — имя ключа внутри тоже.

    ``"name"`` — то, что показывает документация, и не более того. Деплою,
    контракт которого зовёт этот ключ иначе, захардкоженное имя стоило бы роли
    по каждому делу, причём молча.
    """
    field_map = copy.deepcopy(DOCUMENTED_MAP)
    field_map["arbitr_person"]["options"] = {"participant_name_key": "party.full_name"}
    settings, maps = deployment(live_settings, tmp_path, field_map)
    row = {
        "case_number": "А57-10442/2025",
        "participants": {
            "defendants": [{"party": {"full_name": "Тестов Андрей Сергеевич"}}],
        },
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("arbitr_person", data=[row]))
    )

    result = await NewDBArbitrationProvider(settings, maps).fetch(inn_subject)

    record = result.records[0]
    assert isinstance(record, CourtCase)
    assert record.role is CourtCaseRole.DEFENDANT


@respx.mock
async def test_a_stranger_in_the_participant_list_does_not_make_a_role(
    live_settings: Settings, tmp_path: Path, inn_subject: SearchSubject
) -> None:
    """Свобода в написании имени — не свобода совпадений.

    Дело нашлось по ИНН должника, но в списках его самого нет: он проходит
    третьим лицом или дело досталось нам за компанию. Роль остаётся OTHER, и в
    иски к должнику дело не попадает.
    """
    settings, maps = deployment(live_settings, tmp_path, DOCUMENTED_MAP)
    row = {
        "case_number": "А57-10442/2025",
        "participants": {
            "plaintiffs": [{"name": "ПАО Сбербанк"}],
            "defendants": [{"name": "Тестова Мария Сергеевна"}, {"name": "Тестов П.С."}],
        },
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("arbitr_person", data=[row]))
    )

    result = await NewDBArbitrationProvider(settings, maps).fetch(inn_subject)

    record = result.records[0]
    assert isinstance(record, CourtCase)
    assert record.role is CourtCaseRole.OTHER
    assert not record.is_against_debtor


@respx.mock
@pytest.mark.parametrize(
    "defendant",
    [
        # Однофамилец с чужим отчеством: раньше это было «коротким совпадением»,
        # и должник становился ответчиком по чужому иску.
        "Тестов Андрей Петрович",
        # Строка, которую нельзя прочитать как имя. КАД так и пишет, когда
        # адрес и лицо скрыты.
        "Данные скрыты",
        # Одна фамилия — это не человек.
        "Тестов",
    ],
)
async def test_a_participant_who_is_not_our_debtor_does_not_make_a_role(
    live_settings: Settings, tmp_path: Path, inn_subject: SearchSubject, defendant: str
) -> None:
    """Роль назначает доказательство, а не «что угодно, кроме противоречия».

    Проверка была ``compare_names(...) is not NameMatch.NONE``, а ``NONE``
    возвращалось только при прямом противоречии: нечитаемая строка в списке
    ответчиков делала должника ответчиком по чужому делу, и −10 в скоринге
    доставались ему за чужой иск.
    """
    settings, maps = deployment(live_settings, tmp_path, DOCUMENTED_MAP)
    row = {
        "case_number": "А57-10442/2025",
        "participants": {
            "plaintiffs": [{"name": "ПАО Сбербанк"}],
            "defendants": [{"name": defendant}],
        },
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("arbitr_person", data=[row]))
    )

    result = await NewDBArbitrationProvider(settings, maps).fetch(inn_subject)

    record = result.records[0]
    assert isinstance(record, CourtCase)
    assert record.role is CourtCaseRole.OTHER
    assert not record.is_against_debtor


@respx.mock
async def test_arbitration_marks_a_decided_case_closed(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    row = {
        "CaseNumber": "А40-1/2025",
        "Participant": "Тестов Андрей Сергеевич",
        "ParticipantRole": "Ответчик",
        "CaseStatus": "Дело рассмотрено",
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("arbitr_person", data=[row]))
    )

    result = await NewDBArbitrationProvider(newdb_settings, maps).fetch(inn_subject)

    record = result.records[0]
    assert isinstance(record, CourtCase)
    assert not record.is_active
    assert not record.is_against_debtor


# ---------------------------------------------------------------- залоги


@respx.mock
async def test_pledge_by_person_is_active_until_excluded(
    newdb_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    row = {
        "NotificationNumber": "2022-006-123456-789",
        "RegistrationDate": "2022-04-11",
        "Pledgor": "Тестов Андрей Сергеевич",
        "PledgorBirthDate": "1985-03-12",
        "Pledgee": 'АО "Демонстрационный банк"',
        "PledgeSubject": "Автомобиль LADA VESTA, 2021",
        "VIN": "xta1234567890abcd",
    }
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("pledge_person", data=[row]))
    )

    result = await NewDBPledgeProvider(newdb_settings, maps).fetch(person_subject)

    assert result.status is ProviderStatus.SUCCESS
    record = result.records[0]
    assert isinstance(record, PledgeRecord)
    assert record.status is PledgeStatus.ACTIVE
    assert record.is_active
    assert record.vin == "XTA1234567890ABCD"
    assert record.pledgor_birth_date == date(1985, 3, 12)

    body = json.loads(route.calls[0].request.content)
    assert body["params"]["method"] == "pledge_person"


@respx.mock
async def test_pledge_by_person_sends_the_date_of_birth_this_method_documents(
    live_settings: Settings, tmp_path: Path, person_subject: SearchSubject
) -> None:
    """``datebirth``, а не ``dob``: у pledge_person параметр зовётся иначе.

    Заодно проверяется вся строка ФНП: уведомления лежат во вложенном ``fnp``,
    дата регистрации приходит временем, а состояния у записи нет — есть тип
    сообщения, который карта переводит в «действует».
    """
    settings, maps = deployment(live_settings, tmp_path, DOCUMENTED_MAP)
    container = {
        "fnp": [
            {
                "reference_number": "2025-012-232030-634",
                "message_type": "Возникновение залога",
                "pledgor": "Тестов Андрей Сергеевич",
                "json_extra": {"registrationTime": "2025-11-24T11:04:56"},
            }
        ],
        "fedresurs": [],
    }
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("pledge_person", data=[container]))
    )

    result = await NewDBPledgeProvider(settings, maps).fetch(person_subject)

    record = result.records[0]
    assert isinstance(record, PledgeRecord)
    assert record.status is PledgeStatus.ACTIVE
    assert record.registered_at == date(2025, 11, 24)

    params = json.loads(route.calls[0].request.content)["params"]
    assert params["datebirth"] == "1985-03-12"
    assert "dob" not in params


@respx.mock
async def test_excluded_pledge_is_not_active(
    newdb_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    row = {
        "NotificationNumber": "2022-006-123456-789",
        "RegistrationDate": "2022-04-11",
        "ExclusionDate": "2025-06-01",
        "Pledgor": "Тестов Андрей Сергеевич",
        "PledgeSubject": "Автомобиль LADA VESTA, 2021",
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("pledge_person", data=[row]))
    )

    result = await NewDBPledgeProvider(newdb_settings, maps).fetch(person_subject)

    record = result.records[0]
    assert isinstance(record, PledgeRecord)
    assert record.status is PledgeStatus.TERMINATED
    assert not record.is_active


@respx.mock
async def test_pledge_by_vin_uses_the_vin_method(
    newdb_settings: Settings, maps: NewDBFieldMaps
) -> None:
    subject = SearchSubject(
        search_type="vin",
        vehicle=VehicleDescriptor(vin="XTA1234567890ABCD"),
    )
    row = {
        "NotificationNumber": "2022-006-123456-789",
        "RegistrationDate": "2022-04-11",
        "Pledgee": 'АО "Демонстрационный банк"',
        "PledgeSubject": "Автомобиль LADA VESTA, 2021",
        "VIN": "XTA1234567890ABCD",
    }
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("pledge_vin", data=[row]))
    )

    result = await NewDBPledgeProvider(newdb_settings, maps).fetch(subject)

    assert result.status is ProviderStatus.SUCCESS
    body = json.loads(route.calls[0].request.content)
    assert body["params"]["method"] == "pledge_vin"
    assert body["params"]["vin"] == "XTA1234567890ABCD"


@respx.mock
async def test_pledge_deduplicates_across_both_methods(
    newdb_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    """Both searches legitimately return the same notice; it is one pledge."""
    subject = person_subject.model_copy(
        update={"vehicle": VehicleDescriptor(vin="XTA1234567890ABCD")}
    )
    row = {
        "NotificationNumber": "2022-006-123456-789",
        "RegistrationDate": "2022-04-11",
        "Pledgor": "Тестов Андрей Сергеевич",
        "PledgeSubject": "Автомобиль LADA VESTA, 2021",
        "VIN": "XTA1234567890ABCD",
    }
    respx.post(NEWDB_URL).mock(
        side_effect=[
            httpx.Response(200, json=envelope("pledge_vin", data=[row])),
            httpx.Response(200, json=envelope("pledge_person", data=[row])),
        ]
    )

    result = await NewDBPledgeProvider(newdb_settings, maps).fetch(subject)

    assert len(result.records) == 1


async def test_pledge_with_an_unmapped_method_is_a_config_gap_not_a_data_gap(
    newdb_settings: Settings, person_subject: SearchSubject, tmp_path: Path
) -> None:
    """Искать есть по чему, но метод не описан — это про настройку, не про данные."""
    path = tmp_path / "vin-only.json"
    path.write_text(json.dumps({"pledge_vin": {"fields": {"vin": "VIN"}}}), encoding="utf-8")
    provider = NewDBPledgeProvider(newdb_settings, NewDBFieldMaps.load(path))

    # Человека искать есть по чему, но описан только метод по VIN.
    result = await provider.fetch(person_subject)

    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert result.error_code != "insufficient_query"


async def test_pledge_without_vin_or_identity_is_not_queried(
    newdb_settings: Settings, maps: NewDBFieldMaps
) -> None:
    subject = SearchSubject(search_type="contract", contract_number="EV-1")

    result = await NewDBPledgeProvider(newdb_settings, maps).fetch(subject)

    assert result.error_code == "insufficient_query"


# ---------------------------------------------------------------- extra params


@respx.mock
async def test_extra_params_override_what_the_adapter_would_send(
    live_settings: Settings, tmp_path: Path, inn_subject: SearchSubject
) -> None:
    """The escape hatch for a contract that names a parameter differently."""
    path = tmp_path / "newdb.json"
    path.write_text(
        json.dumps(
            {
                "bankrot_person": {
                    "fields": {"case_number": "CaseNumber"},
                    "extra_params": {"country": "kz", "source": "efrsb"},
                }
            }
        ),
        encoding="utf-8",
    )
    settings = live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": BASE_URL,
            "newdb_field_map": path,
            "provider_max_retries": 0,
        }
    )
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("bankrot_person", data=[]))
    )

    await NewDBBankruptcyProvider(settings, NewDBFieldMaps.load(path)).fetch(inn_subject)

    body = json.loads(route.calls[0].request.content)
    assert body["params"]["country"] == "kz"
    assert body["params"]["source"] == "efrsb"
    # Everything the adapter derives from the subject still travels.
    assert body["params"]["innfiz"] == "770912345601"


# ---------------------------------------------------------------- сборка


def test_registry_replaces_the_stubs_with_the_mapped_sources(
    newdb_settings: Settings,
) -> None:
    """Заглушка обязана уйти, а не встать рядом.

    Отчёт адресуется по имени источника, поэтому две записи под одним именем
    означали бы, что одну из них никто никогда не прочитает.
    """
    from app.domain.enums import ProviderName
    from app.providers.court import NewDBArbitrationProvider as Arbitration
    from app.providers.fedresurs import NewDBBankruptcyProvider as Bankruptcy
    from app.providers.fns import NewDBBusinessProvider as Business
    from app.providers.pledge import NewDBPledgeProvider as Pledge
    from app.providers.registry import build_external_providers

    providers = build_external_providers(newdb_settings)
    by_name = {provider.name: provider for provider in providers}

    assert len(by_name) == len(providers), "источник зарегистрирован дважды"
    assert isinstance(by_name[ProviderName.FEDRESURS], Bankruptcy)
    assert isinstance(by_name[ProviderName.FNS], Business)
    assert isinstance(by_name[ProviderName.PLEDGE], Pledge)
    assert isinstance(by_name[ProviderName.COURT], Arbitration)
    assert {ProviderName.PROPERTY, ProviderName.INHERITANCE} <= set(by_name)


def test_registry_reports_the_mapped_methods_as_configured(
    newdb_settings: Settings,
) -> None:
    from app.domain.enums import ProviderName
    from app.providers.registry import build_external_providers

    configured = {
        provider.name
        for provider in build_external_providers(newdb_settings)
        if provider.is_configured
    }

    assert {
        ProviderName.FSSP,
        ProviderName.FEDRESURS,
        ProviderName.FNS,
        ProviderName.PLEDGE,
        ProviderName.COURT,
    } <= configured


def test_registry_without_a_field_map_leaves_the_new_sources_unconnected(
    live_settings: Settings,
) -> None:
    from app.domain.enums import ProviderName
    from app.providers.registry import build_external_providers

    settings = live_settings.model_copy(
        update={"newdb_api_key": "test-key", "newdb_base_url": BASE_URL}
    )
    by_name = {provider.name: provider for provider in build_external_providers(settings)}

    # ФССП не зависит от карты: его строки разобраны по проверенной схеме.
    assert by_name[ProviderName.FSSP].is_configured
    assert not by_name[ProviderName.PLEDGE].is_configured
    assert not by_name[ProviderName.COURT].is_configured


def test_shipped_example_map_describes_every_documented_method() -> None:
    """Пример в репозитории и код не должны разъезжаться."""
    from app.providers.court import NEWDB_METHOD as ARBITRATION_METHOD
    from app.providers.fedresurs import NEWDB_METHOD as BANKRUPTCY_METHOD
    from app.providers.fns import NEWDB_METHOD as BUSINESS_METHOD
    from app.providers.pledge import PERSON_METHOD, VIN_METHOD

    example = NewDBFieldMaps.load(Path("config/field_maps/example_newdb.json"))

    assert example.methods == {
        BANKRUPTCY_METHOD,
        ARBITRATION_METHOD,
        BUSINESS_METHOD,
        PERSON_METHOD,
        VIN_METHOD,
    }
    # egrul_ip появился 05.09.2026, когда его строки увидели живьём. Раньше его
    # здесь не было намеренно — архивная документация показывала только пустой
    # ответ и описывала строку прозой, без единого имени ключа.
    entry = example.require(BUSINESS_METHOD)
    assert entry.field_map.records_path == "matches"
    # Роль закодирована кодом раздела и без словаря становится «иной ролью»:
    # руководитель ЮЛ, показанный «иной связью», — это потеря смысла находки.
    assert entry.field_map.value_maps["role"]["upr"] == "руководитель"
    assert entry.field_map.value_maps["role"]["uchr"] == "учредитель"
