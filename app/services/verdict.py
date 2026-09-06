"""Движок вердикта.

Детерминированные правила поверх готового :class:`DebtorReport`. Никакой модели,
никакой случайности: тот же отчёт всегда даёт тот же вердикт, и каждое решение
объяснимо одной строкой.

Правила проверяются строго по порядку, и порядок — это и есть содержание:

1. Активное банкротство закрывает вопрос: иск подавать некуда.
2. Нехватка данных — это «проверить руками», а не «не подавать». Мы не знаем,
   а не знаем, что плохо.
3. Экономика: пошлина, несоразмерная долгу, делает взыскание бессмысленным.
4. Низкая перспектива по совокупности — тоже к человеку, а не к автоматике.
5. Дальше — только выбор процедуры: приказ или иск.
"""

from __future__ import annotations

from decimal import Decimal

from app.config import Settings
from app.domain.enums import PROVIDER_TITLES, BusinessRole, ProviderName, ScoreCategory
from app.domain.fees import claim_fee, court_order_fee
from app.domain.models import DebtorReport
from app.domain.verdict import FeeBasis, Verdict, VerdictDecision, VerdictReason
from app.utils.formatting import pluralize_ru
from app.utils.money import format_amount

# Источники, без ответа которых решение принимать рано.
DECISIVE_PROVIDERS: tuple[ProviderName, ...] = (ProviderName.FSSP, ProviderName.FEDRESURS)


class VerdictEngine:
    """Переводит отчёт в действие."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def decide(self, report: DebtorReport) -> VerdictDecision:
        confidence = report.recovery_score.confidence if report.recovery_score else 0.0
        debt = _our_debt(report)

        for rule in (
            self._active_bankruptcy,
            self._debtor_deceased,
            self._unverified_identity,
            self._silent_sources,
            self._no_debt_amount,
            self._fee_outweighs_debt,
            self._company_exposure,
            self._low_prospects,
        ):
            decision = rule(report, debt, confidence)
            if decision is not None:
                return decision

        return self._choose_procedure(report, debt, confidence)

    # ------------------------------------------------------------ blocking

    def _active_bankruptcy(
        self, report: DebtorReport, debt: Decimal | None, confidence: float
    ) -> VerdictDecision | None:
        active = report.active_bankruptcies
        if not active:
            return None
        record = active[0]
        procedure = record.procedure or "процедура банкротства"
        case = f", дело {record.case_number}" if record.case_number else ""
        return VerdictDecision(
            verdict=Verdict.DROP,
            headline=(
                "Идёт банкротство. Требование заявляется в реестр кредиторов, "
                "отдельный иск подавать некуда — пошлина уйдёт впустую."
            ),
            reasons=(
                VerdictReason(
                    code="active_bankruptcy",
                    text=f"{procedure}{case}",
                    source=ProviderName.FEDRESURS,
                ),
            ),
            debt_amount=debt,
            # Пошлина считается и здесь: это сумма, которую прогон уберёг от
            # списания в никуда. FeeBasis.NONE говорит, что платить её не нужно.
            state_fee=self._fee_for(debt) if debt and debt > 0 else None,
            fee_basis=FeeBasis.NONE,
            confidence=confidence,
        )

    def _debtor_deceased(
        self, report: DebtorReport, debt: Decimal | None, confidence: float
    ) -> VerdictDecision | None:
        """Подтверждённое наследственное дело: ответчика больше нет.

        Своим правилом, а не через балл. −35 отправили бы дело в
        ``_low_prospects``, но до него стоят ``_no_debt_amount``,
        ``_fee_outweighs_debt`` и ``_company_exposure``, любое из которых
        перехватило бы решение и напечатало про пошлину и долю в ООО там, где
        речь про смерть должника. А без правила вовсе бесспорный долг дал бы
        «подавать иск» — иск, который суд не примет.

        REVIEW, а не DROP. Долг не исчезает: он переходит к наследникам в
        пределах стоимости наследства, и следующий шаг известен и выполним —
        запрос нотариусу, который ведёт дело, за кругом наследников. DROP
        читался бы как «списать» и стоил бы взыскателю ровно столько же, сколько
        иск к покойному.

        Источник в ``DECISIVE_PROVIDERS`` при этом НЕ добавлен: реестр отвечает
        только с российских адресов, и деплой вне РФ иначе вечно отвечал бы
        «проверить руками» по всем должникам подряд.
        """
        confirmed = report.confirmed_inheritance_cases
        if not confirmed:
            return None
        record = confirmed[0]
        case = f"дело {record.case_number}" if record.case_number else "наследственное дело"
        died = f", смерть {record.death_date.strftime('%d.%m.%Y')}" if record.death_date else ""
        notary = f"; нотариус: {record.notary_name}" if record.notary_name else ""
        # Состояние дела — из записи, а не из слова «открыто» в шаблоне. Оно
        # печаталось всегда одинаково, и отчёт называл открытым дело, помеченное
        # закрытым строкой выше; следующий шаг у них разный.
        state = "открыто" if record.is_open else "закрыто"
        # Размер пула однофамильцев доходит до заголовка, потому что заголовок —
        # это то, что оператор читает в выгрузке очереди и несёт в суд. «Должник
        # умер» на основании одной записи из 1730 и на основании одной записи из
        # одной — утверждения разной силы, и до сих пор они выглядели одинаково.
        return VerdictDecision(
            verdict=Verdict.REVIEW,
            headline=(
                f"Наследственное дело ({state}): должник умер, дата рождения совпала. "
                "Иск к нему суд не примет — требование предъявляется наследникам "
                "или к наследственному имуществу в пределах его стоимости."
                f"{_namesake_note(record.namesake_count)}"
            ),
            reasons=(
                VerdictReason(
                    code="confirmed_probate_case",
                    text=f"{case} ({state}){died}{notary}",
                    source=ProviderName.INHERITANCE,
                ),
            ),
            debt_amount=debt,
            # Пошлина считается: взыскание не отменяется, оно адресуется иначе.
            state_fee=self._fee_for(debt) if debt and debt > 0 else None,
            fee_basis=self._basis_for(debt) if debt and debt > 0 else FeeBasis.NONE,
            confidence=confidence,
        )

    # ------------------------------------------------------------ data gaps

    def _unverified_identity(
        self, report: DebtorReport, debt: Decimal | None, confidence: float
    ) -> VerdictDecision | None:
        """Без даты рождения или ИНН совпадение по ФИО подтвердить нельзя."""
        if report.subject.identity_key.has_strong_identifier:
            return None
        return VerdictDecision(
            verdict=Verdict.REVIEW,
            headline=(
                "В карточке нет даты рождения или ИНН. По одному ФИО совпадение "
                "подтвердить нельзя — любая найденная запись может быть чужой."
            ),
            reasons=(
                VerdictReason(
                    code="weak_identity",
                    text="нет уточняющих идентификаторов",
                    source=ProviderName.INTERNAL,
                ),
            ),
            debt_amount=debt,
            confidence=confidence,
        )

    def _silent_sources(
        self, report: DebtorReport, debt: Decimal | None, confidence: float
    ) -> VerdictDecision | None:
        """Источник, который не ответил, — это не чистая проверка."""
        silent = [
            provider
            for provider in DECISIVE_PROVIDERS
            if (result := report.result_for(provider)) is None or not result.is_answered
        ]
        if not silent:
            return None
        titles = ", ".join(PROVIDER_TITLES.get(name, name.value) for name in silent)
        # Без числительного, поэтому и без родительного падежа: «ключевые
        # источника» и «ключевые источник» — то, что получалось из pluralize_ru,
        # согласованного только с существительным. Список источников тут же,
        # считать их за оператора незачем.
        subject = "ключевой источник" if len(silent) == 1 else "ключевые источники"
        verb = "Не ответил" if len(silent) == 1 else "Не ответили"
        return VerdictDecision(
            verdict=Verdict.REVIEW,
            headline=(f"{verb} {subject}: {titles}. Решение на неполных данных принимать рано."),
            reasons=tuple(
                VerdictReason(
                    code="source_silent",
                    text=_silence_reason(report, provider),
                    source=provider,
                )
                for provider in silent
            ),
            debt_amount=debt,
            confidence=confidence,
        )

    def _no_debt_amount(
        self, report: DebtorReport, debt: Decimal | None, confidence: float
    ) -> VerdictDecision | None:
        if debt is not None and debt > 0:
            return None
        return VerdictDecision(
            verdict=Verdict.REVIEW,
            headline="В выгрузке нет суммы долга — цену иска и пошлину посчитать не из чего.",
            reasons=(
                VerdictReason(
                    code="no_debt_amount",
                    text="сумма задолженности не указана",
                    source=ProviderName.INTERNAL,
                ),
            ),
            debt_amount=debt,
            confidence=confidence,
        )

    # ------------------------------------------------------------ economics

    def _fee_outweighs_debt(
        self, report: DebtorReport, debt: Decimal | None, confidence: float
    ) -> VerdictDecision | None:
        assert debt is not None and debt > 0  # гарантировано предыдущим правилом
        fee = self._fee_for(debt)
        ratio = Decimal(str(self._settings.min_debt_to_fee_ratio))
        if debt >= fee * ratio:
            return None
        return VerdictDecision(
            verdict=Verdict.DROP,
            headline=(
                f"Пошлина {format_amount(fee)} несоразмерна долгу {format_amount(debt)}. "
                "Взыскание не окупает процесс."
            ),
            reasons=(
                VerdictReason(
                    code="fee_outweighs_debt",
                    text=f"долг меньше пошлины в {ratio:g}× — порог целесообразности",
                ),
            ),
            debt_amount=debt,
            state_fee=fee,
            fee_basis=FeeBasis.NONE,
            confidence=confidence,
        )

    def _company_exposure(
        self, report: DebtorReport, debt: Decimal | None, confidence: float
    ) -> VerdictDecision | None:
        """Две ясные конфигурации, при которых решать должен человек.

        Балл они не двигают — арбитраж юрлиц в скоринг сознательно не заведён,
        потому что обороты ООО это не активы участника. Но и молчать о них
        нельзя: банкротство компании при должнике-руководителе — это разговор о
        субсидиарной ответственности, а живая дебиторка компании при
        должнике-участнике — про стоимость доли, на которую обращается
        взыскание (ст. 74 ФЗ-229, ст. 25 ФЗ-14).
        """
        reasons = tuple(_company_exposure_reasons(report))
        if not reasons:
            return None
        return VerdictDecision(
            verdict=Verdict.REVIEW,
            headline=(
                "Проверить долю в уставном капитале / риск субсидиарной "
                "ответственности — вручную. Имущество ООО не является имуществом "
                "участника."
            ),
            reasons=reasons,
            debt_amount=debt,
            state_fee=self._fee_for(debt) if debt and debt > 0 else None,
            fee_basis=self._basis_for(debt) if debt and debt > 0 else FeeBasis.NONE,
            confidence=confidence,
        )

    def _low_prospects(
        self, report: DebtorReport, debt: Decimal | None, confidence: float
    ) -> VerdictDecision | None:
        score = report.recovery_score
        if score is None or score.category != ScoreCategory.LOW.value:
            return None
        assert debt is not None
        return VerdictDecision(
            verdict=Verdict.REVIEW,
            headline=(
                f"Перспектива низкая: {score.score}/100. Формальных препятствий нет, "
                "но решение стоит принять человеку."
            ),
            reasons=tuple(
                VerdictReason(code=factor.name, text=factor.reason, source=factor.source)
                for factor in score.factors
                if factor.delta < 0
            ),
            debt_amount=debt,
            state_fee=self._fee_for(debt),
            fee_basis=self._basis_for(debt),
            confidence=confidence,
        )

    # ------------------------------------------------------------ procedure

    def _choose_procedure(
        self, report: DebtorReport, debt: Decimal | None, confidence: float
    ) -> VerdictDecision:
        assert debt is not None
        threshold = Decimal(str(self._settings.court_order_max_amount))
        fee = self._fee_for(debt)
        reasons = _positive_reasons(report)

        if debt <= threshold:
            return VerdictDecision(
                verdict=Verdict.ORDER,
                headline=(
                    f"Долг бесспорный и не превышает {format_amount(threshold)}. "
                    "Приказ дешевле и быстрее иска: пошлина вдвое ниже."
                ),
                reasons=reasons,
                debt_amount=debt,
                state_fee=fee,
                fee_basis=FeeBasis.COURT_ORDER,
                confidence=confidence,
            )

        return VerdictDecision(
            verdict=Verdict.FILE,
            headline="Препятствий к взысканию не найдено. Долг выше порога судебного приказа.",
            reasons=reasons,
            debt_amount=debt,
            state_fee=fee,
            fee_basis=FeeBasis.CLAIM,
            confidence=confidence,
        )

    # ------------------------------------------------------------ helpers

    def _basis_for(self, debt: Decimal) -> FeeBasis:
        threshold = Decimal(str(self._settings.court_order_max_amount))
        return FeeBasis.COURT_ORDER if debt <= threshold else FeeBasis.CLAIM

    def _fee_for(self, debt: Decimal) -> Decimal:
        if self._basis_for(debt) is FeeBasis.COURT_ORDER:
            return court_order_fee(debt)
        return claim_fee(debt)


def _our_debt(report: DebtorReport) -> Decimal | None:
    record = report.internal_record
    return record.debt_amount if record else None


def _namesake_note(namesake_count: int | None) -> str:
    """Сколько дел реестр вернул по этому ФИО — если больше одного.

    Реестр ФНП ищет только по ФИО и отдаёт всех однофамильцев разом. Одно дело
    из 1730 подтверждено датой рождения — этого достаточно, чтобы не подавать
    иск к покойному, и недостаточно, чтобы читатель отчёта не захотел проверить
    руками. При единственном найденном деле оговорка не нужна и не печатается.
    """
    if not namesake_count or namesake_count <= 1:
        return ""
    noun = pluralize_ru(namesake_count, "дело", "дела", "дел")
    return (
        f" По этому ФИО реестр вернул {namesake_count} {noun} — "
        "совпадение подтверждено датой рождения."
    )


def _silence_reason(report: DebtorReport, provider: ProviderName) -> str:
    result = report.result_for(provider)
    title = PROVIDER_TITLES.get(provider, provider.value)
    if result is None:
        return f"{title}: не опрашивался"
    if result.error_code == "insufficient_query":
        return f"{title}: {result.error_message or 'недостаточно данных'}"
    return f"{title}: {result.error_message or result.status.value}"


def _company_exposure_reasons(report: DebtorReport) -> list[VerdictReason]:
    reasons: list[VerdictReason] = []
    seen: set[str] = set()
    for case in report.legal_entity_cases:
        company = case.company_name or case.company_inn
        if (
            case.is_active
            and not case.is_claim_against_company
            and case.company_role is BusinessRole.FOUNDER
        ):
            code = f"company_receivables:{case.company_inn}"
            text = f"{company} взыскивает дебиторку, должник — участник"
        else:
            continue
        if code in seen:
            continue
        seen.add(code)
        reasons.append(VerdictReason(code=code, text=text, source=ProviderName.COURT_LEGAL))
    return reasons


def _positive_reasons(report: DebtorReport) -> tuple[VerdictReason, ...]:
    """Что говорит в пользу взыскания — из уже посчитанных факторов."""
    score = report.recovery_score
    if score is None:
        return ()
    return tuple(
        VerdictReason(code=factor.name, text=factor.reason, source=factor.source)
        for factor in score.factors
        if factor.delta > 0
    )
