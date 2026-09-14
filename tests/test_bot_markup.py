"""Разметка карточки — и цена, которую она не имеет права стоить.

Бот всю жизнь писал голым текстом, и это была защита, а не лень: в карточку
едет чужой текст — фамилия из выгрузки, эхо того, что оператор напечатал
руками, — и один «<» в нём превращает сообщение в ошибку Telegram. Владелец
назвал результат «серо» и был прав, но серая карточка лучше ненаступившей.

Поэтому защиты две, и здесь проверяются обе: каждая подставляемая величина
экранируется, а если где-то всё-таки не экранирована — сообщение уходит без
разметки, а не пропадает.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from app.bot import common, view
from app.bot.markup import HTML, bold, esc, strip_tags
from app.config import Settings
from app.domain.enums import SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import DebtorReport
from app.services.scoring import RecoveryScoreEngine
from app.services.verdict import VerdictEngine

# Фамилия, которой достаточно, чтобы уронить отправку размеченного сообщения.
HOSTILE_NAME = 'Тестов<b> & "Ко"'


def card(report: DebtorReport, settings: Settings, **kwargs: Any) -> str:
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    decision = VerdictEngine(settings).decide(report)
    return view.report_card(report, decision, **kwargs)


def hostile_report() -> DebtorReport:
    subject = SearchSubject(
        search_type=SearchType.PERSON.value,
        name=PersonName(last_name=HOSTILE_NAME, first_name="Андрей", middle_name="Сергеевич"),
        birth_date=date(1985, 3, 12),
    )
    return DebtorReport(subject=subject)


# ------------------------------------------------------------- экранирование


def test_a_hostile_name_never_reaches_telegram_unescaped(settings: Settings) -> None:
    """Фамилия с «<» и «&» приезжает экранированной, а не как разметка.

    Это тот самый случай, ради которого разметку так долго не включали. Имя
    берётся из чужой выгрузки, проверить его содержимое мы не можем, и «<b>»
    внутри фамилии обязан остаться текстом.
    """
    text = card(hostile_report(), settings)

    assert "&lt;b&gt;" in text
    assert "&amp;" in text
    # Собственная разметка на месте: экранирование не должно её съесть.
    assert "<b>" in text


def test_the_echo_of_what_the_operator_typed_is_escaped(settings: Settings) -> None:
    """Эхо разбора — самый чужой текст в карточке: его печатал человек."""
    text = card(hostile_report(), settings, notes=["<b>77091234560</b> — это ИНН организации."])

    assert "&lt;b&gt;77091234560&lt;/b&gt;" in text


def test_every_opening_tag_is_closed(settings: Settings) -> None:
    """Непарный тег Telegram отвергает так же, как чужой «<»."""
    text = card(hostile_report(), settings, notes=["оговорка"])

    assert text.count("<b>") == text.count("</b>")


# ------------------------------------------------------- снятие разметки


def test_stripping_tags_gives_back_the_plain_card(settings: Settings) -> None:
    """Запасной путь возвращает тот же текст — без тегов и без сущностей.

    Если экранирование где-то пропущено, карточка уходит простым текстом. Она
    обязана при этом остаться читаемой: «&amp;» вместо «&» и висящие «<b>» —
    это не запасной путь, а вторая поломка.
    """
    plain = strip_tags(card(hostile_report(), settings))

    assert HOSTILE_NAME in plain
    # Считаем точно, а не «тегов нет»: «<b>» стоит в самой фамилии, и снятие
    # разметки обязано его сохранить — это часть чужого текста, а не наша
    # разметка. Уйти должны ровно те теги, которые добавили мы.
    assert plain.count("<b>") == HOSTILE_NAME.count("<b>")
    assert "&lt;" not in plain and "&amp;" not in plain


def test_stripping_is_the_exact_inverse_of_escaping() -> None:
    """Двойное экранирование разворачивается ровно на один шаг.

    «&amp;lt;» обязано стать «&lt;», а не «<»: иначе запасной путь придумывает
    разметку там, где в исходном тексте стояли просто символы.
    """
    for value in ("&", "<b>", "&lt;", "&amp;lt;", 'a & b < c > d "e"'):
        assert strip_tags(esc(value)) == value
        assert strip_tags(bold(esc(value))) == value


# ------------------------------------------------- карточка доходит всегда


class _Rejects:
    """Сообщение, которое Telegram отвергает из-за разметки.

    Отвергает ровно один раз — как настоящий: вторая отправка, уже без тегов,
    проходит. Так же ведёт себя живой Telegram, и именно на это рассчитан
    запасной путь.
    """

    def __init__(self) -> None:
        self.attempts: list[tuple[str, str | None]] = []

    async def _accept(self, text: str, parse_mode: str | None) -> None:
        self.attempts.append((text, parse_mode))
        if parse_mode is not None:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=1, text=text),
                message="can't parse entities: Unsupported start tag",
            )

    async def edit_text(
        self, text: str, *, parse_mode: str | None = None, reply_markup: object = None
    ) -> None:
        await self._accept(text, parse_mode)

    async def answer(
        self, text: str, *, parse_mode: str | None = None, reply_markup: object = None
    ) -> None:
        await self._accept(text, parse_mode)


@pytest.mark.asyncio
async def test_a_rejected_markup_costs_the_bold_and_not_the_card() -> None:
    """Отвергнутая разметка не имеет права стоить владельцу проверки.

    Экранирование живёт в десятке мест, и одной новой строки без ``esc``
    достаточно, чтобы Telegram отверг карточку целиком. Серая карточка —
    неприятность; неотправленная — потерянный результат прогона, за который
    заплачено.
    """
    notice = _Rejects()

    await common._edit_html(notice, f"{bold('Вердикт')}\nтекст", None)  # type: ignore[arg-type]

    assert len(notice.attempts) == 2
    first, second = notice.attempts
    assert first[1] == HTML and "<b>" in first[0]
    # Вторая попытка — тот же текст без тегов, и она уже без parse_mode.
    assert second[1] is None and second[0] == "Вердикт\nтекст"
