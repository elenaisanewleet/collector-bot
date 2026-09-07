"""Страница очереди взыскания.

Главный экран продукта. Одиночный отчёт — витрина; деньги считаются здесь, на
выгрузке в восемьсот строк, где вопрос ровно один: на кого тратить госпошлину.

Четыре решения, из которых собрана страница.

**Сверху стоит ответ про деньги, а не таблица.** Первым экраном идёт число
должников, по которым суд окупается, долг и пошлина по ним, и — крупно —
пошлина, которую прогон уберёг от списания в никуда. Эта цифра считалась и
раньше (``QueueSnapshot.saved_fees``), но пряталась в примечании под таблицей;
она и есть то, за что продукт покупают.

**Полоса пошлины.** Один горизонтальный брусок, ширина которого — рубли:
сколько пошлины мы платим, сколько оставлено на решение человека и сколько не
заплатим вовсе. Шкала одна на все три сегмента, поэтому сравнивать их можно
глазом. Строки, которые проверить не удалось, в брусок не попадают — их цена
неизвестна, и подмешивать их шириной было бы враньём; вместо этого брусок
получает рваный правый край и подпись, сколько строк осталось непосчитанными.

**Неполнота — это форма, а не сноска.** У каждой строки очереди слева кромка
цвета вердикта: сплошная, если ответили все источники, и рваная, если часть
молчала. Поэтому сортировка по баллу физически не может выглядеть надёжнее
данных под ней: строки с дырами видно в том же движении глаза, что и сам балл,
а рядом с баллом стоит шкала покрытия.

**Провалившаяся проверка — собственное состояние**, а не жёлтый вердикт
«проверить руками». Строка, которую не удалось проверить, не попадает ни в
счётчик проверенных, ни в денежные итоги: это тот же инвариант проекта, только
на уровне прогона.

Фильтры, поиск, сортировка и группировка работают без сервера: строки уже на
странице, а оператор кликает часто и ждать запроса на каждый клик незачем.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db.models import BatchItem
from app.domain.verdict import VERDICT_TITLES, Verdict
from app.services.batch import QueueSnapshot, RunStatus
from app.utils.dates import format_datetime, utcnow
from app.utils.formatting import group_digits, pluralize_ru
from app.utils.masking import mask_name
from app.utils.money import format_amount
from app.web.render import (
    ExportLinks,
    cell,
    document,
    e,
    navigation,
    print_footer,
    raw_cell,
    section,
    table,
)

ORDER = (Verdict.FILE, Verdict.ORDER, Verdict.REVIEW, Verdict.DROP)
TONE = {
    Verdict.FILE: "good",
    Verdict.ORDER: "accent",
    Verdict.REVIEW: "warn",
    Verdict.DROP: "crit",
}
# Пятое состояние строки: проверка не выполнена. Отдельный тон, отдельный чип и
# отдельный фильтр — иначе сбой читается как вердикт «проверить руками».
FAILED = "failed"
FAILED_TITLE = "Не проверено"
#: Прочерк вместо суммы. Отличается от «0 ₽» ровно тем же, чем «не спрашивали»
#: от «не нашли», — и цена ошибки та же, только в рублях.
UNKNOWN_AMOUNT = "—"

# Ниже этого покрытия строка считается проверенной частично. Не «почти
# полностью»: уверенность здесь — доля источников, которые вообще ответили, и
# 89% значит, что чего-то мы не видели.
FULL_CONFIDENCE = 90
# После этого молчания идущий прогон считается зависшим. Полчаса на восемьсот
# должников — норма, десять минут без единой новой строки — уже нет.
STALE_AFTER = timedelta(minutes=10)

PRIVACY_NOTE = (
    "ФИО в таблице сокращены: чтобы решить, кого нести в суд, полное имя здесь "
    "не нужно — оно есть в отчёте по конкретному должнику."
)
COVERAGE_NOTE = (
    "«Частично» значит, что часть источников молчала: вердикт и балл по таким "
    "строкам посчитаны по неполным данным, и в таблице у них рваная кромка. "
    "«Не проверено» — проверка не выполнена вовсе, это не «ничего не найдено»: "
    "такие строки не попадают ни в счётчик проверенных, ни в суммы."
)
SCORE_SORT_NOTE = (
    "Сортировка по баллу не делает данные полнее: у строк с рваной кромкой "
    "часть источников молчала, и балл по ним посчитан не по всему."
)
# Что вердикт означает в деньгах — по-человечески, без юридического словаря.
MEANING = {
    Verdict.FILE: "пошлину платим, долг выше порога судебного приказа",
    Verdict.ORDER: "пошлина вдвое ниже иска, дело идёт без заседания",
    Verdict.REVIEW: "решение за человеком — данных не хватило",
    Verdict.DROP: "пошлина не платится, это и есть сэкономленное",
}
FAILED_MEANING = "проверка не выполнена, цену считать не из чего"

_GROUPS = ("file", "order", "review", "drop", FAILED)
_HEADERS = ("Вердикт", "Должник и договор", "Долг", "Пошлина", "Балл", "Обоснование")


# ---------------------------------------------------------------- полнота


@dataclass(frozen=True, slots=True)
class Coverage:
    """Сколько строк прогона можно принимать всерьёз.

    Четыре состояния, которые обязаны сходиться в общее число: проверено
    полностью, проверено частично, проверка провалилась, до строки ещё не
    дошли. Три из них раньше сливались в одно бодрое «обработано N из M».
    """

    full: int
    partial: int
    failed: int
    pending: int
    total: int
    # Прогон кончился, не дойдя до конца. Меняет не числа, а слова: строки, до
    # которых не дошли, «ещё в очереди» только пока очередь есть.
    torn: bool = False

    @property
    def checked(self) -> int:
        """Счётчик проверенных. Непроверенное сюда не попадает — в этом смысл."""
        return self.full + self.partial

    @property
    def pending_title(self) -> str:
        return "Не проверялись" if self.torn else "Ещё в очереди"

    @property
    def pending_note(self) -> str:
        if self.torn:
            return "прогон до них не дошёл и уже не дойдёт"
        return "до этих строк прогон не дошёл"

    @property
    def shown(self) -> int:
        return self.full + self.partial + self.failed

    @property
    def has_gaps(self) -> bool:
        return bool(self.partial or self.failed)


def coverage_of(snapshot: QueueSnapshot) -> Coverage:
    """Разложить строки очереди по состояниям проверки."""
    failed = sum(1 for item in snapshot.items if item.error)
    full = sum(
        1 for item in snapshot.items if not item.error and (item.confidence or 0) >= FULL_CONFIDENCE
    )
    partial = len(snapshot.items) - failed - full
    return Coverage(
        full=full,
        partial=partial,
        failed=failed,
        pending=max(snapshot.total - len(snapshot.items), 0),
        total=max(snapshot.total, len(snapshot.items)),
        torn=snapshot.is_torn,
    )


# ---------------------------------------------------------------- страница


def render_queue_page(
    snapshot: QueueSnapshot,
    *,
    app_name: str,
    demo_mode: bool = False,
    exports: ExportLinks | None = None,
    print_mode: bool = False,
    now: datetime | None = None,
) -> str:
    from app.web.render import demo_banner

    cover = coverage_of(snapshot)
    parts: list[str] = []
    if demo_mode:
        parts.append(demo_banner())
    parts.append(_hero(snapshot, cover, exports=None if print_mode else exports))
    if snapshot.is_running:
        parts.append(_running(snapshot, now=now or utcnow(), print_mode=print_mode))
    elif snapshot.is_torn:
        parts.append(_torn(snapshot))
    parts.append(_ledger(snapshot, cover))
    parts.append(_coverage_section(cover))
    parts.append(_queue(snapshot, cover, print_mode=print_mode))
    parts.append(f"<footer>{e(_footer_text(snapshot))}</footer>")

    nav = navigation(
        app_name,
        (("money", "Деньги"), ("fullness", "Полнота"), ("queue", "Очередь")),
        (
            f"Прогон №{snapshot.run_id}",
            f"от {format_datetime(snapshot.started_at)}",
        ),
    )
    return document(
        title=f"Очередь взыскания №{snapshot.run_id} — {app_name}",
        nav=nav,
        body="".join(parts),
        foot=print_footer(app_name, snapshot.finished_at or snapshot.started_at),
        auto_print=print_mode,
    )


# ---------------------------------------------------------------- ответ про деньги


def _hero(snapshot: QueueSnapshot, cover: Coverage, *, exports: ExportLinks | None) -> str:
    """Первый экран: на скольких из восьмисот суд окупается и во что это встанет."""
    head = (
        '<header class="hero queue">'
        f'<p class="eyebrow">Прогон №{snapshot.run_id} · '
        f"{e(format_datetime(snapshot.started_at))}</p>"
        "<h1>Очередь взыскания</h1>"
    )
    if cover.total == 0:
        # Нули под подписью «суд окупается» читаются как результат проверки:
        # «проверили — окупается ноль». Проверять было нечего, и так и сказано.
        return (
            f"{head}"
            '<p class="feenone">Прогон был пустым: в базе не было ни одного должника, '
            "и считать нечего. Загрузите выгрузку командой /import, "
            "потом запустите проверку через /batch.</p>"
            f"{_actions(exports)}</header>"
        )

    noun = pluralize_ru(cover.total, "должника", "должников", "должников")
    figures = "".join(
        (
            _figure("Долг по ним", _money(snapshot.actionable_debt, snapshot.actionable)),
            _figure(
                "Пошлина за них",
                _money(snapshot.actionable_fee, snapshot.actionable),
                "столько уйдёт из кассы",
            ),
            _figure("Долг всего в прогоне", _money(snapshot.total_debt, cover.checked)),
        )
    )
    return (
        f"{head}"
        '<div class="answer"><span class="lbl">Суд окупается</span>'
        f"<b>{snapshot.actionable}</b>"
        f'<span class="of">из {cover.total} {noun}</span></div>'
        f'<div class="nums">{figures}</div>'
        f"{_saved(snapshot)}"
        f"{_fee_bar(snapshot, cover)}"
        f"{_coverage_line(cover)}"
        f"{_actions(exports)}"
        "</header>"
    )


def _figure(label: str, value: str, note: str = "") -> str:
    small = f"<small>{e(note)}</small>" if note else ""
    return f'<div><span class="lbl">{e(label)}</span><b>{e(value)}</b>{small}</div>'


def _money(amount: Decimal, rows: int) -> str:
    """Рубли по набору строк — или прочерк, если складывать было нечего.

    Ноль рублей по непустому набору не бывает: должник с нулевым долгом не
    доходит до вердикта с деньгами, его забирает правило «в выгрузке нет суммы
    долга». Значит ноль здесь означает ровно одно — сумм не было ни у кого, и
    печатать его цифрой значит утверждать, что взыскивать нечего.

    На выгрузке заказчика это не теория: там нет колонки с суммой вовсе, и
    страница печатала «Долг всего в прогоне 0 ₽» по 2052 живым должникам —
    первым числом в шапке, на листе, который несут в суд. Правило уже было
    записано строкой ниже, у непроверенных строк: «ноль означал бы, что строки
    ничего не стоят, а правда — что сколько они стоят, мы не знаем». Здесь оно
    просто не применялось.
    """
    return format_amount(amount) if amount or not rows else UNKNOWN_AMOUNT


def _saved(snapshot: QueueSnapshot) -> str:
    """Сэкономленная пошлина — единственная хорошая новость на странице.

    Печатается всегда, в том числе нулём: «безнадёжных пока не нашли» — это
    тоже ответ, а отсутствие строки читается как отсутствие экономии.
    """
    dropped = snapshot.count(Verdict.DROP)
    if snapshot.saved_fees > 0:
        verb = pluralize_ru(dropped, "отсеян", "отсеяно", "отсеяно")
        note = f"{dropped} безнадёжных {verb} — эти деньги останутся в кассе"
    else:
        note = "безнадёжных в этом прогоне пока не нашлось"
    tone = "" if snapshot.saved_fees > 0 else " flat"
    return (
        f'<div class="saved{tone}"><span class="lbl">Сэкономлено пошлины</span>'
        f"<b>{e(format_amount(snapshot.saved_fees))}</b>"
        f"<small>{e(note)}</small></div>"
    )


def _fee_bar(snapshot: QueueSnapshot, cover: Coverage) -> str:
    """Полоса пошлины: ширина сегмента — рубли, а не доля строк.

    Три сегмента на одной шкале: платим, решает человек, не платим. Строки,
    которые не удалось проверить, ширины не получают — их пошлина не посчитана,
    и любая ширина здесь была бы выдуманной. Вместо этого у полосы рваный
    правый край: она заведомо не полна.
    """
    pay = snapshot.actionable_fee
    review = snapshot.fee(Verdict.REVIEW)
    saved = snapshot.saved_fees
    whole = pay + review + saved
    if whole <= 0:
        if not cover.shown:
            return ""
        return (
            '<p class="feenone">Пошлину пока не из чего считать: '
            "ни по одной строке сумма долга не подтверждена.</p>"
        )

    segments = [
        ("pay", pay, "Платим", snapshot.actionable),
        ("hold", review, "Решает человек", max(snapshot.count(Verdict.REVIEW) - cover.failed, 0)),
        ("save", saved, "Не платим", snapshot.count(Verdict.DROP)),
    ]
    bars = "".join(
        f'<span class="seg {key}" style="width:{_share(value, whole):.2f}%"></span>'
        for key, value, _title, _rows in segments
        if value > 0
    )
    keys = "".join(
        f'<span class="key {key}"><i></i>{e(title)} — '
        f"<b>{e(format_amount(value))}</b> "
        f'<span class="rows">{rows} {pluralize_ru(rows, "строка", "строки", "строк")}</span>'
        "</span>"
        for key, value, title, rows in segments
        if value > 0
    )
    open_end = " open" if cover.failed or cover.pending else ""
    tail = ""
    if cover.failed or cover.pending:
        missing = cover.failed + cover.pending
        why = ", ".join(
            part
            for part in (
                f"{cover.failed} не проверено" if cover.failed else "",
                f"{cover.pending} ещё в очереди" if cover.pending else "",
            )
            if part
        )
        tail = (
            f'<p class="tail">Полоса неполная: {missing} '
            f"{pluralize_ru(missing, 'строка', 'строки', 'строк')} без пошлины — "
            f"{why}.</p>"
        )
    label = "; ".join(
        f"{title} {format_amount(value)}" for _key, value, title, _rows in segments if value > 0
    )
    return (
        '<figure class="feebar">'
        f'<div class="bar{open_end}" role="img" aria-label="Пошлина прогона: {e(label)}">'
        f"{bars}</div>"
        f'<figcaption class="keys">{keys}</figcaption>{tail}</figure>'
    )


def _coverage_line(cover: Coverage) -> str:
    """Полнота — рядом с деньгами, тем же весом, что и цифры."""
    counter = (
        f"Проверено полностью <b>{cover.full}</b>, "
        f"частично <b>{cover.partial}</b>, "
        f"не проверено <b>{cover.failed}</b>"
    )
    if cover.pending:
        counter += f", {cover.pending_title.lower()} <b>{cover.pending}</b>"
    if not cover.has_gaps and not cover.pending:
        return f'<p class="coverage">{counter}. <a href="#fullness">Что это значит</a></p>'
    return (
        f'<p class="coverage gap">{counter}. '
        f'<a href="#fullness">Что это значит</a>'
        '<span class="miss">Суммы и балл посчитаны только по проверенным строкам.</span></p>'
    )


def _actions(exports: ExportLinks | None) -> str:
    if exports is None:
        return ""
    return (
        '<div class="actions">'
        f'<a href="{e(exports.print_url)}">Распечатать или сохранить в PDF</a>'
        f'<a href="{e(exports.text_url)}" download>Скачать таблицей (CSV, без ФИО)</a>'
        "</div>"
    )


# ---------------------------------------------------------------- прогон идёт


def _running(snapshot: QueueSnapshot, *, now: datetime, print_mode: bool) -> str:
    """Плашка незавершённого прогона.

    Незаконченный срез, показанный теми же итоговыми плитками, читается как
    результат: ноль в «безнадёжно» выглядит как «безнадёжных нет». Здесь же
    называется цена: прогон тратит платные запросы, и оператор должен видеть,
    сколько уже потрачено, а не только сколько сделано.
    """
    done = _share(Decimal(snapshot.processed), Decimal(snapshot.total)) if snapshot.total else 0.0
    left = max(snapshot.total - snapshot.processed, 0)
    spent = snapshot.processed * snapshot.providers_per_debtor
    cost = (
        f"Потрачено запросов к платным источникам: до {group_digits(spent)} "
        f"({snapshot.providers_per_debtor} на должника; взятые из кэша не оплачиваются). "
        if snapshot.providers_per_debtor
        else "Внешние источники не подключены — прогон идёт по внутренней базе. "
    )
    body = (
        f"<b>Прогон идёт: {snapshot.processed} из {snapshot.total}</b>"
        f'<div class="meter live"><i style="width:{done:.1f}%"></i></div>'
        f"<p>Осталось {left} "
        f"{pluralize_ru(left, 'должник', 'должника', 'должников')}. {cost}"
        "Счётчики выше — промежуточные, они посчитаны только по проверенным.</p>"
    )
    stalled = _stalled_note(snapshot, now=now)
    script = "" if print_mode else _RELOAD_SCRIPT
    if stalled:
        return f'<div class="running stalled">{body}{stalled}</div>'
    hint = (
        '<p class="tick">Страница обновится сама, пока вы её не трогаете.</p>'
        if not print_mode
        else ""
    )
    return f'<div class="running">{body}{hint}</div>{script}'


def _stalled_note(snapshot: QueueSnapshot, *, now: datetime) -> str:
    """Прогон, который перестал двигаться, обязан сказать это сам.

    Оборванный прогон снаружи неотличим от идущего: статус остаётся
    «running», а страница бодро перезагружается каждые полминуты. Единственный
    честный признак — время последней записанной строки.
    """
    # Строки без времени записи (срез собран не из базы) в расчёт не берутся:
    # смешать их с датами — уронить главный экран на TypeError.
    written = [item.created_at for item in snapshot.items if item.created_at is not None]
    last = max(written, default=snapshot.started_at)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    if now - last < STALE_AFTER:
        return ""
    minutes = int((now - last).total_seconds() // 60)
    return (
        f'<p class="halt">Новых строк нет уже {minutes} '
        f"{pluralize_ru(minutes, 'минуту', 'минуты', 'минут')} — "
        f"последняя записана в {e(format_datetime(last))}. Похоже, прогон оборвался: "
        "проверьте бота и запустите проверку заново. Всё, что успело посчитаться, "
        "ниже и остаётся верным.</p>"
    )


def _torn(snapshot: QueueSnapshot) -> str:
    """Прогон кончился, не дойдя до конца выгрузки.

    Третье состояние страницы, и оно не сводится к двум прежним. «Идёт» —
    неправда: никто больше ничего не напишет, и ждать нечего. «Завершён» —
    неправда опаснее: под этим словом очередь читается как полный ответ по всей
    базе, а в ней не хватает строк, и решение о госпошлине принимают по ней.

    Печатается и на бумаге: лист с неполной очередью, потерявший эту оговорку,
    становится документом, утверждающим больше, чем было проверено.
    """
    left = snapshot.unchecked
    why = (
        "Источник перестал отвечать (обычно это кончившийся баланс или "
        "отклонённый ключ), и прогон был остановлен, чтобы не платить за "
        "ответы, которых всё равно не будет."
        if snapshot.status == RunStatus.STOPPED
        else "Прогон не пережил сбоя и остановился на середине."
    )
    return (
        '<div class="running stalled">'
        f"<b>Прогон неполный: проверено {snapshot.processed} из {snapshot.total}</b>"
        f'<p class="halt">{e(why)} Оставшиеся {left} '
        f"{pluralize_ru(left, 'должник', 'должника', 'должников')} не проверялись вовсе — "
        "это не «ничего не найдено». Всё, что ниже, посчитано верно, но это "
        "ответ по части выгрузки, а не по всей.</p>"
        "<p>Запустите проверку заново в боте: за уже проверенных второй раз "
        "платить не придётся, они возьмутся из кэша.</p>"
        "</div>"
    )


# Незавершённый прогон обновляет сам себя: иначе оператор смотрит на застывший
# срез и не знает, что он застыл. Перезагрузка отодвигается любым действием —
# страницу не должно выдёргивать из-под руки на середине поиска.
_RELOAD_SCRIPT = """<script>
(function () {
  var timer = null;
  function plan() {
    clearTimeout(timer);
    timer = setTimeout(function () { location.reload(); }, 30000);
  }
  ['click', 'input', 'keydown'].forEach(function (name) {
    document.addEventListener(name, plan, true);
  });
  plan();
})();
</script>"""


# ---------------------------------------------------------------- деньги по вердиктам


def _ledger(snapshot: QueueSnapshot, cover: Coverage) -> str:
    """Смета прогона: строка на вердикт, деньги и что вердикт значит.

    Таблица, а не поле одинаковых плиток: плитки уравнивают в весе счётчик и
    сумму, а вопрос здесь один — куда уходят деньги.
    """
    if not cover.shown:
        return section(
            "money",
            "Куда идут деньги",
            '<p class="empty">Считать пока нечего: ни одна строка прогона не посчитана.</p>',
        )

    rows: list[tuple[str, ...]] = []
    attrs: list[str] = []
    for verdict in ORDER:
        count = snapshot.count(verdict)
        if verdict is Verdict.REVIEW:
            count = max(count - cover.failed, 0)
        fee = snapshot.fee(verdict)
        rows.append(
            (
                raw_cell(_tag(TONE[verdict], "•", VERDICT_TITLES[verdict]), label="Вердикт"),
                _sum_cell(str(count), label="Строк"),
                _sum_cell(_money(snapshot.debt(verdict), count), label="Долг"),
                _sum_cell(_money(fee, count), label="Пошлина"),
                cell(MEANING[verdict], label="Что это значит"),
            )
        )
        # Кромки вердикта здесь нет намеренно: чип с названием стоит в той же
        # строке, и полоса рядом с ним ничего не добавляет. Кромка работает там,
        # где названия нет под рукой, — в очереди на восемьсот строк.
        attrs.append("")

    rows.append(
        (
            raw_cell(_tag("unchecked", "!", FAILED_TITLE), label="Вердикт"),
            _sum_cell(str(cover.failed), label="Строк"),
            # Не ноль: ноль означал бы «эти строки ничего не стоят», а правда —
            # «сколько они стоят, мы не знаем».
            _sum_cell("—", label="Долг"),
            _sum_cell("—", label="Пошлина"),
            cell(FAILED_MEANING, label="Что это значит"),
        )
    )
    attrs.append("")

    rows.append(
        (
            raw_cell("<b>Итого проверено</b>", label="Вердикт"),
            _sum_cell(str(cover.checked), label="Строк"),
            _sum_cell(_money(snapshot.total_debt, cover.checked), label="Долг"),
            _sum_cell(_money(snapshot.total_fee, cover.checked), label="Пошлина"),
            cell(
                # «Из них не будет уплачено 0 ₽» рядом с итогом-прочерком —
                # доля от того, чего мы не знаем. Экономия считается от пошлины,
                # а пошлины тут не посчитано ни по одной строке.
                f"из них не будет уплачено {format_amount(snapshot.saved_fees)} — "
                "это и есть экономия"
                if snapshot.total_fee or not cover.checked
                else "экономию посчитаем, когда у должников появится сумма долга",
                label="Что это значит",
            ),
        )
    )
    attrs.append('class="total"')

    grid = table(
        ("Вердикт", "Строк", "Долг", "Пошлина", "Что это значит"),
        rows,
        row_attrs=attrs,
    )
    return section("money", "Куда идут деньги", grid)


def _sum_cell(value: str, *, label: str) -> str:
    """Итог в смете: моноширинный и по правому краю, но без рамки-«плашки».

    Плашка в этой системе значит «идентификатор, который копируют»: номер
    производства, ИНН, госномер. Счётчик строк и сумма долга — не они.
    """
    return raw_cell(e(value), label=label, classes="n r")


def _tag(tone: str, mark: str, title: str) -> str:
    return f'<span class="tag {e(tone)}"><span class="mark">{e(mark)}</span>{e(title)}</span>'


# ---------------------------------------------------------------- полнота проверки


def _coverage_section(cover: Coverage) -> str:
    states = [
        ("full", "Проверено полностью", cover.full, "ответили все источники"),
        ("partial", "Проверено частично", cover.partial, "часть источников молчала"),
        ("failed", FAILED_TITLE, cover.failed, "проверка не выполнена"),
        ("pending", cover.pending_title, cover.pending, cover.pending_note),
    ]
    bars = "".join(
        f'<div class="crow {key}"><span class="nm">{e(title)}</span>'
        f'<span class="track"><i style="width:{_share(Decimal(count), Decimal(cover.total)):.1f}%">'
        "</i></span>"
        f"<b>{count}</b><small>{e(note)}</small></div>"
        for key, title, count, note in states
    )
    counted = (
        f"В счётчик проверенных попали {cover.checked} "
        f"{pluralize_ru(cover.checked, 'строка', 'строки', 'строк')} из {cover.total}. "
        f"Непроверенные {cover.failed} в него не входят."
    )
    return section(
        "fullness",
        "Полнота проверки",
        f'<div class="cover">{bars}</div>'
        f'<p class="note">{e(COVERAGE_NOTE)}</p>'
        f'<p class="note">{e(counted)}</p>',
    )


# ---------------------------------------------------------------- очередь


def _queue(snapshot: QueueSnapshot, cover: Coverage, *, print_mode: bool = False) -> str:
    if not snapshot.items:
        return section("queue", "Очередь", _empty(snapshot))

    grouped = _grouped(snapshot.items)
    rows: list[tuple[str, ...]] = []
    attrs: list[str] = []
    for key in _GROUPS:
        bucket = grouped.get(key)
        if not bucket:
            continue
        rows.append(_group_head(key, bucket))
        attrs.append(f'data-group="{key}"')
        for item in bucket:
            rows.append(_row(item))
            attrs.append(_row_attrs(item))

    grid = table(_HEADERS, rows, row_attrs=attrs)
    thin = cover.partial + cover.failed
    body = "" if print_mode else _tools(snapshot) + _score_note(thin) + _status(len(snapshot.items))
    tail = "" if print_mode else _more_button()
    script = "" if print_mode else _QUEUE_SCRIPT
    return section(
        "queue",
        f"Очередь — {len(snapshot.items)}",
        body + grid + tail + f'<p class="note">{e(PRIVACY_NOTE)}</p>' + script,
    )


def _empty(snapshot: QueueSnapshot) -> str:
    """Пустая очередь. Пусто по разным причинам — и говорить надо разное."""
    if snapshot.is_running:
        return (
            '<p class="empty unchecked">Строки появятся, как только прогон запишет '
            "первую страницу результатов. Страница обновится сама.</p>"
        )
    if snapshot.total == 0:
        return (
            '<p class="empty">Прогон был пустым: в базе не было ни одного должника. '
            "Загрузите выгрузку командой /import и запустите проверку через /batch.</p>"
        )
    return (
        '<p class="empty unchecked">Ни одна из '
        f"{snapshot.total} строк не дошла до очереди. Прогон завершился, ничего не записав — "
        "запустите проверку заново через /batch.</p>"
    )


def _grouped(items: list[BatchItem]) -> dict[str, list[BatchItem]]:
    """Разложить строки по вердиктам, вынув сбои в собственную группу.

    Сбой в базе лежит под вердиктом «проверить руками» — так его записал
    прогон, у которого нет отчёта. На экране он обязан стоять отдельно.
    """
    buckets: dict[str, list[BatchItem]] = {key: [] for key in _GROUPS}
    for item in items:
        buckets[_row_tone(item)].append(item)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: -(item.debt_kopecks or 0))
    return buckets


def _group_head(key: str, bucket: list[BatchItem]) -> tuple[str, ...]:
    title = FAILED_TITLE if key == FAILED else VERDICT_TITLES[Verdict(key)]
    debt = sum((item.debt_amount or Decimal("0") for item in bucket), Decimal("0"))
    money = f" · {format_amount(debt)}" if debt else ""
    return (
        f'<td colspan="{len(_HEADERS)}"><button type="button" class="ghead" '
        f'aria-expanded="true"><span class="caret" aria-hidden="true"></span>'
        f'{e(title)} · <span class="gcount">{len(bucket)}</span>'
        f'<span class="gmoney">{e(money)}</span></button></td>',
    )


def _tools(snapshot: QueueSnapshot) -> str:
    counts: dict[str, int] = dict.fromkeys(_GROUPS, 0)
    for item in snapshot.items:
        counts[_row_tone(item)] += 1

    chips = [
        '<button type="button" data-filter="all" aria-pressed="true">'
        f"Все · {len(snapshot.items)}</button>"
    ]
    for verdict in ORDER:
        if counts[verdict.value]:
            chips.append(
                f'<button type="button" data-filter="{verdict.value}" aria-pressed="false">'
                f"{e(VERDICT_TITLES[verdict])} · {counts[verdict.value]}</button>"
            )
    if counts[FAILED]:
        chips.append(
            f'<button type="button" data-filter="{FAILED}" aria-pressed="false">'
            f"{FAILED_TITLE} · {counts[FAILED]}</button>"
        )
    return (
        '<div class="tools">'
        '<p class="find"><label class="lbl" for="q-find">Найти по фамилии или договору</label>'
        '<input id="q-find" type="search" placeholder="Фамилия" '
        'autocomplete="off" spellcheck="false"></p>'
        '<p class="sortby"><label class="lbl" for="q-sort">Сначала</label>'
        '<select id="q-sort">'
        '<option value="verdict">по вердикту</option>'
        '<option value="debt">по сумме долга</option>'
        '<option value="score">по баллу</option>'
        "</select></p></div>"
        f'<div class="filters" id="filters">{"".join(chips)}</div>'
    )


def _score_note(thin: int) -> str:
    if not thin:
        return ""
    return f'<p class="note thin" id="q-score-note" hidden>{e(SCORE_SORT_NOTE)}</p>'


def _status(total: int) -> str:
    """Счётчик показанного. Без скриптов на странице видны все строки сразу."""
    noun = pluralize_ru(total, "строка", "строки", "строк")
    return f'<p class="qstatus" id="q-status">Показаны все {total} {noun}</p>'


def _more_button() -> str:
    return '<div class="more"><button type="button" id="q-more" hidden>Показать ещё</button></div>'


def _row(item: BatchItem) -> tuple[str, ...]:
    debtor = item.debtor
    # Полное ФИО по одной ссылке на восемьсот строк — это выгрузка базы; для
    # решения «нести или не нести» достаточно сокращённого.
    name = mask_name(debtor.fio) if debtor and debtor.fio else None
    contract = (debtor.contract_number if debtor else None) or ""
    name = name or contract or "—"

    if item.error:
        tone, key, title, mark = "unchecked", FAILED, FAILED_TITLE, "!"
    else:
        verdict = _verdict_of(item.verdict)
        tone, key = TONE.get(verdict, "mute"), verdict.value
        title, mark = VERDICT_TITLES.get(verdict, item.verdict), "•"
    return (
        raw_cell(_tag(tone, mark, title), label="Вердикт", value=key),
        # Договор живёт под фамилией, а не в собственной колонке: на семи
        # колонках обоснование уезжало за правый край, а именно его читают,
        # чтобы понять вердикт.
        raw_cell(_debtor_cell(name, contract), label="Должник"),
        _money_cell(item.debt_amount, label="Долг"),
        _money_cell(item.state_fee, label="Пошлина"),
        _score_cell(item),
        raw_cell(
            '<span class="why">'
            + e(f"Проверка не выполнена: {item.error}" if item.error else item.headline)
            + "</span>",
            label="Обоснование",
        ),
    )


def _debtor_cell(name: str, contract: str) -> str:
    """Фамилия и номер договора одной ячейкой: номер копируется кнопкой."""
    if not contract or contract == name:
        return f'<span class="who">{e(name)}</span>'
    return (
        f'<span class="who">{e(name)}</span>'
        f'<button type="button" class="copy" data-copy="{e(contract)}">{e(contract)}</button>'
    )


def _money_cell(amount: Decimal | None, *, label: str) -> str:
    """Сумма кнопкой: показывает рубли, копирует число.

    Оператор вставляет цену иска в заявление, где «12 400 ₽» — мусор, а
    «12400» — то, что нужно.
    """
    if amount is None or amount == 0:
        return cell("—", label=label, numeric=True, right=True)
    return raw_cell(
        f'<button type="button" class="copy" data-copy="{e(_plain(amount))}">'
        f"{e(format_amount(amount))}</button>",
        label=label,
        classes="n r",
    )


def _score_cell(item: BatchItem) -> str:
    """Балл вместе со шкалой покрытия — в одной ячейке, а не в разных концах.

    Балл без покрытия рядом сортируется так же уверенно, как измеренная
    величина; шкала под числом не даёт этого забыть.
    """
    if item.error or item.score is None:
        return raw_cell('<span class="score none">—</span>', label="Балл")
    confidence = max(0, min(100, item.confidence or 0))
    if confidence >= FULL_CONFIDENCE:
        # Полное покрытие подписью не сопровождается: словами отмечается
        # исключение, иначе «данные 100%» под каждой из восьмисот строк
        # превращается в фон, и на нём теряется «данные 40%».
        return raw_cell(
            f'<span class="score"><b>{item.score}</b>'
            '<span class="track" title="ответили все источники"><i style="width:100%"></i></span>'
            "</span>",
            label="Балл",
        )
    return raw_cell(
        f'<span class="score thin"><b>{item.score}</b>'
        f'<span class="track" title="данных хватило на {confidence}%">'
        f'<i style="width:{confidence}%"></i></span>'
        f"<small>данные {confidence}%</small></span>",
        label="Балл",
    )


def _row_attrs(item: BatchItem) -> str:
    """Ключи сортировки и поиска — на строке, а не выковыриваются из ячеек."""
    debtor = item.debtor
    name = mask_name(debtor.fio) if debtor and debtor.fio else ""
    contract = (debtor.contract_number if debtor else "") or ""
    score = -1 if item.error or item.score is None else item.score
    return (
        f'data-tone="{e(_row_tone(item))}" '
        f'data-cov="{e(_row_coverage(item))}" '
        f'data-order="{_GROUPS.index(_row_tone(item))}" '
        f'data-debt="{item.debt_kopecks or 0}" '
        f'data-score="{score}" '
        f'data-name="{e(f"{name} {contract}".strip().lower())}"'
    )


def _row_tone(item: BatchItem) -> str:
    """Кромка строки. У сбоя своя, отличная от жёлтой «проверить руками»."""
    return FAILED if item.error else _verdict_of(item.verdict).value


def _row_coverage(item: BatchItem) -> str:
    """Полнота данных под строкой. Рисуется кромкой, а не прячется в подпись."""
    if item.error:
        return FAILED
    return "full" if (item.confidence or 0) >= FULL_CONFIDENCE else "partial"


_QUEUE_SCRIPT = """<script>
(function () {
  var root = document.getElementById('queue');
  if (!root) return;
  var body = root.querySelector('tbody');
  if (!body) return;
  var rows = [].slice.call(body.querySelectorAll('tr[data-tone]'));
  var heads = [].slice.call(body.querySelectorAll('tr[data-group]'));
  var chips = document.getElementById('filters');
  var find = document.getElementById('q-find');
  var sorter = document.getElementById('q-sort');
  var status = document.getElementById('q-status');
  var more = document.getElementById('q-more');
  var note = document.getElementById('q-score-note');

  // Окно рендера. Восемьсот карточек на телефоне — это мёртвая прокрутка и
  // секунда на каждый фильтр, поэтому список выдаётся порциями. На фильтры,
  // поиск и сортировку окно не влияет: они всегда идут по всем строкам.
  var STEP = window.matchMedia('(max-width:600px)').matches ? 25 : 100;
  var limit = STEP;
  var want = 'all';
  var query = '';
  var mode = 'verdict';
  var shut = {};

  function keep(key, value) { try { sessionStorage.setItem('q:' + key, value); } catch (err) {} }
  function recall(key, fallback) {
    try { return sessionStorage.getItem('q:' + key) || fallback; } catch (err) { return fallback; }
  }
  // Фамилия в хранилище не кладётся: за ссылкой персональные данные, и
  // переживать вкладку запросу незачем.

  function num(row, key) { return parseInt(row.dataset[key], 10) || 0; }
  function plural(n, one, few, many) {
    var mod10 = n % 10, mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
  }

  function byDebt(a, b) { return num(b, 'debt') - num(a, 'debt'); }
  var order = {
    verdict: function (a, b) { return num(a, 'order') - num(b, 'order') || byDebt(a, b); },
    debt: byDebt,
    // Балл без оценки уходит вниз: строка, которую не посчитали, не должна
    // делить место с посчитанными нулями.
    score: function (a, b) { return num(b, 'score') - num(a, 'score') || byDebt(a, b); }
  };

  function fits(row) {
    if (want !== 'all' && row.dataset.tone !== want) return false;
    if (query && row.dataset.name.indexOf(query) < 0) return false;
    return true;
  }

  function apply() {
    var matched = rows.filter(fits);
    matched.sort(order[mode] || order.verdict);
    rows.forEach(function (row) { row.hidden = true; });

    var plan = [];
    if (mode === 'verdict') {
      // Группы имеют смысл только в порядке по вердикту: в сортировке по сумме
      // соседями оказываются разные вердикты, и заголовок группы лгал бы.
      heads.forEach(function (head) {
        var key = head.dataset.group;
        var mine = matched.filter(function (row) { return row.dataset.tone === key; });
        head.hidden = mine.length === 0;
        var button = head.querySelector('.ghead');
        button.setAttribute('aria-expanded', String(!shut[key]));
        head.querySelector('.gcount').textContent = mine.length;
        plan.push(head);
        if (!shut[key]) plan = plan.concat(mine);
      });
    } else {
      heads.forEach(function (head) { head.hidden = true; });
      plan = matched;
    }

    var used = 0;
    var frag = document.createDocumentFragment();
    plan.forEach(function (node) {
      if (node.dataset.tone) {
        node.hidden = used >= limit;
        if (!node.hidden) used += 1;
      }
      frag.appendChild(node);
    });
    body.appendChild(frag);

    if (status) {
      status.textContent = used === rows.length
        ? 'Показаны все ' + rows.length + ' ' + plural(rows.length, 'строка', 'строки', 'строк')
        : 'Показано ' + used + ' из ' + matched.length +
          (matched.length === rows.length ? '' : ' подходящих (всего ' + rows.length + ')');
    }
    if (more) {
      var rest = matched.length - used;
      more.hidden = rest <= 0;
      more.textContent = 'Показать ещё ' + Math.min(rest, STEP * 2);
    }
    if (note) note.hidden = mode !== 'score';
  }

  if (chips) {
    chips.addEventListener('click', function (event) {
      var button = event.target.closest('button[data-filter]');
      if (!button) return;
      want = button.dataset.filter;
      limit = STEP;
      chips.querySelectorAll('button').forEach(function (other) {
        other.setAttribute('aria-pressed', String(other === button));
      });
      keep('filter', want);
      apply();
    });
  }
  if (sorter) {
    sorter.addEventListener('change', function () {
      mode = sorter.value;
      limit = STEP;
      keep('sort', mode);
      apply();
    });
  }
  if (find) {
    find.addEventListener('input', function () {
      query = find.value.trim().toLowerCase();
      limit = STEP;
      apply();
    });
  }
  if (more) {
    more.addEventListener('click', function () { limit += STEP * 2; apply(); });
  }
  body.addEventListener('click', function (event) {
    var head = event.target.closest('.ghead');
    if (head) {
      var key = head.closest('tr').dataset.group;
      shut[key] = !shut[key];
      apply();
      return;
    }
    // Обоснование в таблице обрезано двумя строками: иначе восемьсот абзацев
    // невозможно просмотреть. Клик по строке раскрывает её целиком.
    if (event.target.closest('button, a, input, select')) return;
    var row = event.target.closest('tr[data-tone]');
    if (row) row.classList.toggle('open');
  });

  // На бумагу уходит то, что человек отобрал, — но не обрезанное окном рендера
  // и не срезанное по двум строкам: окно это бюджет отрисовки, а не выбор
  // оператора, и потерянная на листе строка — потерянный факт.
  window.addEventListener('beforeprint', function () {
    limit = rows.length;
    shut = {};
    apply();
  });

  mode = recall('sort', 'verdict');
  if (sorter) sorter.value = mode;
  want = recall('filter', 'all');
  if (chips) {
    var active = chips.querySelector('button[data-filter="' + want + '"]');
    if (!active) want = 'all';
    chips.querySelectorAll('button').forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.dataset.filter === want));
    });
  }
  apply();
})();
</script>"""


# ---------------------------------------------------------------- вспомогательное


def _verdict_of(value: str) -> Verdict:
    try:
        return Verdict(value)
    except ValueError:
        return Verdict.REVIEW


def _share(part: Decimal, whole: Decimal) -> float:
    if whole <= 0:
        return 0.0
    return float(part / whole * 100)


def _plain(amount: Decimal) -> str:
    """Сумма без пробелов и знака валюты — то, что вставляют в форму."""
    if amount == amount.to_integral_value():
        return str(int(amount))
    return f"{amount:f}"


def _footer_text(snapshot: QueueSnapshot) -> str:
    finished = (
        f"Прогон завершён {format_datetime(snapshot.finished_at)}. "
        if snapshot.finished_at
        else "Прогон ещё идёт, цифры промежуточные. "
    )
    return (
        finished
        + "Оценка аналитическая и не заменяет юридическую проверку. "
        + "Ссылка временная и открывается без пароля — не пересылайте её посторонним."
    )
