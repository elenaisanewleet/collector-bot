"""Domain vocabulary.

These names are part of the contract between providers, services and storage;
they are persisted, so their string values must stay stable.
"""

from __future__ import annotations

from enum import StrEnum


class ProviderStatus(StrEnum):
    """Outcome of one provider call.

    The distinction between :attr:`NO_RESULTS` and everything else is the single
    most important invariant in this codebase: only ``NO_RESULTS`` means the
    source was actually consulted and had nothing. A missing key is not an
    absence of records.
    """

    SUCCESS = "success"
    NO_RESULTS = "no_results"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    ERROR = "error"

    @property
    def is_answered(self) -> bool:
        """True when the source genuinely responded (with or without records)."""
        return self in {ProviderStatus.SUCCESS, ProviderStatus.NO_RESULTS}


class ProviderName(StrEnum):
    INTERNAL = "internal"
    FSSP = "fssp"
    FEDRESURS = "fedresurs"
    FNS = "fns"
    COURT = "court"
    VEHICLE = "vehicle"
    PROPERTY = "property"
    PLEDGE = "pledge"
    INHERITANCE = "inheritance"


PROVIDER_TITLES: dict[ProviderName, str] = {
    ProviderName.INTERNAL: "Наши данные",
    ProviderName.FSSP: "ФССП",
    ProviderName.FEDRESURS: "ЕФРСБ",
    ProviderName.FNS: "ФНС",
    ProviderName.COURT: "Суды",
    ProviderName.VEHICLE: "Авто",
    ProviderName.PROPERTY: "Недвижимость",
    ProviderName.PLEDGE: "Залоги",
    ProviderName.INHERITANCE: "Наследственные дела",
}


class SearchType(StrEnum):
    PERSON = "person"
    VEHICLE_PLATE = "vehicle_plate"
    VIN = "vin"
    VEHICLE = "vehicle"
    ADDRESS = "address"
    PASSPORT = "passport"
    CONTRACT = "contract"


SEARCH_TYPE_TITLES: dict[SearchType, str] = {
    SearchType.PERSON: "Физлицо",
    SearchType.VEHICLE_PLATE: "Госномер",
    SearchType.VIN: "VIN",
    SearchType.VEHICLE: "Автомобиль",
    SearchType.ADDRESS: "Адрес",
    SearchType.PASSPORT: "Паспорт",
    SearchType.CONTRACT: "Договор / заявка",
}


class Region(StrEnum):
    """Regions the business actually works in, plus an explicit escape hatch."""

    MOSCOW = "moscow"
    MOSCOW_OBLAST = "moscow_oblast"
    OTHER = "other"


REGION_TITLES: dict[Region, str] = {
    Region.MOSCOW: "Москва",
    Region.MOSCOW_OBLAST: "Московская область",
    Region.OTHER: "Другой регион",
}

# Codes used by the ФССП public API region dictionary. They are supplied to the
# adapter as data rather than hard-coded into request building, so a deployment
# can correct them without a code change.
REGION_FSSP_CODES: dict[Region, int] = {
    Region.MOSCOW: 77,
    Region.MOSCOW_OBLAST: 50,
}


class MatchLevel(StrEnum):
    """How confident we are that an external record is *this* person."""

    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    WEAK = "weak"


MATCH_LEVEL_TITLES: dict[MatchLevel, str] = {
    MatchLevel.CONFIRMED: "Подтверждённое совпадение",
    MatchLevel.PROBABLE: "Возможное совпадение",
    MatchLevel.WEAK: "Слабое совпадение",
}


class ScoreCategory(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


SCORE_CATEGORY_TITLES: dict[ScoreCategory, str] = {
    ScoreCategory.LOW: "НИЗКАЯ",
    ScoreCategory.MEDIUM: "СРЕДНЯЯ",
    ScoreCategory.HIGH: "ВЫСОКАЯ",
}


class EntityType(StrEnum):
    INDIVIDUAL = "individual"
    SOLE_PROPRIETOR = "sole_proprietor"
    LEGAL_ENTITY = "legal_entity"


class BusinessRole(StrEnum):
    SOLE_PROPRIETOR = "sole_proprietor"
    DIRECTOR = "director"
    FOUNDER = "founder"
    OTHER = "other"


BUSINESS_ROLE_TITLES: dict[BusinessRole, str] = {
    BusinessRole.SOLE_PROPRIETOR: "ИП",
    BusinessRole.DIRECTOR: "руководитель ЮЛ",
    BusinessRole.FOUNDER: "учредитель ЮЛ",
    BusinessRole.OTHER: "иная роль",
}


class BusinessStatus(StrEnum):
    """Состояние ИП или юрлица в реестре.

    ``UNKNOWN`` — не «прекращено»: запись найдена, а состояние в ответе
    источника не прочитано. Действующее ИП с непрочитанным статусом — это плюс
    к перспективе взыскания, и терять его нельзя.
    """

    ACTIVE = "active"
    TERMINATED = "terminated"
    UNKNOWN = "unknown"


BUSINESS_STATUS_TITLES: dict[BusinessStatus, str] = {
    BusinessStatus.ACTIVE: "действует",
    BusinessStatus.TERMINATED: "прекращено",
    # Не «прекращено»: непрочитанное состояние, поданное как закрытая
    # регистрация, убирает из поля зрения оператора живое ИП.
    BusinessStatus.UNKNOWN: "состояние не определено",
}


class BankruptcyStatus(StrEnum):
    """Состояние процедуры банкротства.

    ``UNKNOWN`` — не «нет процедуры» и не «процедура завершена»: дело найдено, а
    его состояние в ответе источника не прочитано. Это самостоятельный ответ, и
    показывать его надо им же.
    """

    ACTIVE = "active"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


BANKRUPTCY_STATUS_TITLES: dict[BankruptcyStatus, str] = {
    BankruptcyStatus.ACTIVE: "активно",
    BankruptcyStatus.COMPLETED: "завершено",
    # Не «завершено»: непрочитанное состояние, поданное как завершённая
    # процедура, читается взыскателем как «путь свободен» — и ровно этим
    # заканчивается для него дело, если процедура на самом деле идёт.
    BankruptcyStatus.UNKNOWN: "состояние процедуры не определено",
}


class ProceedingStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class PledgeStatus(StrEnum):
    """Состояние записи в реестре уведомлений о залоге движимого имущества.

    ``TERMINATED`` — залогодержатель подал уведомление об исключении: вещь
    свободна. ``ACTIVE`` — запись действует, и на эту вещь есть кредитор,
    который стоит впереди нас.
    """

    ACTIVE = "active"
    TERMINATED = "terminated"
    UNKNOWN = "unknown"


PLEDGE_STATUS_TITLES: dict[PledgeStatus, str] = {
    PledgeStatus.ACTIVE: "действует",
    PledgeStatus.TERMINATED: "исключён",
    # Не «исключён»: непрочитанное состояние записи, поданное как снятый залог,
    # читается взыскателем как «вещь свободна» — ровно наоборот.
    PledgeStatus.UNKNOWN: "состояние записи не определено",
}


class CourtCaseRole(StrEnum):
    """Кем должник проходит по делу."""

    DEFENDANT = "defendant"
    PLAINTIFF = "plaintiff"
    OTHER = "other"


COURT_CASE_ROLE_TITLES: dict[CourtCaseRole, str] = {
    CourtCaseRole.DEFENDANT: "ответчик",
    CourtCaseRole.PLAINTIFF: "истец",
    CourtCaseRole.OTHER: "иная роль",
}
