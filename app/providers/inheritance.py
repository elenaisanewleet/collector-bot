"""Реестр наследственных дел Федеральной нотариальной палаты.

**Зачем взыскателю.** Если должник умер, иск к нему подавать некуда:
производство прекращается, а требование предъявляется наследникам или к
наследственному имуществу в пределах его стоимости. Не узнать об этом — значит
год судиться с покойным и потерять и пошлину, и срок. Открытое наследственное
дело вдобавок называет нотариуса, который единственный знает круг наследников,
и его контакт — это следующий практический шаг, а не справка.

**Контракт снят с живого сервиса.** Два обращения, ровно два:

``GET  /ru-ru/help/probate-cases/``  страница, с которой снимаются cookie и
                                     токен из ``<meta name="csrf-token">``
``POST /api/probate-cases``          тело ``{"name": "<ФИО>", "args": {}}``

Без cookie, ``X-CSRFToken``, ``Referer`` на ту же страницу и обычного
``User-Agent`` сервис отвечает 400 или 403. Токен живёт недолго, поэтому
берётся перед каждой проверкой и нигде не кэшируется; cookie живут в
``httpx.AsyncClient``, который создаётся на одну проверку и закрывается.

Ответ: ``{"count": 1730, "records": [ {...}, ... ]}``.

**Главная ловушка источника, она же причина, по которой этот модуль такой
длинный.** Реестр ищет ТОЛЬКО ПО ФИО и возвращает всех однофамильцев разом.
Проверено живьём: «Иванов Иван Иванович» — ``count`` 1730, и все 1730 записей
приходят одним ответом; передача даты рождения в ``args`` их число не меняет, а
поле ``BirthDate`` в самих записях часто ``null``. Наивное подключение
показало бы 1730 чужих дел как дела должника и сообщило бы, что живой человек
умер. Отсюда всё устройство ниже:

*   отбор по дате рождения делается ЗДЕСЬ, потому что сервис этого не умеет;
*   запись, у которой дата рождения совпала, уезжает дальше и будет
    подтверждена матчером (0.60 за ФИО + 0.30 за дату = 0.90) — если она такая
    одна. Две записи с одной датой рождения и разными датами смерти
    подтверждение снимают: один человек умирает один раз, значит, среди
    однофамильцев есть ещё и однофамилец с тем же днём рождения;
*   размер пула однофамильцев едет вместе с записью
    (:attr:`InheritanceCase.namesake_count`) и доходит до вердикта и до фактора
    оценки. «Дело такое-то» и «дело такое-то из 1730» — утверждения разной силы,
    и выглядеть одинаково они не имеют права;
*   запись без даты рождения не подтверждаема по построению — и не скрывается:
    когда неразличимых записей немного, несколько из них несутся как
    «возможные совпадения», а когда их сотни, отчёт получает не список, а
    число и слова о том, что различить их нечем;
*   запись, чья дата смерти раньше даты рождения должника, отбрасывается: это
    единственный различитель, работающий при пустом ``BirthDate``.

**«Дел не найдено» здесь имеют право означать ровно две вещи**, и обе —
проверенные. Первая: ``count == 0`` на HTTP 200, реестр пуст. Вторая: все
найденные дела ПОЛОЖИТЕЛЬНО отнесены к другим людям — чужая дата рождения,
смерть раньше рождения должника — и ответ при этом полный. Второе не тревога, а
самый сильный ответ, который этот источник умеет давать.

Всё остальное — не «не найдено». Недоступность, 403, страница без csrf-токена,
тело незнакомой формы — это ``UNAVAILABLE``/``ERROR``, за которые скоринг не
платит ничего. Ответ, из которого мы видели не всё (реестр сообщил о делах,
которых не прислал; записи, которые не прочитались), несёт ``is_partial`` и
говорит об этом словами.

**Источник бесплатный**, в отличие от NewDB: за настройкой стоимости он не
прячется и в массовом прогоне не выключается. Отсюда же — ``is_free`` и
``credentialed=False``: 403 от сайта, которому мы не предъявляем ключа, это
«запрос не понравился», а не «ключ отклонён», и остановить массовый прогон на
восемьсот платных должников бесплатный источник не может. Но это чужой сайт, а
не API по договору, поэтому один запрос на проверку, свой скромный ретрай и
никакого перебора.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from app.config import Settings
from app.domain.enums import MissingInput, ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import InheritanceCase, ProviderResult
from app.logging_setup import get_logger
from app.providers.base import (
    NO_CONTEXT,
    BaseProvider,
    FetchContext,
    ProviderUnavailableError,
)
from app.providers.http import (
    ProviderBadResponseError,
    RetryPolicy,
    build_client,
    request_json,
    request_text,
)
from app.providers.mapping import as_text
from app.utils.dates import parse_date, utcnow
from app.utils.formatting import pluralize_ru

logger = get_logger(__name__)

SEARCH_PAGE_PATH = "/ru-ru/help/probate-cases/"
API_PATH = "/api/probate-cases"
PUBLIC_SEARCH_URL = "https://notariat.ru/ru-ru/help/probate-cases/"

# Обычный браузерный агент. Сервис отвечает публичной страницей поиска, и
# запрос, не похожий на браузерный, он отклоняет вместе с cookie-сессией.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_CSRF_META = re.compile(
    r"<meta[^>]+name=[\"']csrf-token[\"'][^>]+content=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_CSRF_META_REVERSED = re.compile(
    r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+name=[\"']csrf-token[\"']",
    re.IGNORECASE,
)

# Подтверждённых дел на одного человека единицы; двадцать — потолок с запасом,
# и он же граница, за которой в БД перестают уезжать чужие персональные данные.
MAX_RECORDS = 20
# Сколько несопоставленных однофамильцев показать записями — и до какого числа
# найденного это вообще осмысленно. Пять чужих дел оператор глазами разберёт,
# тысячу семьсот — нет, и там нужны не строки, а число и ссылка на ручную
# проверку. Границы выбраны по соседям (MAX_LISTED_* в отчёте), а не выведены.
MAX_UNMATCHED_SHOWN = 5
UNMATCHED_LIST_LIMIT = 20

# Раньше начала регистрации актов о смерти в нынешнем виде дат в этом реестре не
# бывает, а «20991231» бывает — опечаткой на стороне нотариуса или чужим полем,
# попавшим в разбор. Напечатанная как «дата смерти 31.12.2099», такая строка
# выглядит как факт; неразобранная — как отсутствие даты, то есть как «различить
# нечем». Второе честнее.
MIN_PLAUSIBLE_YEAR = 1900

NAME_REQUIRED = "Для проверки наследственных дел нужно ФИО: реестр ФНП ищет только по нему"


class NotariatInheritanceProvider(BaseProvider):
    """Наследственные дела через открытый поиск notariat.ru."""

    name = ProviderName.INHERITANCE
    title = "Наследственные дела"
    # Ни ключа, ни счёта. Отсюда же следует, что отказ этого источника не имеет
    # права остановить массовый прогон: см. ``BaseProvider.is_free``.
    is_free = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def is_configured(self) -> bool:
        return self._settings.inheritance_configured

    def missing_input_for(self, subject: SearchSubject) -> tuple[MissingInput, ...]:
        """Нужно только ФИО.

        Дата рождения здесь НЕ называется, хотя без неё ни одна запись не будет
        подтверждена. Объявить её обязательной значило бы не спрашивать
        бесплатный реестр вовсе — а «нашли 1730 дел на это ФИО, различить
        нечем» это полезный ответ, в отличие от молчания.
        """
        return () if subject.name is not None else (MissingInput.NAME,)

    def planned_calls(self, subject: SearchSubject, context: FetchContext = NO_CONTEXT) -> int:
        """Ноль: источник бесплатный.

        ``planned_calls`` считает платные обращения — это число оператор
        подтверждает перед тем, как потратить деньги. Реестр ФНП не
        тарифицируется, и завышать им смету прогона на восемьсот должников
        нельзя.
        """
        return 0

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        # Гейт зовёт тот же предикат, что показывает карточка запроса: иначе
        # обещание «добавьте ФИО — откроется источник» и поведение источника
        # станут двумя разными кусками кода. См. BaseProvider.missing_input_for.
        missing = self.missing_input_for(subject)
        if missing or subject.name is None:
            return self.insufficient_query(NAME_REQUIRED, missing=missing)

        payload = await self._search(subject.name.full)
        return _build_result(self.name, subject, answer=_unpack(payload))

    async def _search(self, full_name: str) -> Any:
        """Страница за токеном, затем запрос. Один клиент, чтобы cookie доехали.

        Ретрай сознательно свой, а не из ``PROVIDER_MAX_RETRIES``: на настройке
        по умолчанию одна проверка превратилась бы в шесть обращений к чужому
        сайту.
        """
        retry = RetryPolicy(max_retries=1, backoff_seconds=0.5)
        base_url = self._settings.inheritance_base_url
        async with build_client(
            base_url=base_url,
            timeout_seconds=self._settings.request_timeout_seconds,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
        ) as client:
            page = await request_text(
                client,
                "GET",
                SEARCH_PAGE_PATH,
                retry=retry,
                provider=self.name.value,
                # Ключа мы этому источнику не предъявляем, поэтому отвергнуть
                # его 403 не может: это «запрос не понравился», а не «ключ
                # отклонён». См. ``http._classify``.
                credentialed=False,
            )
            token = _csrf_token(page)
            if token is None:
                # Именно UNAVAILABLE, а не пустой результат. Это самый вероятный
                # способ, которым «не проверено» превратилось бы в «чисто»:
                # редирект, капча или переверстанная страница выглядят как
                # успешный GET, после которого просто нечего искать.
                raise ProviderUnavailableError(
                    "csrf_missing",
                    "страница поиска не отдала csrf-токен — форма запроса изменилась",
                )
            # Токен только что снят с живой страницы и нигде не кэшируется: он
            # живёт минуты, а проверки идут врозь и редко.
            _apply_csrf(client, token, base_url=base_url)
            payload, _raw = await request_json(
                client,
                "POST",
                API_PATH,
                json_body={"name": full_name, "args": {}},
                retry=retry,
                provider=self.name.value,
                credentialed=False,
            )
            return payload


@dataclass(frozen=True, slots=True)
class _Answer:
    """Что реестр сказал о себе — до всякого нашего отбора.

    Три числа, и они не совпадают. ``total`` — сколько дел реестр НАШЁЛ по ФИО.
    ``delivered`` — сколько записей он прислал в теле. ``rows`` — сколько из них
    удалось прочитать как наследственное дело. Каждое расхождение между ними
    означает, что мы видели не всё, и обязано доехать до ``is_partial``: молчание
    здесь — это «проверено, ничего не найдено» вместо «проверено не всё».
    """

    total: int
    delivered: int
    rows: list[Mapping[str, Any]]

    @property
    def dropped(self) -> int:
        """Записи, приехавшие в теле и не прочитанные."""
        return self.delivered - len(self.rows)

    @property
    def unseen(self) -> int:
        """Дела, о которых реестр сообщил, но которых не прислал."""
        return max(self.total - self.delivered, 0)


def _unpack(payload: Any) -> _Answer:
    """``{"count": N, "records": [...]}`` — и ничего другого.

    ``count`` берётся из ответа, а не считается по длине списка, и это
    принципиально: именно он говорит, сколько реестр НАШЁЛ, тогда как показать
    мы имеем право лишь то, что смогли сопоставить. Тело незнакомой формы —
    ошибка источника, а не пустой реестр.

    **Сторожит эта функция ИСХОДНЫЕ записи, а не отобранные.** Проверка «ни одна
    из разобранных не похожа на дело» пропускала четыре формы битого тела разом:
    записи списками вместо словарей, ``null`` вместо записей, ответ без ключа
    ``count`` и ``count`` строкой. Во всех четырёх отбор давал пустой список,
    пустой список давал чистое «наследственных дел по этому ФИО не найдено», а
    отчёт с этой строкой уходил в суд — иск к покойному без единого следа.
    Поэтому: непрочитанные записи считаются (``dropped``), а не отбрасываются
    молча, и ``count`` обязан быть числом — подставить вместо него длину списка
    значит поверить телу, о котором мы уже знаем, что оно не то.
    """
    if not isinstance(payload, Mapping):
        raise ProviderBadResponseError(
            "unexpected_schema", "ответ реестра наследственных дел не является объектом"
        )
    raw_records = payload.get("records")
    if raw_records is None or not isinstance(raw_records, Sequence) or isinstance(raw_records, str):
        raise ProviderBadResponseError("unexpected_schema", "в ответе реестра нет списка records")
    raw = list(raw_records)
    rows = [row for row in raw if _looks_like_a_case(row)]
    if raw and not rows:
        # Строки пришли, и ни одна не похожа на наследственное дело: схема
        # уехала. Разбор, отдавший ноль записей на непустом ответе, выглядит
        # как чистый реестр — а это ровно та подмена, которой здесь быть нельзя.
        raise ProviderUnavailableError(
            "unexpected_schema", "записи реестра не содержат ни ФИО, ни номера дела"
        )
    return _Answer(total=_total(payload, delivered=len(raw)), delivered=len(raw), rows=rows)


def _total(payload: Mapping[str, Any], *, delivered: int) -> int:
    """Сколько дел реестр нашёл. Без этого числа ответ не разбирается.

    ``count`` — единственное, что говорит о размере пула однофамильцев, а пул
    входит и в вердикт, и в фактор оценки. Пропавший или нечисловой ``count`` —
    это схема, которая уехала; подставленная вместо него длина присланного
    списка превращает «нашли 1730, прислали 12» в «нашли 12» и делает неполноту
    невидимой.
    """
    count = payload.get("count")
    if count is None:
        raise ProviderBadResponseError(
            "unexpected_schema", "в ответе реестра нет числа найденных дел (count)"
        )
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ProviderBadResponseError(
            "unexpected_schema", f"count в ответе реестра — не число дел: {count!r}"
        )
    # Прислать больше, чем нашёл, реестр не может; если прислал — врёт именно
    # ``count``, и меньшее из двух чисел занизило бы размер пула однофамильцев.
    return max(count, delivered)


def _looks_like_a_case(row: Any) -> bool:
    """Похожа ли ИСХОДНАЯ запись на наследственное дело.

    ``row`` намеренно ``Any``: сторожить надо в том числе то, что записью не
    является вовсе — список, строку, ``null``.
    """
    if not isinstance(row, Mapping):
        return False
    return bool(as_text(row.get("Fio")) or as_text(row.get("CaseNumber")))


@dataclass(frozen=True, slots=True)
class _Sorted:
    """Разобранные записи, разложенные по тому, что о них известно."""

    #: Дата рождения из записи сошлась с датой рождения должника.
    matched: list[InheritanceCase]
    #: Различить нечем: даты рождения нет либо в записи, либо у нас.
    undated: list[InheritanceCase]
    #: Положительно исключены: чужая дата рождения или смерть раньше рождения
    #: должника. Не «мы не разобрались», а «это точно другой человек».
    excluded: int


def _build_result(
    provider: ProviderName,
    subject: SearchSubject,
    *,
    answer: _Answer,
) -> ProviderResult:
    """Пять исходов, и «дел не найдено» из них ровно два — оба заслуженные.

    ``NO_RESULTS`` печатается, когда реестр пуст, и когда все найденные дела
    ПОЛОЖИТЕЛЬНО исключены: чужая дата рождения, смерть раньше рождения
    должника. Второе — самый сильный ответ, который этот источник умеет давать
    («ни одно из этих дел не про вашего должника»), и подавать его как
    нерешённую тревогу «найдено 3 дела, сопоставить не удалось ни одного» —
    значит выбросить знание, за которым мы и ходили.

    Остальные три исхода — подтверждённое дело, неразличимые записи списком,
    неразличимых слишком много — «не найдено» не говорят никогда.
    """
    if answer.total == 0 and not answer.rows:
        return ProviderResult(
            provider=provider,
            status=ProviderStatus.NO_RESULTS,
            # Сырое тело не сохраняется НИКОГДА, независимо от
            # STORE_RAW_RESPONSES: см. комментарий в ветке ниже.
            raw_response=None,
        )

    sorted_rows = _partition(answer.rows, subject.birth_date)
    matched, undated = sorted_rows.matched, sorted_rows.undated
    # Ответ, из которого мы видели не всё: реестр сообщил о делах, которых не
    # прислал, либо прислал записи, которых мы не прочитали.
    incomplete = bool(answer.unseen or answer.dropped)

    if matched:
        # Один человек умирает один раз. Две и более записи с совпавшей датой
        # рождения и РАЗНЫМИ датами смерти — это не «мало данных», а доказанное
        # свидетельство того, что совпадение по дате рождения здесь ничего не
        # различает: где-то среди них однофамилец с тем же днём рождения.
        # Подтверждение снимается целиком — матчер опустит эти записи до
        # «возможного совпадения», вердикт «должник умер» не выставится.
        contested = _contested(matched)
        records = matched[:MAX_RECORDS]
        hidden = len(matched) - len(records)
        for case in records:
            case.namesake_count = answer.total
            case.contested = contested
        return ProviderResult(
            provider=provider,
            status=ProviderStatus.SUCCESS,
            records=list(records),
            is_partial=contested or bool(undated) or bool(hidden) or incomplete,
            notes=_matched_notes(
                total=answer.total,
                matched=len(matched),
                undated=len(undated),
                hidden=hidden,
                answer=answer,
                contested=contested,
            ),
            raw_response=None,
        )

    if not undated and not incomplete and sorted_rows.excluded:
        # Все найденные дела разобраны и все до одного отнесены к другим людям.
        # Ответ полный и отрицательный — единственный случай, когда этот
        # источник имеет право сказать про должника «ничего».
        return ProviderResult(
            provider=provider,
            status=ProviderStatus.NO_RESULTS,
            is_partial=False,
            notes=_excluded_notes(total=answer.total, excluded=sorted_rows.excluded),
            raw_response=None,
        )

    # Ни одного совпадения по дате рождения, и остались неразличимые. Молчать об
    # этом нельзя: «дел не найдено» и «нашли столько-то, различить нечем» —
    # разные ответы, и второй означает ручную проверку, а не чистый реестр.
    #
    # Неразличимых столько, сколько их на самом деле: непоказанные записи из
    # тела ПЛЮС дела, о которых реестр сообщил, но которых не прислал. Считать
    # их по ``total`` было ошибкой — при трёх записях, две из которых чужие,
    # отчёт писал «однофамильцев слишком много, записи не выводятся».
    indistinguishable = len(undated) + answer.unseen
    shown = _shown_unmatched(undated, indistinguishable=indistinguishable)
    for case in shown:
        case.namesake_count = answer.total
    return ProviderResult(
        provider=provider,
        # SUCCESS, если что-то показываем, — иначе список источников напишет
        # «проверено, записей нет» под разделом, который говорит обратное.
        # is_partial стоит в обеих ветках и решает раньше статуса: чип получится
        # «ответ неполный», а не «✓ N зап.», и однофамильцы не прочитаются как
        # находка по должнику.
        status=ProviderStatus.SUCCESS if shown else ProviderStatus.NO_RESULTS,
        records=list(shown),
        is_partial=True,
        notes=_unmatched_notes(
            total=answer.total,
            indistinguishable=indistinguishable,
            shown=len(shown),
            excluded=sorted_rows.excluded,
            answer=answer,
            subject_birth_date=subject.birth_date,
        ),
        # Тело на 1730 записей — это ФИО, адреса, номера актов о смерти и
        # телефоны тысячи посторонних людей. ``redact_sensitive_json`` вырезает
        # закрытый список ключей (снилс, паспорт, место рождения), и ключей
        # этого источника в нём нет. Не «пока не добавили» — не сохранять
        # вообще: у нас нет причин держать в своей базе паспортизованные
        # сведения о смерти чужих людей.
        raw_response=None,
    )


def _contested(matched: Sequence[InheritanceCase]) -> bool:
    """Совпадение по дате рождения, опровергнутое другим таким же совпадением."""
    return len(matched) >= 2 and len({case.death_date for case in matched}) >= 2


def _partition(rows: Sequence[Mapping[str, Any]], birth_date: date | None) -> _Sorted:
    """Разложить сырые строки на «это он», «различить нечем» и «это точно не он».

    Записи с ДРУГОЙ датой рождения не попадают никуда: матчер и так уронил бы
    их в 0.05, но не класть чужие персональные данные в свою базу дешевле, чем
    положить и отфильтровать при показе. Считаются они, однако, обязательно:
    исключённая запись — это знание, а не пропажа, и именно из их числа
    складывается ответ «все найденные дела относятся к другим людям».

    Модели строятся только для того, что несём дальше. Разбирать 1730 записей,
    чтобы выбросить 1729, незачем.
    """
    matched: list[InheritanceCase] = []
    undated: list[InheritanceCase] = []
    excluded = 0
    for row in rows:
        record_birth = _parse_compact_date(as_text(row.get("BirthDate")))
        if birth_date is not None and record_birth is not None and record_birth != birth_date:
            # Чужая дата рождения. Дальше эта запись не едет вовсе.
            excluded += 1
            continue
        case = _to_case(row)
        if birth_date is None or record_birth is None:
            # Сопоставлять не с чем — либо реестр не назвал дату рождения, либо
            # её нет у нас. Подтвердить такую запись невозможно по построению.
            # Единственный различитель, который здесь ещё работает, — дата
            # смерти: умереть до собственного рождения нельзя.
            if case.contradicts_birth_date(birth_date):
                excluded += 1
            else:
                undated.append(case)
            continue
        matched.append(case)
    return _Sorted(matched=matched, undated=undated, excluded=excluded)


def _shown_unmatched(
    undated: Sequence[InheritanceCase], *, indistinguishable: int
) -> list[InheritanceCase]:
    """Сколько неразличимых записей показать — и когда не показывать вовсе.

    Скрыть их целиком нельзя: «не нашли» и «нашли, но не смогли сопоставить» —
    разные вещи. Но и вывалить сотни чужих дел нельзя: таблица на сорок строк
    читается как «вот что нашли про него», и никакая подпись в последней
    колонке этого не перебивает. Поэтому граница: пока неразличимых немного,
    показываем несколько записей и отдаём матчеру решать, насколько они
    похожи; дальше — только число и ссылка на ручную проверку.

    Граница считается по ФАКТИЧЕСКОМУ числу неразличимых, а не по размеру пула
    однофамильцев: три записи, из которых две отнесены к другим людям, —
    это три строки на экране, а не повод написать «однофамильцев слишком много».
    """
    if indistinguishable > UNMATCHED_LIST_LIMIT:
        return []
    return list(undated[:MAX_UNMATCHED_SHOWN])


def _incompleteness_notes(answer: _Answer) -> list[str]:
    """Чего мы не видели. Печатается всегда, когда есть о чём."""
    notes: list[str] = []
    if answer.unseen:
        noun = pluralize_ru(answer.unseen, "дело", "дела", "дел")
        notes.append(
            f"Реестр сообщил о {answer.total} делах, а прислал {answer.delivered}: "
            f"ещё {answer.unseen} {noun} не разбирались."
        )
    if answer.dropped:
        noun = pluralize_ru(answer.dropped, "запись", "записи", "записей")
        notes.append(
            f"{answer.dropped} {noun} из ответа реестра прочитать не удалось — они не проверены."
        )
    return notes


def _matched_notes(
    *,
    total: int,
    matched: int,
    undated: int,
    hidden: int,
    answer: _Answer,
    contested: bool,
) -> tuple[str, ...]:
    notes: list[str] = []
    if contested:
        noun = pluralize_ru(matched, "дело", "дела", "дел")
        notes.append(
            f"Дата рождения совпала сразу у {matched} {noun}, и даты смерти в них разные. "
            "Один человек умирает один раз — значит, среди них однофамилец с тем же днём "
            "рождения, и подтвердить смерть должника по этим записям нельзя."
        )
    if undated:
        noun = pluralize_ru(undated, "дело", "дела", "дел")
        notes.append(
            f"Ещё {undated} {noun} на это ФИО реестр вернул без даты рождения — "
            "сопоставить их с должником нечем."
        )
    if hidden:
        noun = pluralize_ru(hidden, "дело", "дела", "дел")
        notes.append(f"Показаны не все: ещё {hidden} совпавших {noun} не выведены.")
    notes.extend(_incompleteness_notes(answer))
    if total:
        notes.append(f"Всего по этому ФИО в реестре ФНП найдено дел: {total}.")
    return tuple(notes)


def _excluded_notes(*, total: int, excluded: int) -> tuple[str, ...]:
    """Самый сильный ответ источника: ни одно из найденных дел не про должника."""
    noun = pluralize_ru(total, "дело", "дела", "дел")
    return (
        f"В реестре наследственных дел ФНП по этому ФИО найдено {total} {noun}, "
        f"и все {excluded} относятся к другим людям: даты рождения не совпали с датой "
        "из карточки должника либо смерть наступила раньше, чем должник родился.",
        f"Проверить вручную: {PUBLIC_SEARCH_URL}",
    )


def _unmatched_notes(
    *,
    total: int,
    indistinguishable: int,
    shown: int,
    excluded: int,
    answer: _Answer,
    subject_birth_date: date | None,
) -> tuple[str, ...]:
    """Словами и с числом — как того требует раздел «нашли много, различить нечем»."""
    noun = pluralize_ru(total, "дело", "дела", "дел")
    head = (
        f"В реестре наследственных дел ФНП по этому ФИО найдено {total} {noun}. "
        "Реестр ищет только по ФИО; сопоставить по дате рождения не удалось ни одного дела."
    )
    lines = [head]
    if excluded:
        noun = pluralize_ru(excluded, "дело", "дела", "дел")
        lines.append(f"Из них {excluded} {noun} отнесены к другим людям и не показаны.")
    if shown:
        remaining = indistinguishable - shown
        tail = f", ещё {remaining} не выведены" if remaining > 0 else ""
        noun = pluralize_ru(shown, "дело", "дела", "дел")
        lines.append(
            f"Ниже показано {shown} {noun} как возможные совпадения{tail}: "
            f"{_why_indistinguishable(subject_birth_date)}"
        )
    else:
        noun = pluralize_ru(indistinguishable, "запись", "записи", "записей")
        lines.append(
            f"Однофамильцев слишком много, чтобы показывать их списком: "
            f"неразличимых {indistinguishable} {noun}, они не выводятся."
        )
    lines.extend(_incompleteness_notes(answer))
    lines.append(f"Проверить вручную: {PUBLIC_SEARCH_URL}")
    return tuple(lines)


def _why_indistinguishable(subject_birth_date: date | None) -> str:
    """Чей пробел мешает сопоставить — реестра или наш.

    Две разные причины, и раньше печаталась одна: «даты рождения в этих записях
    реестр не указал» стояло и там, где даты в записях есть, а нет её у нас, в
    карточке должника. Отчёт перекладывал наш пробел на источник и закрывал
    оператору самое дешёвое действие, какое здесь бывает: дописать дату рождения
    и переспросить бесплатный реестр.
    """
    if subject_birth_date is None:
        return (
            "в карточке должника нет даты рождения — сопоставлять не с чем. "
            "Добавьте дату рождения в карточку и повторите проверку: "
            "она бесплатная, а различить однофамильцев больше нечем."
        )
    return "даты рождения в этих записях реестр не указал."


def _to_case(row: Mapping[str, Any]) -> InheritanceCase:
    """Одна запись реестра. Поля проверены на живом ответе.

    Адрес умершего, номер и дата актовой записи о смерти, адрес и телефон
    нотариуса в модель не переносятся: см. докстроку :class:`InheritanceCase`.
    """
    closed_at = as_text(row.get("CaseCloseDate"))
    return InheritanceCase(
        deceased_name=as_text(row.get("Fio")),
        deceased_birth_date=_parse_compact_date(as_text(row.get("BirthDate"))),
        death_date=_parse_compact_date(as_text(row.get("DeathDate"))),
        case_number=as_text(row.get("CaseNumber")),
        # Даты дела разбираются тем же разбором, что и даты человека. Общий
        # ``parse_date`` читает восьмизначную строку как ДДММГГГГ и на
        # «20150301» отдаёт ``None`` — дело с непрочитанной датой закрытия
        # объявлялось открытым, а открытое и закрытое дело — это разные
        # следующие шаги и разные сроки.
        case_date=_parse_compact_date(as_text(row.get("CaseDate"))),
        case_close_date=_parse_compact_date(closed_at),
        # Состояние берётся из записи, а не выводится из разобранной даты: поле
        # заполнено — дело закрыто, и это верно даже тогда, когда саму дату
        # прочитать не удалось.
        case_closed=closed_at is not None,
        notary_name=as_text(row.get("NotaryName")),
        chamber_name=as_text(row.get("ChamberName")),
        district_name=as_text(row.get("DistrictName")),
        source_url=PUBLIC_SEARCH_URL,
        fetched_at=utcnow(),
    )


def _parse_compact_date(raw: str | None) -> date | None:
    """``"19760330"`` — это ГГГГММДД, а не ДДММГГГГ.

    Общий :func:`app.utils.dates.parse_date` восьмизначную строку читает как
    ДДММГГГГ, и на этом источнике молча возвращает ``None``
    (``parse_date("19760330")`` — тридцатого месяца не бывает). Тихая ``None``
    в дате рождения — это запись, которая никогда не подтвердится, а тихая
    ``None`` в дате смерти — исчезнувший из отчёта факт.

    Чинить общий парсер нельзя: для остальных источников ДДММГГГГ верно, а
    ``"01021990"`` неразрешимо неоднозначен. Поэтому формат разбирается здесь,
    где он известен из контракта.
    """
    if raw is None:
        return None
    text = raw.strip()
    if len(text) != 8 or not text.isdigit():
        # Не восемь цифр — может быть ISO из другой ветки ответа; общий парсер
        # с этим справится, и он же отвергнет невозможное.
        return _plausible(parse_date(text))
    try:
        return _plausible(datetime.strptime(text, "%Y%m%d").date())
    except ValueError:
        return None


def _plausible(value: date | None) -> date | None:
    """Дата, которой в этом реестре быть не может, — это не дата.

    Календарно «20991231» безупречно, и напечатанная как «дата смерти
    31.12.2099» она читается оператором как факт. Ни смерти, ни рождения, ни
    заведения дела в будущем не бывает, и до 1900 года в этом реестре тоже
    ничего нет. Возвращается ``None``: «даты нет» здесь означает «различить
    нечем», то есть осторожную сторону, а не выдуманный факт.
    """
    if value is None:
        return None
    # Сутки запаса на часовой пояс: реестр живёт по московскому времени, наши
    # часы — по UTC, и «сегодня» в Москве бывает «завтра» относительно UTC.
    # Отбраковывать по этой разнице настоящую дату было бы хуже, чем пропустить
    # завтрашнюю: до 2099 года запас не дотягивается, а до опечатки в дне — да.
    if value.year < MIN_PLAUSIBLE_YEAR or value > utcnow().date() + timedelta(days=1):
        logger.warning("inheritance.implausible_date", value=value.isoformat())
        return None
    return value


def _csrf_token(page: str) -> str | None:
    """Токен из ``<meta name="csrf-token" content="...">``.

    Порядок атрибутов в теге не гарантирован ничем, кроме сегодняшней вёрстки
    чужого сайта, поэтому проверяются оба. Не нашли — это недоступность
    источника, а не отсутствие дел; решение принимает вызывающий.
    """
    for pattern in (_CSRF_META, _CSRF_META_REVERSED):
        found = pattern.search(page)
        if found:
            token = found.group(1).strip()
            if token:
                return token
    return None


def _apply_csrf(client: httpx.AsyncClient, token: str, *, base_url: str) -> None:
    """Проставить клиенту заголовки, без которых сервис отвечает 400 или 403."""
    client.headers["X-CSRFToken"] = token
    client.headers["Referer"] = f"{base_url}{SEARCH_PAGE_PATH}"


__all__ = ["MAX_RECORDS", "PUBLIC_SEARCH_URL", "NotariatInheritanceProvider"]
