"""Recovery-score rules.

The weights live here, apart from the engine that applies them, so the business
rules can be reviewed and tuned without reading orchestration code. The engine
is deterministic arithmetic — no model, no randomness, no hidden state.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from app.domain.enums import ScoreCategory

BASE_SCORE: Final = 50
MIN_SCORE: Final = 0
MAX_SCORE: Final = 100

# ---------------------------------------------------------------- positive
ACTIVE_SOLE_PROPRIETOR_BONUS: Final = 10
ACTIVE_LEGAL_ENTITY_ROLE_BONUS: Final = 5
CONFIRMED_PROPERTY_BONUS: Final = 15
CONFIRMED_VEHICLE_BONUS: Final = 8
NO_BANKRUPTCY_BONUS: Final = 10
NO_ENFORCEMENT_BONUS: Final = 5

# Bonuses that reward "we looked and found nothing bad" are capped so a person
# with several idle business roles cannot inflate the score indefinitely.
MAX_BUSINESS_BONUS: Final = 20

# ---------------------------------------------------------------- negative
ACTIVE_BANKRUPTCY_PENALTY: Final = -35
COMPLETED_BANKRUPTCY_PENALTY: Final = -10

# Enforcement-proceeding count bands, evaluated top-down.
ENFORCEMENT_COUNT_PENALTIES: Final[tuple[tuple[int, int], ...]] = (
    (6, -25),  # more than five
    (3, -15),  # three to five
    (1, -5),  # one or two
)

# Penalty bands for the total confirmed enforcement debt, in roubles.
ENFORCEMENT_AMOUNT_PENALTIES: Final[tuple[tuple[Decimal, int], ...]] = (
    (Decimal("1000000"), -15),
    (Decimal("500000"), -10),
    (Decimal("100000"), -5),
)

TERMINATED_BUSINESS_PENALTY: Final = -3
MAX_TERMINATED_BUSINESS_PENALTY: Final = -9

# ---------------------------------------------------------------- categories
LOW_CATEGORY_MAX: Final = 34
MEDIUM_CATEGORY_MAX: Final = 69


def categorize(score: int) -> ScoreCategory:
    if score <= LOW_CATEGORY_MAX:
        return ScoreCategory.LOW
    if score <= MEDIUM_CATEGORY_MAX:
        return ScoreCategory.MEDIUM
    return ScoreCategory.HIGH


def clamp(score: int) -> int:
    return max(MIN_SCORE, min(MAX_SCORE, score))


# ---------------------------------------------------------------- confidence
#
# Confidence answers "how much of the picture did we actually see?", which is a
# different question from the score itself. A perfect-looking score built on one
# reachable source is not a trustworthy score.

# Relative importance of each source when computing coverage.
PROVIDER_CONFIDENCE_WEIGHTS: Final[dict[str, float]] = {
    "internal": 0.15,
    "fssp": 0.35,
    "fedresurs": 0.30,
    "fns": 0.20,
}

# Applied when the subject was identified by name alone.
WEAK_IDENTITY_CONFIDENCE_FACTOR: Final = 0.7
# Applied when some matched records are only probable, not confirmed.
PROBABLE_MATCH_CONFIDENCE_FACTOR: Final = 0.9
MIN_CONFIDENCE: Final = 0.05
