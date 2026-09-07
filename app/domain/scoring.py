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
# "Проверено, и чисто" по залогам и арбитражу стоит меньше, чем по банкротству:
# оба источника закрывают более узкий вопрос.
NO_PLEDGE_BONUS: Final = 3
NO_COURT_CLAIMS_BONUS: Final = 3

# Bonuses that reward "we looked and found nothing bad" are capped so a person
# with several idle business roles cannot inflate the score indefinitely.
MAX_BUSINESS_BONUS: Final = 20

# ---------------------------------------------------------------- negative
ACTIVE_BANKRUPTCY_PENALTY: Final = -35
# Производство окончено без взыскания: ст. 46 ч. 1 п. 3 (должника и его
# имущество не нашли) или п. 4 (имущества нет).
#
# Самая тяжёлая новость, какую ФССП сообщает о должнике: пристав с полномочиями
# шире наших уже искал — и не нашёл. До сих пор такая запись не доезжала до
# балла вовсе, а вместо неё выдавался бонус «производств не найдено».
#
# Штрафуется весь факт, а не каждая запись: четыре окончания по одному и тому
# же адресу — это один вывод, а не четыре, и линейный штраф просто утопил бы
# любого должника с историей.
WRITTEN_OFF_ENFORCEMENT_PENALTY: Final = -20
# Производство просто окончено, без указания статьи 46 (в том числе фактическим
# исполнением). Бонус за «чисто» снимается — приставы у должника уже были, — но
# штрафа нет: окончание по исполнению говорит ровно обратное.
CLOSED_ENFORCEMENT_PENALTY: Final = 0
COMPLETED_BANKRUPTCY_PENALTY: Final = -10
# Дело о банкротстве есть, а состояние процедуры источник не сообщил.
#
# Стоит столько же, сколько активное, и это осознанно. Разница между активным и
# завершённым банкротством — 25 баллов, то есть скидка, и выдавать её за
# непрочитанное состояние значит платить должнику за молчание источника.
# Ошибиться можно в обе стороны, но цена ошибок разная: недооценённая живая
# процедура — это поданный иск и потраченная впустую пошлина, а переоценённая
# завершённая — низкая перспектива, которая по правилам вердикта уходит человеку
# на проверку, а не в отказ. Фактор назван отдельно от активного банкротства
# именно поэтому: он не должен читаться как «идёт процедура».
UNKNOWN_BANKRUPTCY_STATE_PENALTY: Final = ACTIVE_BANKRUPTCY_PENALTY

# Подтверждённое наследственное дело: должник умер.
#
# По цене это событие уровня активного банкротства — иск к этому ответчику
# подавать некуда, — но фактор назван своим именем, как и у непрочитанного
# состояния процедуры выше: «открыто наследственное дело» не должно читаться
# как «идёт банкротство», ни в «Рисках» отчёта, ни в причинах вердикта.
#
# Штраф, а не обнуление: долг не исчезает. Он переходит к наследникам в
# пределах стоимости наследства, то есть перспектива не нулевая, а другая —
# дороже, дольше и с потолком. −35 от базовых 50 даёт 15, то есть НИЗКУЮ
# категорию, и дело уходит человеку, а не в отказ.
#
# Начисляется ТОЛЬКО за подтверждённое совпадение. Реестр ФНП ищет по одному
# ФИО, поэтому «возможное совпадение» здесь означает буквально «однофамилец» —
# и −35 за чужую смерть обнулили бы балл живого платёжеспособного должника.
CONFIRMED_PROBATE_CASE_PENALTY: Final = -35

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

# Действующий залог: вещь есть, но залогодержатель удовлетворяется раньше нас.
# Это не отсутствие имущества, а имущество, до которого мы не дотянемся, —
# поэтому штраф, а не бонус.
ACTIVE_PLEDGE_PENALTY: Final = -8
MAX_PLEDGE_PENALTY: Final = -16

# Живой иск к должнику — это кредитор, который уже впереди нас в очереди и
# вот-вот превратит требование в исполнительное производство.
CLAIM_AGAINST_DEBTOR_PENALTY: Final = -10
MAX_CLAIM_PENALTY: Final = -20

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

# Relative importance of each source when computing coverage. The table holds
# exactly the sources that move the score: a source that cannot change the
# number cannot make the number more or less trustworthy either.
#
# Залоги и арбитраж попали сюда вместе со своими факторами, и это сознательно
# опускает уверенность там, где их не подключили: отчёт, посчитанный без них,
# действительно видит меньше — и в «Ограничениях оценки» теперь прямо сказано,
# чего именно он не видел.
#
# Наследственные дела здесь по тому же правилу: подтверждённое дело двигает
# балл на −35, значит неподключённый или молчащий реестр действительно
# оставляет отчёт без ответа на вопрос «а он вообще жив». Вес маленький: у
# источника нет положительного фактора, и большая его часть работы —
# предупредить, а не подтвердить.
PROVIDER_CONFIDENCE_WEIGHTS: Final[dict[str, float]] = {
    "internal": 0.15,
    "fssp": 0.30,
    "fedresurs": 0.25,
    "fns": 0.15,
    "pledge": 0.10,
    "court": 0.05,
    "inheritance": 0.05,
}

# Applied when the subject was identified by name alone.
WEAK_IDENTITY_CONFIDENCE_FACTOR: Final = 0.7
# Applied when some matched records are only probable, not confirmed.
PROBABLE_MATCH_CONFIDENCE_FACTOR: Final = 0.9
MIN_CONFIDENCE: Final = 0.05
