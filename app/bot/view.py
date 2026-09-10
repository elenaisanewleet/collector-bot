"""Что бот показывает в чате.

Отчёт живёт на веб-странице, а в чате остаётся карточка: вердикт, две-три
цифры и кнопка. Так и задумано — в сообщении Telegram нет ни таблиц, ни
навигации, и попытка уместить туда весь отчёт даёт ровно ту простыню, которую
никто не читает.

Эмодзи здесь только на кнопках, где они работают как иконки и помогают
выцепить нужную глазами. В тексте их нет: маркеры списка из смайликов — это
не оформление, а шум.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.enums import (
    MISSING_INPUT_TITLES,
    PROVIDER_TITLES,
    MissingInput,
    ProviderName,
    SearchType,
)
from app.domain.identity import SearchSubject
from app.domain.models import DebtorReport
from app.domain.verdict import FeeBasis, Verdict, VerdictDecision
from app.services.reporting import DEMO_BANNER, SourceStateCode, source_state
from app.utils.dates import format_date, format_datetime
from app.utils.formatting import format_phone, truncate
from app.utils.money import format_amount

# Полоса прогресса рисуется символами: Telegram правит сообщение на месте, и
# меняющаяся полоса читается как движение, а не как новое сообщение.
BAR_WIDTH = 18
BAR_FULL = "▰"
BAR_EMPTY = "▱"

VERDICT_LEAD = {
    Verdict.FILE: "Можно подавать иск",
    Verdict.ORDER: "Можно подавать заявление о судебном приказе",
    Verdict.REVIEW: "Нужна ручная проверка",
    Verdict.DROP: "Подавать не стоит",
}

STAGES = (
    "Ищу во внутренней базе",
    "Опрашиваю источники",
    "Сопоставляю записи",
    "Считаю перспективу",
)


def progress_bar(fraction: float, *, width: int = BAR_WIDTH) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def searching(
    stage_index: int,
    *,
    subject_name: str,
    accepted: str | None = None,
    note: str | None = None,
    waited_seconds: int | None = None,
) -> str:
    """Сообщение о ходе проверки. Правится на месте, а не шлётся заново.

    ``accepted`` — что бот понял из строки. Показывается сразу, до результата:
    разбор свободной строки обязан быть виден там же, где начинается ожидание, —
    иначе ошибка распознавания всплывёт только в отчёте, за деньги.

    ``note`` — чего не хватило и что это закрывает. Стоит здесь, а не только под
    отчётом, потому что ничего не стоит и учит: оператор читает её, пока ждёт.

    ``waited_seconds`` — сколько идёт проверка. Появляется, когда стадии
    кончились, а ответа ещё нет: четыре стадии проходят за пять секунд, а один
    источник вправе думать до полутора минут. Без этой строки сообщение
    застывало на «Считаю перспективу…» и читалось как зависший бот — именно так
    его и прочитали.
    """
    stage_index = max(0, min(len(STAGES) - 1, stage_index))
    fraction = (stage_index + 1) / (len(STAGES) + 1)
    lines = [f"Проверяю: {subject_name}"]
    if accepted:
        lines.append(accepted)
    stage = STAGES[stage_index] + "…"
    if waited_seconds is not None:
        stage = f"{stage} {waited_seconds} с"
    lines.extend(("", progress_bar(fraction), stage))
    if waited_seconds is not None:
        lines.append(WAITING_NOTE)
    if note:
        lines.append(note)
    return "\n".join(lines)


#: Сколько ещё ждать — единственное, что человеку в ожидании нужно.
#:
#: Здесь стояло «Источники отвечают по-разному — жду самый медленный». Строка
#: описывала наше устройство: что источники опрашиваются разом и что общее время
#: задаёт самый медленный из них. Владельцу это не говорит ничего — ни что
#: происходит, ни сколько осталось, — и читается как оправдание.
#:
#: Число не выдумано: по журналу поставщика медиана его ответа 79 с, самый
#: медленный метод (ФССП) — 108 с, а идут они одновременно, так что типичная
#: проверка это около трёх минут. «Обычно» здесь несёт всю честность: рядом
#: тикает реальное время, и если проверка выбьется из нормы, оператор увидит это
#: сам, а не будет обманут обещанием.
WAITING_NOTE = "Обычно это две-три минуты."


def accepted_line(subject: SearchSubject) -> str | None:
    """«Принял: ФИО · дата рождения 15.03.1980».

    Эхо разбора. Оператор пишет одной строкой в любом порядке, бот угадывает по
    форме — и обязан показать, что именно он угадал: «77091234560» (ИНН с
    потерянной цифрой) неотличим от мобильного, и единственное, что спасает от
    молчаливой подмены, — эта строка.

    ВСЁ ПЕЧАТАЕТСЯ ЦЕЛИКОМ, и это здесь главное. С автопрогоном по номеру
    телефона карточка не рисуется вовсе — бот сразу уходит в реестры, — и эта
    строка осталась единственным местом, где владелец видит, ЧТО именно нашлось
    по номеру: паспорт, дату его выдачи, СНИЛС и адрес. Ради них обращение и
    оплачено, и заявление в суд подают с ними.

    Телефон и VIN тоже без маски. Маска здесь была рефлексом, а не защитой: бот
    закрыт списком допуска, номер прислал сам оператор, а маскированный VIN
    нельзя ни сверить с ПТС, ни вбить в поиск. Скрывать от человека то, что он
    сам только что ввёл, — не приватность, а помеха.

    Адрес стоит рядом с документами не для красоты: он единственный открывает
    ЕГРН, и его отсутствие — причина, по которой раздел про недвижимость пишет
    «недостаточно данных». Видно это должно быть сразу, а не после отчёта.
    """
    if subject.search_type != SearchType.PERSON.value:
        return None
    parts: list[str] = []
    if subject.name:
        parts.append("ФИО")
    if subject.birth_date:
        parts.append(f"дата рождения {format_date(subject.birth_date)}")
    if subject.inn:
        parts.append(f"ИНН {subject.inn}")
    if subject.passport:
        issued = (
            f", выдан {format_date(subject.passport_issued)}" if subject.passport_issued else ""
        )
        parts.append(f"паспорт {subject.passport}{issued}")
    if subject.snils:
        parts.append(f"СНИЛС {subject.snils}")
    if subject.address:
        parts.append(f"адрес {truncate(subject.address, 90)}")
    if subject.phone:
        parts.append(f"телефон {format_phone(subject.phone)}")
    if subject.vehicle and subject.vehicle.plate:
        parts.append(f"госномер {subject.vehicle.plate}")
    if subject.vehicle and subject.vehicle.vin:
        parts.append(f"VIN {subject.vehicle.vin}")
    if not parts:
        return None
    return "Принял: " + " · ".join(parts)


def identifiers_line(subject: SearchSubject) -> str | None:
    """Чем этот человек опознан — одной строкой под его именем в отчёте.

    ЗАЧЕМ ОНА В ОТЧЁТЕ, ЕСЛИ ЕСТЬ «ПРИНЯЛ». Затем, что «Принял» к этому моменту
    уже стёрт: отчёт ПРАВИТ то же самое сообщение (``_edit_or_send``), и всё,
    что стояло в нём до проверки, исчезает. Паспорт, СНИЛС и дата выдачи жили
    ровно до конца ожидания, а потом пропадали из чата совсем.

    И главное — ИНН. Его добывает мост «паспорт → ИНН» ВНУТРИ поиска, за
    деньги, и до сих пор он не попадал никуда: в «Принял» его ещё нет (строка
    считается до моста), в отчёте не было идентификаторов вовсе. Владелица
    сказала прямо: «мы телефон ввели, чтобы в ответе были все поля».

    Имени здесь нет: оно стоит строкой выше, отдельно.
    """
    if subject.search_type != SearchType.PERSON.value:
        return None
    parts: list[str] = []
    if subject.birth_date:
        parts.append(f"дата рождения {format_date(subject.birth_date)}")
    if subject.inn:
        parts.append(f"ИНН {subject.inn}")
    if subject.passport:
        issued = (
            f", выдан {format_date(subject.passport_issued)}" if subject.passport_issued else ""
        )
        parts.append(f"паспорт {subject.passport}{issued}")
    if subject.snils:
        parts.append(f"СНИЛС {subject.snils}")
    return " · ".join(parts) if parts else None


def batch_progress(processed: int, total: int, failed: int, *, spent: str | None = None) -> str:
    """Прогресс массовой проверки: сколько сделано и во что это уже обошлось.

    ``spent`` стоит здесь, а не в отдельном сообщении, по той же причине, по
    которой полоса правится на месте: прогон длится полчаса, и всё это время
    оператор смотрит на одно сообщение. Если цена не в нём, её не видно вовсе.
    """
    fraction = processed / total if total else 0.0
    lines = [
        "Проверяю базу",
        "",
        f"{progress_bar(fraction)}  {round(fraction * 100)}%",
        f"{processed} из {total}",
    ]
    if spent:
        lines.append(spent)
    if failed:
        lines.append(f"Не удалось проверить: {failed}")
    return "\n".join(lines)


def report_card(
    report: DebtorReport,
    decision: VerdictDecision,
    *,
    notes: Sequence[str] = (),
    demo_mode: bool = False,
) -> str:
    """Короткая карточка в чат. Подробности — на странице по кнопке.

    ``notes`` — то, что бот отбросил при разборе строки. Стоит рядом с эхом
    «Принял:» и по той же причине: отчёт обязан показывать не только то, что он
    учёл, но и то, чего он не учёл, — иначе «проверено» и «нечем было
    проверить» сливаются в одну бодрую карточку.

    Порядок первых строк — баннер, имя, эхо, оговорки — не произвольный:
    баннер относится ко всему тексту и обязан стоять до него, а эхо и оговорки
    относятся к субъекту и идут за его именем.
    """
    lines: list[str] = []
    if demo_mode:
        # Тот же баннер, что в текстовом отчёте: карточка с выдуманными данными
        # не должна быть неотличима от настоящей проверки.
        lines.extend((DEMO_BANNER, ""))
    lines.append(report.subject.display_name)
    # Идентификаторы — под именем. Раньше их здесь не было, с доводом «эхо уже
    # сказано сообщением о ходе проверки». Довод оказался неверным: отчёт ПРАВИТ
    # это самое сообщение, и всё, что в нём стояло, стирается. Паспорт и СНИЛС
    # исчезали из чата, а добытый мостом ИНН не появлялся вовсе — он приходит
    # позже, чем печаталось эхо.
    identifiers = identifiers_line(report.subject)
    if identifiers:
        lines.append(identifiers)
    lines.extend(notes)
    lines.extend(("", VERDICT_LEAD[decision.verdict], decision.headline, ""))

    if decision.debt_amount is not None:
        lines.append(f"Наш долг: {format_amount(decision.debt_amount)}")
    if decision.state_fee is not None:
        basis = "не платится" if decision.fee_basis is FeeBasis.NONE else "к уплате"
        lines.append(f"Пошлина: {format_amount(decision.state_fee)} — {basis}")

    score = report.recovery_score
    if score is not None:
        # По-русски. «Recovery Score» — единственная латиница на экране,
        # который читает заказчик, и она стояла ровно там, где решают, платить
        # ли пошлину. Формулировка из ТЗ дословно: «понял перспективу
        # взыскания».
        lines.append(
            f"Перспектива взыскания: {score.score} из 100, "
            f"данные полны на {round(score.confidence * 100)}%"
        )

    facts = _facts(report)
    if facts:
        lines.append("")
        lines.extend(facts)

    # Состояние источника определяет общая таблица (``reporting.source_state``);
    # карточка решает только, как сгруппировать и назвать. Плоского списка «Не
    # проверено: ФССП, Авто» здесь быть не должно: три беды с тремя разными
    # действиями оператора он схлопывает в одну строку.
    gaps = _gaps(report)
    if gaps:
        lines.append("")
        lines.extend(gaps)

    if report.from_cache:
        lines.append("")
        lines.append(f"Данные проверки от {format_datetime(report.cached_at)}")

    # «Подробный отчёт — по кнопке ниже» отсюда убрано: кнопка «Открыть отчёт»
    # стоит следующей строкой и подписана числом записей. Строка объясняла
    # кнопку, которая объясняет себя сама, и была последним, что человек читал
    # в карточке.
    return "\n".join(lines)


#: Что показывать строкой в карточке и как это назвать. Порядок — по тому, что
#: решает судьбу взыскания: сперва производства и банкротство (они и есть ответ
#: на «есть ли смысл»), потом остальное. Источники, которых нет в этом списке,
#: в карточку не идут: их место в полном отчёте по кнопке.
_FACT_TITLES: tuple[tuple[ProviderName, str], ...] = (
    (ProviderName.FSSP, "Исполнительные производства"),
    (ProviderName.FEDRESURS, "Банкротство"),
    (ProviderName.INHERITANCE, "Наследственные дела"),
    # Имущество и транспорт названы в ТЗ прямым текстом: «увидел инфу о
    # имуществе». Источников под них сейчас нет (ЕГРН не подключён), поэтому в
    # карточке они и не появятся: печатается только то, на что ответили. Строки
    # стоят здесь заранее, чтобы при подключении реестра ничего не пришлось
    # вспоминать — иначе источник отвечает, а карточка о нём молчит.
    (ProviderName.PROPERTY, "Имущество"),
    (ProviderName.VEHICLE, "Транспорт"),
    (ProviderName.PLEDGE, "Залоги"),
    (ProviderName.FNS, "Бизнес"),
    (ProviderName.COURT, "Суды"),
)


def _facts(report: DebtorReport) -> list[str]:
    """Что нашли — по строке на источник, без объяснений.

    Это главное, зачем карточку читают, и в референсе владелицы оно стоит
    именно так: список «ключ: значение», а подробности — по кнопке. Раньше
    карточка показывала вердикт, деньги и балл, но не сами факты: чтобы узнать,
    есть ли производства, приходилось открывать страницу.

    Печатается только то, на что ответили: найденное числом, ненайденное словом
    «нет». Неспрошенное сюда не попадает — не потому, что о нём молчат, а
    потому, что о нём говорят иначе: блоком ниже, причиной и сразу за всех
    («нужна дата рождения»). Пять строк «не спрашивали» подряд читаются как
    пять бед, хотя беда одна и чинится одним действием.
    """
    by_provider = {result.provider: result for result in report.provider_results}
    lines: list[str] = []
    for provider, title in _FACT_TITLES:
        result = by_provider.get(provider)
        if result is None:
            continue
        state = source_state(result)
        if state.code is SourceStateCode.FOUND:
            lines.append(f"{title}: {len(result.records)}")
        elif state.code is SourceStateCode.EMPTY:
            lines.append(f"{title}: нет")
        # Неспрошенное строкой не печатается вовсе, и это правило владелицы:
        # карточка называет, чего не хватает, но не перечисляет источники.
        # Пропущенное не теряется — про него говорит блок ниже, причинами и
        # сгруппированно: «нужна дата рождения» одной строкой на всех, а не
        # пять строк «не спрашивали» подряд.
    return lines


def _gaps(report: DebtorReport) -> list[str]:
    """Почему в отчёте пусто — по причинам, а не одним списком.

    «Не проверено» было одной строкой на три разные беды, и оператор не мог
    отличить своё упущение от нашего. Теперь их три, и чинятся они по-разному:

    ``Нечем спросить``  источнику не хватило данных — это чинит оператор, и
                        строка называет, чем именно.
    ``Не подключено``   источника у нас нет — оператор не сделает ничего.
    ``Не ответили``     источник упал — поможет «Обновить».

    Группировка по причине, а не по источнику: «ФССП, Залоги — нужна дата
    рождения» это одно действие, а пять строк подряд про одно и то же читаются
    как пять проблем. Причина сравнивается машинно
    (:attr:`ProviderResult.missing_input`), потому что группировать по подстроке
    сообщения — значит развалить карточку от первой правки формулировки.

    Само состояние источника определяется не здесь: его называет общая таблица
    :func:`app.services.reporting.source_state`, та же, что кормит список
    источников на странице и в текстовом отчёте. Второй разбор статусов в чате
    уже был, и он ровно так и разъезжается: порядок «сначала insufficient, потом
    unavailable» правится в одном месте и забывается в другом. Карточка решает
    только, как сгруппировать и какими словами позвать оператора к действию.
    """
    unqueried: dict[tuple[str, ...], list[str]] = {}
    unspecified: dict[str, list[str]] = {}
    not_configured: list[str] = []
    unavailable: list[str] = []

    for result in report.provider_results:
        state = source_state(result)
        if state.answered:
            continue
        title = PROVIDER_TITLES.get(result.provider, result.provider.value)
        match state.code:
            case SourceStateCode.NOT_CONFIGURED:
                not_configured.append(title)
            case SourceStateCode.INSUFFICIENT:
                if result.missing_input:
                    unqueried.setdefault(tuple(result.missing_input), []).append(title)
                else:
                    # Записи из кэша поля не несут — колонки под него нет. Откат
                    # на текст провайдера: группировка теряется, честность нет.
                    unspecified.setdefault(
                        result.error_message or "нечем было спросить", []
                    ).append(title)
            case _:
                unavailable.append(title)

    lines: list[str] = []

    # Чего не хватило — одной строкой и без перечисления источников. Раньше здесь
    # выходило до пяти строк вида «Нечем спросить: ФССП, Залоги — нужна дата
    # рождения» и «Не подключено: Объект по адресу (ЕГРН), Наследственные дела,
    # Авто». Это внутреннее устройство, а заказчику нужны телефон и сводка;
    # разбор по источникам никуда не делся и стоит в отчёте, где его читают.
    #
    # Позвать оператора к действию берут на себя кнопки под карточкой
    # (:func:`app.bot.report_actions._offers`): они и короче текста, и точнее —
    # нажатие сразу открывает нужное поле. Поэтому здесь остаётся назвать, чего
    # не хватает, а не объяснять это абзацем.
    asks = sorted({missing_reason(missing) for missing in unqueried})
    if asks:
        lines.append(f"Чтобы проверить полнее — {'; '.join(asks)}.")
    # Записи из кэша поля не несут, и вместо короткого «нужна дата рождения»
    # остаётся целое предложение от источника. Ставим его отдельной строкой:
    # после тире получалось «Чтобы проверить полнее — Для поиска в ФССП нужна
    # дата рождения» — с заглавной буквы посреди фразы.
    lines.extend(sorted(unspecified))

    # А это обязано остаться при любом сокращении: источник, который не ответил
    # или не подключён, не должен выглядеть источником, который ответил «чисто».
    # Не перечисляем какой — важно, что проверка неполная, и это меняет цену
    # решения о пошлине.
    if unavailable:
        lines.append("Часть источников не ответила — проверка неполная.")
    elif not_configured:
        lines.append("Проверено не всё — что именно, видно в отчёте.")
    return lines


def missing_reason(missing: tuple[str, ...]) -> str:
    """Почему источник не спросили — теми же словами, что и везде.

    Публичная, потому что этих слов теперь два потребителя: карточка отчёта и
    накопительная карточка запроса (:mod:`app.bot.card_view`). Четвёртой
    формулировки «нужна дата рождения» в проекте появиться не должно — от того,
    совпадают ли слова до отчёта и в отчёте, зависит, поверит ли оператор, что
    это про одно и то же.

    Порядок полей — как их назвал провайдер, а не отсортированный.

    «нужно ФИО и нужна дата рождения» читается в том порядке, в каком их
    спрашивают. Неизвестное значение (запись из будущей версии) молча
    выбрасывать нельзя, поэтому оно печатается как есть.
    """
    titles = [
        MISSING_INPUT_TITLES[MissingInput(item)] if item in _KNOWN_MISSING else item
        for item in missing
    ]
    return " и ".join(titles) if titles else "нечем было спросить"


_KNOWN_MISSING = {item.value for item in MissingInput}
