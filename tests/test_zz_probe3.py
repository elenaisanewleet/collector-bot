from __future__ import annotations
import copy, json
from datetime import date
from pathlib import Path
from typing import Any
import httpx, pytest, respx
from app.config import Settings
from app.domain.enums import ProviderName, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.providers.court import NewDBArbitrationProvider
from app.providers.pledge import NewDBPledgeProvider
from app.providers.newdb import NewDBFieldMaps
from app.services.aggregation import Aggregator
from app.services.reporting import render_report
from app.services.scoring import RecoveryScoreEngine

BASE_URL="https://api.example.test"; NEWDB_URL=f"{BASE_URL}/v2"
SHIPPED_MAP=Path("config/field_maps/example_newdb.json"); LIVE=Path(__file__).parent/"data"
LITIGANT=SearchSubject(search_type=SearchType.PERSON.value,name=PersonName(last_name="Тестов",first_name="Андрей",middle_name="Викторович"),inn="644600011111")
PLEDGOR=SearchSubject(search_type=SearchType.PERSON.value,name=PersonName(last_name="Петров",first_name="Сергей",middle_name="Андреевич"),birth_date=date(1985,5,10))
def live(n): return json.loads((LIVE/f"newdb_live_{n}.json").read_text(encoding="utf-8"))
@pytest.fixture
def shipped_maps(): return NewDBFieldMaps.load(SHIPPED_MAP)
@pytest.fixture
def shipped_settings(live_settings: Settings):
    return live_settings.model_copy(update={"newdb_api_key":"k","newdb_base_url":BASE_URL,"newdb_method_path":"/v2","newdb_field_map":SHIPPED_MAP,"provider_max_retries":0,"provider_retry_backoff_seconds":0.0})
async def report_of(p,s,body):
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200,json=body))
    r=await p.fetch(s); rep=Aggregator().build(s,[r]); rep.recovery_score=RecoveryScoreEngine().evaluate(rep); return rep
def block(text,head):
    i=text.find(head); return text[i:text.find("\n\n",i)]

@respx.mock
async def test_probe_partial_case_loss(shipped_settings,shipped_maps):
    """Одно дело с номером, два без — total_count честно 3."""
    body=live("arbitr_person"); w=body["results"]["arbitr_person"]["result"]["data"][0]
    case=w["cases"][0]
    good=copy.deepcopy(case)
    lost=[]
    for i in range(2):
        c=copy.deepcopy(case); c.pop("case_number"); c["card"].pop("case_number",None); c["case_url"]=f"https://kad.arbitr.ru/Card/x{i}"; lost.append(c)
    w["cases"]=[good,*lost]; w["total_count"]=3; w["pagination"]["returned"]=3
    rep=await report_of(NewDBArbitrationProvider(shipped_settings,shipped_maps),LITIGANT,body)
    res=rep.result_for(ProviderName.COURT)
    print("3 cases in, records",len(res.records),"partial",res.is_partial,"notes",res.notes)
    print(block(render_report(rep),"СУДЫ"))

@respx.mock
async def test_probe_mixed_match_pledge(shipped_settings,shipped_maps):
    """Два уведомления: одно наше, одно чужое."""
    body=live("pledge_person_unmatched"); node=body["results"]["pledge_person"]["result"]["data"][0]
    mine={"reference_number":"2020-000-000001-000","json_extra":{"registrationTime":"2020-01-01T00:00:00"},"pledgor":"Петров Сергей Андреевич","pledgor_detail_birthdate":"10.05.1985","pledgee":"Банк","pledge_subject_ids_raw":"XUS22270280002514","message_type":"возникновение залога","fnp_url":"https://x/1"}
    other={**copy.deepcopy(mine),"reference_number":"2020-000-000002-000","pledgor":"Сидоров Пётр Иванович","pledgor_detail_birthdate":"01.01.1970","fnp_url":"https://x/2"}
    node["fnp"]=[mine,other]; node["fnp_urls"]=["https://x/1","https://x/2"]
    rep=await report_of(NewDBPledgeProvider(shipped_settings,shipped_maps),PLEDGOR,body)
    res=rep.result_for(ProviderName.PLEDGE)
    print("records",len(res.records),"usable",[r.is_usable for r in rep.pledges],"partial",res.is_partial,"notes",res.notes)
    print(block(render_report(rep),"ЗАЛОГИ"))

@respx.mock
async def test_probe_fnp_urls_absent_map(shipped_settings,shipped_maps):
    """fnp непуст, fnp_url в записях отсутствует -> все ссылки объявлены неразобранными."""
    body=live("pledge_person_unmatched"); node=body["results"]["pledge_person"]["result"]["data"][0]
    mine={"reference_number":"2020-000-000001-000","json_extra":{"registrationTime":"2020-01-01T00:00:00"},"pledgor":"Петров Сергей Андреевич","pledgor_detail_birthdate":"10.05.1985","pledgee":"Банк","pledge_subject_ids_raw":"XUS22270280002514","message_type":"возникновение залога"}
    node["fnp"]=[mine]; node["fnp_urls"]=["https://x/1"]
    rep=await report_of(NewDBPledgeProvider(shipped_settings,shipped_maps),PLEDGOR,body)
    res=rep.result_for(ProviderName.PLEDGE)
    print("records",len(res.records),"partial",res.is_partial,"notes",res.notes)
