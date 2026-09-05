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
from app.providers.fedresurs import NewDBBankruptcyProvider
from app.providers.pledge import NewDBPledgeProvider
from app.providers.newdb import NewDBFieldMaps
from app.services.aggregation import Aggregator
from app.services.reporting import render_report
from app.services.scoring import RecoveryScoreEngine

BASE_URL="https://api.example.test"; NEWDB_URL=f"{BASE_URL}/v2"
SHIPPED_MAP=Path("config/field_maps/example_newdb.json"); LIVE=Path(__file__).parent/"data"
LITIGANT=SearchSubject(search_type=SearchType.PERSON.value,name=PersonName(last_name="Тестов",first_name="Андрей",middle_name="Викторович"),inn="644600011111")
BANKRUPT=SearchSubject(search_type=SearchType.PERSON.value,name=PersonName(last_name="Пыжова",first_name="Анна",middle_name="Петровна"),birth_date=date(1979,3,14),inn="270311112222")
PLEDGOR=SearchSubject(search_type=SearchType.PERSON.value,name=PersonName(last_name="Петров",first_name="Сергей",middle_name="Андреевич"),birth_date=date(1985,5,10))

def live(n:str)->dict[str,Any]: return json.loads((LIVE/f"newdb_live_{n}.json").read_text(encoding="utf-8"))

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
async def test_probe_court_block_text(shipped_settings,shipped_maps):
    body=live("arbitr_person"); w=body["results"]["arbitr_person"]["result"]["data"][0]
    case=w["cases"][0]; broken=[]
    for i in range(3):
        c=copy.deepcopy(case); c.pop("case_number"); c["card"].pop("case_number",None); c["case_url"]=f"https://kad.arbitr.ru/Card/x{i}"; broken.append(c)
    w["cases"]=broken; w["total_count"]=3; w["pagination"]["returned"]=3
    rep=await report_of(NewDBArbitrationProvider(shipped_settings,shipped_maps),LITIGANT,body)
    print("COURT BLOCK >>>"); print(block(render_report(rep),"СУДЫ")); print("<<<")

@respx.mock
async def test_probe_bankrot_truncation(shipped_settings,shipped_maps):
    body=live("bankrot_person"); row=body["results"]["bankrot_person"]["result"]["data"][0]
    case=row["bankruptcy"][0]
    row["bankruptcy"]=[{**copy.deepcopy(case),"case_number":f"А73-{i}/2017"} for i in range(60)]
    rep=await report_of(NewDBBankruptcyProvider(shipped_settings,shipped_maps),BANKRUPT,body)
    res=rep.result_for(ProviderName.FEDRESURS)
    print("BANKROT sent 60 -> records",len(res.records),"partial",res.is_partial,"notes",res.notes)

@respx.mock
async def test_probe_pledge_truncation(shipped_settings,shipped_maps):
    body=live("pledge_person_unmatched")
    res_node=body["results"]["pledge_person"]["result"]["data"][0]
    print("pledge container keys",list(res_node.keys()))
    notice={"reference_number":"2020-000-000000-000","json_extra":{"registrationTime":"2020-01-01T00:00:00"},"pledgor":"Петров Сергей Андреевич","pledgor_detail_birthdate":"10.05.1985","pledgee":"Банк","pledge_subject_ids_raw":"XUS22270280002514","message_type":"возникновение залога","fnp_url":"https://x/1"}
    res_node["fnp"]=[{**copy.deepcopy(notice),"reference_number":f"2020-000-{i:06d}-000","fnp_url":f"https://x/{i}"} for i in range(150)]
    res_node["fnp_urls"]=[]
    rep=await report_of(NewDBPledgeProvider(shipped_settings,shipped_maps),PLEDGOR,body)
    res=rep.result_for(ProviderName.PLEDGE)
    print("PLEDGE sent 150 -> records",len(res.records),"partial",res.is_partial,"notes",res.notes)
    print(block(render_report(rep),"ЗАЛОГИ"))
