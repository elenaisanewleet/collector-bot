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

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from enum import StrEnum

from app.config import Settings
from app.db.models import ShareLink
from app.db.repository import AuditRepository, ShareLinkRepository
from app.db.session import Database
from app.logging_setup import get_logger

logger = get_logger(__name__)

# 32 байта случайности: угадать такой токен перебором невозможно, а ссылка
# остаётся достаточно короткой, чтобы её переслать одним сообщением.
TOKEN_BYTES = 32


class ShareKind(StrEnum):
    REPORT = "report"
    QUEUE = "queue"
    # Справочник должников целиком. Отдельный вид, а не «очередь без прогона»:
    # за ним вся база, а не результат одной проверки, и срок жизни у него свой.
    BASE = "base"


@dataclass(frozen=True, slots=True)
class ShareTarget:
    kind: ShareKind
    target_id: int


@dataclass(frozen=True, slots=True)
class RevokeOutcome:
    """Итог отзыва.

    Двух чисел, а не одного, потому что «отозвано ноль» и «отозвано ноль, а
    отчёт всё ещё открывается» — разные новости, и вторую человек обязан
    услышать: отзыв здесь единственная аварийная кнопка, и уйти после неё с
    верой в мёртвую ссылку хуже, чем не нажать её вовсе.

    ``left_to_others`` — живые ссылки на те же отчёты, выданные другим
    операторам. Погасить их этот человек не может: у каждого свой адрес, и
    чужой не он пересылал.
    """

    revoked: int
    left_to_others: int = 0


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

        По умолчанию переиспользует действующую ссылку этого же оператора на
        тот же отчёт: иначе каждое открытие плодило бы новый адрес, и отозвать
        их все стало бы нечем. Явный перевыпуск (``reuse=False``) сначала гасит
        старую — иначе «выдать новую и забыть прежнюю» не работало бы, а именно
        этого от перевыпуска и ждут.

        Именно «этого же оператора». Кэш отчётов общий: второй сотрудник,
        проверивший того же должника, попадал в кэш, нового запроса не
        появлялось, и бот присылал ему ссылку, выданную первому. Пересланная не
        туда, она не гасилась его ``/revoke`` — тот честно не находил ни одной
        своей ссылки и отвечал «отзывать нечего». Своя ссылка у каждого — и есть
        то, что делает аварийную кнопку рабочей.
        """
        if not self.enabled:
            return None

        async with self._database.session() as session:
            repo = ShareLinkRepository(session)
            # Протухшие записи чистятся здесь же: связка «оператор → какой
            # отчёт он смотрел» — готовая карта доступа к персданным, и
            # хранить её вечно незачем.
            purged = await repo.purge_expired()
            if reuse:
                existing = await repo.find_for_target(
                    target.kind.value, target.target_id, telegram_user_id=telegram_user_id
                )
                if existing is not None:
                    return self.url_for(existing.token, target.kind)
            else:
                await repo.revoke(
                    kind=target.kind.value,
                    target_id=target.target_id,
                    telegram_user_id=telegram_user_id,
                )
            link = await repo.create(
                token=secrets.token_urlsafe(TOKEN_BYTES),
                kind=target.kind.value,
                target_id=target.target_id,
                telegram_user_id=telegram_user_id,
                ttl_hours=self._ttl_for(target.kind),
            )
            token = link.token
            link_id = link.id
            # В аудит уходит идентификатор записи, но не токен: аудит не должен
            # сам стать хранилищем ключей доступа.
            await AuditRepository(session).record(
                telegram_user_id=telegram_user_id,
                action="share.issued",
                entity_id=str(link_id),
                detail=f"{target.kind.value}:{target.target_id}",
            )

        logger.info("share.issued", kind=target.kind.value, user_id=telegram_user_id, purged=purged)
        return self.url_for(token, target.kind)

    def _ttl_for(self, kind: ShareKind) -> int:
        """Срок жизни ссылки.

        У очереди он свой и заметно короче: за одной ссылкой на отчёт стоит
        один человек, а за ссылкой на прогон — вся выгрузка целиком. Радиус
        поражения отличается на три порядка, значит и обращение должно.
        """
        if kind in (ShareKind.QUEUE, ShareKind.BASE):
            return self._settings.share_queue_ttl_hours
        return self._settings.share_link_ttl_hours

    def person_token(self, link: ShareLink, debtor_id: int) -> str:
        """Токен на ОДНОГО должника, производный от ссылки на весь список.

        Зачем он вообще. Список — это вся база, и ссылка на него открывает всё.
        Если строка списка вела бы на «/список/человек/12», то переслать
        коллеге одного должника было бы нельзя: получатель отрезал бы хвост и
        получил остальные две тысячи. Токен на человека обязан быть отдельным.

        Почему производный, а не своя строка в базе. Строк понадобилось бы
        столько же, сколько должников, и заводились бы они при каждом открытии
        списка — две тысячи записей на один просмотр. Подпись даёт то же самое
        без единой записи.

        Что он гарантирует. Подделать нельзя: подпись на серверном секрете.
        Подставить чужой номер нельзя: номер входит в подпись. И, главное, он
        умирает вместе с общей ссылкой — её идентификатор тоже подписан, а при
        открытии проверяется, что она ещё жива. Отозвали список — погасли и все
        ссылки на людей из него.
        """
        payload = f"{link.id}.{debtor_id}"
        return f"{payload}.{self._sign(payload)}"

    def read_person_token(self, token: str) -> tuple[int, int] | None:
        """Разобрать токен человека в ``(id ссылки, id должника)``.

        ``None`` — подпись не сошлась или форма не та. Ошибка одна на все
        случаи намеренно: подробности здесь помогают только подбирающему.
        """
        parts = token.split(".")
        if len(parts) != 3:
            return None
        link_id, debtor_id, signature = parts
        if not secrets.compare_digest(signature, self._sign(f"{link_id}.{debtor_id}")):
            return None
        try:
            return int(link_id), int(debtor_id)
        except ValueError:
            return None

    def _sign(self, payload: str) -> str:
        """Подпись на секрете развёртывания.

        Секрет — токен бота: он есть всегда, уникален для установки и уже
        хранится как секрет. Заводить второй ключ ради подписи значило бы
        завести второе место, где его забудут поменять.
        """
        digest = hmac.new(
            self._settings.telegram_bot_token.encode(),
            payload.encode(),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:32]

    def url_for(self, token: str, kind: ShareKind) -> str:
        prefix = {ShareKind.REPORT: "r", ShareKind.QUEUE: "q", ShareKind.BASE: "b"}[kind]
        return f"{self._settings.web_public_url}/{prefix}/{token}"

    def export_urls(self, url: str, kind: ShareKind) -> tuple[str, str]:
        """Адреса выгрузки за тем же токеном: (текст или CSV, печать)."""
        suffix = "report.txt" if kind is ShareKind.REPORT else "queue.csv"
        return f"{url}/{suffix}", f"{url}/print"

    async def resolve(self, token: str, kind: ShareKind) -> ShareLink | None:
        """Найти живую ссылку и отметить открытие."""
        async with self._database.session() as session:
            repo = ShareLinkRepository(session)
            link = await repo.find_active(token)
            if link is None or link.kind != kind.value:
                return None
            await repo.mark_opened(link.id)
            # Открытие страницы с персданными обязано быть видно в /audit:
            # после инцидента это первое, куда смотрят.
            await AuditRepository(session).record(
                telegram_user_id=link.telegram_user_id,
                action="share.opened",
                entity_id=str(link.id),
                detail=f"{link.kind}:{link.target_id}",
            )
            return link

    async def revoke(self, target: ShareTarget, *, telegram_user_id: int) -> RevokeOutcome:
        """Погасить свои ссылки на один отчёт."""
        async with self._database.session() as session:
            repo = ShareLinkRepository(session)
            count = await repo.revoke(
                kind=target.kind.value,
                target_id=target.target_id,
                telegram_user_id=telegram_user_id,
            )
            others = await repo.count_live_of_others(
                [(target.kind.value, target.target_id)], telegram_user_id=telegram_user_id
            )
            if count:
                await AuditRepository(session).record(
                    telegram_user_id=telegram_user_id,
                    action="share.revoked",
                    entity_id=f"{target.kind.value}:{target.target_id}",
                    detail=f"links:{count}",
                )
        logger.info("share.revoked", kind=target.kind.value, count=count, others=others)
        return RevokeOutcome(revoked=count, left_to_others=others)

    async def revoke_all(self, *, telegram_user_id: int) -> RevokeOutcome:
        """Погасить все живые ссылки оператора.

        Чужие ссылки на те же отчёты не трогает — их пересылал не он, и у
        коллеги за таким адресом может стоять уже отправленный юристу отчёт. Но
        и молчать о них нельзя: считаются они до отзыва своих, потому что после
        отличить «моя» от «чужой» в отчёте уже не по чему.
        """
        async with self._database.session() as session:
            repo = ShareLinkRepository(session)
            targets = await repo.live_targets(telegram_user_id=telegram_user_id)
            others = await repo.count_live_of_others(targets, telegram_user_id=telegram_user_id)
            count = await repo.revoke_all(telegram_user_id=telegram_user_id)
            if count:
                await AuditRepository(session).record(
                    telegram_user_id=telegram_user_id,
                    action="share.revoked_all",
                    detail=f"links:{count}",
                )
        logger.info("share.revoked_all", user_id=telegram_user_id, count=count, others=others)
        return RevokeOutcome(revoked=count, left_to_others=others)
