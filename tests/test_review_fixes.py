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
"""

from __future__ import annotations

import asyncio
from datetime import date

from aiogram import Bot, Dispatcher

from app.container import Container
from app.domain.enums import ProviderStatus, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import DebtorReport
from app.services.batch import BatchAlreadyRunningError
from app.services.scoring import RecoveryScoreEngine
from tests.conftest import make_proceeding
from tests.test_scoring import build_report

from .bot_harness import OPERATOR_ID, SentMessages, feed, make_callback, make_message

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
