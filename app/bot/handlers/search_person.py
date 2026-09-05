"""Поиск по человеку: одна строка вместо пяти вопросов.

Раньше бот вёл допрос — ФИО, дата рождения, телефон, регион, паспорт, — и
четыре шага из пяти можно было пропустить кнопкой. То есть он требовал того, без
чего прекрасно обходится, и брал за это семь взаимодействий на каждого
должника. Теперь оператор пишет одной строкой всё, что у него есть, в любом
порядке, :func:`app.bot.identifiers.parse_query` разбирает её, и проверка идёт
с тем, что дали.

Из этого следуют четыре правила, и каждое из них — ответ на конкретную жалобу.

**Понятое не переспрашивается.** Всё, что разобрано из строки, в вопросы больше
не попадает. Ноль нажатий на типичном вводе «Тестов Андрей Сергеевич
15.03.1980»: одно сообщение оператора, одно сообщение бота, которое на месте
превращается в карточку.

**Полнота не блокирует проверку.** Не хватает ИНН — три источника отвечают
«нечем спросить», это видно в карточке и в отчёте, и добрать недостающее
предлагается кнопкой ПОСЛЕ результата (:mod:`app.bot.report_actions`), а не
допросом до него. Регион и телефон не спрашиваются никогда: пустой ``regions``
уже означает «все регионы», а по телефону во внешних реестрах не ищут вовсе.

**Блокируют ровно два случая, и оба — не про полноту.** Искать нечего вовсе
(ни ФИО, ни ИНН, ни госномера) — и данные противоречивы: оператор дал дату, но
она не дата, либо десять цифр с девятки одинаково читаются паспортом и
телефоном. Молча выбросить и то и другое значило бы соврать: исправление стоит
одного символа, а второй прогон — ещё одних денег.

**Пропущенное не притворяется проверенным.** Ни одна ветка здесь не выкидывает
источник из прогона. Источник, которому нечем спросить, вызывается и честно
отказывается — иначе из отчёта пропала бы и строка «не проверено», и снижение
уверенности.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from datetime import date

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import report_actions
from app.bot.common import answer_callback, callback_message, run_and_send_report
from app.bot.identifiers import ParsedQuery, ProblemKind, TenDigits, parse_query
from app.bot.keyboards import (
    MENU_PREFIX,
    REGION_COMBINED,
    REGION_PREFIX,
    cancel_keyboard,
    region_keyboard,
)
from app.bot.report_actions import (
    FIELD_BIRTH_DATE,
    FIELD_INN,
    FIELD_PASSPORT,
    FIELD_REGION,
    PERSON_ADD_PREFIX,
)
from app.bot.states import PersonSearch
from app.container import Container
from app.domain.enums import Region, SearchType
from app.domain.identity import (
    INN_ENTITY_LENGTH,
    INN_INDIVIDUAL_LENGTH,
    PASSPORT_LENGTH,
    SearchSubject,
    VehicleDescriptor,
    normalize_passport,
)
from app.utils.dates import parse_date
from app.utils.masking import mask_passport

# ---------------------------------------------------------------- тексты
#
# Все они короткие и говорят, что откроется, а не что «требуется». Оператор
# читает их с телефона на бегу и между сотней должников.

ASK_QUERY = (
    "Пришлите одной строкой всё, что есть о должнике: ФИО, дату рождения, ИНН — "
    "в любом порядке.\n\n"
    "Пример: Иванов Иван Иванович 01.01.1985 770912345601\n\n"
    "Чего не знаете — не пишите. Спрошу только то, без чего проверка не запустится."
)

NOTHING_PARSED = (
    "Не нашёл в строке ни ФИО, ни ИНН, ни госномера.\n\n"
    "Пришлите фамилию и имя целиком — и, если есть, дату рождения и ИНН. "
    "Одной строкой, в любом порядке.\n"
    "Пример: Иванов Иван Иванович 01.01.1985 770912345601"
)

BAD_NAME_TAIL = (
    "Пришлите ФИО ещё раз — фамилия, имя, при желании отчество.\n"
    "Если ФИО не разбирается, но есть ИНН физлица, пришлите его: "
    f"{INN_INDIVIDUAL_LENGTH} цифр откроют банкротство, ИП и арбитраж без ФИО."
)

BAD_DATE_TAIL = (
    "Пришлите дату в формате ДД.ММ.ГГГГ.\n"
    "Или проверю без неё: тогда ФССП и залоги не опрашиваются — "
    "по одному ФИО они вернут чужие производства."
)

AMBIGUOUS_TEN = (
    "«{token}» — это паспорт или телефон?\n"
    "Десять цифр с девятки бывают и серией паспорта (90xx, 92xx), "
    "и мобильным без кода страны."
)

ASK_INN = (
    f"Пришлите ИНН физлица — {INN_INDIVIDUAL_LENGTH} цифр.\n\n"
    "Он откроет банкротство (ЕФРСБ), статус ИП и связи с юрлицами (ФНС) и арбитраж.\n"
    "Проверка запустится заново — это ещё три платных запроса."
)
BAD_INN = f"Нужно ровно {INN_INDIVIDUAL_LENGTH} цифр."
BAD_INN_ENTITY = (
    f"{{value}} — это ИНН организации. Человека ищут по {INN_INDIVIDUAL_LENGTH} цифрам."
)

ASK_BIRTH = (
    "Дата рождения в формате ДД.ММ.ГГГГ.\n\n"
    "Она откроет ФССП и залоги ФНП — оба источника без неё не ищут.\n"
    "Проверка запустится заново."
)
BAD_BIRTH = "Не разобрал дату. Формат: ДД.ММ.ГГГГ."

# Свой текст, не ASK_PASSPORT из search_misc: там паспорт действительно никуда
# не уходит, здесь — уходит, и обещать обратное нельзя.
ASK_PASSPORT_FOR_INN = (
    f"Серия и номер паспорта ({PASSPORT_LENGTH} цифр).\n\n"
    "По паспорту ФНС выдаёт ИНН, а без ИНН три источника — банкротство,\n"
    "статус ИП и арбитраж — не проверяются вовсе.\n"
    "Серия и номер уходят в ФНС через агрегатор и не сохраняются в базе.\n"
    "Ваше сообщение с номером я удалю.\n"
    "Запрос платный: повторное нажатие — ещё одна оплата."
)
ASK_PASSPORT_FOR_INN_STORED = (
    f"Серия и номер паспорта ({PASSPORT_LENGTH} цифр).\n\n"
    "По паспорту ФНС выдаёт ИНН, а без ИНН три источника — банкротство,\n"
    "статус ИП и арбитраж — не проверяются вовсе.\n"
    "Серия и номер уходят в ФНС через агрегатор и сохраняются в базе,\n"
    "потому что включён STORE_SENSITIVE_IDENTIFIERS.\n"
    "Ваше сообщение с номером я удалю.\n"
    "Запрос платный: повторное нажатие — ещё одна оплата."
)
BAD_PASSPORT_FOR_INN = f"Нужно ровно {PASSPORT_LENGTH} цифр (серия и номер). Попробуйте ещё раз."

ASK_REGION = (
    "Сейчас ищу по всем регионам.\n"
    "Выбор региона сузит поиск ФССП: производства из других регионов в отчёт "
    "не попадут, а «Москва + МО» — это два платных запроса вместо одного."
)

STALE_TOKEN = "Данные устарели, запустите поиск заново."

#: Проверка без даты рождения, о которой оператора предупредили. Едет в карточку
#: вместе с отчётом, а не только в сообщение прогресса: «я выбросил вашу дату» —
#: ровно то утверждение, которое обязано пережить правку сообщения.
SKIPPED_DATE_NOTE = "Проверяю без даты рождения — ФССП и залоги останутся неопрошенными."

#: Минимум букв в слове, чтобы счесть его попыткой написать имя. «asdf» — да,
#: «?» и эмодзи — нет, и это разница между «поправьте ФИО» и «я вообще ничего
#: не понял».
_NAME_ATTEMPT_LETTERS = 2


# ---------------------------------------------------------------- субъект


def build_subject(
    parsed: ParsedQuery,
    *,
    regions: tuple[str, ...] = (),
    birth_date: date | None = None,
) -> SearchSubject:
    """Собрать субъект из разобранной строки.

    ``regions`` пуст по умолчанию, и это не заглушка: ``fssp._region_codes``
    читает пустой кортеж как ``ALL_REGIONS_CODE``, то есть «все регионы» одним
    запросом. Спрашивать регион шагом значило бы менять один платный запрос на
    два и терять чужие производства ради этого.

    Тип поиска выбирается по тому, что нашлось. Голый госномер или VIN — это не
    человек: сделать его ``person`` значило бы отправить в ФССП субъект без ФИО
    и получить «нужно ФИО» там, где вопрос был про машину.
    """
    vehicle = (
        VehicleDescriptor(plate=parsed.plate, vin=parsed.vin)
        if (parsed.plate or parsed.vin)
        else None
    )
    person_fields = (parsed.name, parsed.inn, parsed.passport, parsed.phone)
    if vehicle is not None and not any(person_fields):
        search_type = SearchType.VIN if parsed.vin else SearchType.VEHICLE_PLATE
        return SearchSubject(search_type=search_type.value, vehicle=vehicle, regions=regions)

    return SearchSubject(
        search_type=SearchType.PERSON.value,
        name=parsed.name,
        birth_date=birth_date if birth_date is not None else parsed.birth_date,
        phone=parsed.phone,
        inn=parsed.inn,
        passport=parsed.passport,
        vehicle=vehicle,
        regions=regions,
    )


def regions_for(choice: str) -> tuple[str, ...]:
    """``Москва + МО`` прогоняет оба региона и склеивает результаты."""
    if choice == REGION_COMBINED:
        return (Region.MOSCOW.value, Region.MOSCOW_OBLAST.value)
    try:
        return (Region(choice).value,)
    except ValueError:
        return (Region.OTHER.value,)


def _ask_passport_text(container: Container) -> str:
    """Последняя фраза текста зависит от того, правдива ли она."""
    if container.settings.store_sensitive_identifiers:
        return ASK_PASSPORT_FOR_INN_STORED
    return ASK_PASSPORT_FOR_INN


def _looks_like_a_name_attempt(parsed: ParsedQuery) -> bool:
    """Пытался ли человек написать имя — или прислал мусор.

    От ответа зависит, что он услышит: разбор ФИО умеет объяснить своё
    возражение («нужны как минимум фамилия и имя»), и подменять это объяснение
    общей фразой про «ни ФИО, ни ИНН» — потеря.
    """
    return any(
        sum(1 for char in word if char.isalpha()) >= _NAME_ATTEMPT_LETTERS
        for word in parsed.leftover
    )


# ---------------------------------------------------------------- разбор строки


async def handle_line(
    message: Message,
    state: FSMContext,
    container: Container,
    user_id: int,
    *,
    raw: str,
    ten_digits_as: TenDigits | None = None,
    skip_date: bool = False,
    birth_date: date | None = None,
) -> None:
    """Единственная воронка: строка на входе, отчёт или один вопрос на выходе.

    Все уточнения возвращаются сюда же и перечитывают исходную строку целиком,
    а не дописывают полуразобранное состояние. Перечитать дешевле: держать
    между сообщениями частичный разбор — значит завести второй источник правды
    рядом с самой строкой.
    """
    parsed = parse_query(raw, ten_digits_as=ten_digits_as)

    if parsed.ambiguity is not None:
        await state.set_state(PersonSearch.waiting_field)
        await state.update_data(field="ten", raw=raw)
        await message.answer(
            AMBIGUOUS_TEN.format(token=parsed.ambiguity.token),
            reply_markup=report_actions.ten_digits_keyboard(),
        )
        return

    bad_date = parsed.problem(ProblemKind.BAD_DATE)
    if bad_date is not None and birth_date is None and not skip_date:
        # Единственный блок, кроме «искать нечего». Дату оператор дал — молча
        # выбросить её значит соврать, а вопрос стоит одного символа против
        # второго платного прогона.
        await state.set_state(PersonSearch.waiting_field)
        await state.update_data(
            field=FIELD_BIRTH_DATE,
            raw=raw,
            ten=ten_digits_as.value if ten_digits_as else None,
        )
        await message.answer(
            f"{bad_date.text}.\n\n{BAD_DATE_TAIL}",
            reply_markup=report_actions.bad_date_keyboard(),
        )
        return

    if not parsed.has_subject:
        await state.set_state(PersonSearch.waiting_query)
        if parsed.name_error and _looks_like_a_name_attempt(parsed):
            await message.answer(
                f"{parsed.name_error}\n\n{BAD_NAME_TAIL}", reply_markup=cancel_keyboard()
            )
        else:
            await message.answer(NOTHING_PARSED, reply_markup=cancel_keyboard())
        return

    notes = [
        problem.text for problem in parsed.problems if problem.kind is not ProblemKind.BAD_DATE
    ]
    if bad_date is not None and birth_date is None:
        notes.append(SKIPPED_DATE_NOTE)
    if parsed.name_error and _looks_like_a_name_attempt(parsed):
        # ФИО не разобралось, но ИНН есть — проверка идёт, и оператор должен
        # знать, что имя в неё не поехало.
        notes.append(f"ФИО не разобрал: {parsed.name_error} Проверяю без него.")

    await state.clear()
    subject = build_subject(parsed, birth_date=birth_date)
    await run_and_send_report(message, container, subject, user_id=user_id, notes=notes)


# ---------------------------------------------------------------- дополнение


async def _rerun_with(
    message: Message,
    state: FSMContext,
    container: Container,
    user_id: int,
    subject: SearchSubject,
    *,
    notes: Sequence[str] = (),
) -> None:
    """Перезапуск после добора поля.

    ``force_refresh`` здесь не нужен и был бы вредным: изменённое поле входит в
    ``build_query_hash``, поэтому кэш и так не поднимется, а флаг заодно сбросил
    бы кэш прежнего, ещё живого запроса.
    """
    await state.clear()
    await run_and_send_report(message, container, subject, user_id=user_id, notes=notes)


async def _stored_subject(
    state: FSMContext, container: Container
) -> tuple[SearchSubject | None, dict[str, object]]:
    data = await state.get_data()
    token = data.get("token")
    subject = container.subject_store.get(str(token)) if token else None
    return subject, data


# ---------------------------------------------------------------- роутеры


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="search_person")

    @router.callback_query(F.data == f"{MENU_PREFIX}:{SearchType.PERSON.value}")
    async def start_person_search(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(PersonSearch.waiting_query)
        message = callback_message(callback)
        if message:
            await message.answer(ASK_QUERY, reply_markup=cancel_keyboard())
        await answer_callback(callback)

    @router.message(PersonSearch.waiting_query, F.text)
    async def receive_query(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await handle_line(message, state, container, user_id, raw=message.text or "")

    # ------------------------------------------------------- десять цифр

    @router.callback_query(
        PersonSearch.waiting_field,
        F.data.in_({report_actions.TEN_AS_PASSPORT, report_actions.TEN_AS_PHONE}),
    )
    async def resolve_ten_digits(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        data = await state.get_data()
        raw = str(data.get("raw") or "")
        choice = (
            TenDigits.PASSPORT
            if callback.data == report_actions.TEN_AS_PASSPORT
            else TenDigits.PHONE
        )
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return
        await handle_line(message, state, container, user_id, raw=raw, ten_digits_as=choice)

    # ------------------------------------------------------- дата

    @router.callback_query(PersonSearch.waiting_field, F.data == report_actions.RUN_WITHOUT_DATE)
    async def run_without_date(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        data = await state.get_data()
        raw = str(data.get("raw") or "")
        ten = data.get("ten")
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return
        await handle_line(
            message,
            state,
            container,
            user_id,
            raw=raw,
            ten_digits_as=TenDigits(str(ten)) if ten else None,
            skip_date=True,
        )

    # ------------------------------------------------------- регион

    @router.callback_query(PersonSearch.waiting_field, F.data.startswith(f"{REGION_PREFIX}:"))
    async def receive_region(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        subject, _ = await _stored_subject(state, container)
        choice = (callback.data or "").split(":", maxsplit=1)[-1]
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return
        if subject is None:
            await state.clear()
            await message.answer(STALE_TOKEN)
            return
        narrowed = subject.model_copy(update={"regions": regions_for(choice)})
        await _rerun_with(message, state, container, user_id, narrowed)

    # ------------------------------------------------------- добор поля

    @router.callback_query(F.data.startswith(f"{PERSON_ADD_PREFIX}:"))
    async def offer_field(callback: CallbackQuery, state: FSMContext, container: Container) -> None:
        parts = (callback.data or "").split(":", maxsplit=2)
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None or len(parts) < 3:
            return
        field, token = parts[1], parts[2]
        if container.subject_store.get(token) is None:
            await message.answer(STALE_TOKEN)
            return

        await state.set_state(PersonSearch.waiting_field)
        await state.update_data(field=field, token=token, raw=None)
        if field == FIELD_REGION:
            await message.answer(ASK_REGION, reply_markup=region_keyboard())
            return
        prompts = {
            FIELD_INN: ASK_INN,
            FIELD_BIRTH_DATE: ASK_BIRTH,
            FIELD_PASSPORT: _ask_passport_text(container),
        }
        await message.answer(
            prompts.get(field, ASK_QUERY), reply_markup=report_actions.add_keyboard()
        )

    @router.message(PersonSearch.waiting_field, F.text)
    async def receive_field(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        data = await state.get_data()
        field = str(data.get("field") or "")
        text = (message.text or "").strip()

        if field == FIELD_BIRTH_DATE and data.get("raw"):
            # Правка даты ДО прогона: строка перечитывается целиком, дата
            # подставляется вместо той, что не разобралась.
            corrected = parse_date(text)
            if corrected is None:
                await message.answer(BAD_BIRTH, reply_markup=report_actions.bad_date_keyboard())
                return
            ten = data.get("ten")
            await handle_line(
                message,
                state,
                container,
                user_id,
                raw=str(data.get("raw") or ""),
                ten_digits_as=TenDigits(str(ten)) if ten else None,
                birth_date=corrected,
            )
            return

        if field == "ten":
            # Оператор написал вместо того, чтобы нажать. Значит это новая
            # строка, а не ответ, — разбираем её как новую.
            await handle_line(message, state, container, user_id, raw=text)
            return

        subject, _ = await _stored_subject(state, container)
        if subject is None:
            await state.clear()
            await message.answer(STALE_TOKEN)
            return

        if field == FIELD_INN:
            await _receive_inn(message, state, container, user_id, subject, text)
            return
        if field == FIELD_BIRTH_DATE:
            await _receive_birth_date(message, state, container, user_id, subject, text)
            return
        if field == FIELD_PASSPORT:
            await _receive_passport(message, state, container, user_id, subject, text)
            return

        # Поле неизвестно — состояние из прошлой версии бота после рестарта.
        await state.clear()
        await message.answer(STALE_TOKEN)

    return router


async def _receive_inn(
    message: Message,
    state: FSMContext,
    container: Container,
    user_id: int,
    subject: SearchSubject,
    text: str,
) -> None:
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) == INN_ENTITY_LENGTH:
        # Десять цифр — юрлицо. Отправить их как ``innfiz`` значит купить
        # отклонённый (и всё равно оплаченный) вызов вместо честного ответа.
        await message.answer(
            BAD_INN_ENTITY.format(value=digits), reply_markup=report_actions.add_keyboard()
        )
        return
    if len(digits) != INN_INDIVIDUAL_LENGTH:
        await message.answer(BAD_INN, reply_markup=report_actions.add_keyboard())
        return
    await _rerun_with(
        message, state, container, user_id, subject.model_copy(update={"inn": digits})
    )


async def _receive_birth_date(
    message: Message,
    state: FSMContext,
    container: Container,
    user_id: int,
    subject: SearchSubject,
    text: str,
) -> None:
    parsed_date = parse_date(text)
    if parsed_date is None:
        await message.answer(BAD_BIRTH, reply_markup=report_actions.add_keyboard())
        return
    await _rerun_with(
        message, state, container, user_id, subject.model_copy(update={"birth_date": parsed_date})
    )


async def _receive_passport(
    message: Message,
    state: FSMContext,
    container: Container,
    user_id: int,
    subject: SearchSubject,
    text: str,
) -> None:
    passport = normalize_passport(text)
    if passport is None:
        await message.answer(BAD_PASSPORT_FOR_INN, reply_markup=report_actions.add_keyboard())
        return
    # Убираем сообщение оператора, чтобы номер не остался в истории чата.
    # Best-effort: в группе на это нужны права администратора.
    with suppress(Exception):
        await message.delete()
    await message.answer(f"Проверяю с паспортом {mask_passport(passport)}…")
    await _rerun_with(
        message, state, container, user_id, subject.model_copy(update={"passport": passport})
    )


def build_free_text_router() -> Router:
    """Ловит строку, присланную без единого нажатия, — типичный случай.

    Регистрируется в корне ПОСЛЕДНИМ и только с ``StateFilter(None)``. Оба
    условия обязательны: ``search_person`` включается раньше, чем поиск по
    госномеру, VIN, адресу и договору, и без фильтра по пустому состоянию этот
    хэндлер съедал бы их ввод. Только ``F.text`` — документы остаются импорту,
    команды исключены явно.
    """
    router = Router(name="search_person_free_text")

    @router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
    async def receive_free_line(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await handle_line(message, state, container, user_id, raw=message.text or "")

    return router
