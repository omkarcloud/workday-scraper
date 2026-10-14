"""Live endpoint smoke tests: one call per /workday/* route against a
running service, with example values proven to return data (2026-10-14).
The listing tooling reads each route's FIRST call from this file's AST as
its working example, so the values stay literals.

Skipped unless WORKDAY_BASE points at a running service:

    ONLY_SCRAPER=workday python run.py
    WORKDAY_BASE=http://127.0.0.1:6002 python -m pytest workday/test_endpoints.py -q
"""
import os

import pytest

BASE = os.environ.get("WORKDAY_BASE", "").rstrip("/")

pytestmark = pytest.mark.skipif(not BASE, reason="set WORKDAY_BASE to run live endpoint tests")


def call(path, status=200, **params):
    from curl_cffi import requests
    resp = requests.get(BASE + path, params=params, timeout=300)
    assert resp.status_code == status, f"{path} {params} -> {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert body, f"{path} returned an empty body"
    return body


def test_search():
    page = call("/workday/jobs/search", site="nvidia", query="deep learning")
    assert page["count"] > 50 and page["next"] and len(page["jobs"]) == 20
    assert page["jobs"][0]["requisition_id"] and page["jobs"][0]["link"].startswith("https://nvidia.wd5.myworkdayjobs.com/")
    us = call("/workday/jobs/search", site="nvidia", query="deep learning", country="US", location="Santa Clara")
    assert 0 < us["count"] < page["count"]
    adobe = call("/workday/jobs/search", site="adobe/external_experienced", query="engineer")
    assert adobe["jobs"][0]["link"].startswith("https://adobe.wd5.myworkdayjobs.com/")
    assert any(f["param"] == "jobFamilyGroup" for f in page["filters"])
    filtered = call("/workday/jobs/search", site="https://adobe.wd5.myworkdayjobs.com/external_experienced",
                    category="Engineering", country="United States of America")
    assert 0 < filtered["count"] < call("/workday/jobs/search", site="adobe")["count"]
    assert any(v["is_selected"] for f in filtered["filters"] for v in f["values"])
    assert call("/workday/jobs/search", site="adobe", page=2)["current_page"] == 2
    hydrated = call("/workday/jobs/search", site="workday", query="engineer", include_details="true")
    assert hydrated["jobs"][0]["description"] and hydrated["jobs"][0]["location"]["country"]["code"]
    assert call("/workday/jobs/search", site="salesforce", locale="fr-FR")["site"]["link"].endswith("/fr-FR/External_Career_Site")


def test_details():
    job = call("/workday/jobs/details",
               job="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Senior-Deep-Learning-Compiler-Engineer_JR2016814")["job"]
    assert job["requisition_id"] == "JR2016814" and job["description"] and job["description_html"]
    assert job["posted"]["date"] and job["location"]["country"]["code"] == "US" and job["similar_jobs"]
    assert job["location"]["count"] > 1 and job["location"]["additional"]
    adobe = call("/workday/jobs/details",
                 job="https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/Seattle/Research-Engineer_R166886")["job"]
    assert adobe["requisition_id"] == "R166886"
    assert call("/workday/jobs/details", job="R166886", site="adobe")["job"]["id"] == "Research-Engineer_R166886"
    assert call("/workday/jobs/details", job="Research-Engineer_R166886", site="adobe.wd5/external_experienced")["job"]["title"]
    batch = call("/workday/jobs/batch", jobs="JR2016814,JR2023497-1", site="nvidia")
    assert batch["count"] == 2 and batch["not_found"] == []
    mixed = call("/workday/jobs/batch", jobs="R166886,Research-Engineer_R166886,NOPE_R1", site="adobe")
    assert mixed["count"] == 2 and mixed["not_found"] == ["NOPE_R1"]
    similar = call("/workday/jobs/similar", job="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Senior-Deep-Learning-Compiler-Engineer_JR2016814")
    assert similar["count"] > 0 and similar["similar_jobs"][0]["link"]


def test_filters_latest_all():
    filters = call("/workday/jobs/filters", site="nvidia")
    assert filters["total_jobs"] > 100 and any(f["param"] == "jobFamilyGroup" for f in filters["filters"])
    target = call("/workday/jobs/filters", site="https://target.wd5.myworkdayjobs.com/targetcareers", region="Minnesota")
    assert any(f["param"] == "Location_Region_State_Province" for f in target["filters"])
    latest = call("/workday/jobs/latest", site="nvidia")
    assert latest["sitemap_count"] == 100 and len(latest["jobs"]) == 20
    hydrated = call("/workday/jobs/latest", site="adobe", limit=3, include_details="true")
    assert len(hydrated["jobs"]) == 3 and hydrated["jobs"][0]["title"]
    everything = call("/workday/jobs/all", site="nvidia", query="deep learning")
    assert everything["count"] == 500 and everything["total_count"] > 500


def test_companies():
    found = call("/workday/companies/resolve", company="NVIDIA")
    assert found["companies"][0]["tenant"] == "nvidia" and "NVIDIAExternalCareerSite" in [s["id"] for s in found["companies"][0]["sites"]]
    pfizer = call("/workday/companies/resolve", company="Pfizer")
    assert pfizer["companies"][0]["tenant"] == "pfizer"
    assert call("/workday/companies/resolve", company="capitalone.com")["companies"][0]["host"].endswith(".myworkdayjobs.com")
    details = call("/workday/companies/details", company="NVIDIA")
    assert details["company"]["name"] == "NVIDIA" and details["company"]["about"] and details["company"]["legal_entities"]
    assert details["total_jobs"] > 500 and details["sites"][0]["top_countries"]["values"]
    assert call("/workday/companies/details", company="salesforce")["company"]["name"] == "Salesforce"
    directory = call("/workday/companies/search", query="bank")
    assert directory["count"] >= 3 and directory["companies"][0]["sites"][0]["job_count"] is not None
    assert call("/workday/countries")["count"] > 100


def test_errors():
    # paths via variables so the listing tooling never reads an error case
    # as a route's working example
    search, details, resolve = "/workday/jobs/search", "/workday/jobs/details", "/workday/companies/resolve"
    call(search, status=404, site="adobe/nosuchsite")
    call(search, status=404, site="doesnotexist123")
    call(search, status=400, site="https://www.linkedin.com/jobs")
    call(search, status=400, site="adobe", country="Nowhereland")
    call(search, status=400, site="adobe", bogus="1")
    call(details, status=400, job="R166886")
    call("/workday/jobs/batch", status=400, jobs="R166886,JR2016814")
    call(details, status=404, job="NOPE_R999999", site="adobe")
    call(resolve, status=404, company="zzqqxxyy")
