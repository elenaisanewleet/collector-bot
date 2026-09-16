"""Short-lived store for subjects referenced by callback buttons.

Telegram caps callback data at 64 bytes, which cannot hold a search subject. The
button carries an opaque token instead and the subject stays in memory here.

Deliberately in-process and time-limited: this is UI state, not data. Losing it
on restart costs the operator one re-entry, and keeping personal data out of a
persistent store is worth that.

С появлением паспортного шага в поиске физлица здесь до часа держится и паспорт
— в памяти процесса, на диск он отсюда не попадает ни при каком флаге. Два
следствия, о которых стоит знать: кнопка «повторить» отправит серию и номер в
ФНС ещё раз и оплатит ещё один вызов моста, а до истечения TTL они остаются в
адресном пространстве бота.
"""

from __future__ import annotations

import secrets
from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta

from app.domain.identity import SearchSubject
from app.utils.dates import utcnow

DEFAULT_TTL = timedelta(hours=1)
DEFAULT_CAPACITY = 500
TOKEN_BYTES = 8


class SubjectStore:
    """Bounded, expiring token -> subject map.

    За одним токеном живут ДВА субъекта, и путать их нельзя:

    *   **ответ** (:meth:`get`) — субъект, с которым прошёл прогон: с именем от
        моста, с датой рождения из выгрузки, с ИНН, добытым по паспорту.
        Кнопки «Уточнить данные» и сужение по региону рассуждают о нём, потому
        что говорят о полученном отчёте;
    *   **вопрос** (:meth:`question`) — то, что оператор ввёл. Нужен ровно
        одному действию: «Спросить заново».

    ПОЧЕМУ ВОПРОС ПРИШЛОСЬ ХРАНИТЬ ОТДЕЛЬНО. «Спросить заново» переспрашивала
    источники, но не ЛИЧНОСТЬ: она повторяла прогон с уже обогащённым
    субъектом, а мост по телефону зовётся только когда имени нет
    (``PhoneNameProvider.is_needed``). Имя в замороженном субъекте было —
    значит мост пропускался, и всё, что он однажды вывел неверно, уезжало в
    источники снова. В том числе адрес: по нему уходит ПЛАТНЫЙ запрос в
    Росреестр, и переспросить его было нечем — кнопка с таким названием этого
    не делала.

    Дороже всего это стоило после правки разбора: код уже выбирал адрес верно,
    а кнопка продолжала подставлять адрес, выбранный старым правилом, — и
    выглядело это как «данные закешировались».
    """

    def __init__(self, *, ttl: timedelta = DEFAULT_TTL, capacity: int = DEFAULT_CAPACITY) -> None:
        self._ttl = ttl
        self._capacity = capacity
        self._items: OrderedDict[str, _Entry] = OrderedDict()

    def put(self, subject: SearchSubject, *, question: SearchSubject | None = None) -> str:
        """Запомнить ответ и, если он известен, вопрос, из которого он вышел."""
        self._evict_expired()
        token = secrets.token_urlsafe(TOKEN_BYTES)
        self._items[token] = _Entry(
            answer=subject, question=question, stored_at=utcnow().timestamp()
        )
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)
        return token

    def get(self, token: str) -> SearchSubject | None:
        """Субъект, с которым прошёл прогон."""
        self._evict_expired()
        entry = self._items.get(token)
        return entry.answer if entry else None

    def question(self, token: str) -> SearchSubject | None:
        """То, что оператор ввёл, — чтобы спросить заново с нуля.

        Откат на ответ намеренный: у поисков, которые кладут токен без вопроса
        (например, выбор должника по номеру договора), спрашивать заново
        по-прежнему есть чем. Хуже повторить обогащённый субъект, чем не
        повторить ничего.
        """
        self._evict_expired()
        entry = self._items.get(token)
        if entry is None:
            return None
        return entry.question or entry.answer

    def _evict_expired(self) -> None:
        cutoff = utcnow().timestamp() - self._ttl.total_seconds()
        expired = [key for key, entry in self._items.items() if entry.stored_at < cutoff]
        for key in expired:
            del self._items[key]

    def __len__(self) -> int:
        return len(self._items)


@dataclass(frozen=True, slots=True)
class _Entry:
    answer: SearchSubject
    question: SearchSubject | None
    stored_at: float
