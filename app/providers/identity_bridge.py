"""ИНН физлица по паспорту — мост между тем, что есть у взыскателя, и тем, чего
требуют источники.

Три источника из пяти — банкротство (``bankrot_person``), статус ИП
(``egrul_ip``) и арбитраж (``arbitr_person``) — ищут **только** по ``innfiz``,
двенадцатизначному ИНН физлица; ФИО с датой рождения они не принимают вовсе. У
взыскателя ИНН должника чаще всего нет, а паспорт есть: он в договоре займа.
Метод NewDB ``passport_fns`` меняет одно на другое, и без него три раздела
отчёта остаются непроверенными навсегда.

Почему это провайдер, а не сервис. ``BaseProvider.fetch`` уже даёт всё, что
мосту нужно и что иначе пришлось бы написать заново: изоляцию исключений
(«наружу не летит ничего»), тайминг, ``NOT_CONFIGURED`` без единого обращения и,
главное, запись результата в ``search_results`` — то есть честную строку в блоке
ИСТОЧНИКИ, которая переживает кэш. Отчёт, показанный второй раз, восстанавливается
из БД, и объяснение «почему банкротство не проверено» должно пережить этот
переход вместе с самой строкой «не проверено».

Чего он **не** делает: не приносит записей, не участвует в
``PROVIDER_CONFIDENCE_WEIGHTS`` и не входит в ``configured_names``. Мост не
увеличивает покрытие отчёта — он делает возможной проверку тех, кто в покрытие
уже входит.

Две особенности контракта, обе стоят внимания.

*   **Ответ приходит в ``results.company``**, а не в ``results.passport_fns``.
    Это единственный известный метод NewDB, у которого секция названа не именем
    метода; читать по имени — значит получить ``unexpected_schema`` на каждом
    успешном ответе.
*   **Собственные примеры вендора противоречат друг другу.** ``fiz_02`` отдаёт
    ``"innfiz": "7703245603"`` — десять цифр, ``fiz_04`` отдаёт
    ``"272116001938"`` — двенадцать. Десятизначное — это ИНН юрлица, и
    ``innfiz`` валидируется как двенадцать, так что принять первый вариант
    значило бы купить гарантированно отклонённый (и, судя по ``cost: 1``, всё
    равно оплаченный) вызов у трёх источников.

Приватность. Паспорт чувствительнее всего, что бот обрабатывает, поэтому здесь
две независимые линии: ``raw_response`` не сохраняется никогда, вне зависимости
от ``STORE_RAW_RESPONSES`` (вендор возвращает ``params`` эхом), и текст любой
вендорской ошибки проходит через :func:`scrub_passport` до того, как станет
``error_message`` — эта колонка пишется мимо обоих флагов приватности.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import Settings
from app.domain.enums import PROVIDER_TITLES, MissingInput, ProviderName, ProviderStatus
from app.domain.identity import (
    INN_INDIVIDUAL_LENGTH,
    SearchSubject,
    normalize_inn,
    normalize_passport,
)
from app.domain.models import ProviderResult
from app.logging_setup import get_logger
from app.providers.base import BaseProvider, ProviderError, ProviderUnavailableError
from app.providers.mapping import as_text
from app.providers.newdb import (
    NewDBClient,
    individual_inn,
    person_params_for,
    scrub_passport,
)

logger = get_logger(__name__)

NEWDB_METHOD = "passport_fns"
# Секция ответа названа не именем метода — см. модульный докстринг.
RESULT_SECTION = "company"

SERIA_LENGTH = 4
INN_FIELD = "innfiz"

NO_PASSPORT = "Паспорт не указан — ФНС ищет ИНН только по серии и номеру"
NO_NAME = "Для получения ИНН нужно ФИО"
NO_BIRTH_DATE = "Для получения ИНН нужна дата рождения — ФНС требует её обязательно"
NO_MIDDLE_NAME_NOTE = "отчество не указано — совпадение в ФНС могло не найтись по этой причине"


class InnBridgeResult(ProviderResult):
    """``ProviderResult`` моста плюс сам ИНН.

    Поле живёт только в памяти одного прогона: ``save_provider_results`` его не
    пишет, из кэша оно не восстанавливается и не нужно там — на кэш-хите ИНН
    берётся из сохранённого субъекта (см. ``SearchService._load_cached``).
    """

    inn: str | None = None


class InnBridgeProvider(BaseProvider):
    """Общий контракт моста: обычный провайдер плюс политика «звать или нет».

    Политика живёт здесь целиком, а не размазана по сервису: ``search.py``
    спрашивает :meth:`is_needed` и больше ничего не решает.
    """

    name = ProviderName.INN_BRIDGE
    title = PROVIDER_TITLES[ProviderName.INN_BRIDGE]

    def is_needed(self, subject: SearchSubject) -> bool:
        """Нужен ли мост этому субъекту.

        Именно ``individual_inn``, а не ``subject.inn``: десятизначный ИНН — это
        идентификатор юрлица, три источника его отвергают, значит мост обязан
        отработать и для него. Когда ответ False, сервис моста не зовёт и строки
        в отчёте нет вовсе: объяснять нечего, три раздела будут проверены.
        """
        return individual_inn(subject) is None

    def will_query(self, subject: SearchSubject) -> bool:
        """Дойдёт ли дело до платного вызова.

        Отличается от :meth:`is_needed` тем, что учитывает входные данные: без
        паспорта, ФИО или даты рождения мост отвечает ``insufficient_query`` и
        не тратит ни одного обращения. Нужно смете массового прогона, которая
        обязана показать лишние вызовы до их оплаты.
        """
        return (
            self.is_configured and self.is_needed(subject) and missing_bridge_input(subject) is None
        )


class PassportInnProvider(InnBridgeProvider):
    """ИНН физлица по паспорту через метод NewDB ``passport_fns``.

    Держит голый :class:`NewDBClient`, как ``FSSPProvider``, а не
    ``NewDBMethodProvider``: карты полей здесь нет и не нужно. ``innfiz``
    подтверждён двумя независимыми страницами документации, это одно скалярное
    поле, и ошибка в нём вырождается в «ИНН не получен» → «не проверено», а не в
    ложное «чисто».
    """

    def __init__(self, settings: Settings, client: NewDBClient | None = None) -> None:
        self._settings = settings
        self._client = client or NewDBClient(settings)

    @property
    def is_configured(self) -> bool:
        """Ключ **и** явно включённый флаг.

        Без любого из двух — ``NOT_CONFIGURED``, ноль вызовов, ноль списаний:
        платный вызов, добавляющийся сам собой, — это ровно то, чего смета не
        покажет.
        """
        return self._settings.newdb_configured and self._settings.inn_bridge_enabled

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        missing = missing_bridge_input(subject)
        if missing is not None:
            logger.info("inn_bridge.done", outcome="insufficient_query")
            return self.insufficient_query(missing, missing=missing_bridge_fields(subject))

        passport = normalize_passport(subject.passport)
        assert passport is not None  # проверено в missing_bridge_input
        seria, number = passport[:SERIA_LENGTH], passport[SERIA_LENGTH:]
        params = {**person_params_for(subject), "seria": seria, "number": number}

        try:
            response = await self._client.call(NEWDB_METHOD, params, result_section=RESULT_SECTION)
        except ProviderError as exc:
            # Текст вендора цитирует присланные параметры, а error_message
            # пишется в БД мимо обоих флагов приватности. ``from None``: цепочка
            # исключений несла бы исходное сообщение дальше.
            raise _scrubbed(exc, seria=seria, number=number) from None

        has_middle_name = subject.name is not None and subject.name.has_middle_name
        result = _read_rows(response.rows, has_middle_name=has_middle_name)
        logger.info("inn_bridge.done", outcome=_outcome_of(result))
        return result


def missing_bridge_input(subject: SearchSubject) -> str | None:
    """Чего не хватает, чтобы вообще обращаться в ФНС.

    Порядок сообщений — от того, чего не хватает чаще. Первым стоит паспорт:
    именно его отсутствие делает мост недостижимым в флоу ``PassportSearch``,
    который строит субъект без ФИО и без даты рождения, — и там мост честно не
    делает ни одного вызова, так что обещание «паспорт не передаётся во внешние
    источники» на том экране остаётся правдой.
    """
    gap = _bridge_gap(subject)
    return gap[1] if gap else None


def missing_bridge_fields(subject: SearchSubject) -> tuple[MissingInput, ...]:
    """То же самое поле, но машинно — для группировки строк «нечем спросить»."""
    gap = _bridge_gap(subject)
    return (gap[0],) if gap else ()


def _bridge_gap(subject: SearchSubject) -> tuple[MissingInput, str] | None:
    """Единственная лестница проверок: текст и поле обязаны совпадать всегда."""
    if normalize_passport(subject.passport) is None:
        return MissingInput.PASSPORT, NO_PASSPORT
    if subject.name is None:
        return MissingInput.NAME, NO_NAME
    if subject.birth_date is None:
        return MissingInput.BIRTH_DATE, NO_BIRTH_DATE
    return None


def _read_rows(rows: list[Any], *, has_middle_name: bool) -> ProviderResult:
    """Разобрать ``data[]`` ответа ФНС.

    Пустой ``data[]`` — это ответ («ИНН по этим данным не найден»), а не сбой.
    Всё остальное, что не сходится с контрактом, — сбой, а не пустой ответ.
    """
    if not rows:
        # Обязательность ``secondname`` для passport_fns на живом API не
        # проверена. Отказываться от вызова без отчества нельзя — это
        # заблокировало бы всех, у кого его нет; выдавать «ФНС не нашла» за
        # окончательное — тоже.
        return ProviderResult(
            provider=ProviderName.INN_BRIDGE,
            status=ProviderStatus.NO_RESULTS,
            error_message=None if has_middle_name else NO_MIDDLE_NAME_NOTE,
        )

    values = {
        value
        for row in rows
        if isinstance(row, Mapping)
        if (value := normalize_inn(as_text(row.get(INN_FIELD)))) is not None
    }
    if not values:
        raise ProviderUnavailableError(
            "unexpected_schema", f"В строках ответа ФНС нет поля {INN_FIELD}"
        )
    if len(values) > 1:
        # Взять первый молча запрещено: чужой ИНН подошьёт должнику чужое
        # банкротство и чужие дела — инверсия хуже ненахождения.
        raise ProviderError(
            "ambiguous_identity",
            "ФНС вернула несколько разных ИНН — однозначно определить нельзя",
        )

    value = values.pop()
    if len(value) != INN_INDIVIDUAL_LENGTH:
        # Пример вендора в fiz_02 возвращает десятизначное значение, fiz_04 —
        # двенадцатизначное. Десять цифр — это ИНН юрлица; отдать его в
        # ``innfiz``, который валидируется как двенадцать, значит купить
        # отклонённый и всё равно оплаченный вызов у трёх источников.
        raise ProviderError(
            "unexpected_inn_length",
            f"ФНС вернула ИНН из {len(value)} цифр — это идентификатор юрлица, "
            "для проверки физлица он непригоден",
        )

    return InnBridgeResult(
        provider=ProviderName.INN_BRIDGE,
        status=ProviderStatus.SUCCESS,
        inn=value,
        records=[],
        # Никогда, вне зависимости от STORE_RAW_RESPONSES: вендор возвращает
        # присланные params эхом, то есть серию и номер паспорта.
        raw_response=None,
    )


def _scrubbed(exc: ProviderError, *, seria: str, number: str) -> ProviderError:
    """Та же ошибка с тем же кодом, но с вычищенным паспортом в тексте.

    Класс восстанавливается по статусу, а не копируется: у подклассов
    (``ProviderAuthError``, ``ProviderRateLimitedError``) свои сигнатуры
    конструктора, и статус — единственное, что отсюда уходит дальше.
    """
    message = scrub_passport(exc.message, seria=seria, number=number)
    if exc.status is ProviderStatus.UNAVAILABLE:
        return ProviderUnavailableError(exc.code, message)
    return ProviderError(exc.code, message)


def _outcome_of(result: ProviderResult) -> str:
    """Код исхода для лога. Ни параметров, ни тела, ни ФИО — только он."""
    return result.error_code or result.status.value


__all__ = [
    "NEWDB_METHOD",
    "RESULT_SECTION",
    "InnBridgeProvider",
    "InnBridgeResult",
    "PassportInnProvider",
    "missing_bridge_fields",
    "missing_bridge_input",
]
