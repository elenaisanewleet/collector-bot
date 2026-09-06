"""Карточка запроса в чате: одно сообщение, которое живёт и правится на месте.

Что здесь происходит и чего здесь нет.

**Присланное дописывается, а не запускает проверку.** Любой текст вне чужого
сценария попадает в :meth:`QueryCardService.apply` и ложится в поле карточки.
«Клочкова Елена Николаевна», потом «24 11 1994» — это один человек с датой, а не
два запроса, из которых второй ни о ком. Платит ровно одна кнопка — «Проверить».

**Ни одного состояния FSM.** Карточка помнит всё в своей строке БД, а свободный
текст по-прежнему ловит последний роутер с ``StateFilter(None)``. Порядок
роутеров не тронут, ввод госномера, VIN, адреса, договора и паспорта работает
как работал — их хендлеры привязаны к состояниям и получают свой текст первыми.

**Карточка не исчезает после отчёта.** Она удаляется и переотправляется ПОД
отчётом, потому что иначе навсегда уезжает выше него: оператор правил бы то,
чего не видит. К найденному человеку можно дослать ИНН или паспорт и
перепроверить, не вводя всё заново.

Правка сообщения всегда несёт ``reply_markup``, и это не перестраховка:
``AiohttpSession.build_form_data`` пропускает пустые значения, поэтому
``reply_markup=None`` до Telegram не доедет вовсе — и Telegram снимет клавиатуру
как «не передали». Идентичная правка тоже не отправляется: на неё приходит 400
«message is not modified», а наш обработчик ошибки удалил бы карточку и уронил
её в конец чата.
"""

from __future__ import annotations

from contextlib import suppress

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot import card_view
from app.bot.card_view import Screen
from app.bot.common import answer_callback, callback_message, run_and_send_report
from app.bot.identifiers import Field
from app.bot.keyboards import MENU_PREFIX, REGION_COMBINED, REGION_PREFIX, region_keyboard
from app.bot.report_actions import (
    FIELD_BIRTH_DATE,
    FIELD_INN,
    FIELD_PASSPORT,
    FIELD_REGION,
    PERSON_ADD_PREFIX,
)
from app.bot.view import missing_reason
from app.container import Container
from app.domain.enums import PROVIDER_TITLES, Region, SearchType
from app.domain.identity import SearchSubject
from app.domain.models import DebtorReport, InternalDebtorRecord
from app.logging_setup import get_logger
from app.services import card_identify, coverage
from app.services.query_card import Card
from app.utils.dates import utcnow

logger = get_logger(__name__)

STALE_TOKEN = "Данные устарели, запустите поиск заново."

#: Как называется поле в старых кнопках ``padd:*`` и как — в карточке. Старые
#: кнопки живут в чате бесконечно и обязаны продолжать работать.
_LEGACY_FIELDS: dict[str, Field] = {
    FIELD_BIRTH_DATE: Field.BIRTH_DATE,
    FIELD_INN: Field.INN,
    FIELD_PASSPORT: Field.PASSPORT,
}


# ---------------------------------------------------------------- показ


async def start_person_card(message: Message, container: Container, user_id: int) -> None:
    """Начать проверку человека: сразу вопрос про телефон.

    Раньше «Проверить человека» вело в меню из семи типов проверки, и оператор
    выбирал ещё раз то, что уже выбрал. Теперь ведёт прямо к первому вопросу:
    сценарий из ТЗ — ввёл телефон, получил сводку, — и лишний экран между
    кнопкой и вводом стоял ровно поперёк него. Остальные шесть типов никуда не
    делись, они за «Другие способы поиска».
    """
    card = await container.query_cards.load(user_id, message.chat.id)
    # Начинают — значит карточка обязана быть видна прямо сейчас, даже если её
    # сообщение уже уехало вверх чата.
    container.query_cards.forget_screen(card)
    card.card_message_id = None
    if card.blank:
        # Пустая карточка — это начало разговора, и начинается он вопросами по
        # одному полю. Недособранная не трогается: оператор вернулся к своему
        # человеку, а не начал нового.
        container.query_cards.begin_steps(card)
    await show(message, container, card)


async def show(
    message: Message,
    container: Container,
    card: Card,
    *,
    notice: str | None = None,
) -> None:
    """Нарисовать карточку: поправить своё сообщение или отправить новое.

    Три случая, и каждый — про конкретную беду, которая иначе случается.

    Вид не изменился — не шлём ничего. Telegram отвечает на такую правку 400
    «message is not modified», а обработчик ошибки ниже принял бы это за
    «сообщение удалено» и отправил карточку заново, в конец чата.

    Правка не прошла — сообщение и правда удалили руками. Тогда шлём новое и
    запоминаем его номер. Не переиспользуем ``common._edit_or_send``: он делает
    delete + send и молча теряет ``message_id``, а карточке он нужен, чтобы
    после перезапуска бота править то же самое сообщение.

    ``reply_markup`` передаётся всегда — см. докстринг модуля.
    """
    conflict = _pending_conflict(card)
    screen = card_view.screen(
        card,
        registry=container.registry,
        store_sensitive=container.settings.store_sensitive_identifiers,
        notice=notice,
        conflict=conflict,
    )
    if _unchanged(container, card, screen):
        return

    bot = message.bot
    if bot is not None and card.card_message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=card.chat_id,
                message_id=card.card_message_id,
                text=screen.text,
                reply_markup=screen.markup,
            )
        except Exception:
            logger.debug("query_card.edit_failed")
            card.card_message_id = None
        else:
            await _remember(container, card, screen)
            return

    sent = await message.answer(screen.text, reply_markup=screen.markup)
    card.card_message_id = sent.message_id
    await _remember(container, card, screen)


def _unchanged(container: Container, card: Card, screen: Screen) -> bool:
    previous = container.query_cards.last_screen(card)
    return (
        card.card_message_id is not None
        and previous is not None
        and previous.text == screen.text
        and previous.markup == screen.markup_key
    )


async def _remember(container: Container, card: Card, screen: Screen) -> None:
    container.query_cards.remember_screen(card, screen.text, screen.markup_key)
    await container.query_cards.save(card)


def _pending_conflict(card: Card):  # type: ignore[no-untyped-def]
    """Отложенное ФИО, если вопрос «другой человек или исправление» ещё открыт."""
    from app.domain.identity import NameParseError, parse_fio

    if not card.pending_name:
        return None
    try:
        return parse_fio(card.pending_name)
    except NameParseError:  # pragma: no cover — кладём только разобранное
        return None


# ---------------------------------------------------------------- ввод


async def absorb(message: Message, container: Container, user_id: int) -> None:
    """Дописать присланный текст в карточку.

    Это и есть ответ на «а это к чему вообще»: сообщение не начинает новый
    запрос, оно продолжает предыдущий.
    """
    card = await container.query_cards.load(user_id, message.chat.id)
    applied = container.query_cards.apply(card, message.text or "")
    if applied.delete_message:
        # Паспорт: убираем сообщение оператора, чтобы номер не остался в
        # истории чата. Best-effort — в группе на это нужны права админа.
        with suppress(Exception):
            await message.delete()
    if not applied.changed and applied.notice is None:
        return
    if applied.rejected:
        # Ответ не подошёл под заданный вопрос. Шаг остаётся заданным, вперёд
        # сценарий не идёт: вопрос, который не получил ответа, обязан
        # задаться ещё раз, а не молча пропасть.
        await show(message, container, applied.card, notice=applied.notice)
        return
    await settle(message, container, applied.card, user_id, notice=applied.notice)


# ---------------------------------------------------------------- опознание


async def settle(
    message: Message,
    container: Container,
    card: Card,
    user_id: int,
    *,
    notice: str | None = None,
) -> None:
    """Что делать после того, как в карточку легло новое поле.

    Здесь живёт правило, которое важнее порядка шагов: **как только человек
    опознан в выгрузке однозначно — вопросы прекращаются**. Владелица сказала
    это дословно: «после телефона и фамилии, или даже телефона, если уже найдена
    одна строка в базе 1С, то всё, просто готовим отчёт».

    Выгрузка спрашивается после каждого поля, потому что стоит это ноль: свой
    файл, свой ключ не нужен, ответ локальный. Три исхода — один, несколько,
    ни одного — и каждый решает, задавать ли следующий вопрос и зачем.

    Незакрытые вопросы («паспорт или телефон?», «другой человек или
    исправление?») опознание откладывают. Опознать по данным, которые сами под
    вопросом, значит подтвердить догадку выгрузкой и больше о ней не вспомнить.

    Спрашивается выгрузка только в ведомом сценарии, и это осознанная граница.
    Сценарий сам задал вопрос и сам обещал, что лишних не задаст, — там искать
    за оператора обязанность. Свободная строка — короткий путь, по которому
    оператор ведёт сам: там подставленная из выгрузки дата рождения появилась бы
    в карточке без спроса поверх той, которую он намеренно пропустил, а прогон
    случился бы без нажатия на единственную платящую кнопку.
    """
    if not card.guided or card.pending_ten or card.pending_name:
        await show(message, container, card, notice=notice)
        return

    found = await card_identify.identify(container.search_service, card)
    if found.only is not None:
        await recognised(message, container, card, found.only, user_id, notice=notice)
        return

    container.query_cards.step_forward(card)
    await show(message, container, card, notice=_ambiguous(found, card) or notice)


def _ambiguous(found: card_identify.Identified, card: Card) -> str | None:
    """«Нашёл троих с таким телефоном — уточните фамилию».

    Следующий вопрос обоснован, а не задан по списку: оператор видит, кого бот
    нашёл и зачем ему ещё одно поле. Когда основные шаги кончились, а различить
    их так и не вышло, бот говорит и это — молча выдать первого из трёх нельзя.
    """
    if not found.several:
        return None
    head = card_view.FOUND_MANY.format(count=found.count, who=card_identify.listing(found.records))
    step = card.step
    if step is None:
        return f"{head} {card_view.FOUND_MANY_STUCK}"
    asked = card_view.ASK_NOUNS[step.value]
    return f"{head} {card_view.FOUND_MANY_ASK.format(field=asked)}"


async def recognised(
    message: Message,
    container: Container,
    card: Card,
    record: InternalDebtorRecord,
    user_id: int,
    *,
    notice: str | None = None,
) -> None:
    """Человек опознан однозначно. Спрашивать больше нечего.

    Порядок здесь и есть ответ на «основной источник — 1С». Сначала карточка
    показывает, КОГО нашли и что подставили из выгрузки, — договор, машину,
    долг, дату рождения; и только потом уходит запрос в реестры, которые
    отвечают дольше, стоят денег и иногда молчат. Нашли своего — бот уже
    пригодился, и ждать ФССП, чтобы это увидеть, оператор не должен.

    Автопрогон случается только в ведомом сценарии. Оператор, который пришёл
    сюда свободной строкой, кнопку «Проверить» не нажимал и платного запроса не
    просил; ему карточка просто покажет найденное. А в сценарии бот сам обещал
    «готовлю отчёт» — там молчание было бы обманом.
    """
    filled = card_identify.absorb(card, record)
    container.query_cards.leave_steps(card)

    found = card_view.FOUND_ONE.format(who=card_identify.describe(record))
    if filled:
        found = f"{found} {card_view.FOUND_ONE_FILLED.format(fields=', '.join(filled))}"
    # Оговорка к разбору («„Клочкова“ записал в фамилию») не выбрасывается ради
    # хорошей новости: угаданное поле надо показать даже тогда — особенно тогда,
    # когда по нему сейчас пойдёт платный запрос.
    await show(message, container, card, notice=f"{notice} {found}" if notice else found)

    if card.runnable and card.last_run_hash != container.query_cards.run_hash(card):
        await run_card(message, container, card, user_id)


# ---------------------------------------------------------------- прогон


def run_notes(subject: SearchSubject, container: Container) -> list[str]:
    """Оговорки к прогону: какие источники остались неопрошенными и почему.

    Едут в отчёт, а не только в карточку. Отчёт по неполной карточке обязан
    честно сказать, чего он не спрашивал, — иначе пустой раздел читается как
    «проверили, ничего нет», и это ровно та подмена, ради запрета которой
    написан весь инструмент.

    Формулировка одинаковая для «не спрашивали» и «пропустили»: разница между
    ними — про оператора, а не про качество проверки, и в отчёте ей не место.
    """
    gaps = coverage.blocked(subject, container.registry)
    if not gaps:
        return []
    reasons = "; ".join(
        f"{', '.join(PROVIDER_TITLES.get(name, name.value) for name in names)} — "
        f"{missing_reason(tuple(reason))}"
        for reason, names in gaps.items()
    )
    return [f"Не спрашивали: {reasons}. {card_view.NOT_ASKED}"]


async def run_card(message: Message, container: Container, card: Card, user_id: int) -> None:
    """Проверить то, что собрано, и переотправить карточку под отчётом."""
    subject = card.subject()
    if subject is None:  # pragma: no cover — проверено вызывающим
        return

    await _drop_card_message(message, container, card)
    report = await run_and_send_report(
        message, container, subject, user_id=user_id, notes=run_notes(subject, container)
    )

    card.checked_at = utcnow()
    card.last_run_hash = container.query_cards.run_hash(card)
    card.awaiting_field = None
    card.guided = False
    card.last_run_empty = _came_back_empty(report)
    await show(message, container, card)


def _came_back_empty(report: DebtorReport) -> bool:
    """Стоит ли предлагать меню опций после отчёта.

    Дословно: «если заканчиваются эти параметры и ты ничего не находишь, то
    просто говоришь: вот меню, тогда можно ввести ИНН, ВИН, ГРЗ и остальное».
    Этот случай важнее прочих — пустой отчёт без предложения искать иначе это
    тупик, из которого оператор уходит ни с чем, хотя у него на руках может
    лежать ИНН из договора или госномер машины.

    Считается двумя разными поводами, и оба обязательны. Первый — фактов не
    принёс никто. Второй — источник ответил «недостаточно данных»: он не искал
    вовсе, и предложить ключ, которым он ищет, надо ровно так же, а часто и
    нужнее.

    Оговорка про инвариант: ``last_run_empty`` НЕ означает «должник чист». Он
    означает только «предложи другой ключ». Что именно осталось неопрошенным,
    говорит отчёт и строка «Не спрошено» в самой карточке — здесь этой разницы
    не считают и не показывают.
    """
    facts = (
        report.internal_records,
        report.enforcement_proceedings,
        report.bankruptcies,
        report.business_relations,
        report.court_cases,
        report.pledges,
        report.vehicles,
        report.properties,
        # Только подтверждённые: реестр наследственных дел ищет по одному ФИО,
        # и найденный однофамилец — не факт о должнике. Прогон, в котором нашлись
        # только они, по-прежнему стоит переспросить другим ключом.
        report.confirmed_inheritance_cases,
    )
    if not any(facts):
        return True
    return any(result.missing_input for result in report.provider_results)


async def _drop_card_message(message: Message, container: Container, card: Card) -> None:
    """Убрать прежнюю карточку: она уедет выше отчёта и станет ловушкой."""
    bot = message.bot
    if bot is not None and card.card_message_id is not None:
        with suppress(Exception):
            await bot.delete_message(chat_id=card.chat_id, message_id=card.card_message_id)
    card.card_message_id = None
    container.query_cards.forget_screen(card)


# ---------------------------------------------------------------- роутер


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="query_card")

    @router.callback_query(F.data == f"{MENU_PREFIX}:{SearchType.PERSON.value}")
    async def open_card(callback: CallbackQuery, container: Container, user_id: int) -> None:
        """«Проверить человека» в меню больше не задаёт вопрос — показывает карточку.

        Разница не косметическая: вопрос уезжает вверх чата вместе с ответом,
        карточка остаётся на месте и показывает, что уже собрано.
        """
        message = callback_message(callback)
        await answer_callback(callback)
        if message is None:
            return
        await start_person_card(message, container, user_id)

    @router.callback_query(F.data == card_view.QC_NEXT)
    async def next_step(callback: CallbackQuery, container: Container, user_id: int) -> None:
        """«Дальше» — тот же ответ, что и текст, только пустой.

        Есть у каждого из трёх шагов без исключений: «везде возможность
        пропустить», «то есть ты можешь не написать ничего». Пропуск ставит в
        строку «пропустили», а не прочерк: спросили и не получили ответа — это
        не то же самое, что не спрашивали.
        """
        message = callback_message(callback)
        await answer_callback(callback)
        if message is None:
            return
        card = await container.query_cards.load(user_id, message.chat.id)
        container.query_cards.skip(card)
        container.query_cards.step_forward(card)
        await show(message, container, card)

    @router.callback_query(F.data.startswith(f"{card_view.QC_ASK}:"))
    async def ask_field(callback: CallbackQuery, container: Container, user_id: int) -> None:
        field_name = (callback.data or "").rsplit(":", maxsplit=1)[-1]
        message = callback_message(callback)
        await answer_callback(callback)
        if message is None:
            return
        card = await container.query_cards.load(user_id, message.chat.id)
        await show(message, container, container.query_cards.ask(card, field_name))

    @router.callback_query(F.data == card_view.QC_SKIP)
    async def skip_field(callback: CallbackQuery, container: Container, user_id: int) -> None:
        message = callback_message(callback)
        await answer_callback(callback, card_view.SKIPPED_ANSWER)
        if message is None:
            return
        card = await container.query_cards.load(user_id, message.chat.id)
        await show(message, container, container.query_cards.skip(card))

    @router.callback_query(F.data == card_view.QC_CANCEL)
    async def cancel_ask(callback: CallbackQuery, container: Container, user_id: int) -> None:
        message = callback_message(callback)
        await answer_callback(callback)
        if message is None:
            return
        card = await container.query_cards.load(user_id, message.chat.id)
        await show(message, container, container.query_cards.cancel_ask(card))

    @router.callback_query(F.data.in_({card_view.QC_TEN_PASSPORT, card_view.QC_TEN_PHONE}))
    async def resolve_ten(callback: CallbackQuery, container: Container, user_id: int) -> None:
        message = callback_message(callback)
        await answer_callback(callback)
        if message is None:
            return
        card = await container.query_cards.load(user_id, message.chat.id)
        applied = container.query_cards.resolve_ten(
            card, as_passport=callback.data == card_view.QC_TEN_PASSPORT
        )
        await show(message, container, applied.card, notice=applied.notice)

    @router.callback_query(F.data == card_view.QC_NEW_PERSON)
    async def new_person(callback: CallbackQuery, container: Container, user_id: int) -> None:
        """«Другой человек» — очистить и положить присланное ФИО в чистую карточку."""
        message = callback_message(callback)
        await answer_callback(callback)
        if message is None:
            return
        card = await container.query_cards.load(user_id, message.chat.id)
        name = container.query_cards.take_pending_name(card)
        fresh = await container.query_cards.wipe(user_id, message.chat.id)
        if name is not None:
            fresh.last_name = name.last_name
            fresh.first_name = name.first_name
            fresh.middle_name = name.middle_name
        await show(message, container, fresh)

    @router.callback_query(F.data == card_view.QC_FIX_NAME)
    async def fix_name(callback: CallbackQuery, container: Container, user_id: int) -> None:
        """«Это исправление» — переписать ФИО, всё остальное оставить."""
        message = callback_message(callback)
        await answer_callback(callback)
        if message is None:
            return
        card = await container.query_cards.load(user_id, message.chat.id)
        name = container.query_cards.take_pending_name(card)
        if name is not None:
            card.last_name = name.last_name
            card.first_name = name.first_name
            card.middle_name = name.middle_name
        await show(message, container, card)

    @router.callback_query(F.data == card_view.QC_WIPE)
    async def wipe_card(callback: CallbackQuery, container: Container, user_id: int) -> None:
        message = callback_message(callback)
        await answer_callback(callback, card_view.WIPED)
        if message is None:
            return
        fresh = await container.query_cards.wipe(user_id, message.chat.id)
        # Очистили — значит следующий должник, а следующий должник начинается с
        # тех же трёх вопросов. Иначе после «Новая проверка» оператор получал бы
        # пустую форму с восемью кнопками и никакого «с чего начать».
        container.query_cards.begin_steps(fresh)
        await show(message, container, fresh)

    @router.callback_query(F.data == card_view.QC_RUN)
    async def run(callback: CallbackQuery, container: Container, user_id: int) -> None:
        message = callback_message(callback)
        if message is None:
            await answer_callback(callback)
            return
        card = await container.query_cards.load(user_id, message.chat.id)
        if not card.runnable:
            await callback.answer(card_view.NOTHING_TO_RUN, show_alert=True)
            return
        if card.last_run_hash and card.last_run_hash == container.query_cards.run_hash(card):
            # Тот же набор полей — тот же ответ за те же деньги. «Обновить» под
            # отчётом существует отдельно и говорит о себе честно.
            await callback.answer(card_view.NOTHING_CHANGED, show_alert=True)
            return
        await answer_callback(callback)
        await run_card(message, container, card, user_id)

    # ------------------------------------------------- старые кнопки отчёта

    @router.callback_query(F.data.startswith(f"{PERSON_ADD_PREFIX}:"))
    async def offer_field(callback: CallbackQuery, container: Container, user_id: int) -> None:
        """``padd:<поле>:<токен>`` под отчётами, отправленными до карточки.

        Такая кнопка живёт в чате бесконечно, и убить её обработчик значило бы
        оставить в истории молчащие кнопки. Субъект из ``SubjectStore``
        вливается в карточку — так старый отчёт и новая карточка становятся
        одним разговором, а не двумя.
        """
        parts = (callback.data or "").split(":", maxsplit=2)
        message = callback_message(callback)
        await answer_callback(callback)
        if message is None or len(parts) < 3:
            return
        field_name, token = parts[1], parts[2]
        subject = container.subject_store.get(token)
        if subject is None:
            await message.answer(STALE_TOKEN)
            return

        card = await container.query_cards.load(user_id, message.chat.id)
        _merge_subject(card, subject)
        if field_name == FIELD_REGION:
            await message.answer(ASK_REGION, reply_markup=region_keyboard(token))
            await container.query_cards.save(card)
            return
        field = _LEGACY_FIELDS.get(field_name)
        card.awaiting_field = field.value if field else None
        # Карточка приходит новым сообщением: старый отчёт остаётся выше, а
        # править надо здесь и сейчас.
        card.card_message_id = None
        container.query_cards.forget_screen(card)
        await show(message, container, card)

    @router.callback_query(F.data.startswith(f"{REGION_PREFIX}:"))
    async def receive_region(callback: CallbackQuery, container: Container, user_id: int) -> None:
        """Регион сузит поиск ФССП. Токен субъекта едет в самой кнопке.

        Состояния под него больше нет, а класть токен в карточку незачем: выбор
        региона относится к уже полученному отчёту, а не к тому, что собирают.
        """
        parts = (callback.data or "").split(":")
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return
        subject = container.subject_store.get(parts[2]) if len(parts) > 2 else None
        if subject is None:
            await message.answer(STALE_TOKEN)
            return
        narrowed = subject.model_copy(update={"regions": regions_for(parts[1])})
        await run_and_send_report(message, container, narrowed, user_id=user_id)

    return router


ASK_REGION = (
    "Сейчас ищу по всем регионам.\n"
    "Выбор региона сузит поиск ФССП: производства из других регионов в отчёт "
    "не попадут, а «Москва + МО» — это два платных запроса вместо одного."
)


def regions_for(choice: str) -> tuple[str, ...]:
    """``Москва + МО`` прогоняет оба региона и склеивает результаты."""
    if choice == REGION_COMBINED:
        return (Region.MOSCOW.value, Region.MOSCOW_OBLAST.value)
    try:
        return (Region(choice).value,)
    except ValueError:
        return (Region.OTHER.value,)


def _merge_subject(card: Card, subject: SearchSubject) -> None:
    """Влить в карточку то, с чем прошёл прежний прогон.

    Только пустые поля: карточка новее отчёта, и затирать её тем, что было час
    назад, значило бы отменить правку оператора.
    """
    if subject.name is not None and not card.last_name:
        card.last_name = subject.name.last_name
        card.first_name = subject.name.first_name
        card.middle_name = subject.name.middle_name
    card.birth_date = card.birth_date or subject.birth_date
    card.inn = card.inn or subject.inn
    if subject.vehicle is not None:
        card.plate = card.plate or subject.vehicle.plate
        card.vin = card.vin or subject.vehicle.vin


__all__ = ["absorb", "build_router", "run_card", "run_notes", "show"]
