"""Что помнит токен под кнопками отчёта.

За одним токеном живут два субъекта, и путать их нельзя: **ответ** — тот, с
которым прошёл прогон (с именем от моста, с ИНН, добытым по паспорту), и
**вопрос** — то, что оператор ввёл.

Разделение появилось не из аккуратности. «Спросить заново» повторяла прогон с
обогащённым субъектом, а мост по телефону зовётся только когда имени нет
(``PhoneNameProvider.is_needed``). Имя в замороженном субъекте было — значит
мост пропускался, и всё, что он однажды вывел неверно, уезжало в источники
снова. В том числе адрес: по нему уходит платный запрос в Росреестр.
"""

from __future__ import annotations

from datetime import timedelta

from app.domain.enums import SearchType
from app.domain.identity import PersonName, SearchSubject
from app.services.subject_store import SubjectStore

QUESTION = SearchSubject(search_type=SearchType.PERSON.value, phone="+79990000000")
ANSWER = SearchSubject(
    search_type=SearchType.PERSON.value,
    phone="+79990000000",
    name=PersonName(last_name="Тестова", first_name="Елена", middle_name="Николаевна"),
    address="г Москва, проспект Иной, д 73/2, кв 1",
    inn="770912345601",
)


def test_the_question_and_the_answer_are_kept_apart() -> None:
    """Один токен, два субъекта, и каждая кнопка берёт свой."""
    store = SubjectStore()

    token = store.put(ANSWER, question=QUESTION)

    # «Уточнить данные» и сужение по региону говорят о полученном отчёте.
    assert store.get(token) == ANSWER
    # «Спросить заново» обязана начать с того, что ввёл оператор.
    assert store.question(token) == QUESTION


def test_asking_again_does_not_replay_a_derived_identity() -> None:
    """В вопросе нет ничего, что мост вывел сам, — иначе он не будет вызван.

    Это и есть смысл разделения. Адрес и имя в ответе получены мостом; повтор с
    ними означает, что мост пропустят (имя уже есть), и однажды неверно
    выбранный адрес снова уедет в Росреестр — за деньги и без возможности
    переспросить.
    """
    store = SubjectStore()

    again = store.question(store.put(ANSWER, question=QUESTION))

    assert again is not None
    assert again.name is None, "имя от моста заморозило бы личность"
    assert again.address is None, "адрес от моста ушёл бы в ЕГРН повторно"
    assert again.inn is None
    # А то, что оператор действительно вводил, остаётся на месте.
    assert again.phone == "+79990000000"


def test_a_token_stored_without_a_question_still_answers() -> None:
    """Откат на ответ намеренный: спросить заново есть чем всегда.

    Токен кладут и другие поиски — например, выбор должника по номеру
    договора, — и вопроса при них нет. Хуже повторить обогащённый субъект, чем
    не повторить ничего.
    """
    store = SubjectStore()

    token = store.put(ANSWER)

    assert store.question(token) == ANSWER


def test_an_unknown_token_has_neither() -> None:
    store = SubjectStore()

    assert store.get("нет такого") is None
    assert store.question("нет такого") is None


def test_both_views_expire_together() -> None:
    """Вопрос живёт ровно столько же, сколько ответ: это одна запись."""
    store = SubjectStore(ttl=timedelta(seconds=-1))

    token = store.put(ANSWER, question=QUESTION)

    assert store.get(token) is None
    assert store.question(token) is None
