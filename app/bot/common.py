"""Shared helpers for handlers.

Handlers stay thin: they collect input, call a service and render. Everything
that is neither collection nor rendering lives here or in the service layer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from contextlib import suppress
from datetime import timedelta

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import report_actions, view
from app.container import Container
from app.db.repository import SearchRepository
from app.domain.enums import SearchType
from app.domain.identity import SearchSubject
from app.domain.models import DebtorReport
from app.logging_setup import get_logger
from app.providers.newdb import individual_inn
from app.services.reporting import render_report
from app.services.share import ShareKind, ShareTarget
from app.utils.dates import utcnow
from app.utils.formatting import split_message

logger = get_logger(__name__)

# Как часто двигать полосу, пока идёт проверка. Реже, чем лимит Telegram на
# правку сообщения, и достаточно часто, чтобы это читалось как движение.
STAGE_INTERVAL_SECONDS = 1.6
#: Как часто сообщение обновляется ПОСЛЕ того, как стадии кончились. Реже, чем
#: стадии: сказать больше нечего, кроме «я ещё работаю», а правки Telegram
#: считает.
WAITING_INTERVAL_SECONDS = 5.0

#: Показывается, пока идёт проверка, и только когда правда. Три источника из
#: шести ищут только по ИНН физлица; сказать об этом до отчёта ничего не стоит и
#: избавляет от вопроса «почему тут пусто».
# Кнопка названа по имени: «кнопкой под отчётом» было верно, пока она там была
# одна. Их пять, и оператор ищет глазами ту, которой нет.
NO_INN_NOTE = (
    "Без ИНН не спрошу банкротство, ИП и арбитраж — добавить можно "
    "кнопкой «Уточнить данные» под отчётом."
)

#: Сутки — окно суточной квоты. Скользящее, а не «до полуночи»: календарный
#: день сбрасывал бы счётчик разом всем и превращал бы полночь в окно, когда
#: остаток тратится вдвое быстрее.
QUOTA_WINDOW = timedelta(hours=24)

QUOTA_SPENT = (
    "На сегодня проверки закончились: {limit} в сутки на человека.\n\n"
    "Это не поломка — каждая проверка обращается к платным реестрам, и лимит "
    "бережёт оплаченный остаток. Счётчик отпускает по одной, через сутки после "
    "каждой проверки. Нужно больше — напишите владельцу бота."
)


async def within_quota(message: Message, container: Container, user_id: int) -> bool:
    """Можно ли этому человеку потратить ещё одну платную проверку.

    Владелец без лимита: это его деньги и его решение. Всем остальным — суточная
    квота, потому что бот может работать открытым, и тогда оплаченный остаток
    тратит любой, кто его нашёл.

    Отказ говорит вслух и сразу: молчаливое «ничего не произошло» на нажатие
    оператор читает как поломку бота, а не как исчерпанный лимит.
    """
    limit = container.settings.daily_search_quota
    if limit <= 0 or container.access_service.is_owner(user_id):
        return True
    async with container.database.session() as session:
        spent = await SearchRepository(session).count_for_user_since(
            user_id, utcnow() - QUOTA_WINDOW
        )
    if spent < limit:
        return True
    logger.info("search.quota_spent", telegram_user_id=user_id, limit=limit)
    await message.answer(QUOTA_SPENT.format(limit=limit))
    return False


async def run_and_send_report(
    message: Message,
    container: Container,
    subject: SearchSubject,
    *,
    user_id: int,
    force_refresh: bool = False,
    notes: Sequence[str] = (),
) -> DebtorReport | None:
    """Проверить должника и показать результат. ``None`` — квота на сегодня выбрана.

    Квота проверяется здесь, а не в восьми хендлерах: платный прогон уходит
    только отсюда, и единственная проверка на общем пути не разъедется с
    девятым способом её обойти.

    В чат уходит карточка с вердиктом и кнопкой на веб-отчёт, а не текст на
    три сообщения: таблицу производств в сообщении Telegram всё равно не
    сверстать. Пока идёт проверка, одно и то же сообщение правится на месте —
    так видно, что работа идёт, и чат не засоряется.

    Если публичный адрес не задан, ссылки нет, и бот честно отдаёт полный
    текстовый отчёт: лучше простыня, чем нерабочая кнопка.

    ``notes`` — оговорки к разбору строки: «этот ИНН — организации, проверяю без
    него». Они едут и в прогресс, и в карточку. Именно в карточку, а не только
    в прогресс: прогресс правится на месте, и оговорка, оставленная в нём,
    исчезла бы вместе с ним — а «я выбросил часть вашего ввода» обязано
    остаться на виду рядом с результатом.
    """
    if not await within_quota(message, container, user_id):
        return None

    accepted = view.accepted_line(subject)
    note = "\n".join([*notes, *filter(None, (_progress_note(subject, container),))]) or None
    notice = await message.answer(
        view.searching(0, subject_name=subject.display_name, accepted=accepted, note=note)
    )
    ticker = asyncio.create_task(_tick_stages(notice, subject.display_name, accepted, note))
    try:
        outcome = await container.search_service.search_detailed(
            subject, telegram_user_id=user_id, force_refresh=force_refresh
        )
    finally:
        ticker.cancel()

    report = outcome.report
    decision = container.verdict_engine.decide(report)

    url: str | None = None
    if outcome.request_id is not None:
        url = await container.share_service.issue(
            ShareTarget(ShareKind.REPORT, outcome.request_id), telegram_user_id=user_id
        )

    # Токен кладётся под ТОТ субъект, с которым прошёл прогон, — с ИНН, если его
    # добыл мост. Иначе «Обновить» и предложения под карточкой рассуждали бы о
    # вопросе, а не об ответе.
    token = container.subject_store.put(report.subject)
    keyboard = report_actions.report_keyboard(
        url=url,
        refresh_token=token,
        subject=report.subject,
        bridge=container.registry.inn_bridge,
        records=report.fact_count,
        # Сузить до региона можно только то, что регион и сужает: поиск по
        # ФССП. Если производств не нашлось или регион уже один, кнопка
        # обещала бы результат, которого не будет.
        narrowable=bool(report.enforcement_proceedings) and len(report.subject.regions) != 1,
    )

    if url is None:
        # Без публичного адреса отчёт уходит текстом — но предложения добрать
        # данные остаются: они про субъект, а не про веб-страницу, и деплой без
        # веба не должен молча терять единственный способ открыть три источника.
        await _safe_delete(notice)
        chunks = split_message(render_report(report, demo_mode=container.settings.is_demo))
        for chunk in chunks[:-1]:
            await message.answer(chunk)
        await message.answer(chunks[-1], reply_markup=keyboard)
        return report

    await _edit_or_send(
        notice,
        message,
        view.report_card(report, decision, notes=notes, demo_mode=container.settings.is_demo),
        reply_markup=keyboard,
    )
    return report


def _progress_note(subject: SearchSubject, container: Container) -> str | None:
    """Что не откроется на этих данных. Только для поиска по человеку.

    МОЛЧИТ, ЕСЛИ МОСТ СЕЙЧАС ПОЙДЁТ ЗА ИНН. Строка «Без ИНН не спрошу
    банкротство, ИП и арбитраж» писалась ДО поиска и по субъекту, каким он был
    до него, — а ИНН добывается ВНУТРИ поиска, мостом «паспорт → ИНН». С
    паспортом на руках предсказание оказывалось ложным ровно тогда, когда всё
    получалось: бот обещал не спросить три источника и тут же их спрашивал.

    Это и был вопрос владелицы — «надо же ИНН доставать, почему в ФНС нельзя
    получить ИНН». Можно и достаётся; врала строка, а не мост.
    """
    if subject.search_type != SearchType.PERSON.value:
        return None
    if individual_inn(subject) is not None:
        return None
    bridge = container.registry.inn_bridge
    if bridge is not None and bridge.will_query(subject):
        return None
    return NO_INN_NOTE


async def _tick_stages(
    notice: Message, subject_name: str, accepted: str | None, note: str | None
) -> None:
    """Двигать полосу прогресса, пока идёт проверка.

    Отдельная задача, потому что сам поиск ничего о показе не знает и знать не
    должен. Отменяется, как только результат готов.
    """
    started = time.monotonic()
    try:
        for index in range(1, len(view.STAGES)):
            await asyncio.sleep(STAGE_INTERVAL_SECONDS)
            with suppress(Exception):  # правка сообщения — дело необязательное
                await notice.edit_text(
                    view.searching(index, subject_name=subject_name, accepted=accepted, note=note)
                )
        # Стадии кончились, а проверка — нет, и вот тут раньше всё замирало.
        # Четыре стадии по 1,6 с — это пять секунд, а бюджет одного источника
        # 150: полоса застывала на «Считаю перспективу…» и не двигалась минуту,
        # две, три. Владелица прочитала это ровно так, как оно выглядит: «ну и
        # всё зависло».
        #
        # Поэтому дальше сообщение продолжает жить и называет, сколько идёт.
        # Реже — раз в пять секунд: Telegram считает правки, а нового сказать
        # тут нечего, кроме «я ещё работаю».
        while True:
            await asyncio.sleep(WAITING_INTERVAL_SECONDS)
            waited = int(time.monotonic() - started)
            with suppress(Exception):
                await notice.edit_text(
                    view.searching(
                        len(view.STAGES) - 1,
                        subject_name=subject_name,
                        accepted=accepted,
                        note=note,
                        waited_seconds=waited,
                    )
                )
    except asyncio.CancelledError:  # pragma: no cover - обычный путь отмены
        pass


async def _edit_or_send(
    notice: Message, message: Message, text: str, *, reply_markup: object = None
) -> None:
    """Заменить сообщение о ходе работы результатом.

    Правка на месте вместо нового сообщения: пользователь смотрит туда же, куда
    смотрел, и в чате не остаётся мусора. Если правка не прошла — сообщение
    удалили, прошло слишком много времени — отправляем обычным сообщением.
    """
    try:
        await notice.edit_text(text, reply_markup=reply_markup)  # type: ignore[arg-type]
    except Exception:
        logger.debug("report.edit_failed")
        await _safe_delete(notice)
        await message.answer(text, reply_markup=reply_markup)  # type: ignore[arg-type]


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        logger.debug("notice.delete_failed")


async def reset_state(state: FSMContext) -> None:
    await state.clear()


async def answer_callback(callback: CallbackQuery, text: str = "") -> None:
    await callback.answer(text)


def callback_message(callback: CallbackQuery) -> Message | None:
    """The message a callback is attached to, when it is still editable.

    Telegram delivers inaccessible messages for old inline keyboards; those
    cannot be replied to.
    """
    message = callback.message
    return message if isinstance(message, Message) else None
