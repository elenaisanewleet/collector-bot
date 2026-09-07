"""Search orchestration.

The pipeline, end to end::

    SearchSubject
        -> internal lookup (CSV + database)
        -> external providers, in parallel and individually isolated
        -> IdentityMatcher
        -> Aggregator
        -> RecoveryScoreEngine
        -> persisted DebtorReport

Handlers call :meth:`SearchService.search` and nothing else. Whether a source is
a CSV file, an HTTP API or a future 1С instance is not visible from here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.config import Settings
from app.db.models import SearchResult
from app.db.repository import AuditRepository, SearchRepository
from app.db.session import Database
from app.domain.enums import ProviderName, ProviderStatus, SearchType
from app.domain.identity import SearchSubject
from app.domain.models import (
    DebtorReport,
    FactRecord,
    InternalDebtorRecord,
    ProviderResult,
    RecoveryScore,
)
from app.logging_setup import get_logger
from app.providers.base import NO_CONTEXT, BaseProvider, FetchContext
from app.providers.identity_bridge import InnBridgeResult
from app.providers.internal.base import InternalDebtorProvider, InternalSourceError
from app.providers.registry import ProviderRegistry
from app.services.aggregation import Aggregator
from app.services.identity import IdentityMatcher
from app.services.scoring import RecoveryScoreEngine
from app.utils.dates import iso_or_none
from app.utils.hashing import stable_hash
from app.utils.masking import mask_inn, mask_name, mask_passport, mask_phone

logger = get_logger(__name__)

_RECORDS_ADAPTER: TypeAdapter[list[FactRecord]] = TypeAdapter(list[FactRecord])

# Records reached through an exact identifier are trusted at this level even
# without a date of birth: the identifier is our own and unique.
IDENTIFIER_MATCH_FLOOR = 0.95


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Отчёт вместе с идентификатором сохранённого запроса.

    Идентификатор нужен, чтобы выдать ссылку на веб-отчёт. Он не кладётся в
    саму модель отчёта: отчёт — это доменный объект, а номер строки в таблице
    к предметной области не относится.
    """

    report: DebtorReport
    request_id: int | None


class SearchService:
    """Runs a search and persists everything needed for history and caching."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        registry: ProviderRegistry,
        matcher: IdentityMatcher | None = None,
        aggregator: Aggregator | None = None,
        score_engine: RecoveryScoreEngine | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._registry = registry
        self._matcher = matcher or IdentityMatcher()
        self._aggregator = aggregator or Aggregator(self._matcher)
        self._score_engine = score_engine or RecoveryScoreEngine()
        self._semaphore = asyncio.Semaphore(settings.provider_concurrency)

    @property
    def registry(self) -> ProviderRegistry:
        """Источники, с которыми работает сервис.

        Нужен вызывающему, чтобы оценить стоимость массового прогона до его
        запуска; менять состав через это свойство нельзя.
        """
        return self._registry

    # ------------------------------------------------------------------ api

    async def search(
        self,
        subject: SearchSubject,
        *,
        telegram_user_id: int,
        force_refresh: bool = False,
        batch: bool = False,
    ) -> DebtorReport:
        """Run a full check and store it. Returns a cached report when fresh.

        ``batch`` говорит, что это один должник из прогона, и это не косметика:
        источники, чья цена считается на должника, читают его и решают, звать ли
        себя вообще — чтобы прогон на восемьсот строк не стоил восемьсот лишних
        вызовов молча.
        """
        outcome = await self.search_detailed(
            subject,
            telegram_user_id=telegram_user_id,
            force_refresh=force_refresh,
            batch=batch,
        )
        return outcome.report

    async def search_detailed(
        self,
        subject: SearchSubject,
        *,
        telegram_user_id: int,
        force_refresh: bool = False,
        batch: bool = False,
    ) -> SearchOutcome:
        """То же, что :meth:`search`, но с идентификатором запроса для ссылки."""
        query_hash = build_query_hash(subject)
        context = FetchContext(batch=batch)

        if not force_refresh and self._settings.cache_enabled:
            cached = await self._load_cached(subject, query_hash)
            if cached is not None:
                logger.info(
                    "search.cache_hit",
                    user_id=telegram_user_id,
                    search_type=subject.search_type,
                )
                return SearchOutcome(cached.report, cached.request_id)

        # Внутренняя база — первой, и это не косметика. Оператор вводит номер
        # телефона: заказчик ведёт должников в 1С, выгрузка приходит оттуда, и
        # ФИО с датой рождения лежат в ней рядом с номером. Без переноса их в
        # запрос ФССП и залоги отвечают «недостаточно данных» на человека,
        # которого мы только что нашли у себя.
        internal_records, internal_result, internal_exact = await self.lookup_internal_result(
            subject
        )
        subject = _enrich_from_internal(subject, internal_records, exact=internal_exact)

        # Мост паспорт→ИНН строго после ключа кэша (он считается по вопросу
        # оператора), после внутренней базы (вдруг ИНН уже там — это платный
        # вызов) и строго до внешней волны: три источника ищут только по ИНН.
        subject, bridge_result = await self._resolve_inn(subject)
        provider_results = await self._run_external(subject, context)
        if bridge_result is not None:
            provider_results = [bridge_result, *provider_results]

        report = self._aggregator.build(
            subject, [internal_result, *provider_results], internal_records=internal_records
        )
        report.recovery_score = self._score_engine.evaluate(report)

        request_id = await self._persist(
            report,
            telegram_user_id=telegram_user_id,
            query_hash=query_hash,
        )
        return SearchOutcome(report, request_id)

    async def lookup_internal(self, subject: SearchSubject) -> list[InternalDebtorRecord]:
        """Query our own records using whichever identifiers we have.

        Exact-identifier hits get a confidence floor; a name-based hit goes
        through the ordinary matcher like any external record.
        """
        records, _, _ = await self.lookup_internal_result(subject)
        return records

    async def lookup_internal_result(
        self, subject: SearchSubject
    ) -> tuple[list[InternalDebtorRecord], ProviderResult, bool]:
        """То же самое, но с состоянием источника рядом с записями.

        Внутренняя база — такой же источник, как ФССП, и обязана уметь сказать
        «я отработала и совпадений нет» отдельно от «я не отработала». Без
        этого упавшая база и нечитаемая выгрузка печатались бы той же строкой,
        что честный пустой ответ, а вердикт списывал бы это на неполноту
        выгрузки.
        """
        provider = self._registry.internal
        if not _has_internal_query(subject):
            return (
                [],
                ProviderResult(
                    provider=ProviderName.INTERNAL,
                    status=ProviderStatus.ERROR,
                    error_code="insufficient_query",
                    error_message="для поиска во внутренней базе нужны ФИО, договор или телефон",
                ),
                False,
            )
        try:
            records, exact = await self._internal_candidates(provider, subject)
        except InternalSourceError as exc:
            logger.warning("internal.lookup_failed", error=str(exc))
            return (
                [],
                ProviderResult(
                    provider=ProviderName.INTERNAL,
                    status=ProviderStatus.ERROR,
                    error_code="internal_source_failed",
                ),
                False,
            )

        if not records:
            return (
                [],
                ProviderResult(provider=ProviderName.INTERNAL, status=ProviderStatus.NO_RESULTS),
                False,
            )
        unique = _dedupe_internal(records)
        self._matcher.annotate(
            subject,
            list(unique),
            confidence_floor=IDENTIFIER_MATCH_FLOOR if exact else None,
        )
        # Записи живут в ``report.internal_records`` и не дублируются в
        # результате: здесь важно только состояние источника.
        return (
            unique,
            ProviderResult(provider=ProviderName.INTERNAL, status=ProviderStatus.SUCCESS),
            exact,
        )

    # ------------------------------------------------------------- internals

    async def _resolve_inn(
        self, subject: SearchSubject
    ) -> tuple[SearchSubject, ProviderResult | None]:
        """Обогатить субъект ИНН, полученным по паспорту, до внешней волны.

        Три источника — банкротство, статус ИП и арбитраж — ищут только по ИНН
        физлица, поэтому мост обязан отработать раньше них. Это единственная
        последовательная фаза в поиске: ФССП и залоги, которым ИНН не нужен,
        ждут её вместе со всеми. Цена в худшем случае — плюс
        ``request_timeout_seconds * 2`` (тридцать секунд на умолчаниях), и она
        записана здесь, а не спрятана.

        ``_guarded_fetch`` переиспользуется намеренно: мост получает тот же
        потолок и попадает в тот же семафор, что и остальные источники.

        Когда моста нет или он этому субъекту не нужен (ИНН уже есть), строки в
        отчёте не появляется вовсе: объяснять нечего.
        """
        bridge = self._registry.inn_bridge
        if bridge is None or not bridge.is_needed(subject):
            return subject, None
        result = await self._guarded_fetch(bridge, subject)
        inn = result.inn if isinstance(result, InnBridgeResult) else None
        if inn:
            subject = subject.model_copy(update={"inn": inn})
        return subject, result

    async def _internal_candidates(
        self, provider: InternalDebtorProvider, subject: SearchSubject
    ) -> tuple[list[InternalDebtorRecord], bool]:
        """Returns candidates plus whether they came from an exact identifier."""
        if subject.debtor_id:
            found = await provider.find_by_debtor_id(subject.debtor_id)
            if found:
                return _narrow_by_person(found, subject), True
        if subject.contract_number:
            found = await provider.find_by_contract(subject.contract_number)
            if found:
                return _narrow_by_person(found, subject), True
        if subject.claim_number:
            found = await provider.find_by_claim(subject.claim_number)
            if found:
                return _narrow_by_person(found, subject), True
        if subject.vehicle and subject.vehicle.vin:
            found = await provider.find_by_vin(subject.vehicle.vin)
            if found:
                return _narrow_by_person(found, subject), True
        if subject.vehicle and subject.vehicle.plate:
            found = await provider.find_by_plate(subject.vehicle.plate)
            if found:
                return _narrow_by_person(found, subject), True
        if subject.phone:
            found = await provider.find_by_phone(subject.phone)
            if found:
                return _narrow_by_person(found, subject), True

        candidates: list[InternalDebtorRecord] = []
        if subject.name:
            candidates.extend(
                await provider.find_by_fio(subject.name.full, birth_date=subject.birth_date)
            )
        if not candidates and subject.address:
            candidates.extend(await provider.find_by_address(subject.address))
        return candidates, False

    async def _run_external(
        self, subject: SearchSubject, context: FetchContext
    ) -> list[ProviderResult]:
        """Query every directly-addressable provider concurrently.

        Concurrency is capped, each provider is wrapped in its own timeout, and
        every outcome — including a timeout — becomes a ``ProviderResult``. One
        slow source cannot delay or void the rest of the report.

        Chained sources sit out this phase: their input is another source's
        answer, so they cannot run beside it.
        """
        providers = [item for item in self._registry.external if not item.is_chained]
        if not providers:
            return []
        results = await asyncio.gather(
            *(self._guarded_fetch(provider, subject, context) for provider in providers)
        )
        return list(results)

    async def _run_chained(
        self,
        subject: SearchSubject,
        results: Sequence[ProviderResult],
        context: FetchContext,
    ) -> list[ProviderResult]:
        """Second phase: sources fed by what the first phase found.

        Only one source works this way today — arbitration of the companies the
        debtor runs or owns, whose ИНН come out of the ФНС answer. It is still a
        provider and still produces exactly one ``ProviderResult``, so the
        report lists it beside the rest whether it ran, was switched off, or had
        nothing to work with.
        """
        providers = [item for item in self._registry.external if item.is_chained]
        if not providers:
            return []
        chained_context = FetchContext(batch=context.batch, upstream=tuple(results))
        chained = await asyncio.gather(
            *(self._guarded_fetch(provider, subject, chained_context) for provider in providers)
        )
        return list(chained)

    async def _guarded_fetch(
        self,
        provider: BaseProvider,
        subject: SearchSubject,
        context: FetchContext = NO_CONTEXT,
    ) -> ProviderResult:
        # A provider-level timeout on top of the HTTP timeout, so a provider that
        # polls or retries still has a hard ceiling. The ceiling is its own
        # setting: derived from the single-request timeout it cut asynchronous
        # methods off mid-poll, after the call had already been billed.
        budget = self._settings.provider_budget_seconds
        async with self._semaphore:
            try:
                return await asyncio.wait_for(provider.fetch(subject, context), timeout=budget)
            except TimeoutError:
                logger.warning("provider.budget_exceeded", provider=provider.name.value)
                return ProviderResult(
                    provider=provider.name,
                    status=ProviderStatus.UNAVAILABLE,
                    error_code="timeout",
                    error_message="Источник не ответил вовремя",
                    duration_ms=int(budget * 1000),
                )

    # ------------------------------------------------------------ persistence

    async def _persist(
        self,
        report: DebtorReport,
        *,
        telegram_user_id: int,
        query_hash: str,
    ) -> int | None:
        score = report.recovery_score
        if score is None:  # pragma: no cover - the caller always sets it
            return None
        async with self._database.session() as session:
            search_repo = SearchRepository(session)
            request = await search_repo.create_request(
                telegram_user_id=telegram_user_id,
                search_type=report.subject.search_type,
                normalized_query_hash=query_hash,
                masked_query=describe_subject(report.subject),
                subject_json=json.dumps(
                    redact_subject(
                        report.subject,
                        store_sensitive=self._settings.store_sensitive_identifiers,
                    ),
                    ensure_ascii=False,
                ),
            )
            await search_repo.save_provider_results(
                request.id,
                report.provider_results,
                store_raw=self._settings.store_raw_responses,
            )
            await search_repo.save_score(request.id, score)
            await AuditRepository(session).record(
                telegram_user_id=telegram_user_id,
                action="search.completed",
                entity_id=str(request.id),
                detail=f"{report.subject.search_type}: {score.score}/100",
            )
            return request.id

    async def load_report(self, request_id: int) -> DebtorReport | None:
        """Восстановить сохранённый отчёт по идентификатору запроса.

        Нужен веб-странице: ссылка живёт дольше сообщения в чате, и открывший
        её через день должен увидеть тот же отчёт, а не пустоту. Субъект
        берётся из сохранённого запроса, поэтому страница не зависит от
        состояния диалога.
        """
        async with self._database.session() as session:
            repo = SearchRepository(session)
            request = await repo.get_request(request_id)
            if request is None:
                return None
            subject = subject_from_json(request.subject_json)
            if subject is None:
                return None
            stored_results = await repo.results_for_request(request.id)
            created_at = request.created_at

        return await self._rebuild(subject, stored_results, created_at)

    async def _load_cached(self, subject: SearchSubject, query_hash: str) -> SearchOutcome | None:
        """Rebuild a recent report of the same subject from storage."""
        async with self._database.session() as session:
            repo = SearchRepository(session)
            request = await repo.find_cached_request(
                query_hash, ttl_hours=self._settings.cache_ttl_hours
            )
            if request is None:
                return None
            stored_results = await repo.results_for_request(request.id)
            stored_report = await repo.report_for_request(request.id)
            if stored_report is None:
                return None
            created_at = request.created_at
            request_id = request.id
            stored_subject = subject_from_json(request.subject_json)

        if stored_subject is not None:
            # Прогон шёл по субъекту, дополненному из 1С и мостом: оператор ввёл
            # телефон, а искали уже по ФИО, дате рождения и ИНН. Входящий
            # субъект несёт только исходный вопрос, поэтому дополнения
            # восстанавливаются из сохранённого.
            #
            # ИНН здесь главный. Восстановленные из БД записи его несут — их
            # кладут ``_searched_by_inn`` и ``_to_case``. Без него матчер не
            # начислит INN_MATCH_BONUS, сработает NO_DISCRIMINATOR_PENALTY, и
            # банкротство, показанное в первый раз как подтверждённое, во второй
            # станет WEAK и выпадет из is_usable: найденное исчезло бы при
            # повторном открытии того же отчёта.
            #
            # ФИО и дата рождения — по той же причине, и вдобавок отчёт ими
            # подписан. Пока восстанавливался один ИНН, повторно открытая
            # карточка поиска по телефону озаглавливалась замаскированным ИНН
            # вместо фамилии: тот же должник, найденный тем же запросом,
            # выглядел неопознанным.
            #
            # Заполняются только пустые поля: введённое оператором сейчас
            # приоритетнее сохранённого — он мог узнать про смену фамилии.
            restored = {
                field: getattr(stored_subject, field)
                for field in ("name", "birth_date", "inn")
                if getattr(subject, field) is None and getattr(stored_subject, field) is not None
            }
            if restored:
                subject = subject.model_copy(update=restored)

        if _has_unsettled_source(stored_results):
            # Молчание источника — не ответ, и кэшировать его на сутки значит
            # сделать «не проверено» окончательным. Оператор, повторивший
            # проверку, получал бы тот же провал, и починка на стороне сервиса
            # ничего бы не изменила до истечения кэша. Ровно так и вышло: после
            # починки поиска по телефону тот же номер продолжал отдавать
            # «недостаточно данных» из кэша, снятого до неё.
            #
            # Пересчитывается только неуложившееся: успешные ответы того же
            # запроса остаются в хранилище и подставятся из него.
            logger.info("search.cache_stale_partial", query_hash=query_hash)
            return None

        report = await self._rebuild(subject, stored_results, created_at)
        return SearchOutcome(report, request_id)

    async def _rebuild(
        self,
        subject: SearchSubject,
        stored_results: Sequence[SearchResult],
        created_at: datetime,
    ) -> DebtorReport:
        """Собрать отчёт из сохранённых ответов провайдеров."""
        results: list[ProviderResult] = []
        for row in stored_results:
            # Внутренняя база опрашивается заново, поэтому её сохранённое
            # состояние здесь пропускается: иначе источник попал бы в список
            # дважды и мог бы противоречить сам себе.
            if row.provider == ProviderName.INTERNAL.value:
                continue
            records = _deserialize_records(row.normalized_json)
            results.append(
                ProviderResult(
                    provider=ProviderName(row.provider),
                    status=ProviderStatus(row.provider_status),
                    fetched_at=row.fetched_at,
                    records=records,
                    notes=_deserialize_notes(row.notes_json),
                    error_code=row.error_code,
                    error_message=row.error_message,
                    duration_ms=row.duration_ms,
                    cache_hit=True,
                    is_partial=row.is_partial,
                )
            )

        internal_records, internal_result, _ = await self.lookup_internal_result(subject)
        report = self._aggregator.build(
            subject, [internal_result, *results], internal_records=internal_records
        )
        # The score is recomputed rather than read back: the rules may have
        # changed since the cached run, and recomputation is free.
        report.recovery_score = self._score_engine.evaluate(report)
        report.from_cache = True
        report.cached_at = created_at
        return report


def _deserialize_notes(payload: str) -> tuple[str, ...]:
    """Оговорки о неполноте ответа, сохранённые вместе с ним.

    Битое хранилище не должно превращать неполный ответ в полный, поэтому
    ``is_partial`` читается отдельной колонкой и остаётся верным даже здесь: без
    текста оговорка станет менее внятной, но не исчезнет.
    """
    try:
        raw: Any = json.loads(payload)
    except json.JSONDecodeError:
        return ()
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw)


def _deserialize_records(payload: str) -> list[FactRecord]:
    """Rebuild typed records from stored JSON, tolerating schema drift."""
    try:
        raw: Any = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    try:
        return _RECORDS_ADAPTER.validate_python(raw)
    except ValidationError:
        logger.warning("cache.record_schema_mismatch")
        return []


def _enrich_from_internal(
    subject: SearchSubject,
    records: Sequence[InternalDebtorRecord],
    *,
    exact: bool,
) -> SearchSubject:
    """Дополнить запрос тем, что нашлось в нашей же выгрузке.

    Сценарий, ради которого это написано, у заказчика основной: он ведёт
    должников в 1С, оператор помнит только номер телефона и вводит его. Мы
    находим строку выгрузки — и в ней рядом с номером лежат ФИО и дата
    рождения. Без переноса их в запрос ФССП и залоги отвечают «недостаточно
    данных» о человеке, которого мы только что опознали.

    Условие ``exact`` существенно: дополняем только когда запись найдена по
    точному идентификатору — телефону, номеру договора, идентификатору
    должника. Совпадение по одному имени для этого не годится: подставив дату
    рождения однофамильца, мы получили бы чужие производства, показанные как
    производства должника, и не отличили бы их потом ничем.

    Заполняются только пустые поля. То, что оператор ввёл руками, приоритетнее
    выгрузки: он мог знать про смену фамилии, которой в 1С ещё нет.
    """
    if not exact or not records:
        return subject

    update: dict[str, object] = {}
    if subject.name is None:
        found = next((record.name for record in records if record.name is not None), None)
        if found is not None:
            update["name"] = found
    if subject.birth_date is None:
        birth = next((record.birth_date for record in records if record.birth_date), None)
        if birth is not None:
            update["birth_date"] = birth
    if subject.inn is None:
        # Ради этой строки колонка ИНН и заведена в выгрузке: банкротство,
        # статус ИП и арбитраж ищут только по нему. Есть он в 1С — три
        # источника открываются от одного введённого телефона, и платный
        # запрос ИНН по паспорту не понадобится.
        found_inn = next((record.inn for record in records if record.inn), None)
        if found_inn is not None:
            update["inn"] = found_inn

    return subject.model_copy(update=update) if update else subject


def _narrow_by_person(
    records: list[InternalDebtorRecord], subject: SearchSubject
) -> list[InternalDebtorRecord]:
    """Отсеять из точного попадания тех, кто не сходится по ФИО и дате.

    Один телефон в 1С — обычное дело: семейный номер, номер поручителя, общий
    контакт по двум договорам. Точное попадание по такому ключу возвращает
    несколько РАЗНЫХ людей, и дальше вся цепочка — подстановка даты рождения,
    сумма долга, пошлина — брала первую попавшуюся строку. Отчёт выходил про
    Сидорову с долгом Петрова.

    Фильтр ставится тут, а не в матчере, потому что дело не в уверенности
    сопоставления: у точного попадания она максимальная у всех строк разом.
    Дело в том, что оператор уже сказал фамилию — и строки, которые ей
    противоречат, не про того человека.

    Ослабление одностороннее: если после фильтра не осталось никого, отдаём
    исходный список. Расхождение выгрузки с тем, что оператор набрал руками
    (девичья фамилия, опечатка в 1С), — это факт для отчёта, а не повод
    выбросить единственную найденную строку.
    """
    name = subject.name
    if name is None and subject.birth_date is None:
        return records
    kept = [record for record in records if _same_person(record, subject)]
    return kept or records


def _same_person(record: InternalDebtorRecord, subject: SearchSubject) -> bool:
    """Не противоречит ли строка выгрузки тому, что ввёл оператор.

    Именно «не противоречит», а не «совпадает»: пустое поле в выгрузке ничего
    не опровергает, и строка без даты рождения остаётся кандидатом. Сравниваются
    только те поля, которые есть с обеих сторон.
    """
    if subject.birth_date and record.birth_date and record.birth_date != subject.birth_date:
        return False
    name = subject.name
    if name is None:
        return True
    found = record.name
    if found is None:
        return True
    if not _same_word(found.last_name, name.last_name):
        return False
    if name.first_name and found.first_name and not _same_word(found.first_name, name.first_name):
        return False
    return not (
        name.middle_name
        and found.middle_name
        and not _same_word(found.middle_name, name.middle_name)
    )


def _same_word(left: str | None, right: str | None) -> bool:
    """Сравнение слов имени: регистр и «ё» разницей не считаются."""
    if not left or not right:
        return True
    return left.casefold().replace("ё", "е") == right.casefold().replace("ё", "е")


def _has_internal_query(subject: SearchSubject) -> bool:
    """Есть ли вообще с чем идти во внутреннюю базу."""
    vehicle = subject.vehicle
    return any(
        (
            subject.debtor_id,
            subject.contract_number,
            subject.claim_number,
            subject.phone,
            subject.name,
            subject.address,
            vehicle.vin if vehicle else None,
            vehicle.plate if vehicle else None,
        )
    )


def _dedupe_internal(records: Sequence[InternalDebtorRecord]) -> list[InternalDebtorRecord]:
    seen: set[str] = set()
    unique: list[InternalDebtorRecord] = []
    for record in records:
        key = stable_hash(
            record.debtor_id or "",
            record.full_name,
            iso_or_none(record.birth_date),
            record.contract_number,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def build_query_hash(subject: SearchSubject) -> str:
    """Stable identity of a query, used as the cache key.

    One-way: the stored hash cannot be turned back into the query.

    Считается **по вопросу оператора**, до обогащения: тот же вопрос через час
    должен попасть в кэш, а не оплатить мост заново.

    ``passport`` входит в ключ. Без него у ``SearchType.PASSPORT`` все прочие
    поля ``None``, и **любые** два поиска по паспорту делят одну запись кэша:
    при ``CACHE_TTL_HOURS=24`` второй оператор получил бы чужой отчёт с пометкой
    «из кэша». Хэш односторонний, так что паспорт в него можно класть.
    """
    vehicle = subject.vehicle
    return stable_hash(
        subject.search_type,
        subject.name.normalized if subject.name else None,
        iso_or_none(subject.birth_date),
        subject.phone,
        subject.passport,
        subject.inn,
        subject.address,
        subject.contract_number,
        subject.claim_number,
        subject.debtor_id,
        vehicle.plate if vehicle else None,
        vehicle.vin if vehicle else None,
        ",".join(sorted(subject.regions)),
    )


def redact_subject(subject: SearchSubject, *, store_sensitive: bool) -> dict[str, Any]:
    """Serialize a subject for storage, dropping what must not be retained.

    With ``STORE_SENSITIVE_IDENTIFIERS`` off — the default — the passport and the
    full phone number are removed before the query is written. A re-run of such a
    search therefore proceeds without them, which is the intended trade.

    ``inn`` is kept in both modes, and since the passport bridge it may be
    *derived*: obtained from ФНС by passport rather than typed by the operator.
    It stays because the correctness of the *second* showing depends on it — see
    :meth:`SearchService._load_cached`, where a report rebuilt without it demotes
    a confirmed bankruptcy to a weak match and drops it. The passport itself is
    still stripped; what survives is the twelve digits it was exchanged for.
    """
    payload = subject.model_dump(mode="json")
    if not store_sensitive:
        payload.pop("passport", None)
        payload.pop("phone", None)
    return payload


def subject_from_json(payload: str) -> SearchSubject | None:
    """Rebuild a stored subject; returns ``None`` if the record is unusable."""
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or not raw.get("search_type"):
        return None
    try:
        return SearchSubject.model_validate(raw)
    except ValidationError:
        logger.warning("history.subject_schema_mismatch")
        return None


def describe_subject(subject: SearchSubject) -> str:
    """A masked, human-readable label for the history list.

    Never contains a full phone number or passport.
    """
    search_type = subject.search_type
    if search_type == SearchType.CONTRACT.value:
        return subject.contract_number or subject.claim_number or subject.debtor_id or "—"
    if search_type == SearchType.PASSPORT.value:
        return mask_passport(subject.passport) or "—"
    vehicle_types = {
        SearchType.VEHICLE_PLATE.value,
        SearchType.VIN.value,
        SearchType.VEHICLE.value,
    }
    if search_type in vehicle_types:
        return subject.vehicle.title if subject.vehicle else "—"
    if search_type == SearchType.ADDRESS.value:
        return subject.address or "—"

    parts = [mask_name(subject.name.full) if subject.name else None]
    if subject.birth_date:
        parts.append(str(subject.birth_date.year))
    if subject.inn:
        # Поиск по одному ИНН иначе попадал бы в историю как «—»: ФИО у него
        # нет, а вернуться к такой строке потом невозможно.
        parts.append(mask_inn(subject.inn))
    if subject.phone:
        parts.append(mask_phone(subject.phone))
    label = ", ".join(part for part in parts if part)
    return label or "—"


__all__ = [
    "RecoveryScore",
    "SearchOutcome",
    "SearchService",
    "build_query_hash",
    "describe_subject",
    "redact_subject",
    "subject_from_json",
]


def _has_unsettled_source(stored_results: Sequence[SearchResult]) -> bool:
    """Есть ли в сохранённом отчёте источник, который так и не ответил.

    Улёгшимися считаются три состояния: ответил и нашёл, ответил и не нашёл,
    не подключён. Первые два — настоящие ответы, третье не изменится от
    повторной попытки: источник выключен настройкой, а не сетью.

    Остальное — ошибка обращения и временная недоступность — означает, что
    вопрос остался без ответа. Такой отчёт годится, чтобы показать его сразу,
    но не годится, чтобы подавать его вместо новой проверки: «не проверено» с
    отметкой «из кэша» читается как установленный факт.
    """
    settled = {
        ProviderStatus.SUCCESS.value,
        ProviderStatus.NO_RESULTS.value,
        ProviderStatus.NOT_CONFIGURED.value,
    }
    return any(row.provider_status not in settled for row in stored_results)
