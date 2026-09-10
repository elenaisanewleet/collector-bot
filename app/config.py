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

# Владельцы этой поставки — в коде, а не только в ``.env``.
#
# Так вышло из-за конкретной поломки: на сервере строка OWNER_TELEGRAM_USER_IDS
# осталась пустой, а от неё зависит не только режим одобрения, но и всё дорогое
# в боте — прогон по всей базе, импорт выгрузки, выгрузка очереди. Пустая строка
# в ``.env`` означает «настройку не заполнили», а не «владельцев нет», и читать
# её как второе значит открыть тысячу платных обращений и персональные данные
# всех должников любому, кто нашёл бота.
#
# Убрать владельца — правкой этого списка. Сказать «владельцев правда нет» —
# значением :data:`NO_OWNERS`, иначе это невыразимо.
DEFAULT_OWNER_USER_IDS: frozenset[int] = frozenset({979904739, 1190527666, 41082373})

#: OWNER_TELEGRAM_USER_IDS=- — владельцев нет, и это осознанное решение.
NO_OWNERS = "-"


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
    # Бюджет опроса снят с журнала самого поставщика. 10.09.2026 их поддержка
    # прислала отчёт по нашему аккаунту: 37 запросов, из них ЗАВЕРШЁННЫХ 37,
    # среднее время 80.6 с, медиана 79.3 с, P95 174.4 с, максимум 207.5 с. По
    # методам медианы такие: arbitr_person 31 с, pledge_person 67 с,
    # bankrot_person 71 с, passport_fns 79 с, egrul_ip 101 с, fssp_person 108 с.
    #
    # То есть НОРМА этого поставщика — восемьдесят секунд, а хвост уходит за три
    # минуты. Прежние 25 × 3 = 75 с не дотягивали даже до медианы: бот сдавался
    # раньше, чем источник успевал ответить в половине случаев, вызов при этом
    # был оплачен, а отчёт писал «источник не успел подготовить ответ». Ровно
    # это и выглядело как «шесть источников молчат».
    #
    # 48 × 5 = 240 с перекрывают наблюдённый максимум с запасом. Интервал вырос
    # с трёх секунд до пяти не ради поставщика, а ради нас: опрос не тарифицируется
    # (повторный POST с тем же requestId не создаёт проверку), но 240 с по три
    # секунды — это восемьдесят HTTP-обращений на источник и под пятьсот на
    # должника.
    #
    # Произведение попыток на интервал обязано укладываться в бюджет источника
    # с запасом на HTTP: за этим следит _poll_must_fit_in_budget.
    newdb_poll_attempts: Annotated[int, Field(ge=1, le=60)] = 48
    newdb_poll_interval_seconds: Annotated[float, Field(ge=0.1, le=30)] = 5.0
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
    # Значение ведёт за опросом NewDB: 48 × 5 с плюс 15 с на запрос — 255 с, и
    # потолок обязан быть выше, иначе _poll_must_fit_in_budget не даст боту
    # запуститься. Полторы минуты, стоявшие здесь раньше, обрывали медианный
    # запрос этого поставщика ДВАЖДЫ: сначала по опросу, потом по потолку.
    provider_budget_seconds: Annotated[float, Field(ge=1, le=600)] = 270.0
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

    # ------------------------------------------------- мост «телефон → ФИО»
    # Заказчик формулирует сценарий одной фразой: «ввёл номер — увидел
    # должника». В выгрузке из 1С телефона нет ни одной колонкой, и ни один
    # внешний реестр по номеру не ищет, поэтому без моста этот ввод не находит
    # никого. Мост переводит номер в ФИО, которым уже ищется строка в нашей же
    # таблице; в отчёт его ответ не попадает — это ключ поиска, а не факт.
    #
    # Поставщик не зашит: адрес, авторизация и карта полей задаются настройками,
    # как у остальных внешних адаптеров. Решение, какому сервису доверять и на
    # каком основании, принимает владелец, а не код.
    phone_bridge_enabled: bool = False
    phone_bridge_base_url: str = ""
    phone_bridge_path: str = ""
    phone_bridge_api_key: str = ""
    phone_bridge_auth_style: AuthStyle = AuthStyle.BEARER
    phone_bridge_auth_name: str = "X-Api-Key"
    phone_bridge_field_map: Path | None = None

    # ------------------------------------- мост «ФИО + дата рождения → паспорт»
    #
    # Второй вход в того же поставщика и вторая половина цепочки. Первая ведёт
    # от телефона к личности; эта — от личности к её документу.
    #
    # Зачем он нужен отдельно. Три источника — банкротство, статус ИП и
    # арбитраж — ищут только по ИНН физлица. ИНН добывается по паспорту (мост
    # ФНС), а паспорт есть не у всех: в выгрузке заказчика он заполнен не
    # везде, телефона в ней нет вовсе. Для таких должников цепочка обрывалась
    # на первом же шаге, и три раздела отчёта молчали навсегда.
    #
    # Отдельные настройки, а не переиспользование телефонных: адрес у
    # поставщика тот же, но путь другой, карта полей другая (у него ``fio`` и
    # ``dob`` вместо ``full_name`` и ``birth_date``), и включать эти два входа
    # надо уметь порознь — они стоят разных денег и открывают разное.
    name_bridge_enabled: bool = False
    name_bridge_base_url: str = ""
    name_bridge_path: str = ""
    name_bridge_api_key: str = ""
    name_bridge_auth_style: AuthStyle = AuthStyle.BEARER
    name_bridge_auth_name: str = "X-Api-Key"
    name_bridge_field_map: Path | None = None

    # ---------------------------------------------------------------- import
    max_import_file_bytes: Annotated[int, Field(ge=1024)] = 5 * 1024 * 1024
    max_import_rows: Annotated[int, Field(ge=1)] = 50_000

    # ------------------------------------------------- расчёт долга по тарифу
    # Выгрузка взыскателя-эвакуатора приходит без суммы долга: в учёте она не
    # хранится, а считается по тарифу из двух дат — когда машину привезли и
    # когда забрали. Без неё вердикт по КАЖДОМУ должнику звучит «цену иска и
    # пошлину посчитать не из чего», то есть продукт не отвечает на свой
    # единственный вопрос.
    #
    # Ноль выключает расчёт: сумма остаётся неизвестной, и это честное
    # состояние. Посчитанная сумма несёт признак «расчётная» до самого отчёта и
    # никогда не выдаёт себя за подтверждённую выгрузкой: тариф — оценка, а в
    # цену иска идёт документ.
    #
    # Умолчания — базовые тарифы Московской области, распоряжение Мособлкомцен
    # от 20.12.2022 № 261-Р на 2023–2027 годы, категория B (легковые): в
    # выгрузке заказчика легковые и есть. Тарифы базовые и по годам
    # индексируются, поэтому это настройка, а не константа.
    storage_fee_per_day: Annotated[Decimal, Field(ge=0)] = Decimal("1394")
    # Перемещение. Отдельным тарифом того же распоряжения и зависит от того, чем
    # везли; точного числа у нас нет, поэтому здесь оценка, и она обязана
    # заменяться своей.
    tow_fee: Annotated[Decimal, Field(ge=0)] = Decimal("5000")

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

    # ---------------------------------------------------------------- суточная квота
    # Сколько платных проверок в сутки может сделать НЕ владелец.
    #
    # Ноль — без ограничения. Ограничение нужно ровно потому, что бот может
    # работать открытым (``ALLOWED_TELEGRAM_USER_IDS=*``): в этом режиме любой
    # найденный человек тратит оплаченный остаток, а остаток у поставщика —
    # десятки запросов, не тысячи. Владелец без лимита: это его деньги, и
    # спрашивать у него разрешения тратить их — не наше дело.
    #
    # По умолчанию ВЫКЛЮЧЕНА (0). Решение владельца: доступ и лимиты
    # настраиваются, когда бот уходит заказчику и раздавать доступы начинает он
    # сам. Механизм готов и включается одной переменной — DAILY_SEARCH_QUOTA.
    daily_search_quota: Annotated[int, Field(ge=0, le=10_000)] = 0

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.strip().upper() or "INFO"

    @field_validator(
        "newdb_base_url",
        "web_public_url",
        "fedresurs_base_url",
        "fns_base_url",
        "phone_bridge_base_url",
        "name_bridge_base_url",
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
        """Кому уходят заявки на доступ, кто их одобряет и кому открыто дорогое.

        Владелец допущен всегда, даже если его забыли вписать в список
        допущенных: иначе одобрять заявки было бы некому — их некому было бы и
        увидеть.

        Незаполненная настройка отдаёт :data:`DEFAULT_OWNER_USER_IDS`, а не
        пустоту: без владельцев прогон по всей базе и импорт остались бы открыты
        всем, а именно так бот и уехал на сервер. Чтобы владельцев правда не
        было, в настройке пишут :data:`NO_OWNERS`.
        """
        if self.owner_telegram_user_ids.strip() == NO_OWNERS:
            return frozenset()
        return _parse_user_ids(self.owner_telegram_user_ids) or DEFAULT_OWNER_USER_IDS

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

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, data: object) -> object:
        """Пустая строка в .env — это «не задано», а не «не число».

        Незаполненная настройка — нормальное промежуточное состояние: строку
        пишут ровно тогда, когда собираются заполнить её позже. Так и вышло:
        заготовка «TELEGRAM_LOOKUP_API_ID=» без значения увела бота в цикл
        перезапуска — pydantic не разобрал пустую строку как int, падение
        случилось до настройки логгера, и в логах остался ValidationError без
        имени поля. Бот молчал на все сообщения, пока причину искали.

        Правило общее, а не про одно поле: так же уронила бы любая пустая
        числовая строка — WEB_PORT, CACHE_TTL_HOURS, TOW_FEE. Пустое значение
        выбрасывается, и поле берёт своё умолчание.
        """
        if not isinstance(data, dict):
            return data
        numeric = {
            name
            for name, field in cls.model_fields.items()
            if field.annotation in (int, float, Decimal)
            or (getattr(field.annotation, "__origin__", None) is not None)
        }
        return {
            key: value
            for key, value in data.items()
            if not (isinstance(value, str) and not value.strip() and key.lower() in numeric)
        }

    @property
    def name_bridge_configured(self) -> bool:
        """Мост «ФИО → паспорт» настроен: есть куда идти и чем читать ответ.

        Карта полей обязательна по той же причине, что у телефонного моста, и
        причина здесь даже острее: по одному имени поставщик отвечает десятками
        разных людей, и разбирать их без знания, где лежит дата рождения,
        значит взять паспорт однофамильца.
        """
        return bool(
            self.name_bridge_enabled
            and self.name_bridge_base_url
            and self.name_bridge_path
            and self.name_bridge_field_map is not None
        )

    @property
    def phone_bridge_configured(self) -> bool:
        """Мост настроен, только когда есть куда идти и чем читать ответ.

        Карта полей обязательна наравне с адресом: без неё ответ поставщика —
        произвольный JSON, из которого имя пришлось бы угадывать, а угаданное
        имя поднимет из выгрузки не того человека.
        """
        return bool(
            self.phone_bridge_enabled
            and self.phone_bridge_base_url
            and self.phone_bridge_path
            and self.phone_bridge_field_map is not None
        )

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
