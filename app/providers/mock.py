"""Deterministic demo providers.

``APP_MODE=demo`` wires these in place of the HTTP adapters so the entire
pipeline — search, matching, aggregation, scoring, reporting — can be exercised
and tested without a single credential.

They are deterministic by construction: the same subject always yields the same
records, derived from a hash of the normalized name. Three fictional debtors have
hand-written fixtures covering a high, a medium and a low recovery outcome; any
other name gets a stable synthetic profile so the demo stays explorable.

Every report generated in demo mode is labelled as such. These providers are
never selected when ``APP_MODE=live``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.domain.enums import (
    BankruptcyStatus,
    BusinessRole,
    BusinessStatus,
    CourtCaseRole,
    EntityType,
    PledgeStatus,
    ProceedingStatus,
    ProviderName,
    ProviderStatus,
)
from app.domain.identity import SearchSubject
from app.domain.models import (
    BankruptcyRecord,
    BusinessRelation,
    CourtCase,
    EnforcementProceeding,
    PledgeRecord,
    ProviderResult,
)
from app.providers.base import BaseProvider
from app.providers.identity_bridge import (
    InnBridgeProvider,
    InnBridgeResult,
    missing_bridge_input,
)
from app.utils.hashing import normalize_token

DEMO_SOURCE_NOTE = "demo"


@dataclass(frozen=True, slots=True)
class DemoProfile:
    """A fictional debtor's external footprint."""

    full_name: str
    birth_date: date
    proceedings: tuple[tuple[str, str, Decimal, ProceedingStatus], ...] = ()
    bankruptcy: tuple[str, str, BankruptcyStatus, date | None] | None = None
    businesses: tuple[tuple[str, str, BusinessRole, BusinessStatus], ...] = ()
    # (предмет залога, залогодержатель, VIN, состояние)
    pledges: tuple[tuple[str, str, str | None, PledgeStatus], ...] = ()
    # (номер дела, суд, сумма, роль должника, дело закрыто)
    court_cases: tuple[tuple[str, str, Decimal, CourtCaseRole, bool], ...] = ()
    inn: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Fixtures. All persons are invented; any resemblance to real records is
# unintended.
# ---------------------------------------------------------------------------

DEMO_PROFILES: dict[str, DemoProfile] = {
    # High prospect: no enforcement, no bankruptcy, an active sole proprietorship.
    "тестов андрей сергеевич": DemoProfile(
        full_name="Тестов Андрей Сергеевич",
        birth_date=date(1985, 3, 12),
        inn="770912345601",
        businesses=(
            (
                "770912345601",
                "ИП Тестов Андрей Сергеевич",
                BusinessRole.SOLE_PROPRIETOR,
                BusinessStatus.ACTIVE,
            ),
        ),
    ),
    # Medium prospect: a couple of live proceedings against an active director role.
    "примеров алексей олегович": DemoProfile(
        full_name="Примеров Алексей Олегович",
        birth_date=date(1979, 7, 24),
        inn="503812345602",
        proceedings=(
            (
                "18453/26/50012-ИП",
                "Взыскание задолженности по кредитным платежам",
                Decimal("94300"),
                ProceedingStatus.ACTIVE,
            ),
            (
                "18454/26/50012-ИП",
                "Взыскание исполнительского сбора",
                Decimal("6601"),
                ProceedingStatus.ACTIVE,
            ),
        ),
        businesses=(
            (
                "5038123456",
                'ООО "Демонстрационные решения"',
                BusinessRole.DIRECTOR,
                BusinessStatus.ACTIVE,
            ),
        ),
        # Машина есть — но она в залоге, то есть считать её нашим обеспечением
        # нельзя. Ради этого различия источник и подключён.
        pledges=(
            (
                "Автомобиль LADA VESTA, 2021",
                'АО "Демонстрационный банк"',
                "XTA1234567890ABCD",
                PledgeStatus.ACTIVE,
            ),
        ),
    ),
    # Low prospect: active bankruptcy plus a heavy enforcement load.
    "демов максим игоревич": DemoProfile(
        full_name="Демов Максим Игоревич",
        birth_date=date(1990, 11, 3),
        inn="771812345603",
        proceedings=(
            (
                "77012/26/77018-ИП",
                "Иные взыскания имущественного характера",
                Decimal("412800"),
                ProceedingStatus.ACTIVE,
            ),
            (
                "77013/26/77018-ИП",
                "Взыскание задолженности по кредитным платежам",
                Decimal("318400"),
                ProceedingStatus.ACTIVE,
            ),
            (
                "77014/26/77018-ИП",
                "Взыскание налогов и сборов",
                Decimal("96150"),
                ProceedingStatus.ACTIVE,
            ),
            (
                "77015/26/77018-ИП",
                "Взыскание исполнительского сбора",
                Decimal("41280"),
                ProceedingStatus.ACTIVE,
            ),
            (
                "77016/26/77018-ИП",
                "Взыскание задолженности по договору займа",
                Decimal("205700"),
                ProceedingStatus.ACTIVE,
            ),
            (
                "77017/26/77018-ИП",
                "Взыскание судебных расходов",
                Decimal("18900"),
                ProceedingStatus.ACTIVE,
            ),
        ),
        bankruptcy=(
            "А40-118472/2026",
            "Реализация имущества гражданина",
            BankruptcyStatus.ACTIVE,
            None,
        ),
        businesses=(
            (
                "771812345603",
                "ИП Демов Максим Игоревич",
                BusinessRole.SOLE_PROPRIETOR,
                BusinessStatus.TERMINATED,
            ),
        ),
        pledges=(
            (
                "Автомобиль KIA RIO, 2019",
                'ООО МКК "Демонстрационные займы"',
                "Z94CB41AAKR123456",
                PledgeStatus.ACTIVE,
            ),
        ),
        court_cases=(
            (
                "А40-227414/2026",
                "Арбитражный суд города Москвы",
                Decimal("1180400"),
                CourtCaseRole.DEFENDANT,
                False,
            ),
        ),
    ),
}


def _seed(subject: SearchSubject) -> int:
    """Stable integer seed derived from the subject's name."""
    key = normalize_token(subject.display_name)
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)


def _profile_for(subject: SearchSubject) -> DemoProfile | None:
    if subject.name is None:
        return None
    return DEMO_PROFILES.get(subject.name.normalized)


class DemoFSSPProvider(BaseProvider):
    """Enforcement proceedings, demo edition."""

    name = ProviderName.FSSP
    title = "ФССП (демо)"

    @property
    def is_configured(self) -> bool:
        return True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        if subject.name is None:
            return self.insufficient_query("Нужно ФИО")

        profile = _profile_for(subject)
        if profile is not None:
            records = [
                _proceeding(number, purpose, amount, status, profile)
                for number, purpose, amount, status in profile.proceedings
            ]
        else:
            records = _synthetic_proceedings(subject)

        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
            records=list(records),
        )


def _proceeding(
    number: str,
    purpose: str,
    amount: Decimal,
    status: ProceedingStatus,
    profile: DemoProfile,
) -> EnforcementProceeding:
    return EnforcementProceeding(
        proceeding_number=number,
        debtor_name=profile.full_name,
        debtor_birth_date=profile.birth_date,
        amount=amount,
        status=status,
        subject=purpose,
        department="Демо-отдел судебных приставов",
        source_url=None,
    )


def _synthetic_proceedings(subject: SearchSubject) -> list[EnforcementProceeding]:
    """Stable pseudo-profile for names outside the fixture set."""
    seed = _seed(subject)
    count = seed % 4  # 0..3 proceedings
    assert subject.name is not None
    return [
        EnforcementProceeding(
            proceeding_number=f"{10000 + seed % 80000 + index}/26/77001-ИП",
            debtor_name=subject.name.full,
            debtor_birth_date=subject.birth_date,
            amount=Decimal(str(15000 + (seed % 97) * 1000 + index * 3700)),
            status=ProceedingStatus.ACTIVE,
            subject="Взыскание задолженности (демо-данные)",
            department="Демо-отдел судебных приставов",
        )
        for index in range(count)
    ]


class DemoFedresursProvider(BaseProvider):
    """Bankruptcy register, demo edition.

    Note the difference from the unconfigured live provider: this one genuinely
    "answers", so ``NO_RESULTS`` here does mean "checked, nothing found".
    """

    name = ProviderName.FEDRESURS
    title = "ЕФРСБ (демо)"

    @property
    def is_configured(self) -> bool:
        return True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        if subject.name is None:
            return self.insufficient_query("Нужно ФИО")

        profile = _profile_for(subject)
        if profile is None or profile.bankruptcy is None:
            return ProviderResult(provider=self.name, status=ProviderStatus.NO_RESULTS)

        case_number, procedure, status, completed_at = profile.bankruptcy
        record = BankruptcyRecord(
            debtor_name=profile.full_name,
            debtor_type=EntityType.INDIVIDUAL,
            inn=profile.inn,
            case_number=case_number,
            procedure=procedure,
            status=status,
            started_at=date(2026, 2, 17),
            completed_at=completed_at,
            message_date=date(2026, 2, 25),
        )
        return ProviderResult(provider=self.name, status=ProviderStatus.SUCCESS, records=[record])


class DemoFNSProvider(BaseProvider):
    """ЕГРЮЛ / ЕГРИП relations, demo edition."""

    name = ProviderName.FNS
    title = "ФНС (демо)"

    @property
    def is_configured(self) -> bool:
        return True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        if subject.name is None:
            return self.insufficient_query("Нужно ФИО")

        profile = _profile_for(subject)
        if profile is None:
            return ProviderResult(provider=self.name, status=ProviderStatus.NO_RESULTS)

        records = [
            BusinessRelation(
                inn=inn,
                ogrn=f"3{inn}0000"[:15],
                name=name,
                entity_type=(
                    EntityType.SOLE_PROPRIETOR
                    if role is BusinessRole.SOLE_PROPRIETOR
                    else EntityType.LEGAL_ENTITY
                ),
                status=status,
                role=role,
                registration_date=date(2018, 5, 14),
                termination_date=(
                    date(2024, 9, 30) if status is BusinessStatus.TERMINATED else None
                ),
                # Демо-профиль заведён на конкретного человека: связь с ним —
                # часть выдумки, а не результат сопоставления имён.
                linked_by_identifier=True,
            )
            for inn, name, role, status in profile.businesses
        ]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
            records=list(records),
        )


class DemoPledgeProvider(BaseProvider):
    """Реестр залогов, демо-издание."""

    name = ProviderName.PLEDGE
    title = "Залоги (демо)"

    @property
    def is_configured(self) -> bool:
        return True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        if subject.name is None:
            return self.insufficient_query("Нужно ФИО")

        profile = _profile_for(subject)
        if profile is None:
            return ProviderResult(provider=self.name, status=ProviderStatus.NO_RESULTS)

        records = [
            PledgeRecord(
                registration_number=f"2026-00{index}-123456-{index}",
                registered_at=date(2022, 4, 11),
                terminated_at=None if status is PledgeStatus.ACTIVE else date(2025, 6, 1),
                pledgor_name=profile.full_name,
                pledgor_birth_date=profile.birth_date,
                pledgor_inn=profile.inn,
                pledgee_name=pledgee,
                subject=subject_text,
                vin=vin,
                status=status,
            )
            for index, (subject_text, pledgee, vin, status) in enumerate(profile.pledges, start=1)
        ]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
            records=list(records),
        )


class DemoInnBridgeProvider(InnBridgeProvider):
    """ИНН по паспорту, демо-издание.

    Существует ради одного свойства: ``make demo`` не должен зависеть от ключа
    NewDB. Ни одного сетевого обращения не делает и ничего не стоит, поэтому
    ``is_configured`` здесь True даже при выключенном ``INN_BRIDGE_ENABLED`` —
    флаг сторожит деньги и уход паспорта наружу, а в демо нет ни того, ни
    другого.

    :meth:`is_needed` сужен: без паспорта моста для демо-субъекта не существует
    вовсе. Демо-источники ищут по ФИО и в ИНН не нуждаются, так что строка
    «паспорт не указан» в каждом демо-отчёте объясняла бы то, чего не
    происходит. Введённый паспорт мост честно превращает в ИНН профиля.
    """

    title = "ИНН по паспорту (демо)"

    @property
    def is_configured(self) -> bool:
        return True

    def is_needed(self, subject: SearchSubject) -> bool:
        return bool(subject.passport) and super().is_needed(subject)

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        missing = missing_bridge_input(subject)
        if missing is not None:
            return self.insufficient_query(missing)

        profile = _profile_for(subject)
        if profile is None or profile.inn is None:
            # Демо-источник действительно «ответил»: такого ИНН у него нет.
            return ProviderResult(provider=self.name, status=ProviderStatus.NO_RESULTS)
        return InnBridgeResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS,
            inn=profile.inn,
        )


class DemoCourtProvider(BaseProvider):
    """Арбитражные дела, демо-издание."""

    name = ProviderName.COURT
    title = "Суды (демо)"

    @property
    def is_configured(self) -> bool:
        return True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        if subject.name is None:
            return self.insufficient_query("Нужно ФИО")

        profile = _profile_for(subject)
        if profile is None:
            return ProviderResult(provider=self.name, status=ProviderStatus.NO_RESULTS)

        records = [
            CourtCase(
                case_number=case_number,
                court_name=court,
                case_type="Взыскание задолженности",
                status="Рассмотрение по существу" if not closed else "Дело рассмотрено",
                amount=amount,
                filed_at=date(2026, 5, 20),
                participant_name=profile.full_name,
                inn=profile.inn,
                role=role,
                is_closed=closed,
            )
            for case_number, court, amount, role, closed in profile.court_cases
        ]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
            records=list(records),
        )


def build_demo_providers() -> list[BaseProvider]:
    return [
        DemoFSSPProvider(),
        DemoFedresursProvider(),
        DemoFNSProvider(),
        DemoPledgeProvider(),
        DemoCourtProvider(),
    ]
