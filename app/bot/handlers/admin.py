"""Operational commands for the allowlisted operators.

Every user of this bot is already trusted — the allowlist is the security
boundary — so these are diagnostics rather than a privilege tier.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.container import Container
from app.db.repository import AuditRepository, DebtorRepository
from app.domain.enums import PROVIDER_TITLES
from app.utils.dates import format_datetime
from app.utils.formatting import pluralize_ru
from app.utils.masking import mask_secret


def _flag(value: bool) -> str:
    return "включено" if value else "выключено"


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="admin")

    @router.message(Command("status"))
    async def handle_status(message: Message, container: Container) -> None:
        settings = container.settings
        async with container.database.session() as session:
            debtors = await DebtorRepository(session).count()
            audit_events = await AuditRepository(session).count()
        lines = [
            f"{settings.app_name} — состояние",
            "",
            f"Режим: {settings.app_mode.value}",
            f"Окружение: {settings.app_env}",
            f"Должников в базе: {debtors}",
            f"Событий аудита: {audit_events}",
            f"Кэш: {settings.cache_ttl_hours} ч" if settings.cache_enabled else "Кэш: выключен",
            f"Параллельность запросов: {settings.provider_concurrency}",
            f"Таймаут запроса: {settings.request_timeout_seconds:.0f} с",
            "",
            "Приватность:",
            f"• хранение сырых ответов: {_flag(settings.store_raw_responses)}",
            f"• хранение полных идентификаторов: {_flag(settings.store_sensitive_identifiers)}",
            "",
            "Источники:",
        ]
        lines.extend(
            f"{'✓' if provider.is_configured else '○'} "
            f"{PROVIDER_TITLES.get(provider.name, provider.name.value)}"
            for provider in container.registry.external
        )
        lines.append("")
        # Token presence is confirmed without ever printing the value.
        lines.append(f"Токен бота: {mask_secret(settings.telegram_bot_token)}")
        lines.append(f"Допущенных пользователей: {len(settings.allowed_user_ids)}")
        # Кто вообще попадает в бота — вопрос состояния системы, а не отдельного
        # экрана: открытый бот и бот с одобрением ведут себя по-разному, и
        # увидеть это надо там же, где смотрят всё остальное.
        if settings.telegram_access_is_open:
            lines.append("Доступ: открыт всем (ALLOWED_TELEGRAM_USER_IDS=*)")
        elif settings.access_moderation_enabled:
            owners = len(settings.owner_user_ids)
            lines.append(f"Доступ: по одобрению, владельцев — {owners} (/access)")
        else:
            lines.append("Доступ: только по списку в .env")
        await message.answer("\n".join(lines))

    @router.message(Command("revoke"))
    async def handle_revoke(message: Message, container: Container, user_id: int) -> None:
        """Погасить все свои действующие ссылки.

        Сценарий ровно один и он бытовой: переслал не туда. До этой команды
        утёкшая ссылка жила до конца TTL, и сделать с ней было нечего.

        Ответ обязан совпадать с тем, что произошло, вплоть до чужих ссылок на
        те же отчёты: уйти отсюда с верой в мёртвый адрес, который жив, — хуже,
        чем не нажимать кнопку вовсе.
        """
        outcome = await container.share_service.revoke_all(telegram_user_id=user_id)
        if outcome.revoked:
            noun = pluralize_ru(outcome.revoked, "ссылка", "ссылки", "ссылок")
            lines = [
                f"Отозвано {outcome.revoked} {noun}. Старые адреса больше не открываются — "
                "запросите отчёт заново, бот выдаст новый."
            ]
        else:
            lines = ["Действующих ссылок нет — отзывать нечего."]
        if outcome.left_to_others:
            lines.append(
                "Но на те же отчёты действуют ссылки других сотрудников: "
                f"{outcome.left_to_others}. Эти адреса продолжают открываться — "
                "отозвать их может только тот, кому их выдал бот."
            )
        await message.answer("\n\n".join(lines))

    @router.message(Command("audit"))
    async def handle_audit(message: Message, container: Container) -> None:
        async with container.database.session() as session:
            events = await AuditRepository(session).recent(limit=15)

        if not events:
            await message.answer("Журнал аудита пуст.")
            return

        lines = ["Последние события аудита:", ""]
        lines.extend(
            f"{format_datetime(event.created_at)} · {event.action}\n"
            f"   user={event.telegram_user_id} {event.detail or ''}".rstrip()
            for event in events
        )
        await message.answer("\n".join(lines))

    return router
