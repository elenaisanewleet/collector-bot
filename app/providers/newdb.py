"""NewDB — общий транспорт агрегатора.

`fssp_person` was the first method wired up here, and the envelope it speaks is
the same for every method NewDB exposes. That envelope therefore lives in this
module, and each per-method adapter carries nothing but the parameters it sends
and the rows it reads back.

The split between what is hard-coded here and what is configuration is
deliberate, and it follows the same rule as the rest of this project: *code may
encode what has been verified; everything else is supplied by the deployment.*

**Verified, and therefore hard-coded** — the transport, as published at
https://newdb.net/docs/ and in the OpenAPI document at
https://newdb.net/swagger/openapi.json:

*   ``POST {NEWDB_BASE_URL}/v2`` with ``X-API-KEY``; the body is
    ``{"params": {...}, "requestId": "..."}`` and ``method`` lives *inside*
    ``params``.
*   The call is asynchronous. The envelope carries ``state``, and the whole
    vocabulary the OpenAPI document declares is ``queued``, ``in_progress``,
    ``restart``, ``complete``, ``failed``, ``timeout`` and ``error``. The first
    three plus ``timeout`` mean keep waiting; the other three are terminal.
    Polling is a repeat POST to the same endpoint with the same ``requestId`` —
    documented as *not* starting a second, separately billed task — which also
    keeps the token out of query strings and therefore out of access logs.
*   Rows arrive at ``results.<method>.result.data``.
*   **A rejected or missing token comes back as HTTP 200 with
    ``state: "failed"``**, not as 401/403. Read naively that is an empty result
    — a clean report for a debtor nobody checked. Every terminal ``failed`` is
    mapped to an error status, and a ``complete`` envelope without the expected
    result path is ``unexpected_schema``, never ``NO_RESULTS``.

**Configuration, and now mostly verified too** — the shape of the rows each
method returns. On 05.09.2026 a key was obtained and five methods were called
for real: ``fssp_person``, ``bankrot_person``, ``egrul_ip``, ``arbitr_person``
and ``pledge_person`` / ``pledge_vin``. The captured answers are in
``tests/data/newdb_live_*.json`` and the shipped map
(``config/field_maps/example_newdb.json``) now describes what they actually
contain; where the archived documentation disagreed with the live service, the
live service won, and one of those disagreements — ``arbitr_person`` wrapping
its cases in a per-query container — meant the archived paths read nothing at
all. What is still archive-only is listed in the map's own header.

The row schema nevertheless stays in a field map (``NEWDB_FIELD_MAP``) rather
than in this code: the same mechanism the ЕФРСБ and ФНС vendor adapters use, a
deployment whose contract differs edits a file, and a method with no entry in
that map stays ``NOT_CONFIGURED`` instead of guessing.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.domain.identity import INN_INDIVIDUAL_LENGTH, SearchSubject
from app.logging_setup import get_logger
from app.providers.base import BaseProvider, ProviderError, ProviderUnavailableError
from app.providers.http import RetryPolicy, build_client, request_json
from app.providers.mapping import FieldMap, FieldMapError, RecordDict, as_text, dig
from app.utils.masking import redact_sensitive_json

logger = get_logger(__name__)

COUNTRY_RU = "ru"
# The parameter name ФССП uses for a date of birth. ``pledge_person`` documents
# a different one; see ``person_params``.
DOB_KEY = "dob"

STATE_COMPLETE = "complete"
STATE_FAILED = "failed"
STATE_QUEUED = "queued"
#: Не прошла валидация запроса, либо сломалась сама служба поставщика. Своё
#: состояние — но не своя беда: детализация лежит там же, где у ``failed``, в
#: ``errors_info``, поэтому и разбирается тем же :func:`_failure_error`.
STATE_ERROR = "error"
TERMINAL_FAILURE_STATES = frozenset({STATE_FAILED, STATE_ERROR})
#: Поставщик перестал ЖДАТЬ — он не перестал считать. Так отвечает синхронный
#: ``/v2/run``, когда ожидание превысило его собственный таймаут: задача жива,
#: её ``requestId`` по-прежнему адресует её, и следующий опрос возьмёт результат.
#: Поэтому ``timeout`` стоит среди ожидающих: считать его отказом значило бы
#: выбросить уже оплаченный вызов ровно в тот момент, когда он почти готов.
STATE_TIMEOUT = "timeout"
#: ``timeout`` и ``error`` объявлены в OpenAPI-документе поставщика, но этот код
#: их не знал, и разбирались они последней ветвью — «Ответ NewDB не содержит
#: поля state». То есть про ответ, в котором ``state`` есть, оператору
#: сообщалось, что поля нет: диагностика, ведущая искать поломку не там.
PENDING_STATES = frozenset({STATE_QUEUED, "in_progress", "restart", STATE_TIMEOUT})
# ``queued`` НЕ значит «за задачу не взялись». Здесь стояло правило,
# обрывавшее задачу, которая простояла в очереди десять опросов кряду ни разу не
# начав выполняться: снятое с прода наблюдение — пять источников отвечали
# ``queued`` девяносто секунд и «не двигались с места» — читалось как отказ
# поставщика брать работу.
#
# 10.09.2026 поддержка NewDB прислала журнал по нашему аккаунту, и наблюдение
# оказалось прочитано неверно. Тридцать семь запросов, ЗАВЕРШЁННЫХ тридцать
# семь, ни одного незавершённого; медиана 79 с, P95 174 с, максимум 207 с. Те
# девяносто секунд в очереди были нормальной работой поставщика, а не простоем:
# он всё это время считал и в итоге ответил на каждый запрос.
#
# То есть порог отменял не зависшие задачи, а обычные — за полминуты до того,
# как источник впервые вообще успевал ответить. Вызов при этом оплачен, ответ
# выброшен, а в отчёте появлялось «поставщик не взялся за запрос»: обвинение в
# адрес того, кто работал. Правильный ответ на медленного поставщика — бюджет
# опроса по его собственным таймингам (см. ``newdb_poll_attempts``), а не
# догадка о том, что он бросил задачу.

# ``result.status`` — HTTP-код источника, стоящего за агрегатором, и он лежит
# РЯДОМ с ``data``, а не внутри неё. Все 29 снятых живьём нормальных ответов
# несут 200; недоступность ГУВМ приходит как 500 с ``data`` из одной строки, а
# неразобранный адрес — как 500 с пустой ``data``. Последнее и есть заготовка
# инверсии: пустой список при упавшем источнике читался бы как «объект не
# найден». Гейт по 200 закрывает это для всех методов сразу.
UPSTREAM_OK = 200

# The service reports a bad key and an empty balance through one message; both
# are actionable by the operator and neither is retryable.
AUTH_ERROR_MARKERS = ("токен", "token", "баланс", "balance", "x-api-key")
ERROR_CODE_PAYMENT_REQUIRED = 402
ERROR_CODE_BAD_REQUEST = 400


def result_data_path(section: str) -> str:
    return f"results.{section}.result.data"


# Серия и номер паспорта: 4 и 6 цифр. Всё, что попадает в этот диапазон, из
# текста вычищается — ИНН (10 и 12 цифр) в него не попадает.
_SHORT_DIGIT_RUN = re.compile(r"(?<!\d)\d{4,6}(?!\d)")
_PASSPORT_MASK = "*"


def scrub_passport(text: str, *, seria: str, number: str) -> str:
    """Вычистить серию и номер паспорта из произвольного текста.

    Нужна для сообщений об ошибках вендора: ``error_message`` пишется в БД
    ``repository.save_provider_results`` **безусловно**, мимо обоих флагов
    приватности, а NewDB охотно цитирует присланные параметры в тексте отказа
    (``seria 4015350278 is not valid``).

    Работает по строке и не разбирает JSON: текст отказа приходит в
    произвольной форме, и разбор, который не удался, вернул бы паспорт наружу
    целиком. Сначала убираются сами значения и их склейка, затем — любой
    отдельно стоящий прогон из 4–6 цифр. ИНН не портится: у него 10 или 12
    цифр, то есть вне диапазона.
    """
    if not text:
        return text
    scrubbed = text
    if seria and number:
        for glued in (f"{seria}{number}", f"{seria} {number}", f"{seria}-{number}"):
            scrubbed = scrubbed.replace(glued, _PASSPORT_MASK * len(glued))
    for value in (seria, number):
        if value:
            scrubbed = scrubbed.replace(value, _PASSPORT_MASK * len(value))
    return _SHORT_DIGIT_RUN.sub(lambda match: _PASSPORT_MASK * len(match.group()), scrubbed)


@dataclass(frozen=True, slots=True)
class NewDBResponse:
    """Rows from one or more calls of the same method, plus the raw bodies."""

    rows: list[Any]
    raw: str


class NewDBClient:
    """Speaks the NewDB envelope. Knows nothing about any particular method."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def is_configured(self) -> bool:
        return bool(self._settings.newdb_api_key and self._settings.newdb_base_url)

    async def call(
        self,
        method: str,
        *param_sets: Mapping[str, Any],
        result_section: str | None = None,
    ) -> NewDBResponse:
        """Run ``method`` once per parameter set, on one shared connection.

        Several parameter sets exist for the one case that genuinely needs them:
        ФССП searches Moscow and the region separately. Rows are concatenated;
        deduplication is the caller's business, because only the caller knows
        what makes two rows the same record.

        ``result_section`` names the section of ``results`` to read when it is
        not the method's own name. ``passport_fns`` answers in
        ``results.company`` — the single known method whose section and name
        disagree, so inferring the section from the name is not something to
        rely on.
        """
        retry = RetryPolicy(
            max_retries=self._settings.provider_max_retries,
            backoff_seconds=self._settings.provider_retry_backoff_seconds,
        )
        rows: list[Any] = []
        raw_bodies: list[str] = []

        async with build_client(
            base_url=self._settings.newdb_base_url,
            timeout_seconds=self._settings.request_timeout_seconds,
            headers={"X-API-KEY": self._settings.newdb_api_key},
        ) as client:
            for params in param_sets:
                envelope, raw = await self._run(client, method, params, retry)
                raw_bodies.append(raw)
                rows.extend(_extract_rows(envelope, _section_of(envelope, method, result_section)))

        return NewDBResponse(rows=rows, raw="\n".join(raw_bodies))

    # ------------------------------------------------------------ envelope

    async def _run(
        self,
        client: httpx.AsyncClient,
        method: str,
        params: Mapping[str, Any],
        retry: RetryPolicy,
    ) -> tuple[Any, str]:
        payload = _build_payload(method, params)
        envelope, raw = await self._post(client, method, payload, retry)

        state = _state_of(envelope)
        if state == STATE_COMPLETE:
            return envelope, raw
        if state in TERMINAL_FAILURE_STATES:
            raise _failure_error(envelope)
        if state not in PENDING_STATES:
            # An envelope with no recognizable state is not an empty result.
            raise _unknown_state_error(state)
        return await self._poll(client, method, payload, retry, _progress_of(envelope, method))

    async def _poll(
        self,
        client: httpx.AsyncClient,
        method: str,
        payload: Mapping[str, Any],
        retry: RetryPolicy,
        previous: _Progress,
    ) -> tuple[Any, str]:
        """Re-POST the same requestId until the task settles or the budget ends."""
        last_state: str | None = None
        for _ in range(self._settings.newdb_poll_attempts):
            await asyncio.sleep(self._settings.newdb_poll_interval_seconds)
            envelope, raw = await self._post(client, method, payload, retry)
            state = _state_of(envelope)
            last_state = state
            if state == STATE_COMPLETE:
                return envelope, raw
            if state in TERMINAL_FAILURE_STATES:
                raise _failure_error(envelope)
            if state not in PENDING_STATES:
                # Незнакомое состояние не станет ``complete`` от того, что его
                # опросят ещё двадцать четыре раза. Раньше опрос доходил до
                # конца бюджета и сообщал ``poll_timeout`` — «источник не успел
                # подготовить ответ» о поставщике, который ответил сразу, просто
                # не теми словами.
                raise _unknown_state_error(state)

            progress = _progress_of(envelope, method)
            if progress.stalled_after(previous):
                # Живьём это выглядит так: ``restart`` по кругу, ``dateupdated``
                # замер, а внутри уже лежит терминальная ошибка источника. Ждать
                # дальше нечего — вызов оплачен, и единственное, что ещё можно
                # спасти, это диагностику: «ГУВМ лежит», «адрес не разобран».
                logger.info("newdb.stalled_restart", method=method, status=progress.result_status)
                raise ProviderUnavailableError("upstream_error", progress.failure_message)
            previous = progress

        # Последнее состояние — единственное, что отличает «поставщик думает
        # дольше нашего потолка» от «задача не двинулась с очереди». Первое
        # лечится настройкой NEWDB_POLL_ATTEMPTS, второе — разговором с
        # поставщиком про доступ и баланс, и по одному слову «poll_timeout» их
        # было не различить: на проде пять источников подряд отвалились так, и
        # понять причину по логу оказалось нечем.
        #
        # Различить их по-прежнему нужно, но решает это НЕ обрыв по очереди:
        # журнал поставщика показал, что долгая очередь — его нормальная работа.
        # Оставшийся здесь ``last_state`` попадает и в лог, и в текст отказа, и
        # его хватает, чтобы задать поставщику предметный вопрос.
        waited = self._settings.newdb_poll_attempts * self._settings.newdb_poll_interval_seconds
        logger.info(
            "newdb.poll_timeout",
            method=method,
            attempts=self._settings.newdb_poll_attempts,
            waited_seconds=round(waited),
            last_state=last_state,
        )
        raise ProviderUnavailableError(
            "poll_timeout",
            f"NewDB не успела подготовить результат за {round(waited)} с "
            f"(последнее состояние: {last_state or 'неизвестно'})",
        )

    async def _post(
        self,
        client: httpx.AsyncClient,
        method: str,
        payload: Mapping[str, Any],
        retry: RetryPolicy,
    ) -> tuple[Any, str]:
        return await request_json(
            client,
            "POST",
            self._settings.newdb_method_path,
            json_body=payload,
            retry=retry,
            provider=f"newdb:{method}",
        )


def _build_payload(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "params": {"method": method, **params},
        # Carried through polling: the same id addresses the same task.
        "requestId": str(uuid.uuid4()),
    }


def _state_of(envelope: Any) -> str:
    """The envelope's ``state``, normalized to underscores.

    The live service answers ``"in progress"`` — with a space — where its own
    documentation writes ``in_progress``. Read literally, that value matches
    neither the terminal states nor ``PENDING_STATES``, so a first response that
    arrives already in progress rather than queued would be rejected as an
    unrecognizable envelope and the source reported unavailable. Verified
    against api.newdb.net on 05.09.2026.
    """
    state = as_text(dig(envelope, "state"))
    return state.strip().lower().replace(" ", "_") if state else ""


def _result_node(envelope: Any, method: str) -> Any:
    return dig(envelope, f"results.{method}.result")


def _result_status(envelope: Any, method: str) -> int | None:
    status = dig(_result_node(envelope, method), "status")
    return status if isinstance(status, int) else None


@dataclass(frozen=True, slots=True)
class _Progress:
    """What one poll saw: whether the task moved, and what the source said."""

    updated_at: str | None
    result_status: int | None
    result_error: str | None

    @property
    def is_terminal_failure(self) -> bool:
        return self.result_status is not None and self.result_status != UPSTREAM_OK

    @property
    def failure_message(self) -> str:
        reason = f" ({self.result_error})" if self.result_error else ""
        return f"Источник ответил {self.result_status}{reason}"

    def stalled_after(self, previous: _Progress) -> bool:
        """A repeat of the same terminal error with no movement since last time."""
        return (
            self.is_terminal_failure
            and self.updated_at is not None
            and self.updated_at == previous.updated_at
        )


def _progress_of(envelope: Any, method: str) -> _Progress:
    node = _result_node(envelope, method)
    return _Progress(
        updated_at=as_text(dig(envelope, f"results.{method}.dateupdated")),
        result_status=_result_status(envelope, method),
        result_error=as_text(dig(node, "error")),
    )


def _unknown_state_error(state: str) -> ProviderUnavailableError:
    """Состояние, которого этот код не знает, — не пустой результат.

    Две беды, которые до сих пор печатались одной строкой. Отсутствующее
    ``state`` — сломанный конверт, и про него нужно сказать именно это.
    Присутствующее, но незнакомое — наш словарь, отставший от поставщика: так
    пришли бы ``timeout`` и ``error``, объявленные в его же OpenAPI-документе, и
    оператор читал бы «Ответ NewDB не содержит поля state» про ответ, где это
    поле есть.

    Значение печатается целиком: это служебный токен (``queued``, ``restart``),
    персональных данных в нём нет, а без него непонятно, что добавлять в
    словарь.
    """
    if not state:
        return ProviderUnavailableError("unexpected_schema", "Ответ NewDB не содержит поля state")
    return ProviderUnavailableError(
        "unknown_state", f"NewDB ответила состоянием {state!r}, которого мы не знаем"
    )


def _errors_info(envelope: Any) -> list[Mapping[str, Any]]:
    node = dig(envelope, "errors_info")
    if not isinstance(node, list):
        return []
    return [item for item in node if isinstance(item, Mapping)]


def _failure_error(envelope: Any) -> ProviderError:
    """Translate a terminal ``failed`` envelope into a typed provider error.

    Never a ``NO_RESULTS``: the task did not run, so there is nothing to report
    as absent.
    """
    errors = _errors_info(envelope)
    messages = [text for item in errors if (text := as_text(item.get("error")))]
    message = "; ".join(messages) or as_text(dig(envelope, "error")) or "NewDB отклонила запрос"
    return ProviderError(_failure_code(errors, message), message)


def _failure_code(errors: list[Mapping[str, Any]], message: str) -> str:
    codes = {item.get("error_code") for item in errors}
    if ERROR_CODE_PAYMENT_REQUIRED in codes:
        return "payment_required"
    lowered = message.lower()
    if any(marker in lowered for marker in AUTH_ERROR_MARKERS):
        # A rejected key and an exhausted balance share one message here, and
        # both mean the same thing operationally: the source was not queried.
        return "unauthorized"
    if ERROR_CODE_BAD_REQUEST in codes:
        return "bad_request"
    return "request_failed"


def _section_of(envelope: Any, method: str, preferred: str | None) -> str:
    """В какой секции ``results`` на самом деле лежит ответ.

    Секцию приходится ВЫБИРАТЬ, а не знать, потому что документация вендора и
    его живое API разошлись, и разошлись дорого. Про ``passport_fns`` в
    документации сказано, что он отвечает в ``results.company``; на проде он
    отвечает в ``results.passport_fns``, то есть по имени метода, как все
    остальные. Код читал ``company``, не находил ничего и поднимал
    ``unexpected_schema`` на КАЖДОМ успешном ответе — вместе с уже полученным
    ИНН, который лежал в ответе рядом.

    Стоило это дороже, чем выглядит: без ИНН молчат три источника сразу —
    банкротство, статус ИП и арбитраж, — и в отчёте они честно писали «нужен
    ИНН физлица». То есть дефект выглядел как нехватка данных у заказчика, а не
    как своя поломка, и прожил бы до первой ручной сверки.

    Берётся та секция, которая в ответе ЕСТЬ: сперва названная вызывающим,
    потом одноимённая методу. Обе формы остаются рабочими, и смена вендорского
    поведения обратно ничего не сломает. Если нет ни одной — возвращается
    названная вызывающим, чтобы ``_extract_rows`` пожаловался на неё, а не на
    подобранную втихую.
    """
    for candidate in (preferred, method):
        if candidate and dig(envelope, f"results.{candidate}") is not None:
            return candidate
    return preferred or method


def _extract_rows(envelope: Any, section: str) -> list[Any]:
    """Read the rows of a completed envelope.

    A missing result path on a ``complete`` envelope is a schema problem, not an
    empty result, and is reported as such. So is a non-200 ``result.status``:
    that is the source behind the aggregator refusing to answer, and the empty
    ``data`` that comes with it means "we did not look", never "nothing found".
    """
    status = _result_status(envelope, section)
    if status is not None and status != UPSTREAM_OK:
        error = as_text(dig(_result_node(envelope, section), "error"))
        raise ProviderUnavailableError(
            "upstream_error",
            f"Источник ответил {status}" + (f" ({error})" if error else ""),
        )
    path = result_data_path(section)
    rows = dig(envelope, path)
    if rows is None:
        raise ProviderUnavailableError("unexpected_schema", f"В ответе NewDB нет раздела {path}")
    if not isinstance(rows, list):
        raise ProviderUnavailableError("unexpected_schema", f"{path} не является списком")
    return rows


# ---------------------------------------------------------------- field maps


@dataclass(frozen=True, slots=True)
class MappedRows:
    """Rows the map could read, plus a count of the ones it could not.

    Keeping the two apart is the whole point: "the source answered with
    nothing" and "the map read nothing in the answer" both come out as zero
    records, and they mean opposite things.

    ``containers`` is the third of those answers. It holds ``row_fields`` read
    from each row of ``data`` **whether or not the nested array had anything in
    it** — which is exactly the case the first two cannot express: ФНП answers
    ``"fnp": []`` while ``fnp_urls`` lists thirteen notices, арбитраж answers
    ten cases with ``total_count: 40``. Zero records, nothing unreadable, and
    the source plainly said it found more. Adapters that know what their
    container counts mean compare the two and report the answer as incomplete.
    """

    records: list[RecordDict]
    unreadable: int
    containers: list[RecordDict] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MethodMap:
    """How to read one method's rows, and what to add to its request."""

    method: str
    field_map: FieldMap
    extra_params: Mapping[str, Any] = field(default_factory=dict)
    #: Поля, лежащие в самой строке ``data`` рядом с вложенным массивом.
    row_map: FieldMap | None = None
    #: Настройки разбора, которые нужны адаптеру, а не карте полей.
    options: Mapping[str, Any] = field(default_factory=dict)

    def apply(self, rows: Iterable[Any]) -> MappedRows:
        """Map every row of ``data``, unwrapping the nested array if one is named.

        Half the methods answer with a *container per subject* rather than with
        records: ``pledge_person`` keeps the notices in ``fnp``,
        ``bankrot_person`` the cases in ``bankruptcy``. ``records_path`` names
        that array **inside one row of ``data``**, so a response carrying two
        subjects loses neither — which an absolute path from ``data`` (``0.fnp``)
        would.

        Whose records those are is the other half of the same problem, and it is
        what ``row_fields`` answers. The identity of a container sits *beside*
        the nested array — ``bankrot_person`` keeps it in ``data[].commmon`` —
        and a path counted from inside a case cannot reach it, so the adapters
        used to stamp the ИНН the search was made with onto every row they got
        back. For one subject that is true by construction; for two it hands the
        second subject's cases to the first. ``row_fields`` is read from the
        container and merged into each of its records, so the answer says who
        the record is about instead of the question assuming it.

        Counting is per record, not per row: a container whose second notice the
        map could not read must not be covered up by the first one it could.

        The container is read even when the nested array is empty, and that is
        not a detail. ``pledge_person`` answered live with ``"fnp": []`` beside
        thirteen ``fnp_urls``: the register found thirteen notices and filtered
        all of them out by date of birth. Reading the container only when there
        were records to attach it to made that answer indistinguishable from an
        empty register — nought records, nought unreadable, "залогов нет".

        Считается и то, что лежало в массиве, но записью не оказалось. Массив из
        двух строк вместо двух объектов давал ноль записей и ноль потерь: тип
        самого массива в порядке, а его содержимое никто не пересчитывал. Это
        худший вид пропажи — «найдено два, показано ноль, сказано ничего», —
        поэтому здесь сравнивается длина сырого массива с числом прочитанных из
        него записей (:meth:`app.providers.mapping.FieldMap.read_records`).

        Контейнер, у которого не прочиталось НИ ОДНО поле, тоже потеря, и она
        тише прочих. ``row_fields`` держат личность записей — ФИО, ИНН и дату
        рождения должника из ``data[].commmon``; стоит вендору починить свою
        опечатку (``commmon`` -> ``common``), как все три пути промахнутся, дела
        останутся без личности, и код проставит на них ИНН, по которому шёл
        поиск, — то есть отдаст дела второго субъекта первому и не скажет ни
        слова. Считается только у строки, в которой ЕСТЬ записи: пустой ответ
        живьём приходит с ``commmon: {}``, и там приписывать нечего.
        """
        records: list[RecordDict] = []
        containers: list[RecordDict] = []
        unreadable = 0
        for row in rows:
            if not isinstance(row, Mapping):
                unreadable += 1
                continue
            owner = self.row_map.apply(row) if self.row_map is not None else {}
            if owner:
                containers.append(owner)
            nested, dropped = self.field_map.read_records(row)
            # Элементы массива, которые записями не являются: их не покажешь и
            # не сосчитаешь как «ничего не найдено».
            unreadable += dropped
            if not nested:
                # Пустой массив — это ответ («уведомлений нет»). Массив,
                # которого нет или который пришёл не массивом, — это про карту.
                unreadable += int(self._records_path_unreadable(row))
                continue
            if self.row_map is not None and not _has_any_value(owner):
                # Записи есть, а чьи они — неизвестно. Приписать их субъекту
                # запроса значит угадать; отдать без личности — значит дать
                # отождествлению угадать за нас.
                unreadable += len(nested)
                continue
            for item in nested:
                record = self.field_map.apply(item)
                if _has_any_value(record):
                    records.append(_merged(owner, record))
                else:
                    # Строки внутри есть, но карта не нашла в них ни одного
                    # поля: «не разобрано», а не «ничего не найдено».
                    unreadable += 1
        return MappedRows(records=records, unreadable=unreadable, containers=containers)

    def _records_path_unreadable(self, row: Mapping[str, Any]) -> bool:
        """A named array that is absent — or present as something that is not one.

        ``"fnp": []`` is an answer: no notices. A row with no ``fnp`` at all is
        a row shaped differently from what the map describes. So is a row where
        ``fnp`` came back as ``null``, ``0`` or ``"нет данных"``: reading a
        scalar as an empty list is how a source that said something unexpected
        turns into a source that said nothing.
        """
        path = self.field_map.records_path
        return bool(path) and not isinstance(dig(row, path), (list, Mapping))


def _merged(owner: RecordDict, record: RecordDict) -> RecordDict:
    """Record over container — but a path that missed does not erase a value.

    ``FieldMap.apply`` writes every key it was given, ``None`` included, so a
    plain ``{**owner, **record}`` let a *missed path in the record* overwrite a
    value the container had. The two maps do not share a key in the shipped
    file, which is the only reason that never lied; the trap is laid exactly
    where someone would want to use it, because the obvious use of
    ``row_fields`` is "take it from the container when the record has none".

    Пустая строка затирает ровно так же, как ``None``, и приходит она чаще:
    живой ``bankrot_person`` присылает ``commmon.address: ""``. Поэтому
    побеждает не «непустое над непустым», а «есть значение над его
    отсутствием», и пустой строкой значение контейнера не заменяется.
    """
    merged = dict(owner)
    for key, value in record.items():
        if value not in (None, "") or key not in merged:
            merged[key] = value
    return merged


def container_int(containers: Iterable[RecordDict], key: str) -> int | None:
    """Сумма числового поля контейнеров — например, сколько всего нашёл источник.

    ``None``, когда поля нет ни в одном контейнере или оно пришло не числом:
    «источник не сказал» и «источник сказал ноль» — разные ответы, и второй
    нельзя изобретать из первого.
    """
    total: int | None = None
    for container in containers:
        value = container.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        try:
            number = int(str(value).strip())
        except ValueError:
            continue
        total = number if total is None else total + number
    return total


def container_flag(containers: Iterable[RecordDict], key: str) -> bool:
    """Взведён ли булев признак контейнера хотя бы в одной строке ``data``."""
    return any(_is_true(container.get(key)) for container in containers)


def container_list(containers: Iterable[RecordDict], key: str) -> list[str]:
    """Список строк из поля контейнера — ссылки, которые источник вернул отдельно."""
    values: list[str] = []
    for container in containers:
        node = container.get(key)
        if isinstance(node, str):
            node = [node]
        if not isinstance(node, list):
            continue
        values.extend(text for item in node if (text := as_text(item)))
    return values


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in {"true", "1", "да"}


def _has_any_value(record: Mapping[str, Any]) -> bool:
    """Did the map fill in anything at all?

    An all-``None`` record is not a finding, it is a set of paths that missed.
    Passing it on would turn a wrong map into a bankruptcy with no case number
    and a pledge with no subject — findings about people nobody parsed.
    """
    return any(value is not None for value in record.values())


class NewDBFieldMaps:
    """Row maps for NewDB methods, keyed by method name.

    A method present in the file is a method the deployment has confirmed
    against its own NewDB contract. A method absent from it is a method this
    tool will not pretend to understand.
    """

    def __init__(self, maps: Mapping[str, MethodMap] | None = None) -> None:
        self._maps = dict(maps or {})

    @classmethod
    def load(cls, path: Path | None) -> NewDBFieldMaps:
        """Read the map file. No path configured means no methods enabled."""
        if path is None:
            return cls()
        payload = _read_json_object(path)
        maps: dict[str, MethodMap] = {}
        for method, entry in payload.items():
            if method.startswith("_"):  # comment keys
                continue
            maps[method] = _method_map(path, method, entry)
        return cls(maps)

    def __contains__(self, method: str) -> bool:
        return method in self._maps

    @property
    def methods(self) -> frozenset[str]:
        return frozenset(self._maps)

    def get(self, method: str) -> MethodMap | None:
        return self._maps.get(method)

    def require(self, method: str) -> MethodMap:
        mapping = self._maps.get(method)
        if mapping is None:
            raise FieldMapError(f"метод NewDB {method!r} не описан в NEWDB_FIELD_MAP")
        return mapping


def _read_json_object(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FieldMapError(f"cannot read field map {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FieldMapError(f"field map {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise FieldMapError(f"field map {path} must be a JSON object keyed by NewDB method")
    return payload


def _method_map(path: Path, method: str, entry: Any) -> MethodMap:
    if not isinstance(entry, Mapping):
        raise FieldMapError(f"{path}: запись метода {method!r} должна быть объектом")
    fields = entry.get("fields")
    if not isinstance(fields, Mapping) or not fields:
        # An empty map would map every row onto an all-None record: not an
        # integration, just a source that always answers "nothing known".
        raise FieldMapError(f"{path}: у метода {method!r} нет непустого раздела fields")
    extra = _object_section(path, method, entry, "extra_params")
    options = _object_section(path, method, entry, "options")
    row_fields = _object_section(path, method, entry, "row_fields")
    records_path = str(entry.get("records_path", ""))
    if row_fields and not records_path:
        # Без вложенного массива строка ``data`` и есть запись, и «поля строки»
        # не отличались бы от ``fields`` ничем, кроме места в файле.
        raise FieldMapError(
            f"{path}: row_fields метода {method!r} имеют смысл только вместе с records_path"
        )
    value_maps = entry.get("value_maps", {})
    return MethodMap(
        method=method,
        field_map=FieldMap(
            # Rows of ``data`` are already extracted from the envelope by the
            # client, so ``records_path`` starts *inside one of them*: it names
            # the nested array a method wraps its records in (``fnp``,
            # ``bankruptcy``). Absent, the row itself is the record.
            records_path=records_path,
            fields={str(key): str(value) for key, value in fields.items()},
            value_maps=value_maps,
        ),
        extra_params=dict(extra),
        # Paths counted from the row itself, for what the container knows about
        # its records: who they belong to.
        row_map=(
            FieldMap(
                fields={str(key): str(value) for key, value in row_fields.items()},
                value_maps=value_maps,
            )
            if row_fields
            else None
        ),
        options=dict(options),
    )


def _object_section(
    path: Path, method: str, entry: Mapping[str, Any], key: str
) -> Mapping[str, Any]:
    section = entry.get(key, {})
    if not isinstance(section, Mapping):
        raise FieldMapError(f"{path}: {key} метода {method!r} должны быть объектом")
    return section


def person_params(
    *,
    last_name: str,
    first_name: str,
    middle_name: str | None,
    birth_date: str,
    birth_date_key: str = DOB_KEY,
    country: str = COUNTRY_RU,
) -> dict[str, Any]:
    """The person block NewDB's ``*_person`` methods take.

    Named after ``fssp_person``, whose parameters were read from the published
    contract. The sibling person methods take the same block; a deployment whose
    contract differs adds or overrides keys through ``extra_params`` in the field
    map rather than through a code change.

    The shape below was checked against the live endpoint: an unauthenticated
    ``POST /v2`` validates its parameters before it looks at the key, so the
    contract can be read off the rejections without spending a call.

    ``birth_date_key`` exists because the two person methods disagree about it:
    ``fssp_person`` documents ``dob``, ``pledge_person`` documents ``datebirth``
    in all four places it mentions the parameter. Which name the live service
    accepts for ``pledge_person`` has *not* been checked — the docs are the only
    source left — so the difference is spelled out here rather than hidden.
    """
    params: dict[str, Any] = {
        "country": country,
        "lastname": last_name,
        "firstname": first_name,
        birth_date_key: birth_date,
    }
    # A missing patronymic omits the key. Sending it empty is what the service
    # actually rejects — ``secondname must be non-empty`` — so the earlier
    # reading, that the key must always be present, had it backwards and would
    # have failed every request for a debtor without one.
    if middle_name:
        params["secondname"] = middle_name
    return params


def person_params_for(subject: SearchSubject, *, birth_date_key: str = DOB_KEY) -> dict[str, Any]:
    """The person block for a subject the caller has already checked.

    Each method is sent the smallest set that identifies the subject *for that
    method*, rather than everything known about them. Two reasons: a parameter
    the contract does not expect can be rejected outright, and ``extra_params``
    can add a key but never take one away.
    """
    assert subject.name is not None and subject.birth_date is not None
    return person_params(
        last_name=subject.name.last_name,
        first_name=subject.name.first_name,
        middle_name=subject.name.middle_name,
        birth_date=subject.birth_date.strftime("%Y-%m-%d"),
        birth_date_key=birth_date_key,
    )


def inn_params(inn: str) -> dict[str, Any]:
    """The single-parameter block for a natural person addressed by ИНН.

    ``innfiz``, not ``inn``: the latter is the legal-entity field and is
    validated as ten digits, so a person's twelve-digit ИНН sent under it is
    rejected outright (``innyur / inn is not valid``). Checked against the live
    endpoint, which validates the parameter before the key.
    """
    return {"country": COUNTRY_RU, "innfiz": inn}


def individual_inn(subject: SearchSubject) -> str | None:
    """The subject's ИНН, but only if it can be sent as ``innfiz``.

    ``normalize_inn`` accepts ten digits too, because a ten-digit ИНН is a valid
    identifier — of a legal entity. ``innfiz`` is validated as twelve, so a
    ten-digit value would buy a rejected (and, judging by ``cost: 1`` on the
    live endpoint, still billed) call instead of the honest answer that there is
    nothing to search by.
    """
    inn = subject.inn
    if inn and len(inn) == INN_INDIVIDUAL_LENGTH:
        return inn
    return None


class NewDBMethodProvider(BaseProvider):
    """Base for a source served by NewDB methods whose rows come from the map.

    ``fssp_person`` deliberately does not use this: its rows were read from a
    real response and are parsed by hard-coded keys. Everything else here is
    parsed by the deployment's own description of its contract, and a method
    with no description is a method that reports ``NOT_CONFIGURED``.
    """

    methods: tuple[str, ...] = ()

    def __init__(
        self,
        settings: Settings,
        field_maps: NewDBFieldMaps,
        client: NewDBClient | None = None,
    ) -> None:
        self._settings = settings
        self._field_maps = field_maps
        self._client = client or NewDBClient(settings)

    @property
    def mapped_methods(self) -> tuple[str, ...]:
        return tuple(method for method in self.methods if method in self._field_maps)

    @property
    def is_configured(self) -> bool:
        return self._settings.newdb_configured and bool(self.mapped_methods)

    async def raw_rows_for(
        self, method: str, *param_sets: Mapping[str, Any]
    ) -> tuple[list[Any], str]:
        """Run a method whose rows this code parses itself.

        Used where a field map cannot express the answer: two arrays in
        different branches of one object, or an array of dictionaries where the
        map can only dig out scalars. The rule of the project still holds — code
        may encode only what has been read against a live response.
        """
        response = await self._client.call(method, *param_sets)
        return response.rows, response.raw

    async def rows_for(
        self, method: str, *param_sets: Mapping[str, Any]
    ) -> tuple[list[RecordDict], str]:
        """Строки метода плоскими словарями доменных ключей.

        Для адаптеров, которым не нужно знать, что контейнер сказал о своей
        полноте. Тем, кому нужно, — :meth:`mapped_for`.
        """
        mapped, raw = await self.mapped_for(method, *param_sets)
        return mapped.records, raw

    async def mapped_for(
        self, method: str, *param_sets: Mapping[str, Any]
    ) -> tuple[MappedRows, str]:
        """Run one mapped method: records, container fields and the raw body.

        ``extra_params`` from the map wins over the subject-derived parameters:
        it exists precisely for a deployment whose contract wants something
        different from what this code would send.
        """
        mapping = self._field_maps.require(method)
        merged = [{**params, **mapping.extra_params} for params in param_sets]
        response = await self._client.call(method, *merged)
        mapped = mapping.apply(response.rows)
        if mapped.unreadable:
            # **Any** unreadable record fails the call, not just all of them.
            #
            # The alternative — keep what parsed, log the rest — is what this
            # used to do, and it is the quiet version of the failure this whole
            # project is built against: the report has no way to say "one of the
            # two notices was dropped", so an answer with a lost row is
            # indistinguishable from a complete one. A debtor with two pledges,
            # one of them in a shape the map does not describe, would come back
            # carrying exactly one, and nothing anywhere would say otherwise.
            #
            # Failing the source loses the rows that did parse, and that is the
            # cheaper loss on purpose: ``unexpected_schema`` reports "не
            # проверено", which is honest and visible, while a silently short
            # list reports "вот всё, что есть", which is neither. The map is a
            # deployment artifact — a half-right one is a thing to fix before
            # the run of eight hundred, which is what the first single-debtor
            # run with STORE_RAW_RESPONSES=true is for.
            logger.warning(
                "newdb.unreadable_rows",
                method=method,
                unreadable=mapped.unreadable,
                parsed=len(mapped.records),
            )
            raise ProviderUnavailableError(
                "unexpected_schema",
                f"Карта полей не разобрала {mapped.unreadable} из "
                f"{mapped.unreadable + len(mapped.records)} записей ответа NewDB ({method})",
            )
        return mapped, response.raw

    def option(self, method: str, key: str, default: str) -> str:
        """Настройка разбора из карты полей — та, что не является путём к полю.

        Нужна там, где адаптеру приходится знать имя ключа *внутри* значения:
        участники дела у КАД приходят списком объектов, и какой в них ключ
        держит имя, знает контракт деплоя, а не этот код.
        """
        value = self._field_maps.require(method).options.get(key)
        return str(value) if value not in (None, "") else default

    def raw_for(self, raw: str) -> str | None:
        """The body to store — with the vendor's stray personal data cut out.

        ``STORE_RAW_RESPONSES`` decides whether a body is kept at all. What it
        must never decide is whether somebody else's СНИЛС is kept with it: the
        live ``bankrot_person`` answer carries ``commmon.snils``,
        ``birth_place`` and ``residential_address``, no field map points at any
        of them, and the README tells the operator to run the first debtor with
        this flag on. See :func:`app.utils.masking.redact_sensitive_json`.
        """
        if not self._settings.store_raw_responses:
            return None
        return redact_sensitive_json(raw)


__all__ = [
    "COUNTRY_RU",
    "DOB_KEY",
    "UPSTREAM_OK",
    "MappedRows",
    "MethodMap",
    "NewDBClient",
    "NewDBFieldMaps",
    "NewDBMethodProvider",
    "NewDBResponse",
    "container_flag",
    "container_int",
    "container_list",
    "individual_inn",
    "inn_params",
    "person_params",
    "person_params_for",
    "result_data_path",
    "scrub_passport",
]
