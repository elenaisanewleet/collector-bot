"""Identity value objects and normalization.

Russian names arrive in wildly inconsistent shapes. Everything that compares two
people goes through here so the rules live in exactly one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.utils.hashing import normalize_token

_NAME_SEPARATORS = re.compile(r"[\s ]+")
_NAME_ALLOWED = re.compile(r"^[а-яёa-z\-']+$", re.IGNORECASE)

FIO_MIN_PARTS = 2
FIO_MAX_PARTS = 3
INN_INDIVIDUAL_LENGTH = 12
INN_ENTITY_LENGTH = 10
VIN_LENGTH = 17
# I, O and Q are excluded from the VIN alphabet to avoid confusion with 1 and 0.
_VIN_ALLOWED = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_PLATE_PATTERNS = (
    # Passenger plate: А123ВС77 / А123ВС777
    re.compile(r"^[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}$"),
    # Trailer / motorcycle / public transport variants.
    re.compile(r"^[АВЕКМНОРСТУХ]{2}\d{4}\d{2,3}$"),
    re.compile(r"^\d{4}[АВЕКМНОРСТУХ]{2}\d{2,3}$"),
    re.compile(r"^[АВЕКМНОРСТУХ]{2}\d{3}\d{2,3}$"),
)
# Latin look-alikes routinely typed instead of Cyrillic on plates.
_PLATE_TRANSLITERATION = str.maketrans(
    {
        "A": "А",
        "B": "В",
        "E": "Е",
        "K": "К",
        "M": "М",
        "H": "Н",
        "O": "О",
        "P": "Р",
        "C": "С",
        "T": "Т",
        "Y": "У",
        "X": "Х",
    }
)


class NameParseError(ValueError):
    """Raised when input cannot be read as a name without guessing."""


class PersonName(BaseModel):
    """A parsed Russian name.

    A middle name is optional because plenty of records genuinely lack one; a
    surname and a given name are not, because without them no meaningful
    matching is possible.
    """

    model_config = ConfigDict(frozen=True)

    last_name: str
    first_name: str
    middle_name: str | None = None

    @property
    def full(self) -> str:
        parts = [self.last_name, self.first_name, self.middle_name]
        return " ".join(part for part in parts if part)

    @property
    def normalized(self) -> str:
        return normalize_token(self.full)

    @property
    def normalized_short(self) -> str:
        """Surname + given name only — used for comparing against sources that
        omit the patronymic."""
        return normalize_token(f"{self.last_name} {self.first_name}")

    @property
    def has_middle_name(self) -> bool:
        return bool(self.middle_name)


def parse_fio(raw: str) -> PersonName:
    """Parse ``Фамилия Имя [Отчество]``.

    Deliberately strict: an unparseable name is rejected instead of being split
    on a guess, because a wrong split silently poisons every downstream match.
    """
    if not raw or not raw.strip():
        raise NameParseError("ФИО не указано")
    parts = [part for part in _NAME_SEPARATORS.split(raw.strip()) if part]
    if len(parts) < FIO_MIN_PARTS:
        raise NameParseError("Нужно как минимум фамилия и имя. Пример: Иванов Иван Иванович")
    if len(parts) > FIO_MAX_PARTS:
        raise NameParseError("Слишком много слов. Ожидается: Фамилия Имя Отчество")
    for part in parts:
        if not _NAME_ALLOWED.match(part):
            raise NameParseError(f"Недопустимые символы в «{part}»")
    normalized = [_capitalize_name(part) for part in parts]
    return PersonName(
        last_name=normalized[0],
        first_name=normalized[1],
        middle_name=normalized[2] if len(normalized) == FIO_MAX_PARTS else None,
    )


def _capitalize_name(part: str) -> str:
    """Capitalize each hyphen-separated segment: ``петров-водкин`` -> ``Петров-Водкин``."""
    return "-".join(segment.capitalize() for segment in part.split("-"))


def normalize_phone(raw: str | None) -> str | None:
    """Normalize a Russian phone number to ``+7XXXXXXXXXX``.

    Returns ``None`` for anything that is not a plausible RU number rather than
    padding or truncating it into one.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        return f"+7{digits[1:]}"
    if len(digits) == 10 and digits[0] == "9":
        return f"+7{digits}"
    return None


def normalize_plate(raw: str | None) -> str | None:
    """Normalize a Russian licence plate, transliterating Latin look-alikes."""
    if not raw:
        return None
    cleaned = re.sub(r"[\s\-]", "", raw).upper().translate(_PLATE_TRANSLITERATION)
    if not cleaned:
        return None
    return cleaned if any(pattern.match(cleaned) for pattern in _PLATE_PATTERNS) else None


def normalize_vin(raw: str | None) -> str | None:
    """Validate and normalize a VIN: exactly 17 chars from the legal alphabet."""
    if not raw:
        return None
    cleaned = re.sub(r"[\s\-]", "", raw).upper()
    return cleaned if _VIN_ALLOWED.match(cleaned) else None


def normalize_inn(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits if len(digits) in {INN_ENTITY_LENGTH, INN_INDIVIDUAL_LENGTH} else None


def normalize_passport(raw: str | None) -> str | None:
    """Normalize an RF passport to 10 digits. Never logged, rarely stored."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits if len(digits) == 10 else None


def normalize_address(raw: str | None) -> str | None:
    if not raw:
        return None
    collapsed = " ".join(raw.split())
    return collapsed or None


@dataclass(frozen=True, slots=True)
class IdentityKey:
    """The set of signals used to decide whether two records are one person."""

    name: PersonName | None
    birth_date: date | None
    inn: str | None
    phone: str | None

    @property
    def has_strong_identifier(self) -> bool:
        """True when something better than a name alone is available."""
        return bool(self.birth_date or self.inn or self.phone)


class VehicleDescriptor(BaseModel):
    """What the operator knows about a car — every field optional by design."""

    model_config = ConfigDict(frozen=True)

    make: str | None = None
    model: str | None = None
    plate: str | None = None
    vin: str | None = None

    @property
    def has_unique_identifier(self) -> bool:
        """A make and model identify a *type* of car, never a specific one."""
        return bool(self.plate or self.vin)

    @property
    def title(self) -> str:
        parts = [self.make, self.model]
        label = " ".join(part for part in parts if part)
        identifiers = [self.plate, self.vin]
        suffix = " / ".join(item for item in identifiers if item)
        return " — ".join(item for item in (label, suffix) if item) or "—"


class SearchSubject(BaseModel):
    """Everything known about the subject of one search.

    This is the single input every provider receives. Providers pick the fields
    they can legally use and ignore the rest.
    """

    model_config = ConfigDict(frozen=True)

    search_type: str
    name: PersonName | None = None
    birth_date: date | None = None
    phone: str | None = None
    inn: str | None = None
    passport: str | None = None
    address: str | None = None
    regions: tuple[str, ...] = Field(default_factory=tuple)
    vehicle: VehicleDescriptor | None = None
    contract_number: str | None = None
    claim_number: str | None = None
    debtor_id: str | None = None

    @property
    def identity_key(self) -> IdentityKey:
        return IdentityKey(
            name=self.name,
            birth_date=self.birth_date,
            inn=self.inn,
            phone=self.phone,
        )

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name.full
        for candidate in (self.contract_number, self.claim_number, self.debtor_id):
            if candidate:
                return candidate
        if self.vehicle:
            return self.vehicle.title
        return self.address or "—"
