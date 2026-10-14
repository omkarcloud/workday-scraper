"""Offline tests: ref parsing, the request schemas and the normalizers
against raw Workday CXS responses captured 2026-10-14
(workday/fixtures/*).

    python -m pytest workday/test_parsers.py -q
"""
import json
import os
from datetime import date

import pytest

from schema_fields import load_query
from workday import parsers as P
from workday import refs
from workday import schemas as S
from workday import shared

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
ADOBE = {"tenant": "adobe", "wd": "wd5", "domain": "myworkdayjobs.com", "site": "external_experienced"}
WORKDAY = {"tenant": "workday", "wd": "wd5", "domain": "myworkdayjobs.com", "site": "Workday"}


def fx(name):
    path = os.path.join(FIX, name)
    with open(path, encoding="utf-8") as f:
        return json.load(f) if name.endswith(".json") else f.read()


def _no_camel_or_url_keys(obj, path="$"):
    """Every emitted key is snake_case and no key is named *url*."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert key == key.lower() and " " not in key, f"{path}.{key} not snake_case"
            assert "url" not in key, f"{path}.{key} should be a *link"
            _no_camel_or_url_keys(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            _no_camel_or_url_keys(value, f"{path}[{i}]")


def _no_empty_strings(obj, path="$"):
    if isinstance(obj, dict):
        for key, value in obj.items():
            _no_empty_strings(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            _no_empty_strings(value, f"{path}[{i}]")
    else:
        assert obj != "", f"{path} is an empty string (must be null)"


# ---- refs -----------------------------------------------------------------------------

@pytest.mark.parametrize("value, tenant, wd, site, locale", [
    ("adobe", "adobe", None, None, None),
    ("adobe/external_experienced", "adobe", None, "external_experienced", None),
    ("adobe.wd5/external_experienced", "adobe", "wd5", "external_experienced", None),
    ("https://adobe.wd5.myworkdayjobs.com/external_experienced", "adobe", "wd5", "external_experienced", None),
    ("https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/Seattle/Research-Engineer_R166886",
     "adobe", "wd5", "external_experienced", "en-US"),
    ("https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external_experienced/jobs", "adobe", "wd5", "external_experienced", None),
    ("salesforce.wd12.myworkdayjobs.com/fr-FR/External_Career_Site/details/X_1", "salesforce", "wd12", "External_Career_Site", "fr-FR"),
    ("https://acme.wd1.myworkdaysite.com/recruiting/acme/External/job/X/Y_1", "acme", "wd1", "External", None),
    ("https://adobe.wd5.myworkdayjobs.com/", "adobe", "wd5", None, None),
])
def test_resolve_site(value, tenant, wd, site, locale):
    out = refs.resolve_site(value)
    assert (out["tenant"], out["wd"], out["site"], out["locale"]) == (tenant, wd, site, locale)


@pytest.mark.parametrize("value", ["", "https://www.linkedin.com/jobs", "adobe.nope/site", "adobe/job", "bad tenant!"])
def test_resolve_site_rejects(value):
    with pytest.raises(ValueError):
        refs.resolve_site(value)


@pytest.mark.parametrize("value, job_path, posting_id, req_id, has_site", [
    ("R166886", None, None, "R166886", False),
    ("JR-0109828", None, None, "JR-0109828", False),
    ("JR2025452", None, None, "JR2025452", False),
    ("Research-Engineer_R166886", None, "Research-Engineer_R166886", None, False),
    ("Seattle/Research-Engineer_R166886", "Seattle/Research-Engineer_R166886", None, None, False),
    ("/job/Seattle/Research-Engineer_R166886", "Seattle/Research-Engineer_R166886", None, None, False),
    ("https://adobe.wd5.myworkdayjobs.com/external_experienced/job/Seattle/Research-Engineer_R166886",
     "Seattle/Research-Engineer_R166886", None, None, True),
    ("https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/details/Research-Engineer_R166886",
     None, "Research-Engineer_R166886", None, True),
])
def test_resolve_job(value, job_path, posting_id, req_id, has_site):
    out = refs.resolve_job(value)
    assert (out["job_path"], out["posting_id"], out["req_id"], out["site"] is not None) == (job_path, posting_id, req_id, has_site)
    assert out["input"] == value


@pytest.mark.parametrize("value", ["", "https://adobe.wd5.myworkdayjobs.com/external_experienced", "a/b/c d", "???"])
def test_resolve_job_rejects(value):
    with pytest.raises(ValueError):
        refs.resolve_job(value)


def test_resolve_jobs():
    out = refs.resolve_jobs("R166886, R166886,Research-Engineer_R166886")
    assert [r["req_id"] or r["posting_id"] for r in out] == ["R166886", "Research-Engineer_R166886"]
    with pytest.raises(ValueError):
        refs.resolve_jobs(",".join(f"R{i}" for i in range(21)))


def test_resolve_company_and_slugs():
    assert refs.resolve_company("Capital One") == {"site": None, "name": "Capital One", "domain": None}
    assert refs.resolve_company("capitalone.com")["domain"] == "capitalone.com"
    assert refs.resolve_company("https://pfizer.wd1.myworkdayjobs.com/PfizerCareers")["site"]["tenant"] == "pfizer"
    assert refs.slug_candidates("Capital One")[:2] == ["capitalone", "capital-one"]
    assert "pfizer" in refs.slug_candidates("Pfizer Inc.")


# ---- schemas ----------------------------------------------------------------------------

def test_search_schema():
    data, err = load_query(S.SearchSchema, {"site": "adobe", "query": " engineer ", "country": "US, Canada",
                                            "facets": "jobFamilyGroup:591af8b812fa10737af39db3d96eed9f",
                                            "include_details": "yes", "locale": "fr_fr"})
    assert err is None
    assert data["site"]["tenant"] == "adobe" and data["query"] == "engineer"
    assert data["country"] == ["US", "Canada"] and data["include_details"] is True
    assert data["facets"] == {"jobFamilyGroup": ["591af8b812fa10737af39db3d96eed9f"]}
    assert data["locale"] == "fr-FR" and data["page"] == 1
    _, err = load_query(S.SearchSchema, {"site": "adobe", "facets": "jobFamilyGroup:short"})
    assert err and "facets" in err["errors"]
    _, err = load_query(S.SearchSchema, {"site": "adobe", "bogus": "1"})
    assert err and "bogus" in err["errors"]
    _, err = load_query(S.SearchSchema, {"query": "x"})
    assert err and "site" in err["errors"]


def test_details_and_batch_schemas():
    data, err = load_query(S.DetailsSchema, {"job": "R166886", "site": "adobe"})
    assert err is None and data["job"]["req_id"] == "R166886" and data["site"]["tenant"] == "adobe"
    data, err = load_query(S.BatchSchema, {"jobs": "R166886,R166887"})
    assert err is None and len(data["jobs"]) == 2 and data["site"] is None
    data, err = load_query(S.LatestSchema, {"site": "adobe", "limit": "100"})
    assert err is None and data["limit"] == 100
    _, err = load_query(S.LatestSchema, {"site": "adobe", "limit": "101"})
    assert err
    data, err = load_query(S.AllSchema, {"site": "adobe"})
    assert err is None and data["max_jobs"] == 500
    data, err = load_query(S.DirectorySchema, {})
    assert err is None and data["limit"] == 25


# ---- parsers: search -----------------------------------------------------------------------

def test_search_rows():
    body = fx("search_adobe.json")
    rows = [P.job(r, ADOBE) for r in body["jobPostings"]]
    assert len(rows) == 20
    first = rows[0]
    assert first["id"].endswith("_" + first["requisition_id"])
    assert first["link"].startswith("https://adobe.wd5.myworkdayjobs.com/external_experienced/job/")
    assert first["path"] and "/job/" not in first["path"]
    assert first["posted"]["text"].startswith("Posted") and first["posted"]["days_ago"] is not None
    assert first["location"]["count"] >= 1
    for row in rows:
        _no_camel_or_url_keys(row)
        _no_empty_strings(row)


def test_facets_flatten_and_nested_groups():
    flat = shared.flatten_facets(fx("search_target.json")["facets"])
    params = [f["param"] for f in flat]
    assert "locationMainGroup" not in params
    assert {"Location_Country", "Location_Region_State_Province", "jobFamilyGroup", "remoteType",
            "locationHierarchy1", "locations"} <= set(params)
    public = P.facets(flat, {"jobFamilyGroup": [flat[2]["values"][0]["id"]]} if flat[2]["param"] == "jobFamilyGroup" else {})
    assert all({"id", "label", "count", "is_selected"} <= set(v) for f in public for v in f["values"])
    _no_camel_or_url_keys(public)


def test_match_values():
    flat = shared.flatten_facets(fx("search_target.json")["facets"])
    region = shared._facet_for(flat, "region")
    assert shared._match_value(region, "minnesota", "region") == next(v["id"] for v in region["values"] if v["label"] == "Minnesota")
    assert shared._match_value(region, next(v["id"] for v in region["values"]), "region")
    with pytest.raises(ValueError):
        shared._match_value(region, "Nowhere", "region")
    assert shared._facet_for(flat, "posted_within") is None


# ---- parsers: details ---------------------------------------------------------------------

def test_job_details_multi_location():
    job = P.job_details(fx("job_workday.json"), WORKDAY)
    assert job["id"] == "Cybersecurity-Engineer_JR-0109828" and job["requisition_id"] == "JR-0109828"
    assert job["workday_id"] and job["title"] == "Cybersecurity Engineer"
    assert job["link"] == "https://workday.wd5.myworkdayjobs.com/Workday/job/USAVAReston/Cybersecurity-Engineer_JR-0109828"
    assert job["path"] == "USAVAReston/Cybersecurity-Engineer_JR-0109828"
    assert job["location"]["count"] == 3 and job["location"]["additional"] == ["USA, GA, Atlanta", "USA, CO, Boulder"]
    assert job["location"]["country"]["code"] == "US"
    assert job["remote_type"] == "Flex" and job["time_type"] == "Full Time"
    assert job["posted"]["date"] == "2026-09-16"
    assert job["closes"] == {"date": "2026-10-16", "text": "1 day left to apply"}
    assert job["can_apply"] is True and job["has_resume_parsing"] is True
    assert job["company"]["name"] == "Workday, Inc."
    assert P.job_details(fx("job_adobe.json"), ADOBE)["company"]["name"] == "Adobe Inc."
    assert job["description"].startswith("Your work days are brighter here.") and "<p>" not in job["description"]
    assert job["description_html"].startswith("<p>")
    assert job["similar_jobs"] and job["similar_jobs"][0]["link"].startswith("https://workday.wd5.myworkdayjobs.com/Workday/job/")
    _no_camel_or_url_keys(job)
    _no_empty_strings(job)


def test_job_details_single_location():
    job = P.job_details(fx("job_adobe.json"), ADOBE)
    assert job["location"] == {"text": "Seattle", "count": 1,
                               "country": {"name": "United States of America", "code": "US",
                                           "id": "bc33aa3152ec42d4995f4791a106ed09"}, "additional": []}
    assert job["closes"] is None and job["remote_type"] is None
    assert job["posted"]["date"] == "2026-09-18"


def test_job_details_partial():
    assert P.job_details({}, ADOBE)["id"] is None
    assert P.job_details({"jobPostingInfo": {"title": "X"}}, ADOBE)["title"] == "X"
    assert P.job({}, ADOBE)["id"] is None


# ---- parsers: helpers --------------------------------------------------------------------

def test_posted():
    today = date(2026, 10, 14)
    assert P.posted("Posted Today", today=today) == {"text": "Posted Today", "date": "2026-10-14", "days_ago": 0}
    assert P.posted("Posted Yesterday", today=today)["date"] == "2026-10-13"
    assert P.posted("Posted 26 Days Ago", today=today) == {"text": "Posted 26 Days Ago", "date": "2026-09-18", "days_ago": 26}
    assert P.posted("Posted 30+ Days Ago", today=today) == {"text": "Posted 30+ Days Ago", "date": None, "days_ago": 30}
    assert P.posted("Posted 3 Days Ago", "2026-10-01", today=today) == {"text": "Posted 3 Days Ago", "date": "2026-10-01", "days_ago": 13}
    assert P.posted(None, today=today) == {"text": None, "date": None, "days_ago": None}


def test_html_to_text():
    assert P.html_to_text("<p><b>Hi</b></p><p></p><ul><li>a &amp; b</li><li>c</li></ul>") == "Hi\n\n- a & b\n- c"
    assert P.html_to_text("") is None and P.html_to_text(None) is None


def test_sitemap():
    rows = P.sitemap(fx("sitemap_adobe.xml"))
    assert len(rows) == 100
    assert rows[0]["link"].startswith("https://adobe.wd5.myworkdayjobs.com/external_experienced/job/")
    assert "en-US" in rows[0]["locales"] and "ja-JP" in rows[0]["locales"]
    assert P.sitemap("") == []


def test_robots():
    sites = shared.parse_robots(fx("robots_pfizer.txt"), "pfizer.wd1.myworkdayjobs.com")
    assert [s["id"] for s in sites][:2] == ["PfizerCareers", "ContractorConversions"]
    assert sites[0]["is_crawl_allowed"] and sites[0]["sitemap_link"].endswith("/PfizerCareers/siteMap.xml")
    assert "refreshFacet" not in [s["id"] for s in sites]
    nike = shared.parse_robots(fx("robots_nike.txt"), "nike.wd1.myworkdayjobs.com")
    assert [s["id"] for s in nike] == ["nke", "nke2", "nke4"] and not nike[0]["is_crawl_allowed"]
    assert shared.parse_robots("", "x") == []


def test_pagination():
    assert P.pagination(2, 20, 332) == {"page": 2, "items_per_page": 20, "total_pages": 17, "total_count": 332}
    assert P.pagination(1, 20, None)["total_pages"] == 0


def test_classify_facets_by_label_and_values():
    # NVIDIA puts countries under locationHierarchy1 and office/remote under
    # locationHierarchy2; Target uses locationHierarchy1 for store types.
    nvidia = shared.classify_facets(shared.flatten_facets(fx("search_nvidia.json")["facets"]))
    assert nvidia["country"]["param"] == "locationHierarchy1"
    assert nvidia["remote_type"]["param"] == "locationHierarchy2"
    assert nvidia["location"]["param"] == "locations"
    target = shared.classify_facets(shared.flatten_facets(fx("search_target.json")["facets"]))
    assert target["country"]["param"] == "Location_Country" and target["location_type"]["param"] == "locationHierarchy1"
    assert target["region"]["param"] == "Location_Region_State_Province"
    us = shared._match_value(nvidia["country"], "US", "country")
    assert us == next(v["id"] for v in nvidia["country"]["values"] if v["label"] in ("United States", "United States of America"))


def test_company_name_helpers():
    from workday import companies as C
    assert C.clean_entity("TW07 NVIDIA Singapore Development PTE. Ltd. Taiwan Branch") == "NVIDIA Singapore Development PTE. Ltd. Taiwan Branch"
    assert C.clean_entity("414 salesforce.com France S.A.S.") == "salesforce.com France S.A.S."
    assert C.clean_entity("ADUS-Adobe Inc.") == "Adobe Inc."
    assert C.clean_entity("Workday, Inc.") == "Workday, Inc."
    assert C.clean_entity("") is None
    assert C.clean_entity("7Z4 Pfizer, Inc.") == "Pfizer, Inc."
    assert C.clean_entity("3M Company") == "3M Company"
    assert C.pick_company_name(["NVIDIA Graphics Bengaluru", "Mellanox Technologies, Ltd.", "NVIDIA USA"], "nvidia") == "NVIDIA"
    assert C.pick_company_name(["salesforce.com Germany GmbH", "Salesforce, Inc.", "SFDC Ireland Limited"], "salesforce") == "Salesforce"
    assert C.pick_company_name(["Capital One, N.A.", "Capital One Services, LLC"], "capitalone") == "Capital One"
    assert C.pick_company_name(["The Toronto-Dominion Bank (Canada)", "TD Securities"], "td") == "TD"
    assert C.pick_company_name(["Some Other Ltd", "Some Other Ltd", "X"], "zzz") == "Some Other Ltd"
    assert C.pick_company_name([], "x") is None


def test_locate_tenant_unreachable_is_not_not_found(monkeypatch):
    from workday import fetch
    shared._memo.clear()
    monkeypatch.setattr(shared, "get_text", lambda *a, **k: (None, ""))
    with pytest.raises(fetch.WorkdayUpstreamError):
        shared.locate_tenant("nvidia")
    monkeypatch.setattr(shared, "get_text", lambda *a, **k: (422, '{"errorCode":"HTTP_422"}'))
    shared._memo.clear()
    with pytest.raises(fetch.WorkdayNotFound):
        shared.locate_tenant("nvidia")
    robots = fx("robots_pfizer.txt")
    monkeypatch.setattr(shared, "get_text", lambda url, **k: (200, robots) if url.startswith("https://pfizer.wd1.") else (422, ""))
    shared._memo.clear()
    assert shared.locate_tenant("pfizer")["wd"] == "wd1"
    shared._memo.clear()


def test_resolve_company_surfaces_transport_errors(monkeypatch):
    from workday import companies as C, fetch
    shared._memo.clear()
    monkeypatch.setattr(shared, "get_text", lambda *a, **k: (None, ""))
    with pytest.raises(fetch.WorkdayUpstreamError):
        C.resolve({"site": None, "name": "NVIDIA", "domain": None})
    shared._memo.clear()
