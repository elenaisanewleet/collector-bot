"""Мост «телефон → личность»: что он забирает из ответа и что с этим делает.

Мост существует затем, чтобы номер превратился в человека. Раньше он забирал
только ФИО и дату рождения — а поставщик может отдавать и ИНН, и это меняет
деньги: банкротство, статус ИП и арбитраж ищут ТОЛЬКО по ИНН, и без него бот
идёт за ним в ФНС отдельным платным обращением на каждого должника.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import respx
from httpx import Response

from app.config import AppMode, Settings
from app.domain.enums import ProviderStatus, SearchType
from app.domain.identity import SearchSubject
from app.providers.base import NO_CONTEXT
from app.providers.identity_bridge import InnBridgeProvider
from app.providers.phone_bridge import PhoneNameProvider, PhoneNameResult, build_phone_bridge
from app.providers.registry import build_inn_bridge

BASE = "https://bridge.example.test"


@pytest.fixture
def field_map(tmp_path: Path) -> Path:
    path = tmp_path / "map.json"
    path.write_text(
        '{"records_path": "results", "fields": {"fio": "fio", "birth_date": "bd",'
        ' "inn": "inn", "passport": "passport"}}',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def settings(field_map: Path) -> Settings:
    return Settings(
        app_mode=AppMode.LIVE,
        app_name="t",
        telegram_bot_token="t",
        allowed_telegram_user_ids="*",
        owner_telegram_user_ids="111",
        database_url="sqlite+aiosqlite:///:memory:",
        phone_bridge_enabled=True,
        phone_bridge_base_url=BASE,
        phone_bridge_path="/lookup/{phone}",
        phone_bridge_auth_style="none",
        phone_bridge_field_map=field_map,
        inn_bridge_enabled=True,
        newdb_api_key="fake",
        newdb_base_url="https://newdb.example.test",
        provider_max_retries=0,
        log_level="CRITICAL",
        _env_file=None,
    )


def subject(**over: object) -> SearchSubject:
    return SearchSubject(search_type=SearchType.PERSON.value, phone="+79991234567", **over)


def answer(**fields: str) -> Response:
    return Response(200, json={"results": [{"fio": "Иванов Иван Иванович", **fields}]})


def _phone(settings: Settings) -> PhoneNameProvider:
    """Мост под настройками теста. build_* отдаёт Optional — здесь он всегда есть."""
    bridge = build_phone_bridge(settings)
    assert bridge is not None
    return bridge


@respx.mock
async def test_the_bridge_carries_the_inn_when_the_source_gives_one(settings: Settings) -> None:
    """ИНН из ответа доезжает до субъекта — и это про деньги, а не про полноту.

    С готовым ИНН мост «паспорт → ИНН» не срабатывает вовсе: он проверяет
    ``is_needed``. Минус одно платное обращение с каждого должника, и охват
    всех, а не только тех, у кого паспорт есть в выгрузке.
    """
    respx.get(url__startswith=BASE).mock(
        return_value=answer(bd="15.03.1985", inn="500100732259", passport="45 05 123456")
    )

    result = await _phone(settings).fetch(subject(), NO_CONTEXT)
    assert isinstance(result, PhoneNameResult)

    assert result.status is ProviderStatus.SUCCESS
    assert result.inn == "500100732259"
    assert result.passport == "4505123456", "паспорт нормализуется, как введённый руками"


@respx.mock
async def test_a_ready_inn_switches_the_fns_bridge_off(settings: Settings) -> None:
    """Проверяется сама экономия, а не намерение сэкономить."""
    respx.get(url__startswith=BASE).mock(return_value=answer(bd="15.03.1985", inn="500100732259"))
    inn_bridge: InnBridgeProvider = build_inn_bridge(settings)

    result = await _phone(settings).fetch(subject(), NO_CONTEXT)
    assert isinstance(result, PhoneNameResult)
    enriched = subject().model_copy(
        update={"name": result.name, "birth_date": result.birth_date, "inn": result.inn}
    )

    assert not inn_bridge.is_needed(enriched), "обращение к ФНС не сэкономлено"
    assert inn_bridge.is_needed(enriched.model_copy(update={"inn": None})), (
        "без ИНН мост к ФНС обязан остаться нужным"
    )


@respx.mock
@pytest.mark.parametrize("junk", ["7712345678", "не указан", "—", "1234"])
async def test_a_malformed_inn_is_dropped_not_passed_on(settings: Settings, junk: str) -> None:
    """Кривой ИНН отбрасывается молча, и это не придирка к формату.

    По нему уйдут ПЛАТНЫЕ запросы в банкротство, ИП и арбитраж — и вернут либо
    чужие дела, либо пустоту, неотличимую от честного «ничего не найдено».
    Десять цифр здесь тоже мусор: это ИНН юрлица, у физлица их двенадцать.
    """
    respx.get(url__startswith=BASE).mock(return_value=answer(bd="15.03.1985", inn=junk))

    result = await _phone(settings).fetch(subject(), NO_CONTEXT)
    assert isinstance(result, PhoneNameResult)

    assert result.status is ProviderStatus.SUCCESS, "имя всё равно должно доехать"
    assert result.inn is None, f"мусорный ИНН «{junk}» просочился"


@respx.mock
async def test_the_source_never_overwrites_what_the_operator_typed(settings: Settings) -> None:
    """Названное оператором сильнее найденного мостом.

    Он смотрит в документ, мост — в чужую базу. Это правило уже действует для
    имени; ИНН и паспорт обязаны подчиняться ему же.
    """
    respx.get(url__startswith=BASE).mock(
        return_value=answer(bd="01.01.1990", inn="500100732259", passport="45 05 123456")
    )
    mine = subject(inn="770912345601", passport="1234567890")

    result = await _phone(settings).fetch(mine, NO_CONTEXT)
    assert isinstance(result, PhoneNameResult)
    update = {
        field: getattr(result, field)
        for field in ("inn", "passport")
        if getattr(result, field) is not None and getattr(mine, field) in (None, "")
    }

    assert update == {}, "мост переписал то, что ввёл оператор"
