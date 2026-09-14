"""The provider contract.

One rule governs this layer: a provider never raises at its caller. Whatever
happens inside — a timeout, a 500, JSON that does not parse, a bug — comes back
as a :class:`ProviderResult` with an honest status. The aggregator can then
report "ФССП недоступна" instead of silently producing a clean report.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.domain.enums import MissingInput, ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import ProviderResult
from app.logging_setup import get_logger
from app.utils.masking import redact_sensitive_json

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FetchContext:
    """Everything about *this* lookup that is not the subject.

    Two things live here, and both exist because they change what a source is
    allowed to cost:

    ``batch``     this is one debtor out of a run of eight hundred. Sources
                  priced per debtor are switched off here unless the deployment
                  turned them on knowingly.
    ``upstream``  results the first phase already produced, for the one source
                  whose input is another source's answer.
    """

    batch: bool = False
    upstream: tuple[ProviderResult, ...] = field(default_factory=tuple)

    def result_for(self, provider: ProviderName) -> ProviderResult | None:
        return next((item for item in self.upstream if item.provider is provider), None)


NO_CONTEXT = FetchContext()


class ProviderError(Exception):
    """Internal-to-the-provider failure, translated into a status at the boundary.

    ``raw_response`` — тело ответа, если оно у поставщика уже было на руках,
    когда разбор сломался. Нужно ровно в том случае, ради которого и заведён
    ``STORE_RAW_RESPONSES``: карта полей не сошлась с живым ответом.

    До сих пор тело в этом случае терялось. Успешный результат его нёс, а отказ
    — нет, потому что исключение поднималось раньше, чем собирался
    ``ProviderResult``. Выходило наоборот: ответ, который разобрался, сохранялся
    целиком, а тот единственный, по которому чинят карту, пропадал. Оператору
    при этом в отчёте написано «карта полей не разобрала N записей» — то есть его
    зовут чинить по телу, которого нет.
    """

    status: ProviderStatus = ProviderStatus.ERROR

    def __init__(self, code: str, message: str = "", *, raw_response: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.raw_response = raw_response


class ProviderUnavailableError(ProviderError):
    """The source could not be reached or refused to answer right now."""

    status = ProviderStatus.UNAVAILABLE


class ProviderNotConfiguredError(ProviderError):
    """Credentials or endpoints are missing. Not an error, and never an absence
    of records."""

    status = ProviderStatus.NOT_CONFIGURED

    def __init__(self, message: str = "provider is not configured") -> None:
        super().__init__("not_configured", message)


class BaseProvider(ABC):
    """Base class for every source of facts.

    Subclasses implement :meth:`_fetch`; :meth:`fetch` handles configuration
    checks, timing and exception isolation for all of them.
    """

    name: ProviderName
    title: str = ""
    # A chained source is fed by another source's answer rather than by the
    # subject, so it runs in a second phase. See ``SearchService._run_chained``.
    is_chained: bool = False
    # Источник ничего не стоит: ни ключа, ни счёта, ни строки в смете. Умолчание
    # «платный» намеренно — новый источник по умолчанию считается стоящим денег.
    #
    # Читает флаг массовый прогон: остановка прогона существует ради денег
    # («продолжать — значит платить за пустоту», см. ``services/batch.py``), и
    # отказ источника, за который никто не платит, останавливать её не должен.
    # Это не то же самое, что ``planned_calls() == 0``: там ноль означает «этому
    # субъекту нечем спросить», и его выдают в том числе платные источники.
    is_free: bool = False
    #: Источник ищет ТОЛЬКО по ИНН физлица и без него не делает ни одного вызова.
    #:
    #: Заведён не ради кода — код и так это знает, каждый такой провайдер сам
    #: возвращает ``insufficient_query``. Заведён ради ЭКРАНОВ. Справка, экран
    #: «Откуда данные», подпись моста и строка в ожидании — все четыре
    #: пересказывали это списком в тексте: «банкротство, ИП и арбитраж». Список
    #: был верен, пока источников было три; их шесть, и все четыре экрана стали
    #: врать молча, каждый по-своему.
    #:
    #: Теперь экраны спрашивают реестр провайдеров, а не помнят наизусть.
    #: Подключили источник по ИНН — он появился в тексте сам.
    needs_individual_inn: bool = False

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this provider has everything it needs to make a real call."""

    @property
    def supports_subject(self) -> bool:
        """Overridden by providers that only handle certain search types."""
        return True

    def planned_calls(self, subject: SearchSubject, context: FetchContext = NO_CONTEXT) -> int:
        """How many paid calls this subject will cost, at minimum.

        The batch estimate is what the operator confirms before money moves, so
        it has to count *calls*, not providers: ФССП searches once per region,
        pledges search by person and by VIN, and a source with nothing to search
        by costs nothing at all.
        """
        return 1 if self.is_configured and self.supports_subject else 0

    def runs_in_batch(self) -> bool:
        """Участвует ли источник в массовом прогоне ВООБЩЕ.

        Не про конкретного должника — про сам факт. Смета умножает число
        источников на число должников, и источник, который в прогоне не
        вызывается никогда, приписывает к счёту по обращению на каждого.

        Куплено включением ЕГРН: он в прогоне не участвует (отдельная
        настройка), но попал в ``configured_names``, и смета мгновенно
        подорожала на 2052 несуществующих вызова — четыре тысячи рублей
        воздуха ровно там, где владелец жмёт «Запустить».
        """
        return True

    def max_planned_calls(self, subject: SearchSubject, context: FetchContext = NO_CONTEXT) -> int:
        """The ceiling. Differs from :meth:`planned_calls` only for a chain,
        whose length is not known until the first source has answered."""
        return self.planned_calls(subject, context)

    @abstractmethod
    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        """Perform the lookup. May raise :class:`ProviderError`."""

    def missing_input_for(self, subject: SearchSubject) -> tuple[MissingInput, ...]:
        """Чего не хватает этому субъекту, чтобы источник вообще можно было спросить.

        Пустой кортеж — «спросим». Ответ по умолчанию именно такой: источник,
        который ничего не требует, ничего и не блокирует.

        Существует ради карточки запроса, которая обязана сказать, что
        откроется от каждого недостающего поля («добавьте ИНН — откроются три
        источника»), — и обязана не соврать. Захардкоженный список подписей
        рядом с кнопками уже был и разъезжался с провайдерами при первой же
        правке гейта. Правильный образец лежит рядом: ``passport_would_help``
        спрашивает сам мост через ``will_query``.

        Требование к переопределяющим: **звать эту же функцию из своего
        ``_fetch``**. Иначе гейт и предикат снова станут двумя разными кусками
        кода, и весь смысл потеряется.
        """
        return ()

    async def _dispatch(self, subject: SearchSubject, context: FetchContext) -> ProviderResult:
        """Route one fetch. Providers that read the context override this."""
        return await self._fetch(subject)

    async def fetch(
        self, subject: SearchSubject, context: FetchContext = NO_CONTEXT
    ) -> ProviderResult:
        started = time.perf_counter()
        if not self.is_configured:
            return self._result(
                ProviderStatus.NOT_CONFIGURED,
                started,
                error_code="not_configured",
                error_message=self.not_configured_reason,
            )
        try:
            result = await self._dispatch(subject, context)
        except ProviderError as exc:
            logger.warning(
                "provider.failed",
                provider=self.name.value,
                status=exc.status.value,
                error_code=exc.code,
                # Сообщение, а не только код. У кодов вроде ``upstream_error``
                # причина своя на каждый вызов и целиком лежит здесь; без неё
                # лог отвечал «источник недоступен» на вопрос «почему», и
                # разобраться на сервере было нечем. Сообщения этого слоя
                # короткие и служебные («HTTP 500», «Источник ответил …»):
                # персональных данных в них нет, они строятся из статуса
                # ответа и текста ошибки поставщика.
                error_message=exc.message,
            )
            return self._result(
                exc.status,
                started,
                error_code=exc.code,
                error_message=exc.message,
                raw_response=self.raw_for(exc.raw_response) if exc.raw_response else None,
            )
        except Exception as exc:
            # A provider bug must degrade one section of the report, not the run.
            logger.exception(
                "provider.unhandled",
                provider=self.name.value,
                error_type=type(exc).__name__,
            )
            return self._result(
                ProviderStatus.ERROR,
                started,
                error_code="unhandled_exception",
                error_message=type(exc).__name__,
            )
        if not result.duration_ms:
            result.duration_ms = _elapsed_ms(started)
        return result

    def raw_for(self, raw: str) -> str | None:
        """Тело, которое можно сохранить, — или ``None``, если хранить нельзя.

        Два правила, и оба обязательны. Хранить только при поднятом
        ``STORE_RAW_RESPONSES``: это решение развёртывания, и диагностика не
        повод его обходить. И только вычищенным: в телах лежат СНИЛС, места
        рождения и адреса РОДСТВЕННИКОВ должника — людей, которых никто не
        спрашивал и которые в отчёт не попадают.

        Провайдер без настроек тела не хранит. Таких немного, и все они
        бесплатные вспомогательные: у них нет ни ключа, ни счёта, и сохранять
        им нечего.
        """
        settings = getattr(self, "_settings", None)
        if settings is None or not getattr(settings, "store_raw_responses", False):
            return None
        return redact_sensitive_json(raw)

    def _result(
        self,
        status: ProviderStatus,
        started: float,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        raw_response: str | None = None,
    ) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            raw_response=raw_response,
            status=status,
            error_code=error_code,
            error_message=error_message,
            duration_ms=_elapsed_ms(started),
        )

    @property
    def not_configured_reason(self) -> str:
        """Чего именно не хватает, чтобы источник заработал.

        Общее «Источник не подключён» верно, но бесполезно: оператор видит
        строку и не знает, его это дело или наше. Источник, у которого причина
        одна и чинится настройкой, обязан её назвать — тогда «не подключено»
        становится задачей с решением, а не сообщением о судьбе.

        Переопределяется там, где причина известна точно. Где не известна,
        остаётся общая формулировка: перечислять все мыслимые причины хуже, чем
        не называть ни одной.
        """
        return "Источник не подключён"

    def not_configured(
        self, message: str = "Источник не подключён", *, notes: Sequence[str] = ()
    ) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.NOT_CONFIGURED,
            error_code="not_configured",
            error_message=message,
            notes=tuple(notes),
        )

    def insufficient_query(
        self, message: str, *, missing: tuple[MissingInput, ...] = ()
    ) -> ProviderResult:
        """The source was not queried because the input lacked what it needs.

        Reported as an error rather than ``NO_RESULTS``: we did not look, so we
        must not imply there was nothing to find.

        ``missing`` дублирует сообщение машинно. Текст остаётся главным — его
        печатает и веб-страница, и блок ИСТОЧНИКИ, — но сгруппировать по нему
        несколько источников с одной причиной нельзя, не разбирая собственную
        строку обратно. Поле необязательное: провайдер, который его не
        заполнил, теряет только группировку в карточке.
        """
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.ERROR,
            error_code="insufficient_query",
            error_message=message,
            missing_input=tuple(item.value for item in missing),
        )


class StubProvider(BaseProvider):
    """A source whose shape is known but for which no lawful, stable integration
    exists yet.

    It always answers ``NOT_CONFIGURED``. That is the honest answer, and it keeps
    the report's source list complete instead of pretending the source is not
    part of the picture.
    """

    def __init__(self, name: ProviderName, title: str, note: str) -> None:
        self.name = name
        self.title = title
        self.note = note

    @property
    def is_configured(self) -> bool:
        return False

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:  # pragma: no cover
        raise ProviderNotConfiguredError(self.note)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
