"""Страница «вся база»: вкладки, пошлины и сводка.

Проверяется не разметка, а числа: страницу открывают ради вопроса «сколько на
кону и во что обойдётся это забрать», и ошибка в сумме здесь стоит дороже
любой съехавшей рамки.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.db.models import Debtor
from app.web.render_base import FeeRules, render_base_page

#: Пороги как на проде: судебный приказ до 500 000 ₽, подавать не стоит, если
#: долг меньше пошлины втрое.
RULES = FeeRules(court_order_max=Decimal("500000"), min_debt_to_fee_ratio=Decimal("3"))


def debtor(index: int, amount: str | None) -> Debtor:
    return Debtor(
        id=index,
        dedup_key=f"k{index}",
        fio=f"Иванов Иван {index}",
        fio_normalized=f"иванов иван {index}",
        birth_date=date(1990, 1, 1),
        vehicle_plates="Х376СА797",
        address="Москва, Петровско-Разумовский проезд, д. 1",
        debt_amount=Decimal(amount) if amount else None,
    )


def page(amounts: list[str | None]) -> str:
    rows = [debtor(index, amount) for index, amount in enumerate(amounts)]
    return render_base_page(rows, app_name="Collector Bot", rules=RULES)


def test_the_headline_sums_debts_not_fees() -> None:
    """«Всего требований» — сумма долгов, и это уже было один раз не так.

    Строка списка несёт тройку «должник, способ подачи, пошлина», и сводка
    складывала третье поле вместо долга: восемь должников на три миллиона
    показывались как шестьдесят семь тысяч. Ошибка тихая — числа выглядят
    правдоподобно, а решение по ним принимают денежное.
    """
    html = page(["1000000", "2000000"])

    assert "3 000 000 ₽" in html


def test_each_debtor_is_told_how_to_sue_and_what_it_costs() -> None:
    """Способ подачи и пошлина стоят прямо в строке.

    Иначе ответ на «во что мне обойдётся этот должник» лежит за кликом, а
    таких вопросов у заказчика две тысячи.
    """
    html = page(["11970"])

    assert "Судебный приказ" in html
    # До 100 000 ₽ пошлина по иску — 4 000 ₽, по приказу вдвое меньше.
    assert "2 000 ₽" in html


def test_a_debt_smaller_than_the_fee_is_marked_as_not_worth_it() -> None:
    """Долг, который меньше пошлины втрое, — отдельная вкладка и свой цвет.

    Это единственный случай, когда правильный ответ «не подавать», и он не
    должен выглядеть так же, как остальные: заказчик заплатит пошлину и не
    вернёт долг.
    """
    html = page(["1200"])

    assert "Не окупается" in html
    assert 'class="pill k-thin"' in html
    # И такая пошлина не попадает в сводку: это счёт за суд, которого не будет.
    assert "Пошлины по ним</span><b>—</b>" in html


def test_an_empty_tab_is_not_drawn() -> None:
    """Вкладка «Иск (0)» — приглашение нажать и увидеть пустой список."""
    html = page(["11970"])

    assert 'data-kind="order"' in html
    assert 'data-kind="claim"' not in html


def test_tabs_and_rows_agree_on_colour() -> None:
    """Один исход — один класс, и на вкладке, и в строке.

    Разъедься они, и вкладка «Иск» вела бы к строкам другого цвета.
    """
    html = page(["11970", "640000", "1200", None])

    for key in ("order", "claim", "thin", "none"):
        assert f'class="pill k-{key}"' in html, f"нет строки {key}"
    for key in ("order", "claim", "thin", "none"):
        assert f'class="tab k-{key}"' in html, f"нет вкладки {key}"


# ------------------------------------------------------------ один должник


def person(amount: str | None, *, estimated: bool = False) -> str:
    from app.web.render_base import render_person_page

    row = debtor(1, amount)
    row.debt_is_estimated = estimated
    row.contract_number = "EV-20481"
    row.source_record_ids = "793783, 830279"
    return render_person_page(row, app_name="Collector Bot", rules=RULES, back_url="/b/tok")


def test_the_person_page_answers_before_it_describes() -> None:
    """Первым идёт решение и его цена, а не паспортные поля.

    Страница открывается кликом по имени: имя читатель уже знает, он по нему и
    кликнул. Раньше она начиналась с ФИО и даты рождения, а «как подавать» и
    «сколько это стоит» лежали под таблицей полей.
    """
    # Сравнивается порядок в теле страницы, а не во всём документе: оглавление
    # в шапке перечисляет те же разделы и стоит раньше любого из них.
    body = person("38400").split("<main>", 1)[1]

    assert body.index('class="pill k-order"') < body.index('id="money"')
    assert body.index('id="money"') < body.index('id="facts"')


def test_the_fee_can_be_checked_step_by_step() -> None:
    """Расчёт свёрнут, но раскрывается: «под капотом, но можно посмотреть».

    Дословная просьба владелицы. Развёрнутый расчёт занимает больше места, чем
    ответ, ради которого страницу открыли; спрятанный совсем — превращает
    пошлину в число, которое неоткуда проверить.
    """
    html = person("2500000")

    assert "<details" in html and "Как посчитана пошлина" in html
    assert "ст. 333.19 НК РФ" in html
    # Ступень, ставка и итог — числами, а не общими словами.
    assert "25 000 ₽ + 1% от суммы свыше 1 000 000 ₽" in html
    assert "40 000 ₽" in html


def test_a_fee_larger_than_the_debt_is_not_called_a_multiple() -> None:
    """При долге меньше пошлины отношение меньше единицы, и его нельзя звать «больше».

    Общая формулировка печатала «долг больше пошлины в 0,6 раза» — фразу,
    которая переворачивает смысл ровно там, где ответ «не подавать».
    """
    html = person("1200")

    assert "Пошлина 2 000 ₽ больше самого долга 1 200 ₽" in html
    assert "больше пошлины в 0,6" not in html


def test_the_multiplier_is_declined_like_a_russian_number() -> None:
    """«в 5 раз», а не «в 5 раза»: экран читает человек."""
    from app.web.render_base import _times

    assert _times(Decimal("19.2")) == "19,2 раза"
    assert _times(Decimal("5")) == "5 раз"
    assert _times(Decimal("21")) == "21 раз"
    assert _times(Decimal("22")) == "22 раза"


def test_storage_says_last_not_only() -> None:
    """В базе лежит одна пара дат, а не список задержаний.

    Выгрузка отдаёт задержания строками, строки склеиваются в одного должника,
    и до столбца сохраняется только последний период. Назвать его «периодом
    хранения» значило бы обещать полный перечень, которого у страницы нет.
    """
    html = person("38400")

    assert "Последнее хранение" in html
    assert "Задержаний" in html
