"""Application settings.

Everything that varies between deployments — credentials, endpoints, retention
policy — arrives through the environment. Nothing here has a value that would
make the application talk to a real external system by accident: every provider
stays *not configured* until its credentials are supplied.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppMode(StrEnum):
    """Which provider set the registry assembles.

    ``demo`` wires deterministic in-process providers so the whole pipeline can
    be exercised without credentials. ``live`` wires the real HTTP adapters,
    each of which reports ``NOT_CONFIGURED`` until it has what it needs.
    """

    DEMO = "demo"
    LIVE = "live"


class FNSBackend(StrEnum):
    """Which backend answers ЕГРЮЛ/ЕГРИП lookups.

    The registry data is public, but there is no single canonical free API, so
    the concrete vendor is a deployment choice rather than a code-level one.
    """

    NONE = "none"
    DEMO = "demo"
    GENERIC_JSON = "generic_json"
    # Served by the NewDB aggregator, whose key is already configured for ФССП.
    NEWDB = "newdb"


class FedresursBackend(StrEnum):
    NONE = "none"
    DEMO = "demo"
    GENERIC_JSON = "generic_json"
    NEWDB = "newdb"


class AuthStyle(StrEnum):
    """How a vendor expects credentials to be presented.

    Vendors differ; making this configuration rather than code means a new
    vendor needs an ``.env`` change, not an adapter.
    """

    NONE = "none"
    BEARER = "bearer"
    HEADER = "header"
    QUERY = "query"
    BASIC = "basic"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- app
    app_env: str = "development"
    app_mode: AppMode = AppMode.DEMO
    app_name: str = "Collector Bot"
    log_level: str = "INFO"
    log_json: bool = False

    # ---------------------------------------------------------------- telegram
    telegram_bot_token: str = ""
    allowed_telegram_user_ids: str = ""
    # Владельцы бота. Их доступ не обсуждается и не отзывается кнопкой — это те,
    # кто платит за запросы и отвечает за данные. Отдельная настройка, а не
    # первый элемент allowed_telegram_user_ids: список допущенных правится
    # руками и «*» в нём стирает всякий смысл порядка, а вопрос «кому уходят
    # заявки на доступ» должен иметь однозначный ответ.
    owner_telegram_user_ids: str = ""

    # ---------------------------------------------------------------- storage
    database_url: str = "sqlite+aiosqlite:///./collector_bot.db"
    internal_csv_path: Path = Path("./data/demo_debtors.csv")

    # ---------------------------------------------------------------- ФССП (NewDB)
    # The direct ФССП service (api-ip.fssp.gov.ru) is retired and answers
    # HTTP 410 Gone; enforcement proceedings come from the NewDB aggregator.
    newdb_api_key: str = ""
    newdb_base_url: str = "https://api.newdb.net"
    newdb_method_path: str = "/v2"
    # Живые тайминги: rosreestr — 49 с, arbitr_legal — 59 с. Прежние десять
    # попыток по две секунды бросали оба вызова оплаченными и «недоступными».
    # Произведение попыток на интервал обязано укладываться в бюджет источника
    # с запасом на HTTP: за этим следит _poll_must_fit_in_budget.
    newdb_poll_attempts: Annotated[int, Field(ge=1, le=60)] = 25
    newdb_poll_interval_seconds: Annotated[float, Field(ge=0.1, le=30)] = 3.0
    # Row schemas for every NewDB method except fssp_person, keyed by method
    # name. Only fssp_person has been read against a real response; the rest are
    # described by the deployment, and a method absent from this file is a
    # method that stays NOT_CONFIGURED rather than one this tool guesses at.
    newdb_field_map: Path | None = None

    # ---------------------------------------------------------------- ЕФРСБ
    fedresurs_backend: FedresursBackend = FedresursBackend.NONE
    fedresurs_base_url: str = ""
    fedresurs_username: str = ""
    fedresurs_password: str = ""
    fedresurs_api_key: str = ""
    fedresurs_search_path: str = ""
    fedresurs_auth_style: AuthStyle = AuthStyle.BEARER
    fedresurs_auth_name: str = "X-Api-Key"
    # Maps the vendor's JSON onto BankruptcyRecord. Required for a live backend:
    # no vendor schema is assumed.
    fedresurs_field_map: Path | None = None

    # ---------------------------------------------------------------- ФНС
    fns_provider: FNSBackend = FNSBackend.NONE
    fns_base_url: str = ""
    fns_api_key: str = ""
    fns_search_path: str = ""
    fns_auth_style: AuthStyle = AuthStyle.QUERY
    fns_auth_name: str = "key"
    fns_field_map: Path | None = None

    # ---------------------------------------------------------------- ЕГРН (rosreestr)
    # Отвечает про объект по адресу, а не про имущество должника: ЕГРН сведения
    # о правах конкретного лица выдаёт только самому лицу, суду и приставу.
    # Выключен по умолчанию — каждый вызов платный.
    rosreestr_enabled: bool = False
    # В массовом прогоне (поиск по человеку или договору) не вызывается даже при
    # включённом методе. При поиске по адресу оператор выбрал источник сам.
    rosreestr_in_batch: bool = False

    # ---------------------------------------------------------------- наследственные дела (ФНП)
    # Реестр наследственных дел Федеральной нотариальной палаты. Источник
    # БЕСПЛАТНЫЙ — ни в смету прогона, ни под настройку «в массовой проверке» он
    # не прячется, и когда включён, спрашивается всегда, где есть ФИО.
    #
    # Флаг существует не ради денег, а ради доступности: notariat.ru отвечает
    # только с российских адресов, и с машины разработчика имя даже не
    # резолвится. Выключенный источник говорит «не подключено» и не делает ни
    # одного сетевого обращения — это не «наследственных дел не найдено».
    inheritance_enabled: bool = False
    inheritance_base_url: str = "https://notariat.ru"

    # ---------------------------------------------------------------- арбитраж ЮЛ
    # Цепочка egrul_ip -> ИНН компаний -> arbitr_legal. Умножается на число
    # компаний, поэтому выключена по умолчанию и ограничена сверху колпаком.
    arbitr_legal_enabled: bool = False
    arbitr_legal_max_companies: Annotated[int, Field(ge=1, le=20)] = 3
    arbitr_legal_in_batch: bool = False
    arbitr_legal_concurrency: Annotated[int, Field(ge=1, le=8)] = 2

    # ---------------------------------------------------------------- http
    request_timeout_seconds: Annotated[float, Field(ge=1, le=120)] = 15.0
    # Жёсткий потолок на один источник целиком, поверх таймаута одного HTTP-
    # запроса. Асинхронные методы NewDB опрашиваются по кругу, и потолок,
    # выведенный из таймаута одного запроса, обрывал их раньше, чем агрегатор
    # успевал ответить: вызов оплачен, результат выброшен.
    provider_budget_seconds: Annotated[float, Field(ge=1, le=600)] = 150.0
    provider_concurrency: Annotated[int, Field(ge=1, le=32)] = 5
    provider_max_retries: Annotated[int, Field(ge=0, le=5)] = 2
    provider_retry_backoff_seconds: Annotated[float, Field(ge=0.0, le=10)] = 0.5

    # ---------------------------------------------------------------- cache
    cache_ttl_hours: Annotated[int, Field(ge=0, le=24 * 30)] = 24

    # ---------------------------------------------------------------- privacy
    store_raw_responses: bool = False
    store_sensitive_identifiers: bool = False
    # Сколько дней хранить историю проверок. За ней ФИО, дата рождения и ИНН, а
    # вместе с сырыми ответами — ещё и СНИЛС с адресом. 0 — не чистить, но это
    # осознанное решение, а не значение по умолчанию.
    history_retention_days: Annotated[int, Field(ge=0, le=3650)] = 90
    # Получение ИНН физлица по паспорту (метод NewDB passport_fns). Выключено по
    # умолчанию, и это не осторожность ради осторожности: включение отправляет
    # серию и номер паспорта в ФНС через агрегатор и добавляет ещё один платный
    # вызов на каждого должника — на прогоне в восемьсот строк это восемьсот
    # вызовов сверх сметы.
    inn_bridge_enabled: bool = False

    # ---------------------------------------------------------------- import
    max_import_file_bytes: Annotated[int, Field(ge=1024)] = 5 * 1024 * 1024
    max_import_rows: Annotated[int, Field(ge=1)] = 50_000

    # ---------------------------------------------------------------- вердикт
    # Требования до 500 000 ₽ рассматриваются в приказном порядке (ст. 121 ГПК РФ).
    court_order_max_amount: Annotated[int, Field(ge=0)] = 500_000
    # Во сколько раз долг должен превышать пошлину, чтобы процесс окупался.
    min_debt_to_fee_ratio: Annotated[float, Field(ge=1.0, le=100.0)] = 2.0

    # ---------------------------------------------------------------- веб-отчёты
    # Отчёт отдаётся ссылкой на страницу, а не простынёй в чат: в сообщении
    # Telegram нет ни таблиц, ни навигации, а смотреть надо на сорок строк
    # производств сразу.
    web_enabled: bool = True
    # Слушаем только петлю: наружу порт выставляет TLS-терминатор, а не
    # приложение. Токен ездит в пути URL, и открытый в мир http-порт означает
    # ссылку с персданными открытым текстом на всём маршруте.
    web_host: str = "127.0.0.1"
    web_port: Annotated[int, Field(ge=1, le=65535)] = 8080
    # Публичный адрес, который уходит в ссылку. Пустой — ссылки не отправляются:
    # бот не должен слать URL, по которому оператор не откроет страницу.
    web_public_url: str = ""
    # Разрешить http в публичном адресе. Только для локальной отладки: по http
    # токен доступа виден любому промежуточному узлу.
    web_allow_insecure: bool = False
    # Ссылка живёт ограниченное время: за ней персональные данные должника.
    share_link_ttl_hours: Annotated[int, Field(ge=1, le=24 * 30)] = 72
    # У очереди срок свой и короче: за одной ссылкой стоит вся выгрузка.
    share_queue_ttl_hours: Annotated[int, Field(ge=1, le=24 * 7)] = 12

    # ---------------------------------------------------------------- массовая проверка
    # Каждый должник — это реальные запросы к платным источникам, поэтому прогон
    # ограничен и требует подтверждения оператора.
    batch_max_debtors: Annotated[int, Field(ge=1, le=100_000)] = 5_000
    batch_concurrency: Annotated[int, Field(ge=1, le=32)] = 4
    # Как часто обновлять сообщение с прогрессом, в обработанных должниках.
    batch_progress_every: Annotated[int, Field(ge=1, le=1_000)] = 10
    # Сколько стоит одно обращение к платному источнику, рублей. Ноль — цена не
    # задана, и это не то же самое, что «бесплатно»: смета в этом случае честно
    # говорит, что рублёвую сумму назвать нечем, и остаётся в обращениях.
    # Придумывать здесь значение по умолчанию нельзя — тариф у каждого договора
    # свой, а смета, назвавшая чужую цену, хуже сметы, промолчавшей о цене.
    provider_request_cost: Annotated[Decimal, Field(ge=0)] = Decimal("0")

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.strip().upper() or "INFO"

    @field_validator(
        "newdb_base_url",
        "web_public_url",
        "fedresurs_base_url",
        "fns_base_url",
        "inheritance_base_url",
    )
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @model_validator(mode="after")
    def _poll_must_fit_in_budget(self) -> Settings:
        """Опрос агрегатора обязан укладываться в бюджет источника.

        Настройки разъехались молча и дорого: тридцать попыток по три секунды
        дали ровно девяносто секунд при бюджете в девяносто, и ФССП — источник,
        по которому принимается решение, — обрывалась на последней попытке.
        Вызов уже оплачен, ответ выброшен, в отчёте «источник не ответил».
        Оператор видел «проверить руками» и считал, что должник сложный, тогда
        как сложной была конфигурация.

        Проверка падает на старте, а не правит значения втихую: подогнанный
        бюджет — это тот же молчаливый разъезд, только на шаг позже. Запас
        нужен на сами HTTP-обращения между опросами, их не покрывает интервал.
        """
        poll_seconds = self.newdb_poll_attempts * self.newdb_poll_interval_seconds
        needed = poll_seconds + self.request_timeout_seconds
        if needed > self.provider_budget_seconds:
            raise ValueError(
                f"опрос NewDB требует {needed:.0f} с "
                f"({self.newdb_poll_attempts} попыток × "
                f"{self.newdb_poll_interval_seconds:g} с плюс "
                f"{self.request_timeout_seconds:g} с на запрос), "
                f"а бюджет источника — {self.provider_budget_seconds:g} с. "
                "Увеличьте PROVIDER_BUDGET_SECONDS или уменьшите опрос: "
                "иначе медленный источник всегда обрывается на полпути."
            )
        return self

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        """Parsed allowlist.

        Malformed entries are dropped rather than crashing the bot, but an empty
        result means *nobody* is allowed — the closed bot fails shut.
        """
        return _parse_user_ids(self.allowed_telegram_user_ids)

    @property
    def owner_user_ids(self) -> frozenset[int]:
        """Кому уходят заявки на доступ и кто может их одобрять.

        Владелец допущен всегда, даже если его забыли вписать в список
        допущенных: иначе одобрять заявки было бы некому — их некому было бы и
        увидеть.
        """
        return _parse_user_ids(self.owner_telegram_user_ids)

    @property
    def access_moderation_enabled(self) -> bool:
        """Режим «доступ по одобрению».

        Включается наличием владельца и выключается символом «*»: открытый бот
        пускает всех, и заявка в нём — экран, который никто никогда не увидит.
        Без владельца режима тоже нет — заявку было бы некому показать, и она
        молча легла бы в базу вместо честного «Доступ запрещён».
        """
        return bool(self.owner_user_ids) and not self.telegram_access_is_open

    @property
    def telegram_access_is_open(self) -> bool:
        """``*`` в списке — бот открыт всем, кто его найдёт.

        Осознанное исключение из правила «закрыт по умолчанию»: владелец ключа
        может решить, что доступ открыт. Последствия при этом реальные и не
        техническими средствами компенсируются — каждый чужой запрос тратит
        оплаченный баланс, а данные о людях тянутся настоящие, из официальных
        реестров, под учётной записью владельца. Поэтому открытие требует
        явного символа в настройке, а не пустого значения.
        """
        return "*" in self.allowed_telegram_user_ids

    @property
    def is_demo(self) -> bool:
        return self.app_mode is AppMode.DEMO

    @property
    def web_links_enabled(self) -> bool:
        """Отправлять ли ссылки на веб-отчёт.

        Без публичного адреса ссылка бесполезна, поэтому бот в этом случае
        остаётся на текстовом отчёте, а не шлёт нерабочий URL.
        """
        return self.web_enabled and bool(self.web_public_url)

    @property
    def web_url_is_insecure(self) -> bool:
        """Публичный адрес отдаёт токен доступа открытым текстом.

        Токен в пути URL — это bearer-credential: по http его видит любой узел
        на маршруте, а типовой nginx ещё и пишет полный ``$request_uri`` в
        access.log вместе со всеми его ротациями.
        """
        return bool(self.web_public_url) and not self.web_public_url.startswith("https://")

    @property
    def cache_enabled(self) -> bool:
        return self.cache_ttl_hours > 0

    @property
    def newdb_configured(self) -> bool:
        """Whether the NewDB aggregator can be called at all."""
        return bool(self.newdb_api_key and self.newdb_base_url)

    @property
    def newdb_methods_configured(self) -> bool:
        """Whether NewDB methods beyond ``fssp_person`` can be read.

        The key alone is not enough: without a row map there is nothing to parse
        the answer with, and a source we cannot parse is a source we have not
        checked.
        """
        return self.newdb_configured and self.newdb_field_map is not None

    @property
    def rosreestr_configured(self) -> bool:
        """Ключ есть и источник включён настройкой.

        Карта полей здесь ни при чём: живой ответ ``rosreestr`` прочитан, права
        и обременения в нём — массивы объектов, а плоская карта достаёт только
        скаляры. Разбор поэтому в коде, а гейтом служит настройка.
        """
        return self.newdb_configured and self.rosreestr_enabled

    @property
    def inheritance_configured(self) -> bool:
        """Реестр наследственных дел ФНП спрашивается только по явному флагу.

        Ключа у источника нет — он открытый, — поэтому единственное условие это
        адрес и включённая настройка. Ключа нет и денег он не стоит; настройка
        нужна потому, что сервис отвечает только из России, а «недоступен» не
        должно молча стать «дел не найдено».
        """
        return self.inheritance_enabled and bool(self.inheritance_base_url)

    @property
    def arbitr_legal_configured(self) -> bool:
        return self.newdb_configured and self.arbitr_legal_enabled

    @property
    def fssp_configured(self) -> bool:
        """Whether the ФССП provider can make a real call.

        Named for the source, not the vendor: the domain asks about ФССП, and
        which aggregator serves it stays a configuration detail.
        """
        return self.newdb_configured

    @property
    def fedresurs_configured(self) -> bool:
        if self.fedresurs_backend is FedresursBackend.NONE:
            return False
        if self.fedresurs_backend is FedresursBackend.DEMO:
            return True
        if self.fedresurs_backend is FedresursBackend.NEWDB:
            return self.newdb_methods_configured
        has_auth = bool(
            self.fedresurs_api_key or (self.fedresurs_username and self.fedresurs_password)
        )
        return bool(
            self.fedresurs_base_url
            and self.fedresurs_search_path
            and has_auth
            and self.fedresurs_field_map is not None
        )

    @property
    def fns_configured(self) -> bool:
        if self.fns_provider is FNSBackend.NONE:
            return False
        if self.fns_provider is FNSBackend.DEMO:
            return True
        if self.fns_provider is FNSBackend.NEWDB:
            # Карта не нужна: ``egrul_ip`` разбирается кодом по живому ответу.
            # Две ветки одного объекта — ИП в ``matches`` и компании в
            # ``affiliations`` — одна плоская запись карты не соберёт.
            return self.newdb_configured
        return bool(
            self.fns_base_url
            and self.fns_search_path
            and self.fns_api_key
            and self.fns_field_map is not None
        )


def _parse_user_ids(raw: str) -> frozenset[int]:
    """Числовые Telegram ID из строки настройки.

    Мусорные значения отбрасываются, а не роняют бота: опечатка в одном ID не
    повод отобрать доступ у остальных. Но и не повод пустить кого-то лишнего —
    отброшенное значение никого не открывает.
    """
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError:
            continue
    return frozenset(ids)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that importing modules do not each re-read the environment; tests
    clear the cache or construct :class:`Settings` directly.
    """
    return Settings()
