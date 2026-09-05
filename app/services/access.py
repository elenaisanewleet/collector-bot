"""Доступ к боту: кто пущен, кто ждёт решения, кому отказано.

Раньше ответ на вопрос «пускать ли» жил целиком в ``.env``: список числовых ID,
который правится на сервере с перезапуском бота, — и символ «*», открывающий
бота всем, кто его найдёт. Между этими двумя состояниями не было ничего, а
нужно было именно среднее: незнакомец пишет, владелец видит, кто он, и решает
кнопкой.

Порядок проверки здесь важнее самих проверок и потому записан один раз, в
:meth:`AccessService.is_allowed`:

1. «*» — бот открыт, и это осознанное решение владельца, а не сбой;
2. владелец — допущен всегда, иначе одобрять заявки было бы некому;
3. список из ``.env`` — то, что работало до этого модуля, работает как работало;
4. решение из базы — то, что владелец нажал кнопкой.

Решения лежат в базе, а не в памяти: перезапуск не должен ни возвращать
отобранный доступ, ни отбирать выданный, ни обнулять суточную паузу
отклонённому — иначе паузу обходит любое падение процесса.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from app.config import Settings
from app.db.models import AccessRequest
from app.db.repository import AccessRepository, AuditRepository
from app.db.session import Database
from app.logging_setup import get_logger
from app.utils.dates import utcnow

logger = get_logger(__name__)

# Отклонённый ждёт сутки до следующей заявки. Не защита от злоумышленника — он
# заведёт второй аккаунт, — а защита владельца от уведомлений: без паузы «нет»
# ничего не значит, и человек, которому очень надо, пишет каждые пять минут.
REQUEST_COOLDOWN_HOURS = 24


class AccessStatus(StrEnum):
    """Состояние строки в ``access_requests``.

    ``REJECTED`` и ``REVOKED`` различаются намеренно: первое — «не пускали»,
    второе — «пускали и передумали». Для доступа они значат одно и то же, но
    владельцу в списке это разные строки, а в аудите — разные события.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


class RequestOutcome(StrEnum):
    """Что случилось с попыткой незнакомца попасть в бота."""

    #: Заявка создана — владельцу надо показать её сейчас.
    CREATED = "created"
    #: Заявка уже висит: владельца второй раз не дёргаем.
    PENDING = "pending"
    #: Отказано, и сутки ещё не прошли.
    THROTTLED = "throttled"
    #: Доступ уже есть — сюда попадать не должны, но пусть будет честный ответ.
    ALLOWED = "allowed"


@dataclass(frozen=True, slots=True)
class RequestResult:
    outcome: RequestOutcome
    #: Сколько часов ждать до следующей попытки. Осмысленно только у THROTTLED.
    retry_after_hours: int = 0


class AccessService:
    """Единственное место, где решается вопрос доступа."""

    def __init__(self, settings: Settings, database: Database) -> None:
        self._settings = settings
        self._database = database

    @property
    def moderation_enabled(self) -> bool:
        return self._settings.access_moderation_enabled

    @property
    def owner_ids(self) -> frozenset[int]:
        return self._settings.owner_user_ids

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self._settings.owner_user_ids

    async def is_allowed(self, user_id: int | None) -> bool:
        """Пускать ли этого человека. Порядок проверок — в docstring модуля."""
        if user_id is None:
            return False
        if self._settings.telegram_access_is_open:
            return True
        if user_id in self._settings.owner_user_ids:
            return True
        if user_id in self._settings.allowed_user_ids:
            return True
        if not self.moderation_enabled:
            # Одобрять было некому, значит и одобренных быть не может. Строка в
            # базе от прежней конфигурации не должна пускать в бота, у которого
            # больше нет владельца.
            return False
        async with self._database.session() as session:
            row = await AccessRepository(session).get(user_id)
        return row is not None and row.status == AccessStatus.APPROVED

    async def submit_request(
        self, *, user_id: int, username: str | None, full_name: str | None
    ) -> RequestResult:
        """Заявка от незнакомца.

        Владельцу уходит уведомление только на :attr:`RequestOutcome.CREATED`.
        Всё остальное — «уже отправлено» и «ждите сутки» — человек читает сам, и
        владелец об этих сообщениях не знает.
        """
        async with self._database.session() as session:
            repo = AccessRepository(session)
            row = await repo.get(user_id)

            if row is not None and row.status == AccessStatus.APPROVED:
                return RequestResult(RequestOutcome.ALLOWED)
            if row is not None and row.status == AccessStatus.PENDING:
                return RequestResult(RequestOutcome.PENDING)
            if row is not None:
                remaining = _cooldown_remaining_hours(row)
                if remaining:
                    return RequestResult(RequestOutcome.THROTTLED, retry_after_hours=remaining)

            await repo.upsert_request(
                telegram_user_id=user_id,
                username=username,
                full_name=full_name,
                status=AccessStatus.PENDING,
            )
            # Заявка — это событие, за которым стоит живой человек и решение
            # владельца. В журнале должно остаться и то, и другое.
            await AuditRepository(session).record(
                telegram_user_id=user_id,
                action="access.requested",
                entity_id=str(user_id),
            )
        logger.info("access.requested", user_id=user_id)
        return RequestResult(RequestOutcome.CREATED)

    async def approve(self, user_id: int, *, by: int) -> AccessRequest | None:
        return await self._decide(user_id, status=AccessStatus.APPROVED, by=by)

    async def reject(self, user_id: int, *, by: int) -> AccessRequest | None:
        return await self._decide(user_id, status=AccessStatus.REJECTED, by=by)

    async def revoke(self, user_id: int, *, by: int) -> AccessRequest | None:
        """Отобрать выданный доступ.

        Статус отдельный от :attr:`AccessStatus.REJECTED`, хотя для доступа они
        значат одно и то же. Разница в том, что владелец видит в списке: «не
        пускали» и «пускали и передумали» — это разные истории и разные поводы
        передумать ещё раз. Пауза на новую заявку после отзыва такая же
        суточная: сразу писать «верните» — то же самое, что спорить с отказом.
        """
        return await self._decide(user_id, status=AccessStatus.REVOKED, by=by)

    async def _decide(self, user_id: int, *, status: AccessStatus, by: int) -> AccessRequest | None:
        async with self._database.session() as session:
            row = await AccessRepository(session).set_status(user_id, status=status, decided_by=by)
            if row is None:
                return None
            await AuditRepository(session).record(
                telegram_user_id=by,
                action=f"access.{status.value}",
                entity_id=str(user_id),
                detail=f"by owner {by}",
            )
            # Строка отвязывается от сессии: вызывающий читает её поля уже после
            # выхода из контекста, а session.commit() истёк бы ленивой загрузкой.
            session.expunge(row)
        logger.info("access.decided", user_id=user_id, status=status.value, owner_id=by)
        return row

    async def pending(self) -> list[AccessRequest]:
        return await self._by_status(AccessStatus.PENDING)

    async def approved(self) -> list[AccessRequest]:
        return await self._by_status(AccessStatus.APPROVED)

    async def refused(self) -> list[AccessRequest]:
        return await self._by_status(AccessStatus.REJECTED, AccessStatus.REVOKED)

    async def _by_status(self, *statuses: AccessStatus) -> list[AccessRequest]:
        async with self._database.session() as session:
            rows = await AccessRepository(session).by_status(*(s.value for s in statuses))
            for row in rows:
                session.expunge(row)
        return rows


def _cooldown_remaining_hours(row: AccessRequest) -> int:
    """Сколько часов отклонённому ещё ждать. ``0`` — можно подавать.

    Отсчёт идёт от более позднего из двух событий — подачи и решения. Если
    считать только от подачи, то владелец, разобравший заявку через три дня,
    получил бы новую тем же вечером: пауза, которую он думал поставить нажатием
    «Отклонить», к тому моменту уже истекла бы.

    Округление вверх: сказать «через 0 ч», когда осталось сорок минут, значит
    отправить человека пробовать снова прямо сейчас и получить тот же отказ.
    """
    since = max(row.requested_at, row.decided_at or row.requested_at)
    ready_at = since + timedelta(hours=REQUEST_COOLDOWN_HOURS)
    remaining = (ready_at - utcnow()).total_seconds()
    if remaining <= 0:
        return 0
    return max(1, math.ceil(remaining / 3600))


__all__ = [
    "REQUEST_COOLDOWN_HOURS",
    "AccessService",
    "AccessStatus",
    "RequestOutcome",
    "RequestResult",
]
