"""Кнопки под карточкой отчёта.

Одно правило и один тест на каждый способ его нарушить: **кнопка показывается,
только если она не соврёт**. «Узнать ИНН по паспорту» без ФИО или без даты
рождения обещает проверку, которой не будет — мост ``passport_fns`` требует все
три поля и без них отвечает ``insufficient_query``, не сделав ни одного вызова
и не потратив ни рубля.

Проверяется это не флагом и не списком полей, переписанным из провайдера, а
самим провайдером: ``will_query`` на субъекте с подставленным паспортом. Так
кнопка и мост не могут разойтись — это один и тот же код. Предыдущая попытка
(``passport_bridge_ready`` через атрибут ``resolves_inn_by_passport``) не
работала: атрибут никто не проставлял, функция возвращала False всегда, и кнопка
не появилась бы ни разу.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.bot.report_actions import passport_would_help, report_keyboard
from app.config import Settings
from app.domain.enums import SearchType
from app.domain.identity import PersonName, SearchSubject
from app.providers.identity_bridge import PassportInnProvider


@pytest.fixture
def bridge_settings(live_settings: Settings) -> Settings:
    """Живой мост с ключом и явно включённым флагом."""
    return live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": "https://api.example.test",
            "inn_bridge_enabled": True,
        }
    )


@pytest.fixture
def subject() -> SearchSubject:
    return SearchSubject(
        search_type=SearchType.PERSON.value,
        name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич"),
        birth_date=date(1985, 3, 12),
    )


def test_the_passport_offer_appears_when_it_would_actually_work(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    assert passport_would_help(subject, PassportInnProvider(bridge_settings))


def test_no_bridge_at_all_means_no_offer(subject: SearchSubject) -> None:
    assert not passport_would_help(subject, None)


def test_the_flag_being_off_means_no_offer(live_settings: Settings, subject: SearchSubject) -> None:
    """Без ``INN_BRIDGE_ENABLED`` мост отвечает NOT_CONFIGURED и не звонит никуда."""
    off = live_settings.model_copy(
        update={"newdb_api_key": "test-key", "inn_bridge_enabled": False}
    )
    assert not passport_would_help(subject, PassportInnProvider(off))


def test_an_existing_individual_inn_means_no_offer(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    with_inn = subject.model_copy(update={"inn": "770912345601"})
    assert not passport_would_help(with_inn, PassportInnProvider(bridge_settings))


@pytest.mark.parametrize("missing", ["name", "birth_date"])
def test_a_missing_field_the_bridge_needs_means_no_offer(
    bridge_settings: Settings, subject: SearchSubject, missing: str
) -> None:
    """Без ФИО или без даты мост ответит ``insufficient_query`` — обещать нечего."""
    crippled = subject.model_copy(update={missing: None})
    assert not passport_would_help(crippled, PassportInnProvider(bridge_settings))


def test_a_passport_already_in_hand_means_no_offer(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    with_passport = subject.model_copy(update={"passport": "4509123456"})
    assert not passport_would_help(with_passport, PassportInnProvider(bridge_settings))


def test_a_ten_digit_entity_inn_still_needs_the_bridge(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """Десять цифр — идентификатор юрлица; три источника его отвергают."""
    entity = subject.model_copy(update={"inn": "7709123456"})
    assert passport_would_help(entity, PassportInnProvider(bridge_settings))


# ---------------------------------------------------------------- клавиатура


def button_texts(markup: object) -> list[str]:
    rows = getattr(markup, "inline_keyboard", [])
    return [button.text for row in rows for button in row]


def test_a_complete_subject_keeps_the_keyboard_as_short_as_before(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """ФИО, дата и ИНН — предлагать нечего, кроме региона, ссылки и повтора.

    Добор полей переехал в карточку запроса, которая стоит сразу под отчётом.
    Второй ряд кнопок про то же самое здесь был бы не помощью, а шумом.
    """
    complete = subject.model_copy(update={"inn": "770912345601"})
    markup = report_keyboard(
        url="https://reports.example.test/r/x",
        refresh_token="tok",
        subject=complete,
        bridge=PassportInnProvider(bridge_settings),
    )

    texts = button_texts(markup)
    # Кнопок-предложений нет: добавлять нечего. Проверяем по отсутствию слова
    # «Добавить», а не по эмодзи — их в подписях больше нет вовсе, бот должен
    # выглядеть ненавязчиво.
    assert not any(text.startswith("Добавить") for text in texts)
    assert "Полный отчёт" in texts
    assert "Обновить" in texts


def test_the_export_row_rides_along_with_the_link(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """Выгрузка живёт в той же клавиатуре, что ссылка и сужение по региону.

    Клавиатур под карточкой ровно одна: пока их было две, «Полный отчёт» и
    предложения показывались взаимоисключающе, и оператор терял то одно, то
    другое.
    """
    markup = report_keyboard(
        url="https://reports.example.test/r/x",
        refresh_token="tok",
        subject=subject,
        bridge=PassportInnProvider(bridge_settings),
        text_url="https://reports.example.test/r/x/report.txt",
        print_url="https://reports.example.test/r/x/print",
    )

    texts = button_texts(markup)
    assert "Печать" in texts
    assert "Текстом" in texts
    assert "Сузить до одного региона" in texts


def test_without_a_link_there_is_nothing_to_export(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """Деплой без веба: кнопок выгрузки нет, сужение по региону есть."""
    markup = report_keyboard(
        url=None,
        refresh_token="tok",
        subject=subject,
        bridge=PassportInnProvider(bridge_settings),
    )

    texts = button_texts(markup)
    assert not any(text in texts for text in ("Печать", "Текстом", "Полный отчёт"))
    assert "Сузить до одного региона" in texts


def test_offers_never_appear_for_a_vehicle_search(bridge_settings: Settings) -> None:
    """Госномеру нечего добирать ИНН и датой рождения."""
    vehicle = SearchSubject(search_type=SearchType.VEHICLE_PLATE.value)
    markup = report_keyboard(
        url=None,
        refresh_token="tok",
        subject=vehicle,
        bridge=PassportInnProvider(bridge_settings),
    )

    assert not any(text.startswith(("➕", "📅", "🪪", "📍")) for text in button_texts(markup))


def test_the_callback_payload_fits_telegrams_limit(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """64 байта — жёсткий предел Telegram, и токен субъекта его не переберёт."""
    markup = report_keyboard(
        url=None,
        refresh_token="A" * 22,
        subject=subject,
        bridge=PassportInnProvider(bridge_settings),
    )
    payloads = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]

    assert payloads
    assert all(len(payload.encode("utf-8")) <= 64 for payload in payloads)
