"""Регрессии по критическим находкам ревью от 07.09.2026.

Файл отдельный намеренно. Каждый тест здесь стоит за конкретным дефектом,
который уже случился на стенде и стоил бы денег или неверного решения о
госпошлине на живых данных; собранные вместе, они читаются как список того, что
нельзя сломать обратно. Разложить их по тематическим файлам значило бы потерять
эту связь: в ``test_batch.py`` тест про двойной тап выглядит одним из двадцати.

Дефекты, за которыми стоят тесты:

1. Двойной тап по «Запустить» — два прогона, каждый платит за всех должников.
2. Второй должник подряд — карточка предыдущего, отчёт про другого человека.
3. Один телефон на нескольких людей — дата рождения и долг первой попавшейся.
4. Оконченные производства ФССП исчезали из отчёта и давали бонус к баллу.
5. Повторный платный прогон («Обновить») списывал без вопроса.

Следом — HIGH из того же ревью, взятые в порядке ТЗ:

6. «Отменено» не отменяло заданный карточкой вопрос.
7. Веб-CSV очереди отдавал по ссылке всю базу с ФИО и датами рождения.
8. CSV не обезвреживал ячейки, которые Excel читает как формулу.
9. ``/audit`` показывал любому допущенному, кто из коллег что проверял.
10. Очередь писалась страницами по 200: на реальной выгрузке страница стояла
    пустой весь прогон и объявляла его оборвавшимся.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest
from aiogram import Bot, Dispatcher

from app.bot.handlers.import_csv import render_import_report
from app.config import Settings
from app.container import Container
from app.db.repository import DebtorRepository
from app.db.session import Database
from app.domain.enums import ProviderName, ProviderStatus, SearchType
from app.domain.identity import NameParseError, PersonName, SearchSubject
from app.domain.models import DebtorReport, ProviderResult
from app.domain.verdict import Verdict
from app.providers.phone_bridge import PhoneNameProvider, PhoneNameResult
from app.services.batch import BatchAlreadyRunningError, BatchService
from app.services.scoring import RecoveryScoreEngine
from tests.conftest import make_proceeding
from tests.test_scoring import build_report

from .bot_harness import (
    CHAT_ID,
    OPERATOR_ID,
    SentMessages,
    dispatcher_for,
    feed,
    make_callback,
    make_message,
)

# ---------------------------------------------------------------- 1. двойной прогон


async def test_two_presses_start_one_run(container: Container) -> None:
    """Прогон платит за всю выгрузку, поэтому второй — это вторая оплата.

    Замок стоит в сервисе, а не только в хендлере: у дефекта два входа. Двойной
    тап одного человека приходит в тот же процесс, а второй владелец приходит со
    своим состоянием FSM, и хендлерная защита его не видит вовсе.

    Ответ второму — отказ, а не очередь. Замок, который просто ждёт, здесь хуже
    дефекта: после первого прогона молча начался бы второй, за те же деньги.
    """
    await container.import_service.import_file(container.settings.internal_csv_path)

    first, second = await asyncio.gather(
        container.batch_service.run(telegram_user_id=OPERATOR_ID),
        container.batch_service.run(telegram_user_id=OPERATOR_ID),
        return_exceptions=True,
    )

    outcomes = [first, second]
    refused = [item for item in outcomes if isinstance(item, BatchAlreadyRunningError)]
    finished = [item for item in outcomes if not isinstance(item, BaseException)]
    assert len(finished) == 1, "два нажатия оплатили выгрузку дважды"
    assert len(refused) == 1
    assert refused[0].run_id == finished[0].run_id


async def test_a_finished_run_does_not_block_the_next_one(container: Container) -> None:
    """Замок снимается: иначе один прогон в день и «почему кнопка молчит»."""
    await container.import_service.import_file(container.settings.internal_csv_path)

    await container.batch_service.run(telegram_user_id=OPERATOR_ID)
    again = await container.batch_service.run(telegram_user_id=OPERATOR_ID)

    assert again.run_id


# ---------------------------------------------------------------- 2. второй должник


async def test_the_next_debtor_starts_from_a_clean_card(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Вторая проверка подряд — про второго человека, а не про первого.

    Дефект был тихим и потому дорогим: карточка предыдущего должника
    открывалась заново, новый телефон ложился рядом с чужой фамилией, и отчёт
    выходил про прошлого человека — с его долгом и его пошлиной, подписанный
    номером нового.
    """
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич 12.03.1985"))
    await feed(dispatcher, bot, callback_query=make_callback("qc:run"))
    assert sent.contains("RECOVERY SCORE")

    sent.texts.clear()
    await feed(dispatcher, bot, message=make_message("Проверить человека"))

    assert not sent.contains("Тестов"), "открылась карточка предыдущего должника"


async def test_a_new_phone_after_a_report_is_a_new_person(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Телефон после законченной проверки начинает следующего должника.

    Остальные поля по-прежнему дописываются в ту же карточку («дошлите ИНН —
    перепроверю того же»): телефон здесь единственный ключ, с которого в этом
    боте начинается НОВЫЙ человек.
    """
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич 12.03.1985"))
    await feed(dispatcher, bot, callback_query=make_callback("qc:run"))

    sent.texts.clear()
    await feed(dispatcher, bot, message=make_message("89165550022"))

    assert not sent.contains("Фамилия: Тестов"), "номер лёг в карточку прошлого должника"


# ---------------------------------------------------------------- 3. общий телефон

TWO_PEOPLE_ONE_PHONE = "\n".join(
    [
        "ФИО,Телефон,Дата_рождения,Долг,Договор",
        "Петров Пётр Петрович,+79165550011,01.01.1980,500000,EV-1",
        "Сидорова Анна Ивановна,+79165550011,02.02.1990,3000,EV-2",
    ]
)


async def test_a_shared_phone_does_not_pick_a_random_person(container: Container) -> None:
    """Семейный номер в 1С — обычное дело, и он не должен решать за оператора.

    Точное попадание по телефону возвращало обе строки, а дальше вся цепочка —
    подстановка даты рождения, сумма долга, пошлина — брала первую попавшуюся.
    Отчёт выходил про Сидорову с долгом Петрова и советом заплатить 7 500 ₽
    пошлины вместо вердикта «несоразмерна».
    """
    await container.import_service.import_text(TWO_PEOPLE_ONE_PHONE)

    both = await container.search_service.lookup_internal(
        SearchSubject(search_type=SearchType.PERSON.value, phone="+79165550011")
    )
    assert len(both) == 2, "две строки на один номер — это две строки"

    narrowed = await container.search_service.lookup_internal(
        SearchSubject(
            search_type=SearchType.PERSON.value,
            phone="+79165550011",
            name=PersonName(last_name="Сидорова", first_name="Анна"),
        )
    )
    assert [record.full_name for record in narrowed] == ["Сидорова Анна Ивановна"]
    assert narrowed[0].birth_date == date(1990, 2, 2)


async def test_a_name_the_export_does_not_confirm_still_finds_the_row(
    container: Container,
) -> None:
    """Фильтр односторонний: расхождение с выгрузкой — факт, а не повод молчать.

    Девичья фамилия и опечатка в 1С — обычное дело, и выбросить единственную
    найденную строку из-за них значило бы превратить «нашли, но не сходится» в
    «не нашли».
    """
    await container.import_service.import_text(TWO_PEOPLE_ONE_PHONE)

    found = await container.search_service.lookup_internal(
        SearchSubject(
            search_type=SearchType.PERSON.value,
            phone="+79165550011",
            name=PersonName(last_name="Неизвестнова", first_name="Анна"),
        )
    )

    assert len(found) == 2


# ---------------------------------------------------------------- 4. оконченные ИП

WRITTEN_OFF = "Окончено 01.03.2026, ст. 46 ч.1 п.4"


def _closed_report(subject: SearchSubject, *, reason: str | None = WRITTEN_OFF) -> DebtorReport:
    closed = [
        make_proceeding(f"{index}/26/77001-ИП", active=False, status_text=reason)
        for index in range(1, 5)
    ]
    return build_report(subject, fssp=(ProviderStatus.SUCCESS, closed))


def test_closed_proceedings_do_not_earn_the_clean_bonus(person_subject: SearchSubject) -> None:
    """«Активных не найдено» — не то же самое, что «чисто».

    Четыре окончания по ст. 46 ч. 1 п. 4 значат, что пристав уже искал должника
    и его имущество и не нашёл. До правки эта запись не доезжала никуда: балл
    получал бонус «производств не найдено», а вердикт — довод за уплату
    пошлины по тому, с кого взыскивать нечего.
    """
    score = RecoveryScoreEngine().evaluate(_closed_report(person_subject))

    names = {factor.name for factor in score.factors}
    assert "no_enforcement" not in names
    assert "enforcement_written_off" in names
    assert next(f for f in score.factors if f.name == "enforcement_written_off").delta < 0


def test_a_plain_closure_only_removes_the_bonus(person_subject: SearchSubject) -> None:
    """Окончание фактическим исполнением — не повод штрафовать.

    Оно говорит ровно обратное «46-й»: деньги взыскать удалось. Бонус за
    «проверили и чисто» всё равно снимается — приставы у должника уже были.
    """
    report = _closed_report(person_subject, reason="Окончено фактическим исполнением")
    score = RecoveryScoreEngine().evaluate(report)

    names = {factor.name for factor in score.factors}
    assert "no_enforcement" not in names
    assert "enforcement_written_off" not in names
    assert next(f for f in score.factors if f.name == "enforcement_closed").delta == 0


def test_closed_proceedings_are_printed_in_the_report(person_subject: SearchSubject) -> None:
    """Найденное не должно выглядеть ненайденным — главный инвариант отчёта.

    Раздел печатал «активных производств не найдено», а список источников в том
    же отчёте — «ФССП, 4 зап.». Противоречие было видно прямо на странице,
    которую несут в суд.
    """
    from app.services.reporting import render_report
    from app.web.render import enforcement_section

    report = _closed_report(person_subject)

    text = render_report(report)
    assert "Оконченных производств: 4" in text
    assert "ст. 46 ч.1 п.4" in text

    html = enforcement_section(report)
    assert "Оконченные производства" in html
    assert "слабое совпадение с должником либо непрочитанный статус" not in html


# ---------------------------------------------------------------- 5. платный повтор


async def test_refresh_asks_before_it_spends(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """«Обновить» выглядит как чтение, а стоит как проверка.

    Кнопка идёт мимо кэша — в этом её назначение — и живёт под каждым прошлым
    отчётом бесконечно. При остатке в два десятка запросов четырёх случайных
    нажатий хватало, чтобы баланс кончился к показу заказчику.
    """
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич 12.03.1985"))
    await feed(dispatcher, bot, callback_query=make_callback("qc:run"))
    token = next(iter(container.subject_store._items))

    sent.texts.clear()
    await feed(dispatcher, bot, callback_query=make_callback(f"refresh:{token}"))

    assert sent.contains("Спросить источники заново")
    assert not sent.contains("RECOVERY SCORE"), "нажатие списало деньги без вопроса"

    await feed(dispatcher, bot, callback_query=make_callback(f"refreshgo:{token}"))
    assert sent.contains("RECOVERY SCORE")


async def test_the_daily_quota_is_off_until_the_owner_turns_it_on(container: Container) -> None:
    """Решение владельца: лимиты настраиваются, когда бота отдают заказчику.

    Механизм при этом готов и включается одной переменной — иначе «включим
    потом» означает «напишем потом», то есть никогда.
    """
    assert container.settings.daily_search_quota == 0


async def test_the_quota_stops_a_stranger_when_it_is_on(
    container: Container, bot: Bot, sent: SentMessages
) -> None:
    """Владелец без лимита — это его деньги; остальные считаются."""
    from dataclasses import replace

    from app.bot.common import within_quota

    limited = replace(
        container, settings=container.settings.model_copy(update={"daily_search_quota": 1})
    )
    stranger = 4242
    message = make_message("не важно", user_id=stranger).as_(bot)

    assert await within_quota(message, limited, stranger)

    async with limited.database.session() as session:
        from app.db.repository import SearchRepository

        await SearchRepository(session).create_request(
            telegram_user_id=stranger,
            search_type=SearchType.PERSON.value,
            normalized_query_hash="hash",
            masked_query="Тестов А.",
        )

    assert not await within_quota(message, limited, stranger)
    assert sent.contains("На сегодня проверки закончились")
    # Владельцу тот же лимит не мешает.
    owner_message = make_message("не важно", user_id=OPERATOR_ID).as_(bot)
    assert await within_quota(owner_message, limited, OPERATOR_ID)


# ------------------------------------------------------- 6. отмена, которая отменяет


async def test_cancel_takes_the_question_off_the_card(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """«Отменено» обязано отменять.

    Наследник теста, умершего вместе с диалогом FSM: состояние отменялось, а
    карточка оставалась стоять на шаге «фамилия». Оператор говорил «отмена»,
    получал «Отменено» — и следующее сообщение, каким бы оно ни было, молча
    ложилось в это поле. Здесь проверяется именно это: после отмены присланное
    слово не становится фамилией.

    Само собранное не стирается: это работа оператора, а отмена относится к
    вопросу, а не к карточке.
    """
    await feed(dispatcher, bot, message=make_message("Проверить человека"))
    await feed(dispatcher, bot, message=make_message("89165550033"))
    await feed(dispatcher, bot, message=make_message("/cancel"))
    assert sent.contains("Отменено")

    card = await container.query_cards.load(OPERATOR_ID, CHAT_ID)
    assert card.awaiting_field is None, "карточка осталась ждать ответа на отменённый вопрос"
    assert not card.guided
    # Телефон никуда не делся: отменили вопрос, а не проверку.
    assert card.phone_masked


# ------------------------------------------------------- 7-8. выгрузка очереди


def _queue_item(**fields: object):  # type: ignore[no-untyped-def]
    """Строка очереди с должником — ровно то, что уходит в CSV."""
    from app.db.models import BatchItem, Debtor

    debtor = Debtor(
        dedup_key="k",
        external_debtor_id="DEM-001",
        fio=str(fields.get("fio", "Тестов Андрей Сергеевич")),
        birth_date=date(1985, 3, 12),
        phone_masked="+7 (999) ***-**-01",
        contract_number=str(fields.get("contract", "EV-20481")),
        vehicle_plate="А123ВС77",
    )
    item = BatchItem(
        batch_run_id=1,
        debtor_id=1,
        verdict="order",
        verdict_order=1,
        headline="Долг бесспорный",
        confidence=90,
        score=71,
    )
    item.debtor = debtor
    return item


def test_the_shared_csv_does_not_hand_out_the_whole_base() -> None:
    """Страница маскирует ФИО, а кнопка рядом отдавала его целиком.

    Одна пересланная ссылка на очередь — это выгрузка базы должников заказчика:
    ФИО, дата рождения, договор и госномер на сотни человек. Маска на странице
    без маски в соседнем файле не защищает ничего.
    """
    from app.services.export import queue_to_csv

    shared = queue_to_csv([_queue_item()], mask_personal=True).decode("utf-8-sig")

    assert "Тестов Андрей Сергеевич" not in shared
    assert "12.03.1985" not in shared
    assert "А123ВС77" not in shared
    # Строку по-прежнему есть чем опознать: свой номер должника и договор.
    assert "DEM-001" in shared
    assert "EV-20481" in shared


def test_the_owner_file_stays_whole() -> None:
    """Владельцу в чат уходит полный файл: он идёт юристу, ради этого и написан.

    Получатель здесь — конкретный человек в Telegram, а не тот, у кого оказалась
    ссылка, и это единственная разница между двумя файлами.
    """
    from app.services.export import queue_to_csv

    own = queue_to_csv([_queue_item()]).decode("utf-8-sig")

    assert "Тестов Андрей Сергеевич" in own
    assert "12.03.1985" in own


def test_a_cell_that_looks_like_a_formula_is_defused() -> None:
    """Excel выполняет ячейку, начинающуюся с «=», «+», «-» или «@».

    Это не про злоумышленника: фамилия «-Оглы» или договор, начинающийся со
    знака, уже достаточны. Файл открывают в Excel, и формула из чужой строки в
    лучшем случае покажет ошибку вместо фамилии.
    """
    from app.services.export import queue_to_csv

    body = queue_to_csv([_queue_item(fio="=HYPERLINK(1)", contract="-ЭВ-1")]).decode("utf-8-sig")

    assert "'=HYPERLINK(1)" in body
    assert "'-ЭВ-1" in body


# ------------------------------------------------------- 9. журнал — владельцу


async def test_the_audit_log_is_not_for_everyone(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Журнал отвечает на вопрос про людей, а не про должников.

    Он печатает, кто из коллег какую проверку запускал и с каким результатом.
    Под открытым доступом («*») это получал вообще любой, кто нашёл бота.
    """
    employee = 222
    await feed(dispatcher, bot, message=make_message("/audit", user_id=employee))

    assert not sent.contains("события аудита")
    assert any("только для владельца" in text for text in sent.texts)

    sent.texts.clear()
    await feed(dispatcher, bot, message=make_message("/audit", user_id=OPERATOR_ID))
    assert sent.contains("аудита") or sent.contains("Журнал аудита пуст")


def test_the_audit_command_is_not_promised_to_an_employee() -> None:
    """Команда в синем меню, которая всем отвечает «нельзя», — обещание впустую."""
    from app.bot.commands import commands_for

    employee = [name for name, _ in commands_for(owner=False)]
    owner = [name for name, _ in commands_for(owner=True)]

    assert "audit" not in employee
    assert "audit" in owner


# ------------------------------------------------------- 10. очередь на ходу


async def test_the_queue_fills_up_while_the_run_is_going(container: Container) -> None:
    """Страница очереди обязана наполняться, пока прогон идёт.

    Строки писались одной транзакцией на страницу в 200 должников, то есть на
    любой реальной выгрузке заказчика — один раз, в самом конце. Всё это время
    страница показывала «0 из 220», а через десять минут объявляла живой прогон
    оборвавшимся и звала запустить заново. Послушавшийся оператор запускал
    второй прогон и платил за выгрузку дважды.
    """
    await container.import_service.import_file(container.settings.internal_csv_path)
    # Шаг записи привязан к шагу прогресса, и в демо-базе должников меньше, чем
    # он по умолчанию. Уменьшаем его, чтобы шесть строк дали несколько пачек —
    # на выгрузке заказчика в 220 человек это происходит само.
    service = BatchService(
        settings=container.settings.model_copy(update={"batch_progress_every": 2}),
        database=container.database,
        search_service=container.search_service,
    )
    seen: list[int] = []

    async def watch(progress: object) -> None:
        run_id = getattr(progress, "run_id", None)
        if run_id is None:
            return
        snapshot = await service.queue_snapshot(run_id)
        if snapshot is not None:
            seen.append(len(snapshot.items))

    summary = await service.run(telegram_user_id=OPERATOR_ID, progress=watch)

    assert summary.processed > 0
    # Хоть один снимок до конца прогона уже видел строки.
    assert any(count > 0 for count in seen[:-1]), f"очередь была пуста весь прогон: {seen}"


# ------------------------------------------------- 11. каждая нижняя кнопка жива


async def test_every_bottom_label_reaches_a_handler(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Подпись без обработчика становится фамилией должника.

    Нижняя клавиатура живёт на стороне Telegram и обновляется только со
    следующим /start, поэтому у всех, кто его не нажимал, кнопки остаются
    старыми. Любая подпись — нынешняя или снятая — приходит обычным текстом, и
    без точного совпадения доезжает до карточки запроса, которая разбирает её
    как данные о должнике: нажатие «Главное меню» отвечало вопросом «это другой
    человек или исправление?».

    Тест перебирает ВСЕ подписи, которые бот когда-либо ставил на клавиатуру, и
    требует от каждой осмысленного ответа. Он и поймал этот дефект.
    """
    from app.bot.keyboards import REPLY_BUTTONS

    for label in REPLY_BUTTONS:
        sent.texts.clear()
        await feed(dispatcher, bot, message=make_message(label))

        answered = sent.joined
        assert answered, f"«{label}» осталась без ответа"
        assert "это другой человек или исправление" not in answered, (
            f"«{label}» ушла в разбор свободного текста"
        )
        assert "Фамилия: " + label.split()[0] not in answered, f"«{label}» легла в поле карточки"


# ------------------------------------------- 12. «В меню» не съедает карточку


async def test_going_to_the_menu_keeps_the_card(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Карточка — собранная оператором работа, и меню её не переписывает.

    «В меню» стоит в том числе под карточкой. Пока меню показывалось правкой
    того же сообщения, нажатие превращало карточку с телефоном и ФИО в текст
    меню. Со стороны выглядело так, будто бот на введённый номер не ответил
    вовсе: нажатие инлайн-кнопки не оставляет в чате пузыря, поэтому шаг «я
    нажала В меню» в переписке не виден — виден только исчезнувший ответ.

    Проверяется именно способ доставки: меню приходит НОВЫМ сообщением, а не
    правкой чужого.
    """
    await feed(dispatcher, bot, message=make_message("79851982945"))
    assert sent.contains("Проверка должника"), "на номер не пришла карточка"

    before = len(sent.edits)
    await feed(dispatcher, bot, callback_query=make_callback("menu:home"))

    assert sent.contains("Вы в главном меню")
    assert len(sent.edits) == before, "меню переписало карточку вместо нового сообщения"


async def test_start_shows_two_buttons_under_the_input(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Две кнопки внизу — дословный ориентир владелицы, и их надо видеть.

    Клавиатура ехала отдельным сообщением, которое тут же удалялось: настройка
    на стороне Telegram оставалась, но клиент сворачивал её в значок «≡», и двух
    кнопок никто не видел. Теперь она едет на самом приветствии.
    """
    from aiogram.types import ReplyKeyboardMarkup

    await feed(dispatcher, bot, message=make_message("/start"))

    keyboards = [m for m in sent.markups if isinstance(m, ReplyKeyboardMarkup)]
    assert len(keyboards) == 1, "нижняя клавиатура не пришла"
    labels = [button.text for row in keyboards[0].keyboard for button in row]
    assert labels == ["Главное меню", "Проверить человека"]
    # Ровно одно сообщение: одна фраза при старте.
    assert len(sent.sends) == 1, f"на /start ушло больше одного сообщения: {sent.sends}"


# ------------------------------------------ 14. живая выгрузка: ФИО с частицей


def test_a_patronymic_particle_is_part_of_the_name() -> None:
    """«Ахмед оглы» — одно отчество, а не четвёртое слово.

    В паспорте частица стоит отдельным словом, поэтому такое ФИО состоит из
    четырёх слов, и строгий разбор отвергал его целиком. В выгрузке заказчика
    так записаны 72 человека из 2315. Цена отказа не в том, что имя выглядит
    непричёсанным: неразобранное ФИО не участвует в сверке с ответами
    источников, и найденное по такому должнику производство подписывается
    «слабое совпадение» — то есть найденное выглядит ненайденным.
    """
    from app.domain.identity import parse_fio

    name = parse_fio("Алиев Ислам Ахмед оглы")

    assert name.last_name == "Алиев"
    assert name.first_name == "Ислам"
    assert name.middle_name == "Ахмед оглы"
    assert name.has_middle_name

    # Регистр в выгрузке любой, а частиц несколько.
    assert parse_fio("МАМЕДОВА ЛЕЙЛА МАМЕД КЫЗЫ").middle_name == "Мамед кызы"

    # Четвёртое слово, не являющееся частицей, по-прежнему не угадывается.
    with pytest.raises(NameParseError):
        parse_fio("Тестов Андрей Сергеевич Петрович")


# ------------------------------ 15. живая выгрузка: строки одного должника


async def test_a_second_row_does_not_erase_what_the_first_one_knew(
    container: Container,
) -> None:
    """Внутри одного файла поздняя строка не затирает данные ранней.

    Модуль импорта обещает, что более бедная выгрузка не обнуляет уже известное,
    но обещание держалось только между импортами: внутри файла строки с
    одинаковым ключом заменялись целиком, и ИНН, указанный в первой строке
    человека, исчезал из-за второй, где его не было. ИНН — единственный ключ к
    банкротству, ИП и арбитражу, так что потеря стоит трёх разделов отчёта.
    """
    report = await container.import_service.import_text(
        "ФИО,Дата рождения,ИНН,Телефон,Госномер\n"
        "Тестов Андрей Сергеевич,15.03.1980,500100732259,+79991234501,А123ВС777\n"
        "Тестов Андрей Сергеевич,15.03.1980,,,В456ЕК750"
    )

    assert report.imported == 1
    async with container.database.session() as session:
        rows = await DebtorRepository(session).find_by_fio("Тестов Андрей Сергеевич")
    assert rows[0].inn == "500100732259"
    assert rows[0].phone_masked


async def test_every_car_of_a_repeat_debtor_survives_the_merge(
    container: Container,
) -> None:
    """Один должник, несколько задержаний — машины сохраняются все.

    Выгрузка взыскателя-эвакуатора это список задержаний, а не список людей:
    в живом файле 215 человек приезжают в нём по два-пять раз, у 97 из них
    машины разные. Схлопывать такие строки в одного должника правильно — платная
    проверка человека нужна одна, — но до этой правки выживал только последний
    госномер. Именно из машин складывается требование, с которым идут в суд.

    И это не «возможно, тёзка»: дата рождения совпала, разошлась только машина.
    Сто ложных тревог подряд владелец перестаёт читать вместе с настоящими.
    """
    report = await container.import_service.import_text(
        "ФИО,Дата рождения,Госномер\n"
        "Тестов Андрей Сергеевич,15.03.1980,А123ВС777\n"
        "Тестов Андрей Сергеевич,15.03.1980,В456ЕК750\n"
        "Тестов Андрей Сергеевич,15.03.1980,Е789МН197"
    )

    assert report.imported == 1
    assert report.skipped == 2
    assert report.merged_episodes == 2
    assert report.collapsed_conflicts == []

    async with container.database.session() as session:
        rows = await DebtorRepository(session).find_by_fio("Тестов Андрей Сергеевич")
    assert rows[0].vehicle_plates == "А123ВС777, В456ЕК750, Е789МН197"

    message = render_import_report(report)
    assert "не однофамильцы ли это" not in message
    assert "машины из них сохранены все" in message


async def test_a_namesake_without_a_birth_date_still_raises_the_alarm(
    container: Container,
) -> None:
    """Отличить нечем — тревога остаётся: слить двух людей дороже лишней строки.

    Разделение «тот же человек» / «возможно, тёзка» держится на дате рождения,
    договоре или коде должника. Без них одно голое ФИО — не опознание, и
    расхождение в данных обязано остаться тревогой, а не уехать в тихий счётчик
    повторных задержаний.
    """
    report = await container.import_service.import_text(
        "ФИО,Сумма долга\nТестов Андрей Сергеевич,1000\nТестов Андрей Сергеевич,2000"
    )

    assert report.merged_episodes == 0
    assert report.collapsed_conflicts == ["строки 2 и 3: «Тестов Андрей Сергеевич»"]


# ------------------------------- 16. живая выгрузка: тексты для оператора


async def test_the_import_report_speaks_russian_not_field_names(
    container: Container,
) -> None:
    """Отчёт об импорте читает человек, правящий выгрузку в Excel.

    «Нужно указать fio или contract_number» — это ответ программиста
    программисту: в файле нет колонок с такими названиями, и искать их
    бесполезно. На живой выгрузке эта строка выпадала 363 раза.
    """
    report = await container.import_service.import_text(
        "ФИО,Госномер,Сумма долга\n,А123ВС777,1000\nТестов Андрей Сергеевич,В456ЕК750,не-сумма"
    )
    message = render_import_report(report)

    assert "нет ни ФИО, ни номера договора" in message
    assert "Сумма долга: не распознана" in message
    for leaked in ("fio", "contract_number", "debt_amount", "vehicle_plate"):
        assert leaked not in message


def test_a_car_without_plates_is_not_an_unreadable_plate() -> None:
    """«Б/Н» — это запись об отсутствии номера, а не опечатка в нём.

    Тридцать машин без номера выглядели как тридцать ошибок ввода, и за ними
    терялись настоящие: иностранные номера и опечатки в регионе, которые
    оператор действительно может починить в выгрузке.
    """
    from app.providers.internal.csv_schema import DebtorRow, _parse_optional_plate

    for absent in ("Б/Н", "б\\н", "Б.Н", "БН", "Н/У", "без ГРЗ", "Б/Н МОПЕД"):
        row = DebtorRow()
        assert _parse_optional_plate(absent, row) is None
        assert row.warnings == [], f"«{absent}» поднял ложную тревогу"

    typo = DebtorRow()
    assert _parse_optional_plate("А12ВС777", typo) is None
    assert typo.warnings == ["Госномер: не распознан"]


async def test_a_new_address_is_a_move_not_a_namesake(container: Container) -> None:
    """Адрес разошёлся, ФИО и дата рождения — нет: это переезд, а не второй человек.

    На живой выгрузке все 83 предупреждения «проверьте, не однофамильцы ли это»
    разошлись ровно по адресу при совпавшей дате рождения. Полный тёзка с
    точностью до дня рождения — редкость; другая запись адреса в 1С — норма.
    Тревога, которая срабатывает 83 раза подряд впустую, не осторожность: после
    неё владелец пролистывает и ту, что настоящая.
    """
    report = await container.import_service.import_text(
        "ФИО,Дата рождения,Адрес,Госномер\n"
        "Тестов Андрей Сергеевич,15.03.1980,г. Москва ул. Ленина 1,А123ВС777\n"
        "Тестов Андрей Сергеевич,15.03.1980,Москва Ленина 1 кв 5,В456ЕК750"
    )

    assert report.collapsed_conflicts == []
    assert report.merged_episodes == 1

    # А расхождение по телефону при том же ФИО и дате — по-прежнему тревога.
    contradicting = await container.import_service.import_text(
        "ФИО,Дата рождения,Телефон,Сумма долга\n"
        "Тестова Мария Ивановна,20.07.1975,+79991234501,1000\n"
        "Тестова Мария Ивановна,20.07.1975,+79991234502,2000"
    )
    assert contradicting.collapsed_conflicts


# ------------------------- 15. смета обещала бесплатным то, за что прогон платит


async def test_the_estimate_never_promises_a_cache_that_will_not_serve(
    container: Container,
) -> None:
    """«Из кэша, бесплатно» — только про отчёт, который кэш действительно отдаст.

    Смета считала кэшированным любой запрос в пределах TTL, а выдача отказывалась
    подавать отчёт, в котором источник промолчал, и переспрашивала его заново — за
    деньги. Оператор видел «бесплатно» и платил. Для остановленного прогона, где
    недоспрошенных должников целая пачка, расхождение было гарантировано: именно
    там смету и читают перед тем, как доплатить за остаток.

    Починка не в том, чтобы поправить счётчик, а в том, что «годится ли кэш»
    перестало быть написанным дважды: правило живёт в запросе к базе, и обе
    стороны спрашивают одно и то же.
    """
    from sqlalchemy import update

    from app.db.models import SearchResult

    await container.import_service.import_text(
        "ФИО,Дата рождения,ИНН\nТестов Андрей Сергеевич,15.03.1980,500100732259"
    )
    await container.batch_service.run(telegram_user_id=OPERATOR_ID)

    cached = await container.batch_service.estimate()
    assert cached.cached == 1, "прогон обязан был закэшировать единственного должника"
    assert cached.to_query == 0

    # Так выглядит должник из прогона, который остановили или у которого источник
    # отвалился: отчёт в базе есть, но один источник так и не ответил.
    async with container.database.session() as session:
        await session.execute(
            update(SearchResult).values(provider_status=ProviderStatus.UNAVAILABLE.value)
        )
        await session.commit()

    honest = await container.batch_service.estimate()
    assert honest.cached == 0, "смета обещает бесплатно то, за что прогон заплатит"
    assert honest.to_query == 1


# ------------------------------ 18. пустая карточка вместо полотна инструкции


async def test_an_empty_card_shows_the_form_not_a_catalogue(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Прислали номер, а в выгрузке его нет — это форма, а не просьба текстом.

    Оператор писал номер и получал двадцать строк: заголовок, подпись, шесть
    строк формы, объяснение прочерка, строку о недостающих полях — и следом
    каталог из восьми «+ поле — что оно откроет». Те же восемь полей стоят
    кнопками прямо под сообщением, так что каталог дублировал клавиатуру
    словами.

    Ориентир владелицы называет это прямо, в разделе «что не брать»: «полотно
    инструкции в форме поиска — у нас один вопрос на экран». И там же про этот
    самый случай: если телефона нет в базе, показывается «та же карточка с
    пустыми полями и кнопками-полями, а не текстовая просьба».

    Что остаётся, тест проверяет отдельно: строка про смысл прочерка никуда не
    девается — это инвариант отчёта, а не украшение.
    """
    await feed(dispatcher, bot, message=make_message("89990000001"))

    card = sent.joined
    assert "Проверка должника" in card, "на номер не пришла карточка"
    assert "откроет ещё" not in card, "каталог опций вернулся в форму"
    assert "+ Дата рождения" not in card
    assert "+ Паспорт" not in card
    assert "Прочерк — это «я не спрашивал», а не «не нашли»." in card

    # Правило владелицы: из чего собран ответ, на общих экранах не называется.
    assert "1С" not in card


# ---------------- 19. ссылка назад в учётную систему заказчика


async def test_the_source_record_id_travels_without_splitting_the_debtor(
    container: Container,
) -> None:
    """«ИД» из выгрузки доезжает до базы, но ключом дедупликации не становится.

    Две ошибки здесь одинаково дороги и противоположны. Выбросить ИД — потерять
    единственную ссылку из отчёта в учётную систему заказчика, а заодно тот
    ключ, по которому приедут суммы долга: сейчас их в выгрузке нет вовсе, и
    вердикт по каждому должнику звучит «считать не из чего».

    Взять ИД ключом — разбить людей обратно на эпизоды. Выгрузка эвакуатора это
    список задержаний: на живом файле 2052 человека дают 2631 запись, то есть
    579 лишних платных проверок одних и тех же людей.

    Поэтому ИД накапливается списком, как машины, и в dedup_key не входит.
    """
    report = await container.import_service.import_text(
        "ИД,ФИО,Дата рождения,Госномер\n"
        "793783,Тестов Андрей Сергеевич,15.03.1980,А123ВС777\n"
        "830279,Тестов Андрей Сергеевич,15.03.1980,А123ВС777\n"
        "890166,Тестов Андрей Сергеевич,15.03.1980,В456ЕК750"
    )

    assert report.imported == 1, "ИД разбил одного человека на три платные проверки"
    assert report.merged_episodes == 1

    async with container.database.session() as session:
        rows = await DebtorRepository(session).find_by_fio("Тестов Андрей Сергеевич")
    assert rows[0].source_record_ids == "793783, 830279, 890166"
    assert rows[0].vehicle_plates == "А123ВС777, В456ЕК750"

    # И «ИД» перестал числиться непонятой колонкой — иначе оператор ищет,
    # что бы такое переименовать.
    assert "ИД" not in report.unknown_columns


# ------------- 20. ноль рублей там, где сумма неизвестна


def test_the_queue_page_never_prints_a_debt_of_zero_it_does_not_know() -> None:
    """«Долг всего в прогоне 0 ₽» по должникам, у которых сумма неизвестна.

    В выгрузке заказчика колонки с суммой нет вовсе, поэтому все 2052 должника
    получают вердикт «считать не из чего», а страница складывала их нули и
    печатала итог цифрой — первым числом в шапке, крупным, и следом в смете:
    «Итого проверено 2052 · Долг 0 ₽ · Пошлина 0 ₽ · из них не будет уплачено
    0 ₽ — это и есть экономия». Этот лист печатают на A4 и подшивают к делу: он
    утверждал, что по всей базе взыскивать нечего.

    Ноль здесь не бывает по построению: должник с нулевым долгом до денежного
    вердикта не доходит, его забирает правило «в выгрузке нет суммы». Правило
    уже было записано в этом же файле, у непроверенных строк — «ноль означал бы,
    что строки ничего не стоят, а правда — что сколько они стоят, мы не знаем»,
    — просто не применялось к остальным ячейкам.
    """
    from app.web.render_queue import render_queue_page
    from tests.test_queue_page import item, snapshot

    rows = [
        item(index=i, verdict=Verdict.REVIEW, debt=None, fee=None, score=50) for i in range(1, 6)
    ]
    page = render_queue_page(snapshot(rows), app_name="Collector Bot")

    assert "Долг всего в прогоне</span><b>0 ₽" not in page, (
        "страница напечатала долг, которого не знает"
    )
    assert "Долг всего в прогоне</span><b>—" in page
    assert "не будет уплачено 0 ₽ — это и есть экономия" not in page

    # В смете «Проверить руками» стоит пять строк, а суммы у них нет: прочерк.
    review_row = _ledger_row(page, "Проверить руками")
    assert review_row == ("5", "—", "—"), review_row
    total_row = _ledger_row(page, "Итого проверено")
    assert total_row == ("5", "—", "—"), total_row

    # Ноль, который остаётся честным: строк с вердиктом «иск» в прогоне ноль,
    # и долг по ним действительно нулевой. Правило про прочерк не должно
    # расползтись сюда, иначе «нисколько» превратится в «неизвестно» — та же
    # подмена, только в другую сторону.
    assert _ledger_row(page, "Подавать иск") == ("0", "0 ₽", "0 ₽")

    # А настоящий ноль по-настоящему пустого прогона так и остаётся честным:
    # там нечего складывать, потому что нет строк, и прочерк был бы враньём
    # в другую сторону.
    known = [item(index=1, verdict=Verdict.ORDER, debt=Decimal("120000"), fee=Decimal("2400"))]
    with_money = render_queue_page(snapshot(known), app_name="Collector Bot")
    assert "120 000 ₽" in with_money


def _ledger_row(page: str, title: str) -> tuple[str, str, str]:
    """Строки, долг и пошлина из строки сметы с этим названием."""
    import re

    chunk = page.split(title, 1)[1]
    cells = re.findall(r'<td class="n r" data-l="[^"]+">([^<]*)</td>', chunk)[:3]
    return (cells[0], cells[1], cells[2])


# ---------------- 21. долг, посчитанный по тарифу, и его признак


async def test_the_debt_is_computed_from_dates_and_marked_as_computed(
    container: Container,
) -> None:
    """Сумма долга считается по тарифу — и остаётся видно, что она расчётная.

    В выгрузке взыскателя-эвакуатора суммы нет: в учёте она не хранится, а
    считается из двух дат по тарифу. Без неё вердикт по всем 2052 должникам
    звучал «цену иска и пошлину посчитать не из чего», и очередь целиком стояла
    одинаково пустой — продукт не отвечал на свой единственный вопрос.

    Хранение — только за ПОЛНЫЕ сутки: почасовую оплату отменили, неполные
    сутки не тарифицируются. Это не округление: медиана стоянки на живой
    выгрузке десять часов, и счёт по началу суток завысил бы требование
    тысяче шестистам должникам из двух тысяч.

    Признак «расчётная» едет вместе с суммой. В цену иска идёт документ, а не
    оценка, и это то же правило проекта, только про деньги.
    """
    container.settings.tow_fee = Decimal("5000")
    container.settings.storage_fee_per_day = Decimal("1394")

    await container.import_service.import_text(
        "ИД,ФИО,Дата рождения,Дата постановки,Дата выдачи,Госномер\n"
        # Десять часов — суток хранения ноль, платит только эвакуацию.
        "1,Тестов Андрей Сергеевич,15.03.1980,03.03.2023 01:30,03.03.2023 11:30,А123ВС777\n"
        # Двое полных суток и ещё немного: платит за двое, а не за трое.
        "2,Тестова Мария Ивановна,20.07.1975,01.04.2023 10:00,03.04.2023 18:00,В456ЕК750"
    )

    async with container.database.session() as session:
        repo = DebtorRepository(session)
        short = (await repo.find_by_fio("Тестов Андрей Сергеевич"))[0]
        long = (await repo.find_by_fio("Тестова Мария Ивановна"))[0]

    assert short.debt_amount == Decimal("5000"), "неполные сутки не оплачиваются"
    assert long.debt_amount == Decimal("5000") + Decimal("1394") * 2
    assert short.debt_is_estimated and long.debt_is_estimated


async def test_a_debt_from_the_export_beats_the_tariff(container: Container) -> None:
    """Сумма из выгрузки сильнее расчёта: документ важнее оценки."""
    container.settings.tow_fee = Decimal("5000")
    container.settings.storage_fee_per_day = Decimal("1394")

    await container.import_service.import_text(
        "ФИО,Сумма долга,Дата постановки,Дата выдачи\n"
        "Тестов Андрей Сергеевич,73500,01.04.2023 10:00,10.04.2023 10:00"
    )

    async with container.database.session() as session:
        row = (await DebtorRepository(session).find_by_fio("Тестов Андрей Сергеевич"))[0]

    assert row.debt_amount == Decimal("73500")
    assert not row.debt_is_estimated, "сумма из выгрузки помечена расчётной"


# ---------------- 22. должник находится по любой своей машине


async def test_a_debtor_is_found_by_any_of_their_plates(container: Container) -> None:
    """Госномер — главный ключ поиска в этой выгрузке, и он обязан находить все машины.

    Телефона в выгрузке взыскателя-эвакуатора нет вовсе, ФИО есть не у всех, а
    госномер есть у каждой строки — и машина у оператора буквально на руках,
    именно её он и вводит. При этом один должник приезжает в выгрузке
    несколько раз на разных машинах: на живой выгрузке таких 88.

    Пока искали только по последнему номеру, прежние машины не находились
    вовсе: оператор вводил номер из своего же документа и получал «не найден».
    """
    from app.domain.enums import SearchType
    from app.domain.identity import VehicleDescriptor

    await container.import_service.import_text(
        "ФИО,Дата рождения,Госномер\n"
        "Тестов Андрей Сергеевич,15.03.1980,А123ВС777\n"
        "Тестов Андрей Сергеевич,15.03.1980,В456ЕК750\n"
        "Тестов Андрей Сергеевич,15.03.1980,Е789МН197"
    )

    for plate in ("А123ВС777", "В456ЕК750", "Е789МН197"):
        found = await container.search_service.lookup_internal(
            SearchSubject(
                search_type=SearchType.VEHICLE.value, vehicle=VehicleDescriptor(plate=plate)
            )
        )
        assert found, f"должник не найден по своей же машине {plate}"

    # Чужой номер не должен находиться, и совпадение идёт по границам элемента:
    # обрезанный «Е789МН19» не живёт внутри «Е789МН197». Номера подобраны так,
    # чтобы не совпасть с демо-выгрузкой, которую тот же провайдер читает рядом.
    for stranger in ("А001АА99", "Е789МН19", "789МН197"):
        assert not await container.search_service.lookup_internal(
            SearchSubject(
                search_type=SearchType.VEHICLE.value, vehicle=VehicleDescriptor(plate=stranger)
            )
        ), f"нашёлся посторонний номер {stranger}"


# ------- 23. цепочка «ввёл номер — увидел должника» через мост «телефон → ФИО»


class _PhoneBridgeStub(PhoneNameProvider):
    """Сервис заказчика, отдающий ФИО по номеру. Без сети."""

    @property
    def is_configured(self) -> bool:
        return True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        return PhoneNameResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS,
            records=(),
            name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич"),
            note="ФИО определено по номеру",
        )


async def test_a_phone_reaches_the_debtor_through_the_name_bridge(
    container: Container, database: Database
) -> None:
    """Оператор вводит номер и получает должника, хотя телефона в выгрузке нет.

    Это сценарий из ТЗ дословно: «ввёл ФИО+номер телефона — увидел инфу и понял
    перспективу взыскания». В выгрузке заказчика телефона нет ни одной колонкой
    — там ИД, две даты, стоянка, подразделение, ФИО, дата рождения, место
    рождения, паспорт, адрес, права, марка и госномер, — и ни один внешний
    реестр по номеру тоже не ищет. Без моста самый естественный для оператора
    ввод не находил никого.

    Мост переводит номер в ФИО, и уже им поднимается строка выгрузки, из
    которой считается долг и пошлина.

    Порядок обязателен: мост идёт РАНЬШЕ внутренней базы. Иначе искать строку
    нечем, а без строки нечем обогатить запрос к реестрам.

    И имя из моста — ключ поиска, а не факт: записей он не приносит, в покрытие
    отчёта не входит. В отчёт едут данные взыскателя и ответы реестров.
    """
    from app.providers.registry import (
        ProviderRegistry,
        build_external_providers,
        build_inn_bridge,
        build_internal_provider,
    )
    from app.services.search import SearchService

    container.settings.tow_fee = Decimal("5000")
    container.settings.storage_fee_per_day = Decimal("1394")
    await container.import_service.import_text(
        "ИД,ФИО,Дата рождения,Дата постановки,Дата выдачи,Госномер\n"
        "440466,Тестов Андрей Сергеевич,15.03.1980,01.04.2023 10:00,06.04.2023 12:00,А123ВС777"
    )

    registry = ProviderRegistry(
        internal=build_internal_provider(container.settings, database),
        external=build_external_providers(container.settings),
        inn_bridge=build_inn_bridge(container.settings),
        phone_bridge=_PhoneBridgeStub(container.settings),
    )
    service = SearchService(settings=container.settings, database=database, registry=registry)

    outcome = await service.search_detailed(
        SearchSubject(search_type=SearchType.PERSON.value, phone="+79851982945"),
        telegram_user_id=OPERATOR_ID,
    )
    report = outcome.report
    decision = container.verdict_engine.decide(report)

    assert report.subject.name is not None
    assert report.subject.name.full == "Тестов Андрей Сергеевич"

    found = report.internal_records
    assert found, "строка выгрузки не поднялась по имени из моста"
    # Пять полных суток хранения плюс эвакуация.
    assert found[0].debt_amount == Decimal("5000") + Decimal("1394") * 5
    assert decision.state_fee == Decimal("2000")

    bridge_rows = [
        row for row in report.provider_results if row.provider is ProviderName.PHONE_BRIDGE
    ]
    assert bridge_rows, "мост не оставил строки в блоке ИСТОЧНИКИ"
    assert not bridge_rows[0].records, "мост принёс записи в отчёт — он не источник фактов"
    assert ProviderName.PHONE_BRIDGE not in registry.configured_names


async def test_the_operators_own_name_beats_the_bridge(container: Container) -> None:
    """Названное оператором ФИО сильнее: он смотрит в документ, мост — в чужую базу."""
    bridge = _PhoneBridgeStub(container.settings)
    named = SearchSubject(
        search_type=SearchType.PERSON.value,
        phone="+79851982945",
        name=PersonName(last_name="Сидорова", first_name="Анна"),
    )
    assert not bridge.is_needed(named)
    assert bridge.is_needed(
        SearchSubject(search_type=SearchType.PERSON.value, phone="+79851982945")
    )


# ------ 24. точный ключ свободной строкой поднимает должника из выгрузки


async def test_an_exact_key_in_a_free_line_finds_the_debtor(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Написал номер машины — увидел должника. Без кнопок и без оплаты.

    Ориентир интерфейса, который выбрала владелица, требует этого дословно:
    «никакого экрана "введите телефон", оператор просто пишет номер в чат,
    мгновенный ответ — одно сообщение-карточка». И это же рабочий сценарий
    взыскателя-эвакуатора: машина у него на руках.

    Выгрузка при этом спрашивалась только в ведомом сценарии по кнопкам:
    свободная строка карточку не опознавала вовсе, и оператор, написавший
    номер, получал пустую форму.

    Ослабление точечное — только для точных ключей. По ФИО и дате выгрузка
    по-прежнему не спрашивается: она отвечает похожими, а не теми же, и
    подставленная дата рождения легла бы поверх намеренно пропущенной.
    """
    await feed(dispatcher, bot, message=make_message("А123ВС77"))

    card = sent.joined
    assert "Нашёл" in card, "точный ключ не поднял строку выгрузки"
    # Платит одна кнопка: опознание показывает найденное, но проверку не
    # запускает — «Проверить» оператор не нажимал.
    assert "RECOVERY SCORE" not in card, "свободная строка запустила платный прогон"


async def test_a_free_line_name_still_does_not_touch_the_export(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """ФИО свободной строкой выгрузку не спрашивает: она ответит похожими."""
    await feed(dispatcher, bot, message=make_message("Демов Максим Игоревич"))

    assert "Нашёл" not in sent.joined


async def test_a_phone_alone_runs_the_check_from_the_bot(
    container: Container, bot: Bot, sent: SentMessages
) -> None:
    """«Проверить» по одному номеру доходит до прогона — проверено ЧЕРЕЗ БОТА.

    Мост был написан, покрыт тестами и недостижим: до поиска из бота ведёт одна
    дорога — ``run_card``, а она за ``Card.runnable``, которая телефон
    основанием для прогона не считала. Оператор жал «Проверить» и получал
    всплывашку «нужны фамилия с именем, или ИНН, или госномер». Мост при этом
    не вызывался ни разу.

    Прежний тест этого не увидел, потому что дёргал сервис напрямую, минуя
    хендлер. Отсюда правило для этого файла: сценарий оператора проверяется
    тем же путём, которым идёт оператор.

    Само ограничение было верным ДО моста: телефон, по которому в выгрузке
    никого нет, во внешние реестры не несёт ничего, и платный прогон дал бы пять
    строк «нужно ФИО». С мостом номер перестаёт быть тупиком — и решает это не
    карточка, а подключённость моста в этом развёртывании.
    """
    container.registry._phone_bridge = _PhoneBridgeStub(container.settings)
    await container.import_service.import_text(
        "ФИО,Дата рождения,Госномер\nТестов Андрей Сергеевич,15.03.1980,А123ВС777"
    )
    dispatcher = dispatcher_for(container)

    await feed(dispatcher, bot, message=make_message("79851982945"))
    sent.texts.clear()
    await feed(dispatcher, bot, callback_query=make_callback("qc:run"))

    assert sent.joined, "«Проверить» по номеру не ответило ничем"
    assert "нужны фамилия с именем" not in sent.joined, "мост из бота недостижим"
    assert sent.contains("RECOVERY SCORE"), "прогон по одному номеру не дошёл до отчёта"


async def test_without_the_bridge_a_phone_alone_still_refuses(
    container: Container, bot: Bot, sent: SentMessages
) -> None:
    """Без моста номер по-прежнему тупик, и платный прогон по нему не идёт.

    Пять строк «нужно ФИО» за деньги — это не проверка, а счёт за пустоту.
    """
    assert container.registry.phone_bridge is None
    dispatcher = dispatcher_for(container)

    await feed(dispatcher, bot, message=make_message("79851982945"))
    sent.texts.clear()
    await feed(dispatcher, bot, callback_query=make_callback("qc:run"))

    assert not sent.contains("RECOVERY SCORE"), "прогон ни о ком за деньги"


# ---- 26. паспорт из выгрузки открывает три источника через мост к ИНН


async def test_the_passport_from_the_export_unlocks_the_inn_bridge(
    container: Container,
) -> None:
    """Паспорт доезжает до субъекта поиска — ради ИНН, а не ради отчёта.

    Банкротство, статус ИП и арбитраж физлица ищут ТОЛЬКО по двенадцатизначному
    ИНН. В выгрузке заказчика его нет ни у одного из 2052, поэтому три источника
    из шести молчали по всей базе и молчали бы всегда. Паспорт есть у 1304 из
    них, мост «паспорт → ИНН» написан и включён — не хватало ровно одного:
    импорт выбрасывал колонку.

    Хранение по правилу телефона: маска всегда, сам номер только при поднятом
    STORE_SENSITIVE_IDENTIFIERS. Маска мосту бесполезна, поэтому выключенный
    флаг оставляет три источника молчащими — и это выбор развёртывания, а не
    умолчание кода.
    """
    container.settings.store_sensitive_identifiers = True
    await container.import_service.import_text(
        "ФИО,Дата рождения,Паспорт\nТестов Андрей Сергеевич,15.03.1980,45 09 123456"
    )

    async with container.database.session() as session:
        row = (await DebtorRepository(session).find_by_fio("Тестов Андрей Сергеевич"))[0]
    assert row.passport == "4509123456"
    assert row.passport_masked and "123456" not in row.passport_masked

    # И он доезжает до субъекта: мост ищет паспорт именно там.
    subject = SearchSubject(
        search_type=SearchType.PERSON.value,
        name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич"),
        birth_date=date(1980, 3, 15),
    )
    enriched = await container.search_service.lookup_internal(subject)
    assert enriched and enriched[0].passport == "4509123456"


async def test_words_in_the_passport_column_are_not_an_error(container: Container) -> None:
    """«Сведения отсутствуют» — это не испорченный паспорт, а его отсутствие.

    В выгрузке заказчика колонка наполовину заполнена словами. Звать оператора
    чинить восемьсот строк, в которых чинить нечего, — тот же ложный сигнал, что
    и тридцать «ошибок» в машинах без номера: за ним теряются настоящие.
    """
    report = await container.import_service.import_text(
        "ФИО,Паспорт\n"
        "Тестов Андрей Сергеевич,сведения отсутствуют\n"
        "Тестова Мария Ивановна,нет\n"
        "Тестов Пётр Петрович,45 09 12"
    )

    warnings = " ".join(report.warnings)
    assert "Паспорт" in warnings, "испорченный паспорт промолчал"
    assert report.warning_count == 1, "слова в колонке подняли ложную тревогу"


# ---- 27. пустая настройка в .env не роняет бота


def test_a_blank_numeric_setting_does_not_crash_the_bot() -> None:
    """«TELEGRAM_LOOKUP_API_ID=» без значения — это «не задано», а не ошибка.

    Незаполненная строка в .env — нормальное состояние: её пишут ровно тогда,
    когда собираются заполнить позже. На проде такая строка увела бота в цикл
    перезапуска: pydantic не смог разобрать пустую строку как int, падение
    случалось до логгера, и в логах был только ValidationError без имени поля.

    Цена ошибки несоразмерна причине: бот молчал на все сообщения, а в .env
    стояла заготовка под настройку, которую ещё не включили.
    """

    settings = Settings(_env_file=None, WEB_PORT="", CACHE_TTL_HOURS="", TOW_FEE="")

    assert settings.web_port == 8080
    assert settings.cache_ttl_hours == 24
    assert settings.tow_fee == Decimal("5000")


# ---- 28. долг складывается по всем задержаниям, а не по последнему


async def test_the_debt_sums_every_impound_not_just_the_last(
    container: Container,
) -> None:
    """Три задержания — три эвакуации и три хранения, а не одно.

    Выгрузка взыскателя-эвакуатора — список эпизодов, а не список людей: 183
    должника приезжают в ней по два-пять раз. Схлопывать их в одного должника
    правильно (платная проверка человека нужна одна), но долг при этом
    считался по одной паре дат, и 216 задержаний просто не попадали в
    требование. На живой выгрузке это 1 197 614 ₽ — требования, которых отчёт
    не показывал.

    Занижение здесь дороже, чем кажется: от размера требования зависит сам
    вердикт. Пять эвакуаций по 5000 ₽ — это 25 000 ₽, при которых суд
    очевидно окупается, а 5000 ₽ при пошлине 2000 ₽ — уже пограничный случай.
    """
    container.settings.tow_fee = Decimal("5000")
    container.settings.storage_fee_per_day = Decimal("1394")

    await container.import_service.import_text(
        "ИД,ФИО,Дата рождения,Дата постановки,Дата выдачи,Госномер\n"
        # Три задержания одного человека: два коротких и одно на двое суток.
        "1,Тестов Андрей Сергеевич,15.03.1980,01.04.2023 10:00,01.04.2023 20:00,А123ВС777\n"
        "2,Тестов Андрей Сергеевич,15.03.1980,05.05.2023 10:00,05.05.2023 18:00,В456ЕК750\n"
        "3,Тестов Андрей Сергеевич,15.03.1980,09.06.2023 10:00,11.06.2023 18:00,А123ВС777"
    )

    async with container.database.session() as session:
        row = (await DebtorRepository(session).find_by_fio("Тестов Андрей Сергеевич"))[0]

    # Три эвакуации плюс двое полных суток хранения в третьем эпизоде.
    assert row.debt_amount == Decimal("5000") * 3 + Decimal("1394") * 2
    assert row.source_record_ids == "1, 2, 3"


async def test_a_typo_in_the_dates_does_not_poison_the_sum(container: Container) -> None:
    """Выдали раньше, чем привезли — эпизод не считается, остальные считаются.

    Отрицательный срок хранения это опечатка в выгрузке. Считать по ней нельзя,
    но и терять из-за неё соседние задержания того же человека — тоже.
    """
    container.settings.tow_fee = Decimal("5000")
    container.settings.storage_fee_per_day = Decimal("1394")

    await container.import_service.import_text(
        "ИД,ФИО,Дата рождения,Дата постановки,Дата выдачи\n"
        "1,Тестов Андрей Сергеевич,15.03.1980,01.04.2023 10:00,01.04.2023 20:00\n"
        "2,Тестов Андрей Сергеевич,15.03.1980,09.06.2023 18:00,09.06.2023 10:00"
    )

    async with container.database.session() as session:
        row = (await DebtorRepository(session).find_by_fio("Тестов Андрей Сергеевич"))[0]

    assert row.debt_amount == Decimal("5000"), "испорченный эпизод утащил за собой целый"
