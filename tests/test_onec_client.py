"""Транспорт 1С OData — то единственное, что можно проверить без базы.

Протокол опубликован фирмой «1С» и одинаков у всех конфигураций, поэтому он в
коде и проверяется здесь целиком. Имена коллекций и реквизитов в фикстурах
намеренно синтетические (``Catalog_ПРИМЕР_…``): правдоподобные
``Catalog_Контрагенты`` и ``ИНН`` создали бы иллюзию проверенной интеграции —
ту же самую догадку, только зафиксированную зелёным прогоном.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

from app.providers.base import ProviderError
from app.providers.http import ProviderAuthError, ProviderBadResponseError, RetryPolicy
from app.providers.onec.client import (
    ODataQuery,
    OneCODataClient,
    build_query,
    quote_literal,
    select_from_paths,
    unwrap,
)

BASE_URL = "https://1c.example.test/base/odata/standard.odata"
COLLECTION = "Catalog_ПРИМЕР_Должники"
SELECT = ("ПРИМЕР_КодДолжника", "Description")


def make_client(
    *,
    page_size: int = 2,
    max_pages: int = 3,
    concurrency: int = 2,
    cache_ttl_seconds: int = 0,
    max_retries: int = 0,
) -> OneCODataClient:
    return OneCODataClient(
        BASE_URL,
        "reader",
        "secret",
        timeout_seconds=5,
        page_size=page_size,
        max_pages=max_pages,
        concurrency=concurrency,
        cache_ttl_seconds=cache_ttl_seconds,
        retry=RetryPolicy(max_retries=max_retries, backoff_seconds=0.0),
    )


def envelope(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"odata.metadata": f"{BASE_URL}/$metadata#{COLLECTION}", "value": rows}


def query(filter_expr: str = "ПРИМЕР_КодДолжника eq '42'") -> ODataQuery:
    return ODataQuery(collection=COLLECTION, filter_expr=filter_expr, select=SELECT)


def rows(count: int, *, start: int = 0) -> list[dict[str, Any]]:
    return [
        {"ПРИМЕР_КодДолжника": str(start + index), "Description": "Иванов Иван"}
        for index in range(count)
    ]


# ---------------------------------------------------------------- 1. сборка URL


def test_build_query_keeps_dollar_literal_and_percent_encodes_cyrillic() -> None:
    """Строка сравнивается целиком: именно она уходит в сеть побайтово."""
    assert build_query(
        "Catalog_Тест",
        filter_expr="Имя eq 'Ва''ся'",
        select=("Имя", "Код"),
        orderby="Ref_Key",
        top=100,
        skip=200,
    ) == (
        "Catalog_%D0%A2%D0%B5%D1%81%D1%82"
        "?$format=json"
        "&$select=%D0%98%D0%BC%D1%8F,%D0%9A%D0%BE%D0%B4"
        "&$filter=%D0%98%D0%BC%D1%8F%20eq%20'%D0%92%D0%B0''%D1%81%D1%8F'"
        "&$orderby=Ref_Key"
        "&$top=100&$skip=200"
    )


def test_build_query_encodes_spaces_as_percent20_never_plus() -> None:
    assert "%20" in build_query("C", filter_expr="A eq 'b c'")
    assert "+" not in build_query("C", filter_expr="A eq 'b c'")


# ---------------------------------------------------------------- 2. регрессия


@respx.mock
async def test_request_carries_a_literal_dollar_filter() -> None:
    """Самый важный тест файла: молчаливая потеря фильтра.

    ``params=`` в httpx кодирует ``$filter`` как ``%24filter`` и пробел как
    ``+``. Если 1С разбирает query до percent-декодирования, системный параметр
    не опознаётся, фильтр отбрасывается — и вместо одного должника приходит
    начало справочника. Это не ошибка запроса, это посторонние люди в отчёте.
    """
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, json=envelope(rows(1)))
    )

    await make_client().fetch_rows("by_debtor_id", query())

    url = str(respx.calls[0].request.url)
    assert "$filter=" in url
    assert "%24filter" not in url
    assert "$format=json" in url
    assert "+" not in httpx.URL(url).query.decode()


# ---------------------------------------------------------------- 3. литералы


def test_quote_literal_doubles_the_apostrophe() -> None:
    """Иначе фамилия О'Коннор закрывает строковый литерал на середине."""
    assert quote_literal("О'Коннор") == "%D0%9E%27%27%D0%9A%D0%BE%D0%BD%D0%BD%D0%BE%D1%80"


def test_injected_predicate_stays_inside_one_string_literal() -> None:
    hostile = "x' or 1 eq 1 or '"
    built = build_query(COLLECTION, filter_expr=f"Поле eq '{quote_literal(hostile)}'")
    # Ни одной кавычки, закрывающей литерал раньше времени, и ни одного
    # пробела вне процентного кодирования: второго предиката не возникло.
    assert "$filter=%D0%9F%D0%BE%D0%BB%D0%B5%20eq%20'x%27%20or%201%20eq%201%20or%20%27'" in built
    assert built.count("'") == 2


# ---------------------------------------------------------------- 4. конверт


def test_empty_value_is_an_honest_zero() -> None:
    assert unwrap(envelope([])) == []


def test_envelope_without_value_is_not_an_empty_result() -> None:
    with pytest.raises(ProviderBadResponseError) as exc_info:
        unwrap({"odata.metadata": "…"})
    assert exc_info.value.code == "unexpected_schema"


def test_value_that_is_not_a_list_is_unexpected_schema() -> None:
    with pytest.raises(ProviderBadResponseError) as exc_info:
        unwrap({"value": {"Ref_Key": "x"}})
    assert exc_info.value.code == "unexpected_schema"


@respx.mock
async def test_atom_xml_body_is_loud() -> None:
    """Без ``$format=json`` 1С отвечает Atom XML. Это не пустой ответ."""
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(
            200, text="<feed xmlns='http://www.w3.org/2005/Atom'></feed>"
        )
    )
    with pytest.raises(ProviderBadResponseError) as exc_info:
        await make_client().fetch_rows("by_debtor_id", query())
    assert exc_info.value.code == "malformed_json"


# ---------------------------------------------------------------- 5-6. страницы


@respx.mock
async def test_pagination_stops_on_the_first_short_page() -> None:
    pages = [
        httpx.Response(200, json=envelope(rows(2, start=0))),
        httpx.Response(200, json=envelope(rows(2, start=2))),
        httpx.Response(200, json=envelope(rows(1, start=4))),
    ]
    respx.get(url__startswith=BASE_URL).mock(side_effect=pages)

    found = await make_client(page_size=2, max_pages=5).fetch_rows("by_fio", query())

    assert len(found) == 5
    skips = [httpx.URL(str(call.request.url)).params.get("$skip") for call in respx.calls]
    assert skips == ["0", "2", "4"]
    # Постраничность без сортировки может и потерять строку, и выдать её дважды.
    assert all("$orderby=Ref_Key" in str(call.request.url) for call in respx.calls)


@respx.mock
async def test_hitting_the_page_ceiling_is_an_error_not_a_truncated_result() -> None:
    """Усечённая выборка выглядит как полная — и читается как полная."""
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, json=envelope(rows(2)))
    )
    with pytest.raises(ProviderBadResponseError) as exc_info:
        await make_client(page_size=2, max_pages=3).fetch_rows("by_fio", query())
    assert exc_info.value.code == "page_limit_exceeded"
    assert len(respx.calls) == 3


# ---------------------------------------------------------------- 7. ошибки


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "not_published"),
        (400, "filter_rejected"),
        (401, "unauthorized"),
        (403, "unauthorized"),
    ],
)
@respx.mock
async def test_http_errors_never_become_an_empty_list(status: int, code: str) -> None:
    respx.get(url__startswith=BASE_URL).mock(return_value=httpx.Response(status))
    with pytest.raises(ProviderError) as exc_info:
        await make_client().fetch_rows("by_debtor_id", query())
    assert exc_info.value.code == code


@respx.mock
async def test_server_error_is_retried_then_reported() -> None:
    route = respx.get(url__startswith=BASE_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(ProviderError) as exc_info:
        await make_client(max_retries=2).fetch_rows("by_debtor_id", query())
    assert exc_info.value.code == "server_error"
    assert route.call_count == 3


@respx.mock
async def test_timeout_is_reported_as_timeout() -> None:
    respx.get(url__startswith=BASE_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as exc_info:
        await make_client().fetch_rows("by_debtor_id", query())
    assert exc_info.value.code == "timeout"


@pytest.mark.parametrize("status", [401, 404])
@respx.mock
async def test_deterministic_failures_are_not_retried(status: int) -> None:
    """Повтор запроса к 401 и 404 ничего не чинит и только шумит в чужой базе."""
    route = respx.get(url__startswith=BASE_URL).mock(return_value=httpx.Response(status))
    with pytest.raises((ProviderAuthError, ProviderBadResponseError)):
        await make_client(max_retries=2).fetch_rows("by_debtor_id", query())
    assert route.call_count == 1


# ---------------------------------------------------------------- 8. кэш


@respx.mock
async def test_identical_lookups_hit_the_process_cache() -> None:
    route = respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, json=envelope(rows(1)))
    )
    client = make_client(cache_ttl_seconds=60)

    await client.fetch_rows("by_debtor_id", query())
    await client.fetch_rows("by_debtor_id", query())

    assert route.call_count == 1


@respx.mock
async def test_cache_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    route = respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, json=envelope(rows(1)))
    )
    client = make_client(cache_ttl_seconds=60)
    clock = [1000.0]
    monkeypatch.setattr("app.providers.onec.client.time.monotonic", lambda: clock[0])

    await client.fetch_rows("by_debtor_id", query())
    clock[0] += 61.0
    await client.fetch_rows("by_debtor_id", query())

    assert route.call_count == 2


@respx.mock
async def test_a_different_filter_is_a_different_cache_entry() -> None:
    route = respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, json=envelope(rows(1)))
    )
    client = make_client(cache_ttl_seconds=60)

    await client.fetch_rows("by_debtor_id", query("Поле eq '1'"))
    await client.fetch_rows("by_debtor_id", query("Поле eq '2'"))

    assert route.call_count == 2


# ---------------------------------------------------------------- 9. семафор


@respx.mock
async def test_concurrency_is_capped() -> None:
    """Сеансы 1С — ресурс заказчика, а не наш."""
    in_flight = 0
    peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, json=envelope(rows(1)))

    respx.get(url__startswith=BASE_URL).mock(side_effect=handler)
    client = make_client(concurrency=2)

    await asyncio.gather(
        *(client.fetch_rows(f"lookup_{index}", query(f"Поле eq '{index}'")) for index in range(6))
    )

    assert peak <= 2
    assert len(respx.calls) == 6


# ---------------------------------------------------------------- $select


def test_select_is_derived_from_the_top_segment_of_each_path() -> None:
    assert select_from_paths(
        ["ПРИМЕР_Код", "ПРИМЕР_Контрагент.Description", "ПРИМЕР_Контрагент.ИНН"]
    ) == ("ПРИМЕР_Код", "ПРИМЕР_Контрагент")
