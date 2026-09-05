from __future__ import annotations
import copy, json
from pathlib import Path
import httpx, pytest, respx
from app.config import Settings
from app.domain.enums import ProviderName, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.providers.fns import NewDBBusinessProvider
from app.providers.court import NewDBArbitrationProvider
from app.providers.newdb import NewDBFieldMaps
BASE_URL="https://api.example.test"; NEWDB_URL=f"{BASE_URL}/v2"
SHIPPED_MAP=Path("config/field_maps/example_newdb.json"); LIVE=Path(__file__).parent/"data"
ENT=SearchSubject(search_type=SearchType.PERSON.value,name=PersonName(last_name="Парфёнов",first_name="Антон",middle_name="Орестович"),inn="770600011111")
LIT=SearchSubject(search_type=SearchType.PERSON.value,name=PersonName(last_name="Тестов",first_name="Андрей",middle_name="Викторович"),inn="644600011111")
def live(n): return json.loads((LIVE/f"newdb_live_{n}.json").read_text(encoding="utf-8"))
@pytest.fixture
def maps(): return NewDBFieldMaps.load(SHIPPED_MAP)
@pytest.fixture
def st(live_settings: Settings):
    return live_settings.model_copy(update={"newdb_api_key":"k","newdb_base_url":BASE_URL,"newdb_method_path":"/v2","newdb_field_map":SHIPPED_MAP,"provider_max_retries":0,"provider_retry_backoff_seconds":0.0})

@respx.mock
async def test_egrul_has_more_is_dead(st,maps):
    body=live("egrul_ip"); row=body["results"]["egrul_ip"]["result"]["data"][0]
    for sec in row["affiliations"]["registry_sections"].values():
        sec["has_more"]=True
        sec["items_count"]=40
    # источник отдал 3 из 40, total_items остался счётчиком отданных
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200,json=body))
    res=await NewDBBusinessProvider(st,maps).fetch(ENT)
    print("EGRUL has_more=true everywhere -> partial",res.is_partial,"notes",res.notes)

@respx.mock
async def test_arbitr_total_count_control(st,maps):
    body=live("arbitr_person"); w=body["results"]["arbitr_person"]["result"]["data"][0]
    w["total_count"]=40; w["pagination"]["has_more"]=True
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200,json=body))
    res=await NewDBArbitrationProvider(st,maps).fetch(LIT)
    print("ARBITR control -> partial",res.is_partial,"notes",res.notes)
