"""Persistent tables.

What is stored is deliberately narrow. Full phone numbers and passport numbers
are not written here unless ``STORE_SENSITIVE_IDENTIFIERS`` is on; the history
and audit trails keep masked queries and one-way hashes, which is enough to
answer "who searched for what, when" without becoming a copy of the source data.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Money, UtcDateTime
from app.utils.dates import utcnow


class Debtor(Base):
    """A debtor as our own systems know them, from CSV import or manual entry."""

    __tablename__ = "debtors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    external_debtor_id: Mapped[str | None] = mapped_column(String(64), index=True)
    fio: Mapped[str | None] = mapped_column(String(255), index=True)
    fio_normalized: Mapped[str | None] = mapped_column(String(255), index=True)
    birth_date: Mapped[date | None] = mapped_column(Date)
    # Full number only when the deployment opted in; otherwise the masked form
    # plus a hash is all that is retained.
    phone: Mapped[str | None] = mapped_column(String(20))
    phone_masked: Mapped[str | None] = mapped_column(String(32))
    phone_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    # ИНН физлица не маскируется и не хэшируется, в отличие от телефона: по нему
    # ищут банкротство, ИП и арбитраж, и маскированный он для этого бесполезен.
    inn: Mapped[str | None] = mapped_column(String(12), index=True)
    contract_number: Mapped[str | None] = mapped_column(String(64), index=True)
    claim_number: Mapped[str | None] = mapped_column(String(64), index=True)
    debt_amount: Mapped[Decimal | None] = mapped_column(Money)
    address: Mapped[str | None] = mapped_column(String(512))
    vehicle_plate: Mapped[str | None] = mapped_column(String(16), index=True)
    vin: Mapped[str | None] = mapped_column(String(17), index=True)
    source: Mapped[str] = mapped_column(String(32), default="csv_import")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (Index("ix_debtors_fio_birth", "fio_normalized", "birth_date"),)


class SearchRequest(Base):
    """One search initiated by an authorized operator."""

    __tablename__ = "search_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, index=True)
    search_type: Mapped[str] = mapped_column(String(32))
    # One-way hash of the normalized query: enough to find an equivalent earlier
    # search for caching, without storing the query itself.
    normalized_query_hash: Mapped[str] = mapped_column(String(64), index=True)
    masked_query: Mapped[str] = mapped_column(String(255))
    # The query itself, minus anything the privacy settings exclude. Stored so
    # "повторить проверку" survives a restart; see services.search.redact_subject
    # for exactly what is dropped.
    subject_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)

    results: Mapped[list[SearchResult]] = relationship(
        back_populates="request", cascade="all, delete-orphan", lazy="selectin"
    )
    report: Mapped[DebtorReportRow | None] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )

    __table_args__ = (
        Index("ix_search_requests_user_created", "telegram_user_id", "created_at"),
        Index("ix_search_requests_hash_created", "normalized_query_hash", "created_at"),
    )


class SearchResult(Base):
    """One provider's answer within a search, kept for the cache and for audit."""

    __tablename__ = "search_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_request_id: Mapped[int] = mapped_column(
        ForeignKey("search_requests.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_status: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    normalized_json: Mapped[str] = mapped_column(Text, default="[]")
    # Populated only when STORE_RAW_RESPONSES is enabled.
    raw_response: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(512))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    # «Источник ответил, но прислал не всё» — и чем именно неполон ответ.
    #
    # Хранится, потому что отчёт может быть собран заново из кэша, а без этих
    # двух колонок пересобранный отчёт терял бы оговорку и печатал «залогов не
    # найдено» там, где сутки назад честно писал «найдено 13, сопоставлено 0».
    # Кэш не имеет права быть добрее исходного ответа.
    is_partial: Mapped[bool] = mapped_column(Boolean, default=False)
    notes_json: Mapped[str] = mapped_column(Text, default="[]")

    request: Mapped[SearchRequest] = relationship(back_populates="results")


class DebtorReportRow(Base):
    """The score attached to a completed search."""

    __tablename__ = "debtor_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_request_id: Mapped[int] = mapped_column(
        ForeignKey("search_requests.id", ondelete="CASCADE"), index=True
    )
    score: Mapped[int] = mapped_column(Integer)
    score_confidence: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(16))
    factors_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    request: Mapped[SearchRequest] = relationship(back_populates="report")

    __table_args__ = (UniqueConstraint("search_request_id", name="uq_report_request"),)


class BatchRun(Base):
    """Один прогон массовой проверки по выгрузке."""

    __tablename__ = "batch_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    items: Mapped[list[BatchItem]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class BatchItem(Base):
    """Результат по одному должнику внутри прогона.

    Хранится вердикт и суммы, а не весь отчёт: очередь читается целиком и часто,
    а полный отчёт уже лежит в search_results.
    """

    __tablename__ = "batch_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_run_id: Mapped[int] = mapped_column(
        ForeignKey("batch_runs.id", ondelete="CASCADE"), index=True
    )
    debtor_id: Mapped[int] = mapped_column(ForeignKey("debtors.id", ondelete="CASCADE"))
    search_request_id: Mapped[int | None] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(16), index=True)
    verdict_order: Mapped[int] = mapped_column(Integer, index=True)
    headline: Mapped[str] = mapped_column(String(512), default="")
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    debt_amount: Mapped[Decimal | None] = mapped_column(Money)
    # Money хранится текстом ради точности, а текст сортируется лексикографически
    # («87600» > «154200»). Для порядка в очереди нужен настоящий числовой ключ.
    debt_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    state_fee: Mapped[Decimal | None] = mapped_column(Money)
    fee_basis: Mapped[str] = mapped_column(String(16), default="none")
    score: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    run: Mapped[BatchRun] = relationship(back_populates="items")
    debtor: Mapped[Debtor] = relationship(lazy="selectin")

    __table_args__ = (
        Index("ix_batch_items_run_order", "batch_run_id", "verdict_order", "debt_kopecks"),
    )


class ShareLink(Base):
    """Ссылка на веб-отчёт.

    Токен непредсказуем и живёт ограниченное время: по этому адресу лежат
    персональные данные должника, а страница открывается без авторизации —
    ровно как отчёт по ссылке в знакомых оператору сервисах. Непредсказуемость
    и срок жизни здесь и есть контроль доступа, поэтому токен длинный, а
    просроченная ссылка отдаёт 404, а не содержимое.
    """

    __tablename__ = "share_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    target_id: Mapped[int] = mapped_column(Integer, index=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    opened_count: Mapped[int] = mapped_column(Integer, default=0)
    last_opened_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Отзыв. Утёкшую ссылку надо чем-то закрыть до истечения срока: сценарий,
    # ради которого отзыв и нужен, — «переслал не туда», и он случается.
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class AccessRequest(Base):
    """Заявка на доступ и решение владельца по ней.

    Одна строка на человека, а не журнал заявок: вопрос, на который эта таблица
    отвечает, — «пускать ли его сейчас», и он имеет ровно один ответ. История
    решений при этом не теряется — approve, reject и revoke пишутся в
    ``audit_events``, который для того и append-only.

    Живёт в базе, а не в памяти процесса, потому что перезапуск бота не должен
    ни отбирать выданный доступ, ни возвращать отобранный, ни обнулять суточную
    паузу для отклонённого — иначе она обходится любым падением.

    Имя и username хранятся как строки: владелец решает по ним, кого пускает, а
    к моменту показа списка человек может и не писать боту, чтобы Telegram
    прислал их заново.
    """

    __tablename__ = "access_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(255))
    # pending / approved / rejected / revoked — см. app.services.access.
    status: Mapped[str] = mapped_column(String(16), index=True)
    # Время последней поданной заявки. На нём держится суточная пауза, поэтому
    # оно обновляется при подаче, а не при решении.
    requested_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    decided_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class QueryCard(Base):
    """Накопительная карточка запроса: что оператор уже собрал про должника.

    Живёт в базе, а не в FSM, и это не про надёжность хранилища — про роутеры.
    Все конкурирующие текстовые хендлеры (госномер, VIN, адрес, договор,
    паспорт, импорт) привязаны к состояниям, а свободный текст ловит последний
    роутер с ``StateFilter(None)``. Сделай карточку состоянием — и она забрала
    бы себе весь текст, сняв этот фильтр. Строка в таблице позволяет ей помнить
    всё, не занимая состояние вовсе: ``awaiting_field`` — это колонка, а не
    ``State``.

    Ключ — пара «пользователь + чат», а не один ``chat_id``: бот умеет работать
    в группе, и одна карточка на чат склеила бы двух операторов в одного
    должника.

    Три отдельные nullable-колонки под ФИО, а не :class:`PersonName`: он требует
    фамилию **и** имя, а карточка обязана существовать при одной фамилии —
    ровно ради этого она и заведена. ``PersonName`` собирается лениво, в момент
    «Проверить».

    **Колонок ``passport`` и ``phone`` здесь нет вовсе** — ни под каким флагом.
    ``redact_subject`` уже выбрасывает их перед записью в ``subject_json``, а
    ``SubjectStore`` держит паспорт только в памяти и только час. Карточка —
    черновик, живущий минуты; платить за него отменой действующей политики
    приватности нечем. В базу едут только маски, они необратимы, а сами номера
    лежат в памяти процесса (``app.services.query_card.CardSecrets``). После
    перезапуска карточка честно пишет «сам номер не храню, пришлите заново» —
    это не молчание и не прочерк.
    """

    __tablename__ = "query_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, index=True)
    chat_id: Mapped[int] = mapped_column(Integer)
    # Какое сообщение править. Переживает рестарт: иначе после перезапуска бот
    # присылал бы вторую карточку под первой, и обе выглядели бы живыми.
    card_message_id: Mapped[int | None] = mapped_column(Integer)
    last_name: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(64))
    middle_name: Mapped[str | None] = mapped_column(String(64))
    birth_date: Mapped[date | None] = mapped_column(Date)
    inn: Mapped[str | None] = mapped_column(String(12))
    phone_masked: Mapped[str | None] = mapped_column(String(32))
    passport_masked: Mapped[str | None] = mapped_column(String(32))
    plate: Mapped[str | None] = mapped_column(String(16))
    vin: Mapped[str | None] = mapped_column(String(17))
    # Договор и адрес ищутся только в нашей выгрузке: ни один внешний реестр по
    # ним не работает. В карточке они стоят потому, что выгрузка 1С — главный
    # источник продукта, а не вспомогательный.
    contract_number: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(String(256))
    # Поля, которые оператор пропустил ЯВНО. Отличается от пустого: пустое —
    # «я не спрашивал», пропущенное — «спросил, ответили, что не знают». В
    # отчёт эта разница не едет (там она про оператора, а не про качество
    # проверки), но карточка обязана её показывать, иначе кнопка «Пропустить»
    # неотличима от «Отмена».
    skipped_json: Mapped[str] = mapped_column(Text, default="[]")
    # Какое поле ждём после нажатия кнопки. Заменяет состояние FSM — см.
    # докстринг класса.
    awaiting_field: Mapped[str | None] = mapped_column(String(16))
    # Идём ли по трём основным шагам — телефон, фамилия, имя. Тоже колонка, и
    # по той же причине: оператор бросает недособранную карточку чаще, чем
    # перезапускается бот, и вернуться он должен на тот же вопрос.
    guided: Mapped[bool] = mapped_column(Boolean, default=False)
    # Набор полей прошлого прогона. На нём держится отказ платить дважды за
    # один и тот же запрос: «Проверить» без единого нового поля — это не
    # проверка, это списание.
    last_run_hash: Mapped[str | None] = mapped_column(String(64))
    checked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Последняя проверка не нашла ничего. Пустой отчёт без предложения искать
    # иначе — тупик: у оператора на руках может лежать ИНН из договора или
    # госномер машины, а бот молчит.
    last_run_empty: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("telegram_user_id", "chat_id", name="uq_query_cards_user_chat"),
    )


class AuditEvent(Base):
    """Append-only trail of who did what.

    Never contains query contents — only the action, an entity reference and a
    masked detail string.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
