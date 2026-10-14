"""Lookups shared by the /workday/* endpoint modules:

  * the seed tenant directory (data/tenants.json, verified live at build
    time) and the global country facet ids (data/countries.json);
  * tenant location — which wd host a tenant lives on and which career
    sites it has, from robots.txt (memoised in-process);
  * site resolution — a refs.resolve_site() dict completed with its host and
    default site;
  * facet resolution — the public filter params (country, region, location,
    category, job_type, time_type, remote_type, ...) mapped onto the site's
    own facet ids, by id, label or ISO country code.
"""
import json
import os
import re
import threading
import time

from workday import refs
from workday.fetch import (JOBS_DOMAIN, PAGE_SIZE, WorkdayNotFound, WorkdayUpstreamError, cxs_jobs, get_text, host_of,
                           map_parallel)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Hosts Workday career sites live on, most common first. Every host here
# answers (422 for a tenant that is not its own); wd2 / wd101 / wd104 /
# wd106 / wd110 do not exist (checked 2026-10-14) and must stay out — a
# dead host hangs a probe for the whole timeout.
WD_HOSTS = ["wd1", "wd3", "wd5", "wd12", "wd10", "wd102", "wd103", "wd105", "wd107", "wd108", "wd109",
            "wd501", "wd503"]
PROBE_TIMEOUT = 12

TENANT_TTL = 6 * 3600
FACETS_TTL = 3600

# public filter param -> how to recognise its facet on a site. Tenants name
# facets freely (NVIDIA's countries sit under locationHierarchy1 "Locations",
# Target's store types under the same param), so a facet is matched by its
# param, then its label, then — for countries — by its values. Order matters:
# each facet is claimed once, first come first served.
FILTER_ORDER = ["country", "remote_type", "region", "posted_within", "time_type", "job_type",
                "category", "location", "location_type"]
FILTER_PARAMS = {
    "country": ["location_country", "locationcountry", "location_country_from_job_posting_location",
                "cf_-_rec_-_lrv_-_job_posting_anchor_-_country_from_job_posting_location_extended"],
    "region": ["location_region_state_province", "locationregionstateprovince", "state", "locationregion"],
    "location": ["locations", "location", "jobpostinglocation"],
    "location_type": [],
    "category": ["jobfamilygroup", "jobfamily", "job_family_group", "jobcategory"],
    "job_type": ["workersubtype", "worker_sub_type", "jobtype"],
    "time_type": ["timetype", "time_type"],
    "remote_type": ["remotetype", "remote_type", "workplacetype", "isremote"],
    "posted_within": ["startdate", "postedon", "posting_date"],
}
FILTER_LABELS = {
    "country": re.compile(r"\bcountr(?:y|ies)\b", re.I),
    "region": re.compile(r"\b(?:state|region|province)\b", re.I),
    "location": re.compile(r"\b(?:locations?|sites?|city|cities)\b", re.I),
    "location_type": re.compile(r"\btype\b", re.I),
    "category": re.compile(r"\b(?:categor(?:y|ies)|job famil(?:y|ies)|job area|function|department)\b", re.I),
    "job_type": re.compile(r"\b(?:job|worker|employee|employment) type\b", re.I),
    "time_type": re.compile(r"\b(?:time type|schedule)\b", re.I),
    "remote_type": re.compile(r"\b(?:remote|workplace|work type|work arrangement)\b", re.I),
    "posted_within": re.compile(r"\b(?:post(?:ing|ed)(?: date| on)?|date posted)\b", re.I),
}
_REMOTE_VALUE_RE = re.compile(r"\b(?:remote|hybrid|on-?site|office|flex|in-person)\b", re.I)
_HEX_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_lock = threading.Lock()
_memo = {}


def memo(key, ttl, build):
    now = time.time()
    with _lock:
        hit = _memo.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = build()
    with _lock:
        _memo[key] = (now + ttl, value)
    return value


def _load_data(name):
    """A build-time data file; an absent file is an empty table (the live
    resolvers still work, only the seed directory / ISO codes are missing)."""
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


# ---- static data -----------------------------------------------------------------

def directory():
    """The seed tenant directory: [{tenant, wd, sites: [{id, job_count}],
    disallowed_sites}], indexed by tenant."""
    def build():
        rows = _load_data("tenants.json")
        return {"rows": rows, "by_tenant": {r["tenant"].lower(): r for r in rows}}
    return memo("directory", 24 * 3600, build)


def countries():
    """[{name, code, id}] — the country facet ids, global across tenants."""
    def build():
        rows = _load_data("countries.json")
        by_code = {r["code"].upper(): r for r in rows if r.get("code")}
        by_name = {r["name"].lower(): r for r in rows}
        by_id = {r["id"]: r for r in rows}
        return {"rows": rows, "by_code": by_code, "by_name": by_name, "by_id": by_id}
    return memo("countries", 24 * 3600, build)


# Short / alternative country names some tenants use in their own location
# facets (NVIDIA: "United States", "Korea"); the global facet uses the long form.
COUNTRY_ALIASES = {
    "united states": "US", "usa": "US", "us": "US", "u.s.": "US", "united states of america": "US",
    "uk": "GB", "great britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "korea": "KR", "south korea": "KR", "republic of korea": "KR", "korea, republic of": "KR",
    "russia": "RU", "vietnam": "VN", "taiwan": "TW", "czech republic": "CZ", "czechia": "CZ",
    "turkey": "TR", "türkiye": "TR", "hong kong": "HK", "macau": "MO", "macao": "MO", "china": "CN",
    "uae": "AE", "the netherlands": "NL", "holland": "NL", "ivory coast": "CI", "iran": "IR",
    "bolivia": "BO", "venezuela": "VE", "tanzania": "TZ", "moldova": "MD", "laos": "LA", "syria": "SY",
    "brunei": "BN", "slovak republic": "SK", "north macedonia": "MK", "macedonia": "MK",
}


def country_code_of_label(label):
    """A facet value label ("United States", "Korea, Republic of") -> its ISO
    alpha-2 code, or None when it is not a country."""
    low = (label or "").strip().lower()
    if not low:
        return None
    row = countries()["by_name"].get(low)
    if row and row.get("code"):
        return row["code"]
    return COUNTRY_ALIASES.get(low)


def country_of(descriptor=None, wid=None):
    """-> {name, code, id} for a country facet value / descriptor."""
    table = countries()
    row = None
    if wid:
        row = table["by_id"].get(wid)
    if row is None and descriptor:
        row = table["by_name"].get(descriptor.strip().lower())
    if row is None:
        if not (descriptor or wid):
            return None
        return {"name": descriptor, "code": None, "id": wid}
    return {"name": descriptor or row["name"], "code": row.get("code"), "id": wid or row["id"]}


# ---- tenants ----------------------------------------------------------------------

def parse_robots(text, host):
    """robots.txt -> [{id, is_crawl_allowed, sitemap_link}] in file order."""
    sites, order = {}, []

    def add(site, allowed, sitemap=None):
        key = site.lower()
        if key in ("refreshfacet", "talentcommunity", "login", "wday"):
            return
        row = sites.get(key)
        if row is None:
            row = sites[key] = {"id": site, "is_crawl_allowed": allowed, "sitemap_link": sitemap}
            order.append(key)
        else:
            row["is_crawl_allowed"] = row["is_crawl_allowed"] or allowed
            row["sitemap_link"] = row["sitemap_link"] or sitemap

    for line in (text or "").splitlines():
        match = re.match(r"\s*(Allow|Disallow)\s*:\s*/([^/\s]+)/?\s*$", line, re.I)
        if match:
            add(match.group(2), match.group(1).lower() == "allow")
            continue
        match = re.match(r"\s*Sitemap\s*:\s*(https?://[^/\s]+/([^/\s]+)/siteMap\.xml)\s*$", line, re.I)
        if match:
            add(match.group(2), True, match.group(1))
    return [sites[k] for k in order]


# probe_host outcome when the host could not be read (transport failure):
# not the same as "the tenant does not live here"
UNREACHABLE = "unreachable"


def probe_host(tenant, wd, domain=JOBS_DOMAIN, retries=False):
    """robots.txt on one host -> the site list; None when the tenant does
    not live there (422) / the host is not a career site; UNREACHABLE when
    the host could not be read at all."""
    host = host_of(tenant, wd, domain)
    try:
        status, text = get_text(f"https://{host}/robots.txt", label=f"{host}/robots.txt",
                                timeout=PROBE_TIMEOUT, retries=retries)
    except WorkdayUpstreamError:
        return UNREACHABLE
    if status is None:
        return UNREACHABLE
    if status != 200 or "myworkday" not in text.lower() and "allow" not in text.lower():
        return None
    sites = parse_robots(text, host)
    return sites if sites else None


def locate_tenant(tenant, wd=None, domain=JOBS_DOMAIN):
    """-> {tenant, wd, domain, sites: [...]}: the host the tenant lives on
    (the seed directory, else robots.txt probed on every known host in
    parallel) and its career sites. WorkdayNotFound when every host answered
    and none knows it; WorkdayUpstreamError (retryable, never cached) when a
    host that might have known it could not be read."""
    tenant = tenant.lower()

    def build():
        hosts = [wd] if wd else []
        seed = directory()["by_tenant"].get(tenant)
        if seed and not wd:
            hosts.append(seed["wd"])
        hosts += [h for h in WD_HOSTS if h not in hosts]
        found, unreachable = None, []
        # the known / most likely host alone first (with retries: it is
        # almost certainly the one); then every other at once
        head = hosts[:1] if (wd or seed) else []
        for host in head:
            sites = probe_host(tenant, host, domain, retries=True)
            if sites == UNREACHABLE:
                unreachable.append(host)
            elif sites:
                found = (host, sites)
        if found is None and not wd:
            tail = [h for h in hosts if h not in head]
            results = map_parallel(lambda h: probe_host(tenant, h, domain), tail, workers=len(tail) or 1)
            for host, sites in zip(tail, results):
                if sites == UNREACHABLE:
                    unreachable.append(host)
                elif sites:
                    found = (host, sites)
                    break
        if found is None:
            if unreachable:
                raise WorkdayUpstreamError(f"could not reach {', '.join(unreachable)} to locate tenant "
                                           f"'{tenant}' — retry")
            hint = f" on {wd}" if wd else ""
            raise WorkdayNotFound(f"no Workday career site found for tenant '{tenant}'{hint} "
                                  "(the tenant slug is the first label of the company's "
                                  "myworkdayjobs.com host, e.g. adobe for adobe.wd5.myworkdayjobs.com)")
        return {"tenant": tenant, "wd": found[0], "domain": domain, "sites": found[1]}

    return memo(f"tenant:{domain}:{tenant}:{wd or '*'}", TENANT_TTL, build)


def resolve_site(ref):
    """A refs.resolve_site() dict -> the same with `wd` and `site` filled.
    An explicit host and site are trusted (the jobs call validates them);
    anything missing comes from the tenant's robots.txt."""
    out = dict(ref)
    out.setdefault("domain", JOBS_DOMAIN)
    if out.get("wd") and out.get("site"):
        return out
    located = locate_tenant(out["tenant"], out.get("wd"), out["domain"])
    out["wd"] = located["wd"]
    if not out.get("site"):
        allowed = [s for s in located["sites"] if s["is_crawl_allowed"]] or located["sites"]
        if not allowed:
            raise WorkdayNotFound(f"tenant '{out['tenant']}' lists no career site")
        out["site"] = allowed[0]["id"]
    return out


def site_summary(site, locale=None):
    """The `site` block every response carries."""
    return {
        "tenant": site["tenant"],
        "host": host_of(site["tenant"], site["wd"], site.get("domain") or JOBS_DOMAIN),
        "id": site["site"],
        "link": refs.site_link(site, locale),
    }


# ---- facets ----------------------------------------------------------------------

def flatten_facets(raw):
    """The CXS facets list -> [{param, label, values: [{id, label, count}]}]
    with nested groups (locationMainGroup) lifted to the top level."""
    out = []
    for facet in raw or []:
        if not isinstance(facet, dict):
            continue
        values = [v for v in facet.get("values") or [] if isinstance(v, dict)]
        if values and values[0].get("facetParameter") and values[0].get("values") is not None:
            out.extend(flatten_facets(values))
            continue
        param = facet.get("facetParameter")
        if not param:
            continue
        out.append({
            "param": param,
            "label": (facet.get("descriptor") or "").strip() or None,
            "values": [{"id": v.get("id"), "label": (v.get("descriptor") or "").strip() or None,
                        "count": v.get("count")} for v in values if v.get("id")],
        })
    return out


def site_facets(site, locale=None):
    """The unfiltered facet list of a site (memoised an hour): the vocabulary
    the public filter params resolve against."""
    def build():
        body = cxs_jobs(site, {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}, locale)
        return flatten_facets(body.get("facets"))
    return memo(f"facets:{site['tenant']}:{site['wd']}:{site['site']}:{locale or ''}", FACETS_TTL, build)


def _country_share(facet):
    labels = [v["label"] for v in facet["values"] if v.get("label")]
    if len(labels) < 2:
        return 0.0
    return sum(1 for label in labels if country_code_of_label(label)) / len(labels)


def _remote_share(facet):
    labels = [v["label"] or "" for v in facet["values"]]
    if not labels or len(labels) > 6:
        return 0.0
    return sum(1 for label in labels if _REMOTE_VALUE_RE.search(label)) / len(labels)


def classify_facets(facets):
    """{public filter name: facet} for one site's flattened facets."""
    out, claimed = {}, set()

    def free(facet):
        return id(facet) not in claimed

    def claim(name, facet):
        out[name] = facet
        claimed.add(id(facet))

    for name in FILTER_ORDER:
        params = FILTER_PARAMS[name]
        hit = next((f for f in facets if free(f) and f["param"].lower() in params), None)
        if hit is None and name == "country":
            scored = sorted((f for f in facets if free(f)), key=lambda f: -_country_share(f))
            if scored and _country_share(scored[0]) >= 0.6:
                hit = scored[0]
        if hit is None and name == "remote_type":
            hit = next((f for f in facets if free(f) and _remote_share(f) >= 0.5), None)
        if hit is None:
            hit = next((f for f in facets if free(f) and FILTER_LABELS[name].search(f["label"] or "")), None)
        if hit is None and name == "location":
            hit = next((f for f in facets if free(f) and f["param"].lower().startswith("locationhierarchy")), None)
        if hit is None and name == "location_type":
            hit = next((f for f in facets if free(f) and f["param"].lower().startswith("locationhierarchy")), None)
        if hit is not None:
            claim(name, hit)
    return out


def _facet_for(facets, public_name):
    return classify_facets(facets).get(public_name)


def _match_value(facet, wanted, public_name):
    """A user value (id, label, ISO code for country) -> the facet value id.
    Labels match whole (case-insensitive), else at a word start ("united"
    -> United States of America); `location` values may match anywhere
    (they are street addresses on many sites)."""
    wanted_low = wanted.strip().lower()
    if _HEX_ID_RE.match(wanted_low):
        return wanted_low
    if public_name == "country":
        code = country_code_of_label(wanted_low) or (wanted_low.upper() if len(wanted_low) == 2 and wanted_low.isalpha() else None)
        if code:
            row = countries()["by_code"].get(code)
            for value in facet["values"]:
                if (row and value["id"] == row["id"]) or country_code_of_label(value["label"]) == code:
                    return value["id"]
            if len(wanted_low) == 2:
                name = row["name"] if row else code
                raise ValueError(f"the site has no jobs in {name} ({code})")
    exact = [v for v in facet["values"] if (v["label"] or "").lower() == wanted_low]
    if len(exact) == 1:
        return exact[0]["id"]
    pattern = re.compile(r"(?:^|[^a-z0-9])" + re.escape(wanted_low))
    partial = [v for v in facet["values"] if pattern.search((v["label"] or "").lower())]
    if not partial and public_name == "location":
        partial = [v for v in facet["values"] if wanted_low in (v["label"] or "").lower()]
    if len(partial) == 1 or (partial and public_name == "location"):
        return partial[0]["id"]
    if len(partial) > 1:
        raise ValueError(f"{public_name} '{wanted}' is ambiguous here: "
                         + ", ".join((v["label"] or "") for v in partial[:8]))
    options = ", ".join((v["label"] or "") for v in facet["values"][:12])
    raise ValueError(f"{public_name} '{wanted}' is not a value of this site's "
                     f"'{facet['label'] or facet['param']}' filter (try: {options})")


def build_applied(site, filters, locale=None, raw_facets=None):
    """The public filter params -> the CXS appliedFacets dict.
    `filters` is {public_name: [values]}; `raw_facets` is {facetParameter:
    [ids]} passed straight through."""
    applied = {}
    for param, ids in (raw_facets or {}).items():
        applied.setdefault(param, []).extend(ids)
    wanted = {k: v for k, v in (filters or {}).items() if v}
    if wanted:
        facets = site_facets(site, locale)
        for public_name, values in wanted.items():
            facet = _facet_for(facets, public_name)
            if facet is None:
                available = ", ".join(sorted(classify_facets(facets)))
                raise ValueError(f"this career site has no '{public_name}' filter "
                                 f"(its filters: {available or 'none'})")
            ids = [_match_value(facet, v, public_name) for v in values]
            applied.setdefault(facet["param"], []).extend(ids)
    return {k: list(dict.fromkeys(v)) for k, v in applied.items()}


def search_body(query=None, applied=None, offset=0, limit=PAGE_SIZE):
    return {"appliedFacets": applied or {}, "limit": limit, "offset": offset, "searchText": query or ""}
