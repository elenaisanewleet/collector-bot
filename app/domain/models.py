"""Normalized facts and the aggregated report.

Every fact carries where it came from, when it was fetched and how confident we
are that it belongs to the subject. A fact without provenance is not a fact we
are willing to show an operator.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    BankruptcyStatus,
    BusinessRole,
    BusinessStatus,
    CourtCaseRole,
    EntityType,
    MatchLevel,
    PledgeStatus,
    ProceedingStatus,
    ProviderName,
    ProviderStatus,
)
from app.domain.identity import PersonName, SearchSubject
from app.utils.dates import utcnow

CONFIRMED_MATCH_THRESHOLD = 0.85
PROBABLE_MATCH_THRESHOLD = 0.55


def match_level_for(confidence: float) -> MatchLevel:
    if confidence >= CONFIRMED_MATCH_THRESHOLD:
        return MatchLevel.CONFIRMED
    if confidence >= PROBABLE_MATCH_THRESHOLD:
        return MatchLevel.PROBABLE
    return MatchLevel.WEAK


class SourcedFact(BaseModel):
    """Base for anything a provider returns."""

    model_config = ConfigDict(frozen=False)

    provider: ProviderName
    fetched_at: datetime = Field(default_factory=utcnow)
    source_url: str | None = None
    # Filled in by the IdentityMatcher, not by the provider: a provider cannot
    # know how well its record matches the operator's subject.
    match_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    match_reasons: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def match_level(self) -> MatchLevel:
        return match_level_for(self.match_confidence)

    @property
    def is_confirmed(self) -> bool:
        return self.match_level is MatchLevel.CONFIRMED

    @property
    def is_usable(self) -> bool:
        """Weak matches are displayed but never drive the score."""
        return self.match_level in {MatchLevel.CONFIRMED, MatchLevel.PROBABLE}


class InternalDebtorRecord(SourcedFact):
    """A debtor as our own systems know them."""

    kind: Literal["internal"] = "internal"
    provider: ProviderName = ProviderName.INTERNAL

    debtor_id: str | None = None
    full_name: str | None = None
    birth_date: date | None = None
    # Raw phone only when STORE_SENSITIVE_IDENTIFIERS is enabled; the masked
    # form is always available and is what the report displays.
    phone: str | None = None
    phone_masked: str | None = None
    inn: str | None = None
    contract_number: str | None = None
    claim_number: str | None = None
    debt_amount: Decimal | None = None
    address: str | None = None
    vehicle_plate: str | None = None
    vin: str | None = None
    created_at: datetime | None = None

    @property
    def name(self) -> PersonName | None:
        from app.domain.identity import NameParseError, parse_fio

        if not self.full_name:
            return None
        try:
            return parse_fio(self.full_name)
        except NameParseError:
            return None


class EnforcementProceeding(SourcedFact):
    """Исполнительное производство (ФССП)."""

    kind: Literal["enforcement"] = "enforcement"
    provider: ProviderName = ProviderName.FSSP

    proceeding_number: str
    debtor_name: str | None = None
    debtor_birth_date: date | None = None
    amount: Decimal | None = None
    currency: str = "RUB"
    status: ProceedingStatus = ProceedingStatus.UNKNOWN
    status_text: str | None = None
    subject: str | None = None
    department: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status is ProceedingStatus.ACTIVE


class BankruptcyRecord(SourcedFact):
    """Сведения о банкротстве (ЕФРСБ)."""

    kind: Literal["bankruptcy"] = "bankruptcy"
    provider: ProviderName = ProviderName.FEDRESURS

    debtor_name: str | None = None
    debtor_type: EntityType = EntityType.INDIVIDUAL
    # Дата рождения должника из блока ``commmon`` живого ответа
    # ``bankrot_person``. Нужна ровно для одного — отождествления: дело
    # адресовано по ИНН, а ФИО в ЕФРСБ может быть девичьим или записанным
    # иначе, и без даты рождения несовпадение фамилии обнуляло сопоставление.
    # В отчёт и в логи не попадает: это персональные данные, а не факт о долге.
    debtor_birth_date: date | None = None
    inn: str | None = None
    case_number: str | None = None
    procedure: str | None = None
    status: BankruptcyStatus = BankruptcyStatus.UNKNOWN
    started_at: date | None = None
    completed_at: date | None = None
    message_date: date | None = None

    @property
    def is_active(self) -> bool:
        return self.status is BankruptcyStatus.ACTIVE


class BusinessRelation(SourcedFact):
    """Связь с ИП или юрлицом (ЕГРЮЛ / ЕГРИП)."""

    kind: Literal["business"] = "business"
    provider: ProviderName = ProviderName.FNS

    inn: str | None = None
    ogrn: str | None = None
    name: str | None = None
    # ФИО связанного физлица, когда строка источника описывает не компанию, а
    # человека в реестре: ``egrul_ip`` отдаёт секции ip / upr / uchr, и в них
    # ``name_short`` — это ФИО должника, а не название ЮЛ. Отдельным полем, а не
    # через ``name``, потому что сопоставлять ФИО с названием компании нельзя:
    # так изготавливаются совпадения, которых нет.
    person_name: str | None = None
    entity_type: EntityType = EntityType.LEGAL_ENTITY
    status: BusinessStatus = BusinessStatus.UNKNOWN
    role: BusinessRole = BusinessRole.OTHER
    registration_date: date | None = None
    termination_date: date | None = None
    # Связь получена в ответ на запрос по идентификатору должника (его ИНН), а
    # не по одному ФИО. Для юрлица это единственное доступное основание:
    # название ООО не является именем человека, и сопоставлять их не с чем.
    # Компания, найденная по ФИО, остаётся возможной однофамильческой.
    linked_by_identifier: bool = False

    @property
    def is_active(self) -> bool:
        return self.status is BusinessStatus.ACTIVE

    @property
    def is_legal_entity(self) -> bool:
        return self.entity_type is EntityType.LEGAL_ENTITY

    @property
    def is_active_sole_proprietor(self) -> bool:
        return self.is_active and self.role is BusinessRole.SOLE_PROPRIETOR


class CourtCase(SourcedFact):
    """Судебное дело.

    Served by the NewDB ``arbitr_person`` method where a deployment has mapped
    it; otherwise the source stays unconnected and the shape sits here so that
    connecting one is a registration rather than a redesign.
    """

    kind: Literal["court"] = "court"
    provider: ProviderName = ProviderName.COURT

    case_number: str
    court_name: str | None = None
    case_type: str | None = None
    status: str | None = None
    amount: Decimal | None = None
    filed_at: date | None = None
    # Who the case is about, so the record can be identity-matched rather than
    # trusted because it came back from a search.
    participant_name: str | None = None
    inn: str | None = None
    role: CourtCaseRole = CourtCaseRole.OTHER
    is_closed: bool = False

    @property
    def is_active(self) -> bool:
        return not self.is_closed

    @property
    def is_against_debtor(self) -> bool:
        """A live claim by somebody else — a creditor competing with us."""
        return self.is_active and self.role is CourtCaseRole.DEFENDANT


class PledgeRecord(SourcedFact):
    """Уведомление о залоге движимого имущества (реестр ФНП).

    Matters for exactly one reason: a pledged thing is not free collateral. The
    pledgeholder is satisfied ahead of us, so finding the debtor's only car in
    the register turns an apparent asset into somebody else's security.
    """

    kind: Literal["pledge"] = "pledge"
    provider: ProviderName = ProviderName.PLEDGE

    registration_number: str | None = None
    registered_at: date | None = None
    terminated_at: date | None = None
    pledgor_name: str | None = None
    pledgor_birth_date: date | None = None
    pledgor_inn: str | None = None
    pledgee_name: str | None = None
    subject: str | None = None
    vin: str | None = None
    status: PledgeStatus = PledgeStatus.UNKNOWN

    @property
    def is_active(self) -> bool:
        return self.status is PledgeStatus.ACTIVE


class VehicleRecord(SourcedFact):
    kind: Literal["vehicle"] = "vehicle"
    provider: ProviderName = ProviderName.VEHICLE

    plate: str | None = None
    vin: str | None = None
    make: str | None = None
    model: str | None = None
    year: int | None = None
    owner_name: str | None = None
    restrictions: tuple[str, ...] = Field(default_factory=tuple)


class PropertyRecord(SourcedFact):
    """Объект недвижимости по известному нам адресу или кадастровому номеру.

    Это запись **об объекте**, а не об имуществе должника. ЕГРН сведения о
    правах конкретного лица выдаёт только самому лицу, суду и приставу, поэтому
    правообладателя в ответе нет: живой ответ показывает четыре записи о правах
    и ни одного ФИО. Отсюда :attr:`owner_confirmed`, которое этот источник
    никогда не выставляет в ``True``.
    """

    kind: Literal["property"] = "property"
    provider: ProviderName = ProviderName.PROPERTY

    property_type: str | None = None
    cadastral_number: str | None = None
    address: str | None = None
    # Осталось ради записей, уже сохранённых в БД; rosreestr его не заполняет —
    # долей в ответе может быть несколько, и они лежат в ``shares``.
    share: str | None = None
    encumbrances: tuple[str, ...] = Field(default_factory=tuple)

    area: str | None = None
    cadastral_cost: Decimal | None = None
    cost_date: date | None = None
    registered_at: date | None = None
    cancelled_at: date | None = None
    rights_count: int = 0
    shares: tuple[str, ...] = Field(default_factory=tuple)
    right_types: tuple[str, ...] = Field(default_factory=tuple)
    # Пустой массив обременений в ответе — это проверено и чисто; отсутствие
    # ответа — это не проверено. Различать их без флага нечем.
    encumbrances_checked: bool = False
    # Несущее поле: принадлежность объекта должнику. Источник его не
    # подтверждает, поэтому оно остаётся False, и скоринг смотрит именно сюда.
    owner_confirmed: bool = False


class LegalEntityCase(SourcedFact):
    """Арбитражное дело компании, в которой должник — руководитель или участник.

    Факт о компании, а не о человеке. Матчинг по ФИО к нему неприменим, и блок
    отчёта его по совпадению не фильтрует: дело ООО заведомо не пройдёт
    сопоставление с физлицом, а отбросить его значило бы оплатить находку и
    промолчать о ней.

    Имущество ООО не является имуществом участника (ст. 25 ФЗ-14, ст. 74
    ФЗ-229): обороты компании — это оценка стоимости доли, а не активы должника.
    """

    kind: Literal["legal_case"] = "legal_case"
    provider: ProviderName = ProviderName.COURT_LEGAL

    # ИНН компании ставится кодом — тем, по которому шёл вызов. В разборе
    # карточки ``parties.debtor.inn`` лежит ИНН процессуального оппонента, и
    # запись, взявшая ИНН оттуда, привязалась бы к постороннему юрлицу.
    company_inn: str
    company_name: str | None = None
    company_role: BusinessRole = BusinessRole.OTHER

    case_number: str
    court_name: str | None = None
    status: str | None = None
    is_closed: bool = False
    case_role: CourtCaseRole = CourtCaseRole.OTHER
    opponent_name: str | None = None
    opponent_inn: str | None = None
    amount: Decimal | None = None
    enforcement_signal: bool = False
    personal_asset_risk: str | None = None
    risk_factors: tuple[str, ...] = Field(default_factory=tuple)

    # Оговорка «разобрано N из M» по одной компании.
    total_count: int | None = None
    analyzed_count: int | None = None

    @property
    def is_active(self) -> bool:
        return not self.is_closed

    @property
    def is_claim_against_company(self) -> bool:
        return self.case_role is CourtCaseRole.DEFENDANT


FactRecord = Annotated[
    InternalDebtorRecord
    | EnforcementProceeding
    | BankruptcyRecord
    | BusinessRelation
    | CourtCase
    | LegalEntityCase
    | PledgeRecord
    | VehicleRecord
    | PropertyRecord,
    Field(discriminator="kind"),
]


class ProviderResult(BaseModel):
    """The outcome of one provider call — never an exception.

    A provider that fails still produces one of these, so the report can say
    exactly which sources answered and which did not.
    """

    model_config = ConfigDict(frozen=False)

    provider: ProviderName
    status: ProviderStatus
    fetched_at: datetime = Field(default_factory=utcnow)
    records: list[FactRecord] = Field(default_factory=list)
    # Оговорки самого источника: «разобрано 10 из 47», «проверено 3 компании из
    # 7 — остальные не проверялись». Они сохраняются вместе с результатом и
    # переживают кэш: предел, о котором отчёт умолчал после перезапуска, — это
    # та же инверсия, только отложенная.
    notes: tuple[str, ...] = Field(default_factory=tuple)
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    cache_hit: bool = False
    # Only populated when STORE_RAW_RESPONSES is enabled; otherwise dropped as
    # soon as parsing is done.
    raw_response: str | None = None
    # «Источник ответил, но не всё». Между «проверено, чисто» и «не проверено»
    # есть третий ответ, и до появления этих двух полей его негде было сказать:
    # арбитраж отдаёт десять дел из сорока (``pagination.has_more``), ФНП
    # находит тринадцать уведомлений и ни одного не сопоставляет по дате
    # рождения (``fnp_urls`` при пустом ``fnp``). В обоих случаях записей
    # приходит меньше, чем нашёл источник, и молчание об этом — то самое
    # «найдено, показано как не найдено».
    #
    # ``is_partial`` снимает положительные факторы скоринга («залогов нет»,
    # «исков нет»): их смысл — «мы посмотрели и ничего не увидели», а здесь
    # посмотрели не всё. ``notes`` — то, что об этом обязан сказать отчёт.
    is_partial: bool = False
    # Какие поля субъекта надо было дать, чтобы источник вообще опросили. Живёт
    # рядом с ``error_code == "insufficient_query"`` и существует ради одной
    # вещи: карточка обязана сгруппировать «ФССП, Залоги — нужна дата рождения»
    # машинно, а не разбором собственного текста. Значения — из
    # :class:`app.domain.enums.MissingInput`; хранится как кортеж строк, потому
    # что поле переживает сериализацию в кэш вместе со всей моделью.
    #
    # Умолчание пустое намеренно: записи, восстановленные из БД (колонки под это
    # нет), валидируются без миграции, а отчёт по ним честно откатывается на
    # ``error_message``.
    missing_input: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def is_answered(self) -> bool:
        return self.status.is_answered

    @property
    def is_failure(self) -> bool:
        return self.status in {ProviderStatus.UNAVAILABLE, ProviderStatus.ERROR}


class ScoreFactor(BaseModel):
    """One explainable contribution to the recovery score."""

    model_config = ConfigDict(frozen=True)

    name: str
    delta: int
    reason: str
    source: ProviderName


class RecoveryScore(BaseModel):
    """Score, category, confidence and the full derivation."""

    model_config = ConfigDict(frozen=True)

    score: int = Field(ge=0, le=100)
    base_score: int
    category: str
    confidence: float = Field(ge=0.0, le=1.0)
    factors: tuple[ScoreFactor, ...] = Field(default_factory=tuple)
    confidence_notes: tuple[str, ...] = Field(default_factory=tuple)


class DebtorReport(BaseModel):
    """Everything the operator sees, assembled from all sources."""

    model_config = ConfigDict(frozen=False)

    subject: SearchSubject
    generated_at: datetime = Field(default_factory=utcnow)
    internal_records: list[InternalDebtorRecord] = Field(default_factory=list)
    enforcement_proceedings: list[EnforcementProceeding] = Field(default_factory=list)
    bankruptcies: list[BankruptcyRecord] = Field(default_factory=list)
    business_relations: list[BusinessRelation] = Field(default_factory=list)
    court_cases: list[CourtCase] = Field(default_factory=list)
    legal_entity_cases: list[LegalEntityCase] = Field(default_factory=list)
    pledges: list[PledgeRecord] = Field(default_factory=list)
    vehicles: list[VehicleRecord] = Field(default_factory=list)
    properties: list[PropertyRecord] = Field(default_factory=list)
    provider_results: list[ProviderResult] = Field(default_factory=list)
    recovery_score: RecoveryScore | None = None
    from_cache: bool = False
    cached_at: datetime | None = None

    @property
    def internal_record(self) -> InternalDebtorRecord | None:
        """The best internal match, when there is one."""
        if not self.internal_records:
            return None
        return max(self.internal_records, key=lambda record: record.match_confidence)

    @property
    def active_proceedings(self) -> list[EnforcementProceeding]:
        return [item for item in self.enforcement_proceedings if item.is_active and item.is_usable]

    @property
    def active_bankruptcies(self) -> list[BankruptcyRecord]:
        return [item for item in self.bankruptcies if item.is_active and item.is_usable]

    @property
    def active_pledges(self) -> list[PledgeRecord]:
        return [item for item in self.pledges if item.is_active and item.is_usable]

    @property
    def claims_against_debtor(self) -> list[CourtCase]:
        return [item for item in self.court_cases if item.is_against_debtor and item.is_usable]

    @property
    def total_enforcement_amount(self) -> Decimal:
        return sum(
            (item.amount for item in self.active_proceedings if item.amount is not None),
            Decimal("0"),
        )

    def result_for(self, provider: ProviderName) -> ProviderResult | None:
        return next((item for item in self.provider_results if item.provider is provider), None)
