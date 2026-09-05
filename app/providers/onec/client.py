"""Транспорт 1С OData. Только протокол — ни одного имени из конфигурации.

Что здесь захардкожено, потому что это опубликованный фирмой «1С» интерфейс,
одинаковый у всех конфигураций платформы 8.3:

*   Точка публикации ``<база>/odata/standard.odata/``; коллекции адресуются как
    ``Catalog_Имя``, ``Document_Имя``, ``InformationRegister_Имя``.
*   Системные параметры ``$format``, ``$select``, ``$filter``, ``$orderby``,
    ``$top``, ``$skip``; кириллические имена уходят в URL как percent-UTF-8.
*   Ответ — конверт ``{"odata.metadata": ..., "value": [...]}``.
*   Аутентификация — HTTP Basic.
*   Строковый литерал в ``$filter`` — в одинарных кавычках, внутренняя кавычка
    удваивается.

Чего здесь нет и быть не может: имён справочников, реквизитов и шаблонов
фильтров. Они разные в УТ, ERP, УНФ и в самописных конфигурациях, у нас нет ни
одного образца, и потому они живут в карте (:mod:`app.providers.onec.lookup_map`).

**Почему URL собирается вручную, а не через ``params=``.** ``httpx`` кодирует
ключи параметров: ``$filter`` превращается в ``%24filter``, а пробел — в ``+``.
Если 1С разбирает query-строку до percent-декодирования, системные параметры не
опознаются, фильтр отбрасывается — и сервер отдаёт начало справочника. Это не
ошибка запроса: это посторонние люди в отчёте о должнике. Готовую же строку
``httpx`` сохраняет побайтово, поэтому запрос собирается здесь и передаётся в
:func:`~app.providers.http.request_json` как ``url`` с ``params=None``: так
бесплатно достаются общие ретраи, классификация ошибок и декодирование.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.logging_setup import get_logger
from app.providers.base import ProviderError, ProviderUnavailableError
from app.providers.http import ProviderBadResponseError, RetryPolicy, build_client, request_json

logger = get_logger(__name__)

PROVIDER_LABEL = "onec"
METADATA_PATH = "$metadata"
# Ключ, который платформа даёт каждому объекту. Годится как умолчание для
# $orderby: постраничность без сортировки не гарантирует стабильный порядок, а
# значит может и потерять строку, и выдать её дважды.
DEFAULT_ORDERBY = "Ref_Key"

HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404

# ``_classify`` отдаёт прочие 4xx как ``http_error`` с сообщением ``HTTP <код>``;
# сам код в исключение не кладётся. Разбираем его здесь, чтобы 404 и 400 —
# единственные два, у которых для оператора есть внятный смысл, — не выглядели
# одинаково.
_HTTP_STATUS_IN_MESSAGE = re.compile(r"HTTP (\d{3})")

NOT_PUBLISHED_HINT = (
    "коллекция не опубликована в составе OData, на неё нет прав "
    "или её имя в карте набрано с опечаткой"
)
FILTER_REJECTED_HINT = (
    "1С отвергла запрос — вероятно, реквизита нет, его тип несравним "
    "или составной тип требует cast()"
)


@dataclass(frozen=True, slots=True)
class ODataQuery:
    """Всё, что нужно, чтобы построить один запрос к одной коллекции."""

    collection: str
    filter_expr: str | None = None
    select: tuple[str, ...] = ()
    orderby: str = DEFAULT_ORDERBY
    expand: str | None = None


def quote_literal(value: str) -> str:
    """Строковый литерал ``$filter``: удвоенная кавычка плюс percent-кодирование.

    Единственная защита сразу от двух вещей: от фамилии «О'Коннор», которая
    иначе закрывает литерал на середине, и от подобранного ввода вида
    ``x' or 1 eq 1 or '``, который иначе дописывает в фильтр второй предикат и
    вытаскивает из базы посторонних людей.
    """
    return quote(value.replace("'", "''"), safe="")


def build_query(
    collection: str,
    *,
    filter_expr: str | None = None,
    select: Sequence[str] = (),
    orderby: str | None = None,
    expand: str | None = None,
    top: int | None = None,
    skip: int | None = None,
) -> str:
    """Относительный URL одной страницы. Чистая функция, тестируется отдельно.

    ``$`` остаётся литеральным, пробел кодируется как ``%20``, кириллица — как
    percent-UTF-8. Порядок параметров детерминированный: строку сравнивают
    тесты, и она же попадает в сравнение с тем, что реально ушло по сети.
    """
    params: list[str] = ["$format=json"]
    if select:
        params.append("$select=" + _encode(",".join(select), safe=","))
    if expand:
        params.append("$expand=" + _encode(expand, safe=",/"))
    if filter_expr:
        params.append("$filter=" + _encode_filter(filter_expr))
    if orderby:
        params.append("$orderby=" + _encode(orderby, safe=",/"))
    if top is not None:
        params.append(f"$top={int(top)}")
    if skip is not None:
        params.append(f"$skip={int(skip)}")
    return f"{quote(collection, safe='')}?{'&'.join(params)}"


def unwrap(payload: Any) -> list[Mapping[str, Any]]:
    """Записи из конверта OData.

    ``value: []`` — честный ноль и единственное место во всём адаптере, где
    можно сказать «записей нет». Конверт без ключа ``value`` — не пустой ответ,
    а ответ, который мы не поняли: так выглядит и HTML страницы входа, и
    сообщение об ошибке, отданное с кодом 200.
    """
    if not isinstance(payload, Mapping):
        raise ProviderBadResponseError("unexpected_schema", "ответ 1С не является объектом OData")
    if "value" not in payload:
        raise ProviderBadResponseError(
            "unexpected_schema", "в ответе 1С нет раздела value — это не конверт OData"
        )
    value = payload["value"]
    if not isinstance(value, list):
        raise ProviderBadResponseError("unexpected_schema", "раздел value в ответе 1С не список")
    return [row for row in value if isinstance(row, Mapping)]


class OneCODataClient:
    """Одна публикация OData: страницы, ретраи, кэш и перевод ошибок.

    Не знает ни одного имени справочника — их приносит вызывающий из карты.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout_seconds: float = 15.0,
        verify: bool | str = True,
        page_size: int = 100,
        max_pages: int = 5,
        concurrency: int = 2,
        cache_ttl_seconds: int = 60,
        cache_max_entries: int = 256,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._verify = verify
        self._page_size = page_size
        self._max_pages = max_pages
        self._retry = retry or RetryPolicy()
        # Свой семафор, узкий: рабочая база заказчика — не наш ресурс, и общая
        # PROVIDER_CONCURRENCY рассчитана на публичные API, а не на сеансы 1С.
        self._semaphore = asyncio.Semaphore(concurrency)
        self._cache_ttl = cache_ttl_seconds
        self._cache_max_entries = cache_max_entries
        self._cache: dict[tuple[str, str], tuple[float, list[Mapping[str, Any]]]] = {}
        # Пароль хранится уже свёрнутым в заголовок: так он не попадает ни в
        # repr клиента, ни в аргументы httpx, которые она печатает в ошибках.
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}

    # ---------------------------------------------------------------- api

    async def fetch_rows(self, lookup: str, query: ODataQuery) -> list[Mapping[str, Any]]:
        """Все страницы одного поиска. Ключ кэша — ``(lookup, $filter)``.

        Кэш на процесс и на минуту закрывает две вещи сразу: повторный проход
        каскада поиска внутри одного отчёта и попадание в кэш отчётов, при
        котором внутренний контур всё равно опрашивается заново.
        """
        cache_key = (lookup, query.filter_expr or "")
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        async with self._semaphore:
            # Проверяем ещё раз: пока ждали семафор, соседний поиск мог уже
            # сходить за тем же самым.
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
            rows = await self._fetch_pages(lookup, query)

        self._cache_put(cache_key, rows)
        return rows

    async def metadata_text(self) -> str:
        """Сырой EDMX. Только для ``onec-doctor``, в путь поиска не входит.

        Отдельно от :func:`request_json`, потому что ``$metadata`` — это XML, и
        общий путь честно назвал бы его ``malformed_json``.
        """
        async with self._client() as client:
            response = await client.get(METADATA_PATH)
            response.raise_for_status()
            return response.text

    # ---------------------------------------------------------------- pages

    async def _fetch_pages(self, lookup: str, query: ODataQuery) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        started = time.perf_counter()
        async with self._client() as client:
            for page in range(self._max_pages):
                skip = page * self._page_size
                url = build_query(
                    query.collection,
                    filter_expr=query.filter_expr,
                    select=query.select,
                    orderby=query.orderby or DEFAULT_ORDERBY,
                    expand=query.expand,
                    top=self._page_size,
                    skip=skip,
                )
                page_rows = unwrap(await self._get(client, url, lookup))
                rows.extend(page_rows)
                if len(page_rows) < self._page_size:
                    self._log_done(lookup, rows=len(rows), pages=page + 1, started=started)
                    return rows

        # Дошли до потолка, а страницы всё полные: выдать то, что успели, —
        # значит сказать «вот всё, что есть», не проверив этого.
        raise ProviderBadResponseError(
            "page_limit_exceeded",
            f"выборка 1С не уместилась в {self._max_pages} стр. по {self._page_size} — "
            "сузьте фильтр в карте или поднимите ONEC_MAX_PAGES",
        )

    async def _get(self, client: httpx.AsyncClient, url: str, lookup: str) -> Any:
        try:
            payload, _raw = await request_json(
                client,
                "GET",
                # Строка уже собрана и закодирована; params=None обязателен,
                # иначе httpx перекодирует её и потеряет `$`.
                url,
                params=None,
                retry=self._retry,
                provider=PROVIDER_LABEL,
            )
        except ProviderError as exc:
            raise _translate(exc) from exc
        return payload

    def _client(self) -> httpx.AsyncClient:
        return build_client(
            base_url=self._base_url,
            timeout_seconds=self._timeout,
            headers=self._headers,
            verify=self._verify,
        )

    def _log_done(self, lookup: str, *, rows: int, pages: int, started: float) -> None:
        # Никогда не URL: в нём ФИО, телефон и адрес открытым текстом, а
        # редактор логов чистит по именам ключей и этого не поймает.
        logger.info(
            "onec.lookup_done",
            lookup=lookup,
            rows=rows,
            pages=pages,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    # ---------------------------------------------------------------- cache

    def _cache_get(self, key: tuple[str, str]) -> list[Mapping[str, Any]] | None:
        if self._cache_ttl <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, rows = entry
        if time.monotonic() - stored_at > self._cache_ttl:
            self._cache.pop(key, None)
            return None
        return list(rows)

    def _cache_put(self, key: tuple[str, str], rows: list[Mapping[str, Any]]) -> None:
        if self._cache_ttl <= 0:
            return
        if len(self._cache) >= self._cache_max_entries:
            # Потолок есть, потому что ключ содержит значение поиска: без него
            # кэш растёт по числу проверенных людей и держит их ПДн в памяти.
            oldest = min(self._cache, key=lambda item: self._cache[item][0])
            self._cache.pop(oldest, None)
        self._cache[key] = (time.monotonic(), list(rows))


# ---------------------------------------------------------------- helpers


def _encode(text: str, *, safe: str = "") -> str:
    """Percent-кодирование значения параметра: пробел — ``%20``, а не ``+``."""
    return quote(text, safe=safe)


def _encode_filter(filter_expr: str) -> str:
    """Кодирование ``$filter``, уже собранного из шаблона карты и литералов.

    ``%`` в safe-наборе намеренно: значения прошли через :func:`quote_literal` и
    уже закодированы, повторное кодирование превратило бы ``%27`` в ``%2527``.
    Кавычки, скобки, запятая и косая черта — синтаксис OData, они остаются.
    """
    return quote(filter_expr, safe="%'(),/")


def _translate(exc: ProviderError) -> ProviderError:
    """Человеческие коды вместо ``http_error``. Ни один из них не пустой список."""
    if exc.code != "http_error":
        return exc
    match = _HTTP_STATUS_IN_MESSAGE.search(exc.message)
    status = int(match.group(1)) if match else 0
    if status == HTTP_NOT_FOUND:
        return ProviderBadResponseError("not_published", NOT_PUBLISHED_HINT)
    if status == HTTP_BAD_REQUEST:
        return ProviderBadResponseError("filter_rejected", FILTER_REJECTED_HINT)
    return exc


def select_from_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Верхние сегменты путей карты — то, что просить у 1С в ``$select``.

    ``$select`` обязателен: без него 1С отдаёт все реквизиты объекта, включая
    паспорта и адреса людей, которые нам ничего не должны. Это техническая
    форма минимизации, а не оптимизация трафика.
    """
    fields: list[str] = []
    for path in paths:
        head = path.split(".", 1)[0].strip()
        if head and head not in fields:
            fields.append(head)
    return tuple(fields)


__all__ = [
    "DEFAULT_ORDERBY",
    "ODataQuery",
    "OneCODataClient",
    "ProviderBadResponseError",
    "ProviderUnavailableError",
    "build_query",
    "quote_literal",
    "select_from_paths",
    "unwrap",
]
