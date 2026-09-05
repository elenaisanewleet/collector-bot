"""Арбитраж компаний должника — метод NewDB ``arbitr_legal``.

**Какой вопрос взыскателя это закрывает.** «У человека пусто, а у его ООО
обороты». Единственный из подключаемых источников, который на него отвечает.

**Откуда берётся вход.** Не от субъекта: метод принимает ИНН ЮРЛИЦА. Цепочка —
``egrul_ip`` отдаёт компании, где должник руководитель или участник, оттуда
берутся их ИНН, и по каждому проверяются дела. Поэтому источник запускается
второй фазой, после ФНС, и без ФНС не запускается вовсе.

**Чего он не говорит.** Что обороты компании — активы должника. Имущество ООО
не является имуществом участника: взыскание обращается на долю в уставном
капитале (ст. 74 ФЗ-229, ст. 25 ФЗ-14), а обороты — лишь оценка её стоимости.
Формулировка «у должника активы на N ₽» — прямой повод для иска не к тому лицу,
и в отчёте её нет.

**Почему отдельное имя источника, а не ``COURT``.** Блок «СУДЫ» и его факторы
скоринга написаны про дела самого должника и фильтруются по совпадению личности.
Дело ООО у должника-физлица это сопоставление заведомо не проходит: положенное
туда, оно либо потеряется за порогом, либо будет посчитано иском к человеку.

**Ловушка полей, проверенная живьём.** ``detail_info.parties.debtor.inn`` — это
ИНН процессуального оппонента, а не нашей компании: в живом ответе там
``7716863554`` (ООО «Космос Лоджистик»), тогда как проверялась ``9728012826``.
Нашу компанию несёт только ``query_inn`` обёртки, поэтому ``company_inn``
ставится кодом — тем ИНН, по которому шёл вызов, и ниоткуда больше.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.config import Settings
from app.db.session import Database
from app.domain.enums import (
    BusinessRole,
    BusinessStatus,
    CourtCaseRole,
    ProviderName,
    ProviderStatus,
)
from app.domain.identity import INN_ENTITY_LENGTH, SearchSubject
from app.domain.models import BusinessRelation, LegalEntityCase, ProviderResult
from app.logging_setup import get_logger
from app.providers.base import NO_CONTEXT, FetchContext, ProviderUnavailableError
from app.providers.mapping import as_text, dig
from app.providers.newdb import COUNTRY_RU, NewDBClient, NewDBFieldMaps, NewDBMethodProvider
from app.utils.dates import utcnow
from app.utils.formatting import pluralize_ru
from app.utils.hashing import normalize_token
from app.utils.money import parse_amount

logger = get_logger(__name__)

NEWDB_METHOD = "arbitr_legal"
MAX_CASES_PER_COMPANY = 50
CACHE_PREFIX = "newdb:arbitr_legal"

CASES_KEY = "cases"
DETAILED_CASES_KEY = "detailed_cases"

FNS_SILENT = "Список компаний не получен: ФНС не ответила"
NO_COMPANIES = "Действующих ролей в юрлицах не найдено — проверять нечего"
BATCH_DISABLED = "Арбитраж компаний в массовой проверке выключен настройкой ARBITR_LEGAL_IN_BATCH"

# Роли, при которых дела компании вообще о чём-то говорят взыскателю.
_INTERESTING_ROLES = frozenset({BusinessRole.DIRECTOR, BusinessRole.FOUNDER})

_CLOSED_TOKENS = frozenset(
    {"рассмотрено", "завершено", "завершён", "прекращено", "прекращён", "closed", "completed"}
)

_CASES_ADAPTER: TypeAdapter[list[LegalEntityCase]] = TypeAdapter(list[LegalEntityCase])


class NewDBLegalCasesProvider(NewDBMethodProvider):
    """Дела юрлиц, в которых должник — руководитель или участник."""

    name = ProviderName.COURT_LEGAL
    title = "Арбитраж компаний должника"
    methods = (NEWDB_METHOD,)
    is_chained = True

    def __init__(
        self,
        settings: Settings,
        field_maps: NewDBFieldMaps,
        client: NewDBClient | None = None,
        database: Database | None = None,
    ) -> None:
        super().__init__(settings, field_maps, client)
        self._database = database
        self._gate = asyncio.Semaphore(settings.arbitr_legal_concurrency)

    @property
    def is_configured(self) -> bool:
        return self._settings.arbitr_legal_configured

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        # Источник цепочечный: без ответа ФНС ему не с чем работать, и приходить
        # сюда напрямую он не должен.
        return self.not_configured(FNS_SILENT)

    async def _dispatch(self, subject: SearchSubject, context: FetchContext) -> ProviderResult:
        if context.batch and not self._settings.arbitr_legal_in_batch:
            return self.not_configured(BATCH_DISABLED)

        upstream = context.result_for(ProviderName.FNS)
        if upstream is None or not upstream.is_answered:
            # «ФНС не ответила» и «компаний нет» — разные вещи, и слить их
            # значит выдать непроверенное за проверенное.
            return self.not_configured(FNS_SILENT)

        companies = _companies_of(upstream.records)
        if not companies:
            return ProviderResult(
                provider=self.name,
                status=ProviderStatus.NO_RESULTS,
                notes=(NO_COMPANIES,),
            )

        cap = self._settings.arbitr_legal_max_companies
        checked = companies[:cap]
        results = await asyncio.gather(*(self._cases_for(company) for company in checked))
        records = [case for batch in results for case in batch]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
            records=list(records),
            notes=_cap_notes(companies, checked=len(checked), cap=cap),
        )

    def planned_calls(self, subject: SearchSubject, context: FetchContext = NO_CONTEXT) -> int:
        """Ноль: длина цепочки известна только после ответа ФНС."""
        return 0

    def max_planned_calls(self, subject: SearchSubject, context: FetchContext = NO_CONTEXT) -> int:
        if not self.is_configured:
            return 0
        if context.batch and not self._settings.arbitr_legal_in_batch:
            return 0
        return self._settings.arbitr_legal_max_companies

    # ------------------------------------------------------------- one company

    async def _cases_for(self, company: BusinessRelation) -> list[LegalEntityCase]:
        inn = company.inn or ""
        cached = await self._cached(inn)
        if cached is not None:
            return [_rebind(case, company) for case in cached]
        async with self._gate:
            rows, _raw = await self.raw_rows_for(NEWDB_METHOD, {"country": COUNTRY_RU, "inn": inn})
        cases = _parse_cases(rows, company=company)
        await self._store(inn, cases)
        return cases

    async def _cached(self, inn: str) -> list[LegalEntityCase] | None:
        if self._database is None or not self._settings.cache_enabled:
            return None
        from app.db.repository import VendorCacheRepository

        async with self._database.session() as session:
            payload = await VendorCacheRepository(session).get(
                f"{CACHE_PREFIX}:{inn}", ttl_hours=self._settings.cache_ttl_hours
            )
        if payload is None:
            return None
        try:
            return _CASES_ADAPTER.validate_python(json.loads(payload))
        except (json.JSONDecodeError, ValidationError):
            logger.warning("arbitr_legal.cache_schema_mismatch")
            return None

    async def _store(self, inn: str, cases: Sequence[LegalEntityCase]) -> None:
        if self._database is None or not self._settings.cache_enabled:
            return
        from app.db.repository import VendorCacheRepository

        payload = json.dumps([case.model_dump(mode="json") for case in cases], ensure_ascii=False)
        async with self._database.session() as session:
            await VendorCacheRepository(session).put(f"{CACHE_PREFIX}:{inn}", payload)


# ---------------------------------------------------------------- companies


def _companies_of(records: Sequence[Any]) -> list[BusinessRelation]:
    """Компании, дела которых имеет смысл проверять.

    Действующие юрлица, где должник руководитель или участник, с ИНН из десяти
    цифр. Компании в банкротстве идут первыми: там и субсидиарка, и очередь
    кредиторов, то есть самое интересное на единицу потраченного.
    """
    seen: set[str] = set()
    companies: list[BusinessRelation] = []
    for record in records:
        if not isinstance(record, BusinessRelation) or not record.is_legal_entity:
            continue
        if record.role not in _INTERESTING_ROLES:
            continue
        if record.status is not BusinessStatus.ACTIVE:
            continue
        inn = record.inn or ""
        if len(inn) != INN_ENTITY_LENGTH or inn in seen:
            continue
        seen.add(inn)
        companies.append(record)
    companies.sort(key=lambda item: not item.bankruptcy_flag)
    return companies


def _cap_notes(companies: Sequence[BusinessRelation], *, checked: int, cap: int) -> tuple[str, ...]:
    skipped = len(companies) - checked
    if skipped <= 0:
        return ()
    noun = pluralize_ru(checked, "компания", "компании", "компаний")
    return (
        f"Проверено {checked} {noun} из {len(companies)}. Остальные {skipped} "
        f"не проверялись — лимит настройки ARBITR_LEGAL_MAX_COMPANIES ({cap}). "
        "Отсутствие дел по ним не установлено.",
    )


# ---------------------------------------------------------------- cases


def _parse_cases(rows: Sequence[Any], *, company: BusinessRelation) -> list[LegalEntityCase]:
    inn = company.inn or ""
    cases: list[LegalEntityCase] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        detailed = _mappings(row.get(DETAILED_CASES_KEY))
        listed = _mappings(row.get(CASES_KEY))
        if listed and not detailed:
            raise ProviderUnavailableError(
                "unexpected_schema",
                f"КАД вернул дела компании {inn}, но ни одно не разобрано",
            )
        total = row.get("total_count")
        for entry in detailed[:MAX_CASES_PER_COMPANY]:
            case = _to_case(
                entry,
                company=company,
                total_count=total if isinstance(total, int) else None,
                analyzed_count=len(detailed),
            )
            if case is not None:
                cases.append(case)
    return cases


def _to_case(
    entry: Mapping[str, Any],
    *,
    company: BusinessRelation,
    total_count: int | None,
    analyzed_count: int,
) -> LegalEntityCase | None:
    case_number = as_text(entry.get("case_number"))
    if case_number is None:
        return None
    inn = company.inn or ""
    detail = dig(entry, "card_pdf_analysis.detail_info")
    role = _role_of(entry, detail, inn=inn, name=company.name)
    opponent = _opponent(detail, inn=inn)
    status = as_text(entry.get("status"))
    return LegalEntityCase(
        company_inn=inn,
        company_name=company.name,
        company_role=company.role,
        company_bankruptcy_flag=company.bankruptcy_flag,
        case_number=case_number,
        court_name=as_text(dig(entry, "judges.0.court")),
        status=status,
        is_closed=_is_closed(status),
        case_role=role,
        opponent_name=as_text(dig(opponent, "name")),
        opponent_inn=as_text(dig(opponent, "inn")),
        amount=_amount(dig(detail, "financials.total_amount")),
        enforcement_signal=dig(detail, "risk.enforcement_signal") is True,
        personal_asset_risk=as_text(dig(detail, "risk.personal_asset_risk")),
        risk_factors=tuple(
            text for item in _sequence(dig(detail, "risk.risk_factors")) if (text := as_text(item))
        ),
        total_count=total_count,
        analyzed_count=analyzed_count,
        source_url=as_text(entry.get("source_url")),
        fetched_at=utcnow(),
    )


def _role_of(entry: Mapping[str, Any], detail: Any, *, inn: str, name: str | None) -> CourtCaseRole:
    """Кем компания проходит по делу.

    ИНН из разбора карточки — первый признак: он точнее имён, которые вендор
    печатает то как «ООО», то как «ПАО», то полным наименованием. Списки
    участников — запасной путь.
    """
    if as_text(dig(detail, "parties.claimant.inn")) == inn:
        return CourtCaseRole.PLAINTIFF
    if as_text(dig(detail, "parties.debtor.inn")) == inn:
        return CourtCaseRole.DEFENDANT
    if _names_include(dig(entry, "participants.defendants"), name):
        return CourtCaseRole.DEFENDANT
    if _names_include(dig(entry, "participants.plaintiffs"), name):
        return CourtCaseRole.PLAINTIFF
    return CourtCaseRole.OTHER


def _opponent(detail: Any, *, inn: str) -> Any:
    """Вторая сторона дела — та, чей ИНН не наш."""
    claimant = dig(detail, "parties.claimant")
    debtor = dig(detail, "parties.debtor")
    if as_text(dig(claimant, "inn")) == inn:
        return debtor
    if as_text(dig(debtor, "inn")) == inn:
        return claimant
    return None


def _names_include(participants: Any, name: str | None) -> bool:
    if not name:
        return False
    target = normalize_token(name)
    for item in _sequence(participants):
        raw = item.get("name") if isinstance(item, Mapping) else item
        token = normalize_token(as_text(raw) or "")
        if token and token == target:
            return True
    return False


def _rebind(case: LegalEntityCase, company: BusinessRelation) -> LegalEntityCase:
    """Кэш хранится по ИНН компании, а роль должника в ней — свойство должника.

    Одно и то же ООО у одного человека может быть «руководитель», у другого —
    «учредитель», и подставлять чужую роль из кэша нельзя.
    """
    return case.model_copy(
        update={
            "company_role": company.role,
            "company_name": company.name or case.company_name,
            "company_bankruptcy_flag": company.bankruptcy_flag,
        }
    )


def _is_closed(status: str | None) -> bool:
    token = (status or "").lower()
    return any(marker in token for marker in _CLOSED_TOKENS)


def _amount(raw: Any) -> Decimal | None:
    return parse_amount(raw) if isinstance(raw, (int, float, str, Decimal)) else None


def _mappings(node: Any) -> list[Mapping[str, Any]]:
    return [item for item in _sequence(node) if isinstance(item, Mapping)]


def _sequence(node: Any) -> list[Any]:
    if not isinstance(node, Sequence) or isinstance(node, (str, bytes)):
        return []
    return list(node)


__all__ = ["NEWDB_METHOD", "NewDBLegalCasesProvider"]
