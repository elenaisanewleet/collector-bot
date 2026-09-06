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
    подтверждена матчером (0.60 за ФИО + 0.30 за дату = 0.90);
*   запись без даты рождения не подтверждаема по построению — и не скрывается:
    когда однофамильцев немного, несколько таких записей несутся как
    «возможные совпадения», а когда их сотни, отчёт получает не список, а
    число и слова о том, что различить их нечем;
*   запись, чья дата смерти раньше даты рождения должника, отбрасывается: это
    единственный различитель, работающий при пустом ``BirthDate``.

**Ничего из этого не может выглядеть как «дел не найдено».** ``NO_RESULTS``
выдаётся ровно при ``count == 0`` на HTTP 200. Недоступность, 403, страница без
csrf-токена, тело незнакомой формы — это ``UNAVAILABLE``/``ERROR``, за которые
скоринг не платит ничего.

**Источник бесплатный**, в отличие от NewDB: за настройкой стоимости он не
прячется и в массовом прогоне не выключается. Но это чужой сайт, а не API по
договору, поэтому один запрос на проверку, свой скромный ретрай и никакого
перебора.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

import httpx

from app.config import Settings
from app.domain.enums import MissingInput, ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import InheritanceCase, ProviderResult
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

NAME_REQUIRED = "Для проверки наследственных дел нужно ФИО: реестр ФНП ищет только по нему"


class NotariatInheritanceProvider(BaseProvider):
    """Наследственные дела через открытый поиск notariat.ru."""

    name = ProviderName.INHERITANCE
    title = "Наследственные дела"

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
        total, rows = _unpack(payload)
        return _build_result(self.name, subject, total=total, rows=rows)

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
                client, "GET", SEARCH_PAGE_PATH, retry=retry, provider=self.name.value
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
            )
            return payload


def _unpack(payload: Any) -> tuple[int, list[Mapping[str, Any]]]:
    """``{"count": N, "records": [...]}`` — и ничего другого.

    ``count`` берётся из ответа, а не считается по длине списка, и это
    принципиально: именно он говорит, сколько реестр НАШЁЛ, тогда как показать
    мы имеем право лишь то, что смогли сопоставить. Тело незнакомой формы —
    ошибка источника, а не пустой реестр.
    """
    if not isinstance(payload, Mapping):
        raise ProviderBadResponseError(
            "unexpected_schema", "ответ реестра наследственных дел не является объектом"
        )
    raw_records = payload.get("records")
    if raw_records is None or not isinstance(raw_records, Sequence) or isinstance(raw_records, str):
        raise ProviderBadResponseError("unexpected_schema", "в ответе реестра нет списка records")
    rows = [row for row in raw_records if isinstance(row, Mapping)]
    count = payload.get("count")
    total = count if isinstance(count, int) and not isinstance(count, bool) else len(rows)
    if rows and not any(_looks_like_a_case(row) for row in rows):
        # Строки пришли, и ни одна не похожа на наследственное дело: схема
        # уехала. Разбор, отдавший ноль записей на непустом ответе, выглядит
        # как чистый реестр — а это ровно та подмена, которой здесь быть нельзя.
        raise ProviderUnavailableError(
            "unexpected_schema", "записи реестра не содержат ни ФИО, ни номера дела"
        )
    return max(total, 0), rows


def _looks_like_a_case(row: Mapping[str, Any]) -> bool:
    return bool(as_text(row.get("Fio")) or as_text(row.get("CaseNumber")))


def _build_result(
    provider: ProviderName,
    subject: SearchSubject,
    *,
    total: int,
    rows: Sequence[Mapping[str, Any]],
) -> ProviderResult:
    """Три исхода, и ни один из них не «дел не найдено», кроме первого."""
    if total == 0 and not rows:
        return ProviderResult(
            provider=provider,
            status=ProviderStatus.NO_RESULTS,
            # Сырое тело не сохраняется НИКОГДА, независимо от
            # STORE_RAW_RESPONSES: см. комментарий в ветке ниже.
            raw_response=None,
        )

    matched, undated = _partition(rows, subject.birth_date)

    if matched:
        records = matched[:MAX_RECORDS]
        hidden = len(matched) - len(records)
        return ProviderResult(
            provider=provider,
            status=ProviderStatus.SUCCESS,
            records=list(records),
            is_partial=bool(undated) or bool(hidden),
            notes=_matched_notes(total=total, undated=len(undated), hidden=hidden),
            raw_response=None,
        )

    # Ни одного совпадения по дате рождения. Молчать об этом нельзя: «дел не
    # найдено» и «нашли столько-то, различить нечем» — разные ответы, и второй
    # означает ручную проверку, а не чистый реестр.
    shown = _shown_unmatched(undated, total=total)
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
        notes=_unmatched_notes(total=total, undated=len(undated), shown=len(shown)),
        # Тело на 1730 записей — это ФИО, адреса, номера актов о смерти и
        # телефоны тысячи посторонних людей. ``redact_sensitive_json`` вырезает
        # закрытый список ключей (снилс, паспорт, место рождения), и ключей
        # этого источника в нём нет. Не «пока не добавили» — не сохранять
        # вообще: у нас нет причин держать в своей базе паспортизованные
        # сведения о смерти чужих людей.
        raw_response=None,
    )


def _partition(
    rows: Sequence[Mapping[str, Any]], birth_date: date | None
) -> tuple[list[InheritanceCase], list[InheritanceCase]]:
    """Разложить сырые строки на «это он» и «различить нечем».

    Записи с ДРУГОЙ датой рождения не попадают никуда: матчер и так уронил бы
    их в 0.05, но не класть чужие персональные данные в свою базу дешевле, чем
    положить и отфильтровать при показе.

    Модели строятся только для того, что несём дальше. Разбирать 1730 записей,
    чтобы выбросить 1729, незачем.
    """
    matched: list[InheritanceCase] = []
    undated: list[InheritanceCase] = []
    for row in rows:
        if not _looks_like_a_case(row):
            continue
        record_birth = _parse_compact_date(as_text(row.get("BirthDate")))
        if birth_date is not None and record_birth is not None and record_birth != birth_date:
            # Чужая дата рождения. Дальше эта запись не едет вовсе.
            continue
        case = _to_case(row)
        if birth_date is None or record_birth is None:
            # Сопоставлять не с чем — либо реестр не назвал дату рождения, либо
            # её нет у нас. Подтвердить такую запись невозможно по построению.
            # Единственный различитель, который здесь ещё работает, — дата
            # смерти: умереть до собственного рождения нельзя.
            if not case.contradicts_birth_date(birth_date):
                undated.append(case)
            continue
        matched.append(case)
    return matched, undated


def _shown_unmatched(undated: Sequence[InheritanceCase], *, total: int) -> list[InheritanceCase]:
    """Сколько неразличимых записей показать — и когда не показывать вовсе.

    Скрыть их целиком нельзя: «не нашли» и «нашли, но не смогли сопоставить» —
    разные вещи. Но и вывалить сотни чужих дел нельзя: таблица на сорок строк
    читается как «вот что нашли про него», и никакая подпись в последней
    колонке этого не перебивает. Поэтому граница: пока однофамильцев немного,
    показываем несколько записей и отдаём матчеру решать, насколько они
    похожи; дальше — только число и ссылка на ручную проверку.
    """
    if total > UNMATCHED_LIST_LIMIT:
        return []
    return list(undated[:MAX_UNMATCHED_SHOWN])


def _matched_notes(*, total: int, undated: int, hidden: int) -> tuple[str, ...]:
    notes: list[str] = []
    if undated:
        noun = pluralize_ru(undated, "дело", "дела", "дел")
        notes.append(
            f"Ещё {undated} {noun} на это ФИО реестр вернул без даты рождения — "
            "сопоставить их с должником нечем."
        )
    if hidden:
        noun = pluralize_ru(hidden, "дело", "дела", "дел")
        notes.append(f"Показаны не все: ещё {hidden} совпавших {noun} не выведены.")
    if total:
        notes.append(f"Всего по этому ФИО в реестре ФНП найдено дел: {total}.")
    return tuple(notes)


def _unmatched_notes(*, total: int, undated: int, shown: int) -> tuple[str, ...]:
    """Словами и с числом — как того требует раздел «нашли много, различить нечем»."""
    noun = pluralize_ru(total, "дело", "дела", "дел")
    head = (
        f"В реестре наследственных дел ФНП по этому ФИО найдено {total} {noun}. "
        "Реестр ищет только по ФИО; сопоставить по дате рождения не удалось ни одного дела."
    )
    lines = [head]
    if shown:
        remaining = undated - shown
        tail = f", ещё {remaining} не выведены" if remaining > 0 else ""
        lines.append(
            f"Ниже показаны {shown} из них как возможные совпадения{tail}: "
            "даты рождения в этих записях реестр не указал."
        )
    else:
        lines.append(
            "Однофамильцев слишком много, чтобы показывать их списком — записи не выводятся."
        )
    lines.append(f"Проверить вручную: {PUBLIC_SEARCH_URL}")
    return tuple(lines)


def _to_case(row: Mapping[str, Any]) -> InheritanceCase:
    """Одна запись реестра. Поля проверены на живом ответе.

    Адрес умершего, номер и дата актовой записи о смерти, адрес и телефон
    нотариуса в модель не переносятся: см. докстроку :class:`InheritanceCase`.
    """
    return InheritanceCase(
        deceased_name=as_text(row.get("Fio")),
        deceased_birth_date=_parse_compact_date(as_text(row.get("BirthDate"))),
        death_date=_parse_compact_date(as_text(row.get("DeathDate"))),
        case_number=as_text(row.get("CaseNumber")),
        case_date=parse_date(as_text(row.get("CaseDate"))),
        case_close_date=parse_date(as_text(row.get("CaseCloseDate"))),
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
        return parse_date(text)
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


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
