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
from aiogram.types import InlineKeyboardMarkup

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
    """ФИО, дата и ИНН — предлагать нечего, кроме ссылки, уточнения и повтора.

    Добор полей переехал в карточку запроса, а она теперь за кнопкой
    «Уточнить данные»: под каждым отчётом она приезжала сама и повторяла
    всё уже сказанное — двадцать строк и тринадцать кнопок.
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
    assert "Открыть отчёт" in texts
    assert "Спросить заново" in texts
    # Тупиков нет: с карточки отчёта видно и следующего должника, и меню.
    assert "Новая проверка" in texts
    assert "В меню" in texts


def test_печать_и_файлом_живут_на_странице_а_не_в_чате(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """Кнопок выгрузки под отчётом нет: обе ссылки уже стоят в шапке страницы.

    Они вели ровно туда же, куда «Открыть отчёт», — на тот же хост, тот же
    токен, тот же документ. Три адреса до одного места, один под другим, это
    не выбор, а шум, и именно на такие ряды показывали словами «одни кнопки».
    Печать и выгрузка никуда не делись: их печатает
    :func:`app.web.render._export_actions` в шапке самого отчёта.
    """
    markup = report_keyboard(
        url="https://reports.example.test/r/x",
        refresh_token="tok",
        subject=subject,
        bridge=PassportInnProvider(bridge_settings),
    )

    texts = button_texts(markup)
    assert "Печать" not in texts
    assert "Файлом" not in texts
    assert any(text.startswith("Открыть отчёт") for text in texts)


def test_without_a_link_the_report_still_offers_a_way_on(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """Деплой без веба: ссылки нет, но экран не тупик."""
    markup = report_keyboard(
        url=None,
        refresh_token="tok",
        subject=subject,
        bridge=PassportInnProvider(bridge_settings),
    )

    texts = button_texts(markup)
    assert not any(text.startswith("Открыть отчёт") for text in texts)
    assert "Уточнить данные" in texts
    assert "Новая проверка" in texts


def test_narrowing_by_region_is_offered_only_when_it_would_change_something(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """Регион сужает поиск по ФССП — и предлагается, только когда есть что сужать.

    Раньше кнопка стояла под каждым отчётом по человеку, включая те, где
    производств не нашлось вовсе: нажатие вело к выбору региона и повторному
    поиску с тем же пустым результатом. Это то же правило, по которому здесь
    не показывают «Узнать ИНН по паспорту», — кнопка не должна обещать того,
    чего не будет.
    """
    def keyboard(*, narrowable: bool) -> InlineKeyboardMarkup:
        return report_keyboard(
            url="https://reports.example.test/r/x",
            refresh_token="tok",
            subject=subject,
            bridge=PassportInnProvider(bridge_settings),
            narrowable=narrowable,
        )

    assert "Сузить до одного региона" not in button_texts(keyboard(narrowable=False))
    assert "Сузить до одного региона" in button_texts(keyboard(narrowable=True))


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
