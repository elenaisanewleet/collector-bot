"""Ссылки на веб-отчёты.

Отчёт живёт дольше сообщения в чате: оператор открывает ссылку с телефона,
пересылает юристу, возвращается к ней завтра. Поэтому ссылка — это запись в
базе, а не подпись в URL.

Доступ здесь держится на двух вещах и больше ни на чём: токен непредсказуем и
ссылка истекает. Это осознанный размен — страница открывается без пароля, чтобы
её можно было переслать, и ровно поэтому токен длинный, срок короткий, а
страница закрыта от индексации.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum

from app.config import Settings
from app.db.models import ShareLink
from app.db.repository import ShareLinkRepository
from app.db.session import Database
from app.logging_setup import get_logger

logger = get_logger(__name__)

# 32 байта случайности: угадать такой токен перебором невозможно, а ссылка
# остаётся достаточно короткой, чтобы её переслать одним сообщением.
TOKEN_BYTES = 32


class ShareKind(StrEnum):
    REPORT = "report"
    QUEUE = "queue"


@dataclass(frozen=True, slots=True)
class ShareTarget:
    kind: ShareKind
    target_id: int


class ShareLinkService:
    def __init__(self, settings: Settings, database: Database) -> None:
        self._settings = settings
        self._database = database

    @property
    def enabled(self) -> bool:
        return self._settings.web_links_enabled

    async def issue(
        self, target: ShareTarget, *, telegram_user_id: int, reuse: bool = True
    ) -> str | None:
        """Выдать ссылку. ``None`` — если веб-отчёты выключены.

        По умолчанию переиспользует действующую ссылку на тот же отчёт: иначе
        каждое открытие плодило бы новый адрес, и отозвать их все стало бы
        нечем.
        """
        if not self.enabled:
            return None

        async with self._database.session() as session:
            repo = ShareLinkRepository(session)
            if reuse:
                existing = await repo.find_for_target(target.kind.value, target.target_id)
                if existing is not None:
                    return self.url_for(existing.token, target.kind)
            link = await repo.create(
                token=secrets.token_urlsafe(TOKEN_BYTES),
                kind=target.kind.value,
                target_id=target.target_id,
                telegram_user_id=telegram_user_id,
                ttl_hours=self._settings.share_link_ttl_hours,
            )
            token = link.token

        logger.info("share.issued", kind=target.kind.value, user_id=telegram_user_id)
        return self.url_for(token, target.kind)

    def url_for(self, token: str, kind: ShareKind) -> str:
        prefix = "r" if kind is ShareKind.REPORT else "q"
        return f"{self._settings.web_public_url}/{prefix}/{token}"

    async def resolve(self, token: str, kind: ShareKind) -> ShareLink | None:
        """Найти живую ссылку и отметить открытие."""
        async with self._database.session() as session:
            repo = ShareLinkRepository(session)
            link = await repo.find_active(token)
            if link is None or link.kind != kind.value:
                return None
            await repo.mark_opened(link.id)
            return link
