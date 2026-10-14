"""/workday/companies/* — company -> Workday tenant resolution, a tenant's
career sites with their job counts and top filters, the seed directory and
the global country table.

Workday itself offers no company lookup (the tenant root answers 406 and
DNS is wildcard); robots.txt on each wd host is the only tell. A name is
tried as the tenant slugs companies actually use (capitalone,
capital-one, ...) against every host, after the seed directory of
well-known tenants verified at build time.
"""
import html
import re

from scraper_errors import UpstreamError
from workday import parsers as P
from workday import refs
from workday import shared
from workday.fetch import (RESULT_CAP, WorkdayNotFound, cxs_job, cxs_jobs, get_text, host_of, map_parallel,
                           site_base, soft)

MAX_LIVE_CANDIDATES = 4
TOP_VALUES = 10


def _site_rows(located, locale=None):
    """A located tenant -> its sites block (no network)."""
    rows = []
    for site in located["sites"]:
        resolved = {"tenant": located["tenant"], "wd": located["wd"], "domain": located["domain"], "site": site["id"]}
        rows.append({
            "id": site["id"],
            "link": refs.site_link(resolved, locale),
            "is_crawl_allowed": site["is_crawl_allowed"],
            "sitemap_link": site.get("sitemap_link"),
        })
    return rows


def _company_row(located, source, locale=None):
    return {
        "tenant": located["tenant"],
        "host": host_of(located["tenant"], located["wd"], located["domain"]),
        "link": f"https://{host_of(located['tenant'], located['wd'], located['domain'])}/{located['sites'][0]['id']}"
                if located["sites"] else None,
        "source": source,
        "count": len(located["sites"]),
        "sites": _site_rows(located, locale),
    }


def _directory_matches(name, domain=None):
    """Tenants of the seed directory whose slug or name matches."""
    rows = shared.directory()["rows"]
    slugs = set(refs.slug_candidates(name))
    if domain:
        slugs.add(domain.split(".")[0].lower())
    low = (name or "").lower()
    out = []
    for row in rows:
        tenant = row["tenant"].lower()
        label = (row.get("name") or "").lower()
        if tenant in slugs or (label and (low in label or label in low) and len(low) >= 3):
            out.append(row)
    return out


def _try_locate(tenant, wd=None, errors=None):
    """locate_tenant() that returns None for "not on Workday" and records a
    transport failure in `errors` (so it can surface as a retryable 502
    instead of a cached "not found")."""
    try:
        return shared.locate_tenant(tenant, wd)
    except WorkdayNotFound:
        return None
    except UpstreamError as e:
        if errors is not None:
            errors.append(e)
        return None


def resolve(company):
    """Company name / domain / tenant / link -> the Workday tenants that
    answer for it, with their career sites."""
    ref = company
    found, errors = [], []
    if ref.get("site"):
        site = ref["site"]
        located = shared.locate_tenant(site["tenant"], site.get("wd"), site.get("domain"))
        found.append(_company_row(located, "link"))
        query = site["tenant"]
    else:
        query = ref["domain"] or ref["name"]
        seen = set()
        for row in _directory_matches(ref["name"], ref["domain"]):
            located = _try_locate(row["tenant"], row["wd"], errors)
            if located and located["tenant"] not in seen:
                seen.add(located["tenant"])
                found.append(_company_row(located, "directory"))
        if not found:
            candidates = refs.slug_candidates(ref["name"])
            if ref["domain"]:
                candidates = [ref["domain"].split(".")[0].lower()] + [c for c in candidates
                                                                        if c != ref["domain"].split(".")[0].lower()]
            candidates = [c for c in candidates if c not in seen][:MAX_LIVE_CANDIDATES]
            results = map_parallel(lambda c: _try_locate(c, None, errors), candidates,
                                   workers=len(candidates) or 1)
            for located in results:
                if located and located["tenant"] not in seen:
                    seen.add(located["tenant"])
                    found.append(_company_row(located, "live"))
    if not found and errors:
        raise errors[0]
    if not found:
        raise WorkdayNotFound(f"no Workday career site found for '{query}' — pass the tenant slug "
                              "(the first label of the company's myworkdayjobs.com host) or a career link")
    return {"query": query, "count": len(found), "companies": found}


ENTITY_SAMPLE = 5
_META_RE = re.compile(r'<meta[^>]+(?:name|property)="(og:title|og:description|description|title)"[^>]*?content="([^"]*)"', re.I)


clean_entity = P.clean_entity


def brand_name(entities, tenant):
    """The company's own spelling of its tenant slug inside its legal-entity
    names ('nvidia' -> 'NVIDIA', 'capitalone' -> 'Capital One'): the most
    frequent spelling, capitalised ones first. None when no entity names it."""
    slug = re.sub(r"[^a-z0-9]", "", (tenant or "").lower())
    if not slug or len(slug) > 40:
        return None
    pattern = re.compile(r"(?<![A-Za-z0-9])(" + r"[\s\-.&']?".join(re.escape(c) for c in slug) + r")(?![A-Za-z0-9])", re.I)
    counts = {}
    for name in entities or []:
        match = pattern.search(name or "")
        if match:
            counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    if not counts:
        return None
    return sorted(counts, key=lambda n: (n == n.lower(), -counts[n], len(n)))[0]


def pick_company_name(entities, tenant):
    """The brand spelling when the entities carry it, else the most frequent
    legal entity (shortest on a tie)."""
    if not entities:
        return None
    brand = brand_name(entities, tenant)
    if brand:
        return brand
    counts = {}
    for name in entities:
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts, key=lambda n: (-counts[n], len(n)))[0]


def _site_page(resolved):
    """The career site's own headline and about text (og:title /
    og:description of its landing page)."""
    status, page = get_text(site_base(resolved), label=f"{resolved['tenant']}/{resolved['site']}",
                            timeout=20, retries=False)
    meta = {}
    if status == 200:
        for key, value in _META_RE.findall(page or ""):
            meta.setdefault(key.lower().replace("og:", ""), P.text(html.unescape(value)))
    return {"headline": meta.get("title"), "about": meta.get("description")}


def _site_stats(resolved, locale=None):
    """One site's job total, its top categories / countries / locations, the
    legal entities of its first postings and its landing-page text."""
    body = soft(lambda: cxs_jobs(resolved, shared.search_body(None, {}, 0, ENTITY_SAMPLE), locale))()
    if body is None:
        return None
    flat = shared.flatten_facets(body.get("facets"))
    classified = shared.classify_facets(flat)
    total = P.to_int(body.get("total"))

    def top(public_name):
        facet = classified.get(public_name)
        if not facet:
            return None
        values = sorted(facet["values"], key=lambda v: -(v.get("count") or 0))[:TOP_VALUES]
        return {"label": facet["label"], "param": facet["param"],
                "values": [{"id": v["id"], "label": v["label"], "count": v["count"]} for v in values]}

    paths = [P.job_path_of(r.get("externalPath")) for r in body.get("jobPostings") or []
             if isinstance(r, dict) and r.get("externalPath")]
    details = map_parallel(lambda path: soft(lambda: cxs_job(resolved, path, locale))() or {},
                           paths[:ENTITY_SAMPLE], workers=ENTITY_SAMPLE) if paths else []
    entities = [clean_entity((d.get("hiringOrganization") or {}).get("name")) for d in details]
    page = soft(lambda: _site_page(resolved), default={"headline": None, "about": None})()
    return {
        "job_count": total,
        "is_total_capped": total == RESULT_CAP,
        "headline": page["headline"],
        "about": page["about"],
        "legal_entities": [e for e in dict.fromkeys(entities) if e],
        "_entities": [e for e in entities if e],
        "top_categories": top("category"),
        "top_countries": top("country"),
        "top_locations": top("location"),
        "remote_types": top("remote_type"),
        "filters": [{"param": f["param"], "label": f["label"], "count": len(f["values"])} for f in flat],
    }


def details(company, locale=None):
    """One company's Workday presence: every career site with its job count,
    landing-page text, top categories, countries and locations, and the legal
    entities that hire through it."""
    resolved = resolve(company)
    row = resolved["companies"][0]
    _, wd, domain = row["host"].split(".", 2)
    located = shared.locate_tenant(row["tenant"], wd, domain)
    sites = _site_rows(located, locale)
    stats = map_parallel(lambda s: _site_stats({"tenant": located["tenant"], "wd": located["wd"],
                                                "domain": located["domain"], "site": s["id"]}, locale),
                         sites, workers=min(8, len(sites)) or 1)
    entities = []
    for site, stat in zip(sites, stats):
        if stat is None:
            site.update({"job_count": None, "is_total_capped": None, "headline": None, "about": None,
                         "legal_entities": [], "top_categories": None, "top_countries": None,
                         "top_locations": None, "remote_types": None, "filters": None, "is_reachable": False})
            continue
        entities.extend(stat.pop("_entities"))
        site.update(stat)
        site["is_reachable"] = True
    totals = [s["job_count"] for s in sites if s.get("job_count") is not None]
    main = next((s for s in sites if s.get("about") or s.get("headline")), {})
    return {
        "company": {
            "name": pick_company_name(entities, row["tenant"])
                    or brand_name([s.get("headline") for s in sites] + [s.get("about") for s in sites], row["tenant"]),
            "tenant": row["tenant"],
            "host": row["host"],
            "link": row["link"],
            "headline": main.get("headline"),
            "about": main.get("about"),
            "legal_entities": list(dict.fromkeys(entities)),
        },
        "total_jobs": sum(totals) if totals else None,
        "count": len(sites),
        "sites": sites,
    }


def search(query=None, page=1, limit=25):
    """The seed directory of well-known Workday tenants (tenant slug,
    organization name, host, sites with job counts at harvest time)."""
    rows = shared.directory()["rows"]
    if query:
        low = query.lower()
        rows = [r for r in rows if low in r["tenant"].lower() or low in (r.get("name") or "").lower()]
    start = (page - 1) * limit
    out = []
    for row in rows[start:start + limit]:
        located = {"tenant": row["tenant"], "wd": row["wd"], "domain": "myworkdayjobs.com",
                   "sites": [{"id": s["id"], "is_crawl_allowed": True, "sitemap_link": None} for s in row["sites"]]}
        company = _company_row(located, "directory")
        company["name"] = row.get("name")
        for site, seed in zip(company["sites"], row["sites"]):
            site["job_count"] = seed.get("job_count")
            site.pop("sitemap_link", None)
        out.append(company)
    return {
        "query": query,
        "pagination": P.pagination(page, limit, len(rows)),
        "companies": out,
    }


def countries():
    """Every country Workday filters on, with its ISO code and the facet id
    that is the same on every career site."""
    rows = shared.countries()["rows"]
    return {"count": len(rows), "countries": [{"name": r["name"], "code": r.get("code"), "id": r["id"]} for r in rows]}
