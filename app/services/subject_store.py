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
from datetime import timedelta

from app.domain.identity import SearchSubject
from app.utils.dates import utcnow

DEFAULT_TTL = timedelta(hours=1)
DEFAULT_CAPACITY = 500
TOKEN_BYTES = 8


class SubjectStore:
    """Bounded, expiring token -> subject map."""

    def __init__(self, *, ttl: timedelta = DEFAULT_TTL, capacity: int = DEFAULT_CAPACITY) -> None:
        self._ttl = ttl
        self._capacity = capacity
        self._items: OrderedDict[str, tuple[SearchSubject, float]] = OrderedDict()

    def put(self, subject: SearchSubject) -> str:
        self._evict_expired()
        token = secrets.token_urlsafe(TOKEN_BYTES)
        self._items[token] = (subject, utcnow().timestamp())
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)
        return token

    def get(self, token: str) -> SearchSubject | None:
        self._evict_expired()
        entry = self._items.get(token)
        return entry[0] if entry else None

    def _evict_expired(self) -> None:
        cutoff = utcnow().timestamp() - self._ttl.total_seconds()
        expired = [key for key, (_, stored_at) in self._items.items() if stored_at < cutoff]
        for key in expired:
            del self._items[key]

    def __len__(self) -> int:
        return len(self._items)
