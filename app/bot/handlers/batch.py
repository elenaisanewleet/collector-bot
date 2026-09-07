"""``/batch`` — массовая проверка всей выгрузки.

Главный сценарий продукта в чате: восемьсот должников, и вопрос ровно один —
на кого тратить госпошлину. Отчёт живёт на веб-странице, а бот отвечает за то,
что вокруг неё: решение потратить деньги, ожидание и честный итог.

Пять вещей, из которых собран этот флоу.

**Смета называет деньги, а не «сейчас проверим».** До запуска оператор видит
четыре числа: сколько должников, сколько обращений к источникам, во что это
встанет в рублях и по скольким строкам данных не хватает настолько, что часть
источников по ним не спросят вовсе. Рубли берутся из настройки
``PROVIDER_REQUEST_COST``; если её нет, смета так и говорит — и остаётся в
обращениях, а не подставляет придуманный тариф.

**Подтверждение называет сумму на самой кнопке.** «Запустить проверку» — это
про действие, «Списать до 10 020 ₽» — про последствие, и подписью под пальцем
должно стоять второе. Подтверждается при этом конкретная смета: в callback
уезжает число должников, и если база с тех пор изменилась, бот показывает смету
заново, а не запускает прогон по числам, которых оператор не видел.

**Прогресс правится на месте и называет потраченное.** Одно сообщение вместо
восьмисот, и в нём не только «137 из 800», но и сколько это уже стоило. Ссылка
на очередь появляется с первым же обновлением, а не в конце: страница
заполняется на ходу, и смотреть её можно, пока прогон идёт.

**Прогон кончается тремя разными способами.** Дошёл до конца, остановлен
отказом источника, оборвался на сбое. Под одной подписью «Проверка завершена»
это ложь: очередь в двух случаях из трёх неполная, а решение о госпошлине
принимают по ней. Поэтому итог начинается с того, чем кончился прогон, и лишь
потом называет цифры.

**Что успели — показываем всегда.** Оборванный прогон не отменяет посчитанного:
сводка и ссылка на очередь уходят в чат и после отказа источника, и после сбоя,
вместе с прямой оговоркой, что строк не хватает.
"""

from __future__ import annotations

from contextlib import suppress
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot import view
from app.bot.common import answer_callback, callback_message
from app.bot.keyboards import (
    BATCH_PREFIX,
    batch_confirm_keyboard,
    batch_result_keyboard_with_link,
    batch_running_keyboard,
    main_menu,
)
from app.bot.states import BatchCheck
from app.container import Container
from app.db.models import BatchItem
from app.db.repository import BatchRepository
from app.domain.enums import PROVIDER_TITLES, ProviderName
from app.domain.verdict import VERDICT_TITLES, Verdict
from app.logging_setup import get_logger
from app.services.batch import (
    BatchAlreadyRunningError,
    BatchEstimate,
    BatchProgress,
    BatchSummary,
    RunStatus,
)
from app.services.export import queue_to_csv
from app.services.share import ShareKind, ShareTarget
from app.utils.formatting import group_digits, pluralize_ru, split_message
from app.utils.money import format_amount

logger = get_logger(__name__)

EMPTY_BASE = (
    "Внутренняя база пуста. Загрузите выгрузку должников через /import, и запускайте проверку."
)
NO_RUN = "Прогонов ещё не было. Запустите проверку через /batch."
# Второе нажатие или второй владелец, пока прогон идёт. Отказ, а не очередь:
# прогон платит за всю выгрузку, и второй заплатил бы за неё второй раз.
ALREADY_RUNNING = (
    "Проверка базы уже идёт — второй прогон оплатил бы тех же должников заново.\n\n"
    "Очередь наполняется прямо сейчас, её видно по кнопке ниже. "
    "Итог придёт в чат, когда прогон закончится."
)
LIST_PAGE_SIZE = 15
# Прогон не пережил даже собственного закрытия — сводки нет и взять её неоткуда.
RUN_CRASHED = (
    "Прогон оборвался, и собрать сводку не удалось.\n\n"
    "Всё, что успело посчитаться, осталось в базе и не потеряно. Запустите "
    "проверку заново: за уже проверенных второй раз платить не придётся, они "
    "возьмутся из кэша."
)
# Смету подтверждают по конкретным числам. Если база с тех пор изменилась,
# запускать по старым — значит списать деньги за то, чего оператор не видел.
BASE_CHANGED = (
    "База изменилась с момента, когда я показал смету. Вот пересчитанная — "
    "проверьте числа и подтвердите заново."
)
# Кнопка из сообщения, которое пережило перезапуск бота или пересылку: смета,
# под которой она стояла, боту больше не известна.
STALE_BUTTON = (
    "Эта кнопка из старого сообщения — смета под ней уже не считается. "
    "Вот свежая, её и подтвердите."
)


def render_estimate(estimate: BatchEstimate) -> str:
    """Смета: четыре числа и оговорки, после которых можно нажимать.

    Порядок не произвольный. Сначала объём (сколько должников), потом цена
    (сколько обращений и сколько рублей), потом дыры в данных — и только в конце
    оговорки. Оператор читает сверху вниз и на каждом шаге знает больше, чем на
    предыдущем; цена, поставленная после оговорок, теряется в них.
    """
    noun = pluralize_ru(estimate.debtors, "должник", "должника", "должников")
    lines = [
        "Массовая проверка",
        "",
        f"В базе: {estimate.debtors} {noun}",
        f"Уже проверено недавно: {estimate.cached} — будут взяты из кэша, они бесплатны",
        f"Нужно опросить: {estimate.to_query}",
        *_cost_lines(estimate),
    ]
    gaps = _gap_lines(estimate)
    if gaps:
        lines.append("")
        lines.append("Данных не хватает:")
        lines.extend(gaps)
    if estimate.capped:
        lines.append("")
        lines.append(
            "Прогон ограничен настройкой BATCH_MAX_DEBTORS: проверим первые "
            f"{estimate.debtors}, остальные останутся непроверенными."
        )
    lines.append("")
    lines.append(
        "Запросы к платным источникам списываются с вашего баланса в момент "
        "обращения и не возвращаются, даже если источник ничего не нашёл."
    )
    lines.append(
        "Прогон долгий. Прогресс будет в этом же сообщении, а очередь начнёт "
        "заполняться сразу — ссылку пришлю с первым обновлением."
    )
    return "\n".join(lines)


def _cost_lines(estimate: BatchEstimate) -> list[str]:
    """Цена прогона: в обращениях и в рублях.

    Формулировка «до N» и оговорка про кэш повторяют страницу очереди дословно.
    Это не копипаста, а требование: смета и страница называют одно и то же
    число, и если они назовут его разными словами, оператор решит, что чисел
    два.
    """
    if not estimate.providers_per_debtor:
        return [
            "Внешние источники не подключены — проверка пройдёт по внутренней "
            "базе и не будет стоить ничего."
        ]
    lines = [
        f"Обращений к источникам: до {group_digits(estimate.requests)} "
        f"({estimate.providers_per_debtor} на должника; взятые из кэша не оплачиваются)"
    ]
    cost = estimate.cost
    if cost is None:
        # Ноль рублей здесь означал бы «бесплатно». Правда — «цена неизвестна»,
        # и сказать это надо словами, а не пропущенной строкой.
        lines.append(
            "Во сколько это встанет в рублях — сказать нечем: цена обращения не "
            "задана в настройках (PROVIDER_REQUEST_COST). Считайте в обращениях."
        )
    else:
        lines.append(
            f"Спишется с баланса: до {format_amount(cost)} "
            f"(по {format_amount(estimate.cost_per_request)} за обращение)"
        )
    return lines


def _gap_lines(estimate: BatchEstimate) -> list[str]:
    """Чего не хватает в самой выгрузке — до того, как за прогон заплатят.

    Обе дыры чинятся правкой выгрузки, то есть до запуска, то есть знать о них
    надо здесь. После прогона это уже не информация, а объяснение, почему деньги
    ушли в строки со словом «неизвестно».
    """
    lines: list[str] = []
    if estimate.unusable > 0:
        noun = pluralize_ru(estimate.unusable, "строка", "строки", "строк")
        verb = pluralize_ru(estimate.unusable, "попадёт", "попадут", "попадут")
        lines.append(
            f"Ни ФИО, ни номера договора: {estimate.unusable} {noun}. Искать по "
            f"ним нечего и нечем — они {verb} в очередь как «не проверено», а не "
            "как «ничего не найдено»."
        )
    lines.extend(_bridge_lines(estimate))
    return lines


def _bridge_lines(estimate: BatchEstimate) -> list[str]:
    """Мост «паспорт → ИНН» в смете — отдельной строкой, а не в общем числе.

    Молчаливый ноль читался бы как «бесплатно и работает», тогда как значит он
    обратное: паспортов в выгрузке нет, и три источника по этим должникам не
    будут проверены вовсе. Поэтому вместо ноля печатается контр-строка.
    """
    if estimate.bridge_calls > 0:
        return [f"ИНН по паспорту (ФНС): {estimate.bridge_calls} вызовов — по одному на должника"]
    if estimate.without_inn > 0:
        return [
            "ИНН по паспорту: 0 вызовов — паспорта в выгрузке не хранятся. "
            f"У {estimate.without_inn} должников ИНН неизвестен: банкротство, "
            "статус ИП и арбитраж по ним проверены НЕ будут."
        ]
    return []


def confirm_label(estimate: BatchEstimate) -> str:
    """Подпись кнопки запуска. Называет последствие, а не действие.

    «Запустить проверку» — про то, что произойдёт на экране. Оператор в этот
    момент тратит деньги, и под пальцем у него должна стоять сумма.
    """
    if not estimate.providers_per_debtor:
        return "Запустить проверку"
    cost = estimate.cost
    if cost is None:
        return f"Запустить — до {group_digits(estimate.requests)} платных обращений"
    return f"Запустить и списать до {format_amount(cost)}"


def render_progress(progress: BatchProgress, estimate: BatchEstimate | None = None) -> str:
    """Прогресс с ценой: сколько проверено и во что это уже обошлось."""
    return view.batch_progress(
        progress.processed,
        progress.total,
        progress.failed,
        spent=_spent_line(progress, estimate),
    )


def _spent_line(progress: BatchProgress, estimate: BatchEstimate | None) -> str | None:
    """Сколько прогон уже потратил.

    Верхняя граница, а не точное число: сколько из проверенных пришло из кэша,
    известно только источнику. «До» — то же слово, что в смете и на странице
    очереди, и по той же причине.
    """
    if estimate is None or not estimate.providers_per_debtor:
        return None
    requests = progress.processed * estimate.providers_per_debtor
    line = f"Потрачено: до {group_digits(requests)} обращений"
    if estimate.cost_per_request > 0:
        line += f" — до {format_amount(estimate.cost_per_request * requests)}"
    return line + " (взятые из кэша не оплачиваются)"


def render_summary(summary: BatchSummary) -> str:
    """Итог прогона. Начинается с того, чем прогон кончился.

    Сначала полнота, потом цифры — а не наоборот. Двести строк «можно подавать»,
    прочитанные до сообщения о том, что прогон встал на трёхстах из восьмисот,
    успевают стать планом на неделю; после — остаются тем, что они есть.
    """
    lines = [_head_line(summary)]
    incomplete = _incomplete_lines(summary)
    if incomplete:
        lines.append("")
        lines.extend(incomplete)

    verdicts = _verdict_lines(summary)
    if verdicts:
        lines.append("")
        lines.extend(verdicts)

    lines.append("")
    lines.extend(_money_lines(summary))

    if summary.failed:
        lines.append("")
        noun = pluralize_ru(summary.failed, "строка", "строки", "строк")
        lines.append(
            f"Не удалось проверить: {summary.failed} {noun}. Это не «ничего не "
            "найдено» — по ним не ответил никто, и в суммы выше они не вошли."
        )
    lines.append("")
    lines.append("Оценка аналитическая и не заменяет юридическую проверку.")
    return "\n".join(lines)


def _head_line(summary: BatchSummary) -> str:
    if summary.status == RunStatus.STOPPED:
        return "Прогон остановлен: источник перестал отвечать"
    if summary.status == RunStatus.INTERRUPTED:
        return "Прогон оборвался"
    return f"Проверка завершена: {summary.processed} из {summary.total}"


def _incomplete_lines(summary: BatchSummary) -> list[str]:
    """Почему очередь неполная и что с этим делать.

    Стоит выше цифр и говорит прямым текстом: непроверенные — это не «чисто», а
    «не смотрели». Заканчивается действием, потому что вопрос у оператора здесь
    ровно один — платить ли за прогон второй раз (не платить: кэш).
    """
    if summary.is_complete:
        return []
    left = summary.unchecked
    if left:
        noun = pluralize_ru(left, "должник", "должника", "должников")
        lines = [
            f"Проверено {summary.processed} из {summary.total}. "
            f"Оставшиеся {left} {noun} не проверялись вовсе — это не «ничего не "
            "найдено», до них просто не дошло. Очередь ниже неполная."
        ]
    else:
        # Прогон обошёл всю выгрузку, но закрылся нештатно: строки последней
        # страницы могли не записаться. Счётчик тут ничего не показывает, и
        # молчать об этом нельзя — очередь всё равно под подозрением.
        lines = [
            f"Проверено {summary.processed} из {summary.total}, но прогон "
            "закрылся нештатно: часть результатов могла не попасть в очередь."
        ]
    if summary.status == RunStatus.STOPPED:
        lines.append(_refusal_line(summary))
    elif summary.status == RunStatus.INTERRUPTED:
        detail = f" ({summary.error})" if summary.error else ""
        lines.append(
            f"Прогон не пережил сбоя{detail}. Всё, что успело посчитаться, верно и лежит в очереди."
        )
    lines.append(
        "Запустите /batch заново, когда почините: за уже проверенных второй раз "
        "платить не придётся, они возьмутся из кэша."
    )
    return lines


def _refusal_line(summary: BatchSummary) -> str:
    """Кто именно отказал. Без имени источника чинить нечего."""
    titles = [_source_title(name) for name in summary.refused_sources]
    who = ", ".join(titles) if titles else "источник"
    return (
        f"Отказ пришёл от: {who}. Обычно это кончившийся баланс или отклонённый "
        "ключ доступа. Я остановил прогон, чтобы не платить за ответы, которых "
        "всё равно не будет."
    )


def _source_title(name: str) -> str:
    try:
        return PROVIDER_TITLES.get(ProviderName(name), name)
    except ValueError:
        # Источник из будущей версии. Печатаем как есть: имя без словаря лучше,
        # чем пропущенная строка про то, кто именно отказал.
        return name


def _verdict_lines(summary: BatchSummary) -> list[str]:
    lines = []
    for verdict in (Verdict.FILE, Verdict.ORDER, Verdict.REVIEW, Verdict.DROP):
        count = summary.count(verdict)
        if not count:
            continue
        debt = summary.debt(verdict)
        row = f"{VERDICT_TITLES[verdict]}: {count}"
        if debt:
            row += f" — на {format_amount(debt)}"
        lines.append(row)
    return lines


def _money_lines(summary: BatchSummary) -> list[str]:
    """Две суммы, ради которых прогон и запускался.

    Сэкономленная пошлина печатается всегда, в том числе нулём: отсутствие
    строки читается как отсутствие экономии, а «безнадёжных не нашлось» — это
    другое утверждение и его надо сказать. Тем же правилом живёт страница
    очереди, и слова здесь те же.
    """
    lines = []
    if summary.actionable:
        noun = pluralize_ru(summary.actionable, "должнику", "должникам", "должникам")
        lines.append(
            f"Пошлина по {summary.actionable} {noun}, которых несём в суд: "
            f"{format_amount(summary.actionable_fee)}"
        )
    dropped = summary.count(Verdict.DROP)
    if summary.saved_fees > Decimal("0"):
        verb = pluralize_ru(dropped, "отсеян", "отсеяно", "отсеяно")
        lines.append(
            f"Сэкономлено на пошлинах: {format_amount(summary.saved_fees)} — "
            f"{dropped} безнадёжных {verb}, эти деньги останутся в кассе"
        )
    else:
        lines.append("Сэкономлено на пошлинах: 0 ₽ — безнадёжных в этом прогоне не нашлось")
    return lines


async def offer_batch(
    message: Message, state: FSMContext, container: Container, note: str = ""
) -> None:
    """Смета прогона и предложение подтвердить.

    Модульного уровня, а не вложенная в ``build_router``: тот же экран открывает
    кнопка «📊 Проверить всю базу» с нижней клавиатуры, а её обработчик живёт в
    :mod:`app.bot.handlers.buttons` и до замыкания не дотянулся бы.
    """
    estimate = await container.batch_service.estimate()
    if estimate.debtors == 0:
        await state.clear()
        # Роутер прогона закрыт по владельцу целиком, поэтому меню здесь всегда
        # владельческое: до этой строки не доходит никто другой.
        await message.answer(EMPTY_BASE, reply_markup=main_menu(owner=True))
        return
    await state.set_state(BatchCheck.waiting_confirm)
    text = render_estimate(estimate)
    if note:
        text = f"{note}\n\n{text}"
    await message.answer(
        text,
        reply_markup=batch_confirm_keyboard(confirm_label(estimate), estimate.debtors),
    )


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="batch")

    @router.message(Command("batch"))
    async def handle_batch_command(
        message: Message, state: FSMContext, container: Container
    ) -> None:
        await offer_batch(message, state, container)

    @router.callback_query(F.data == f"{BATCH_PREFIX}:start")
    async def handle_batch_start(
        callback: CallbackQuery, state: FSMContext, container: Container
    ) -> None:
        await answer_callback(callback)
        target = callback_message(callback)
        if target:
            await offer_batch(target, state, container)

    @router.callback_query(BatchCheck.waiting_confirm, F.data.startswith(f"{BATCH_PREFIX}:run"))
    async def handle_batch_run(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return

        # Состояние снимается и клавиатура гасится ДО первого ``await`` наружу.
        # Фильтр состояния — единственное, что отсекает второе нажатие, и пока
        # смета считалась, оно проходило насквозь: два прогона, каждый платит
        # за всех должников. Кнопка со сметы убирается тем же движением —
        # Telegram её сам не гасит, и она остаётся под пальцем весь прогон.
        await state.clear()
        with suppress(Exception):
            await message.edit_reply_markup(reply_markup=None)

        estimate = await container.batch_service.estimate()
        if _confirmed_debtors(callback.data) != estimate.debtors:
            # База изменилась между сметой и нажатием: кто-то импортировал
            # выгрузку, или прошла чистка. Запускать по числам, которых оператор
            # не видел, нельзя — за них платят.
            await offer_batch(message, state, container, note=BASE_CHANGED)
            return

        notice = await message.answer(
            render_progress(BatchProgress(processed=0, total=estimate.debtors, failed=0), estimate)
        )
        last = ""
        keyboard = None

        async def report(progress: BatchProgress) -> None:
            nonlocal last, keyboard
            if keyboard is None and progress.run_id:
                # Очередь наполняется на ходу, и открыть её можно с первой
                # секунды. Ссылка выдаётся один раз и потом только переезжает
                # вместе с текстом.
                keyboard = batch_running_keyboard(
                    await container.share_service.issue(
                        ShareTarget(ShareKind.QUEUE, progress.run_id),
                        telegram_user_id=user_id,
                    )
                )
            text = render_progress(progress, estimate)
            if text == last:
                return
            last = text
            try:
                await notice.edit_text(text, reply_markup=keyboard)
            except Exception:
                logger.debug("batch.progress_edit_failed")

        try:
            summary = await container.batch_service.run(telegram_user_id=user_id, progress=report)
        except BatchAlreadyRunningError as busy:
            # Второе нажатие или второй владелец. Отказ, а не очередь: прогон,
            # молча начавшийся следом за первым, оплатил бы тех же должников
            # ещё раз. Ссылка ведёт в ту очередь, которая наполняется сейчас.
            url = await container.share_service.issue(
                ShareTarget(ShareKind.QUEUE, busy.run_id), telegram_user_id=user_id
            )
            with suppress(Exception):
                await notice.delete()
            await message.answer(ALREADY_RUNNING, reply_markup=batch_running_keyboard(url))
            return
        except Exception:
            # Прогон закрывает себя сам даже на сбое, так что сюда попадает
            # только то, что сломалось вокруг него. Молчать нельзя: оператор
            # смотрит на замерший прогресс и не знает, идёт ли ещё что-то.
            logger.exception("batch.run_crashed")
            await message.answer(RUN_CRASHED, reply_markup=main_menu(owner=True))
            return

        await _deliver(message, notice, summary, container, user_id)

    async def _deliver(
        message: Message,
        notice: Message,
        summary: BatchSummary,
        container: Container,
        user_id: int,
    ) -> None:
        """Итог и кнопки. Уходит одинаково для дошедшего и для оборванного прогона.

        Оборванный прогон — не повод прятать посчитанное: двести проверенных
        строк стоили денег и остаются верными.
        """
        # Ссылка на веб-очередь — главное действие после прогона: таблицу на
        # восемьсот строк в сообщении Telegram не показать.
        url = await container.share_service.issue(
            ShareTarget(ShareKind.QUEUE, summary.run_id), telegram_user_id=user_id
        )
        csv_url, print_url = (
            container.share_service.export_urls(url, ShareKind.QUEUE) if url else (None, None)
        )
        keyboard = batch_result_keyboard_with_link(url, csv_url=csv_url, print_url=print_url)
        text = render_summary(summary)
        try:
            await notice.edit_text(text, reply_markup=keyboard)
        except Exception:
            await message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith(f"{BATCH_PREFIX}:list:"))
    async def handle_batch_list(
        callback: CallbackQuery, container: Container, user_id: int
    ) -> None:
        raw = (callback.data or "").rsplit(":", maxsplit=1)[-1]
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return
        try:
            verdict = Verdict(raw)
        except ValueError:
            return

        async with container.database.session() as session:
            repo = BatchRepository(session)
            run = await repo.latest_run(user_id)
            if run is None:
                await message.answer(NO_RUN)
                return
            items = await repo.queue(run.id, verdict=verdict.value, limit=LIST_PAGE_SIZE)
            total = (await repo.verdict_counts(run.id)).get(verdict.value, 0)
            rows = [_queue_line(index, item) for index, item in enumerate(items, start=1)]

        if not rows:
            await message.answer(f"{VERDICT_TITLES[verdict]}: пусто.")
            return
        header = f"{VERDICT_TITLES[verdict]} — {total}"
        if total > len(rows):
            header += f" (показаны первые {len(rows)}, полный список — в CSV)"
        for chunk in split_message("\n\n".join([header, *rows])):
            await message.answer(chunk)

    @router.callback_query(F.data == f"{BATCH_PREFIX}:export")
    async def handle_batch_export(
        callback: CallbackQuery, container: Container, user_id: int
    ) -> None:
        await answer_callback(callback, "Готовлю файл…")
        message = callback_message(callback)
        if message is None:
            return

        async with container.database.session() as session:
            repo = BatchRepository(session)
            run = await repo.latest_run(user_id)
            if run is None:
                await message.answer(NO_RUN)
                return
            items = await repo.queue(run.id, limit=container.settings.batch_max_debtors)
            payload = queue_to_csv(
                items, include_phone=container.settings.store_sensitive_identifiers
            )
            run_id = run.id

        await message.answer_document(
            BufferedInputFile(payload, filename=f"ochered-{run_id}.csv"),
            caption=(
                f"Очередь взыскания, прогон №{run_id}: {len(items)} строк. Телефоны маскированы."
                if not container.settings.store_sensitive_identifiers
                else f"Очередь взыскания, прогон №{run_id}: {len(items)} строк."
            ),
        )

    @router.callback_query(F.data.startswith(f"{BATCH_PREFIX}:"))
    async def handle_stale_batch_button(
        callback: CallbackQuery, state: FSMContext, container: Container
    ) -> None:
        """Кнопка прогона, под которой сметы уже нет.

        Сюда попадает «Списать до …» из сообщения, пережившего перезапуск бота
        или пересылку: состояния у нажавшего нет, подтверждать нечего, и без
        этого хендлера нажатие висело бы часиками до таймаута Telegram. Деньги
        не тратятся — смета считается заново, и подтверждают уже её.

        Стоит последним в роутере намеренно: все точные совпадения выше
        разбирают своё, сюда доходит только осиротевшая кнопка. Заодно
        осиротевшая кнопка перестала быть дырой в проверке владельца — хендлер
        роутера есть, значит есть и отказ, а не молчание.
        """
        await answer_callback(callback)
        target = callback_message(callback)
        if target:
            await offer_batch(target, state, container, note=STALE_BUTTON)

    return router


def _confirmed_debtors(data: str | None) -> int | None:
    """Сколько должников было в смете, которую подтвердили.

    ``None`` — старая кнопка без числа (сообщение из прошлой версии бота висит
    в чате). Считается несовпадением: показать смету заново дешевле, чем
    списать деньги по числам, которых никто не видел.
    """
    tail = (data or "").rsplit(":", maxsplit=1)[-1]
    return int(tail) if tail.isdigit() else None


def _queue_line(index: int, item: BatchItem) -> str:
    debtor = item.debtor
    name = (debtor.fio if debtor else None) or (debtor.contract_number if debtor else None) or "—"
    parts = [f"{index}. {name}"]
    if item.debt_amount is not None:
        fee = f" · пошлина {format_amount(item.state_fee)}" if item.state_fee else ""
        parts.append(f"   Долг {format_amount(item.debt_amount)}{fee}")
    if debtor and debtor.contract_number:
        parts.append(f"   Договор {debtor.contract_number}")
    parts.append(f"   {item.headline}")
    return "\n".join(parts)
