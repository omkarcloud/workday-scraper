"""/workday/jobs/* — search, details, batch, similar jobs, the site's filter
vocabulary, the newest postings (siteMap.xml) and a full dump, all from the
career site's own CXS JSON API (fetch.py).

Listings are 20 per page (the upstream cap) and one query never reports
more than 2000 matches (`is_total_capped`); `all` slices a capped query by
facet to get past it.
"""
from workday import parsers as P
from workday import refs
from workday import shared
from workday.fetch import (PAGE_SIZE, RESULT_CAP, WorkdayNotFound, cxs_job, cxs_jobs, get_text,
                           map_parallel, site_base, soft)

ALL_WORKERS = 8
DETAIL_WORKERS = 8
FILTER_PARAMS = ("country", "region", "location", "location_type", "category", "job_type",
                 "time_type", "remote_type", "posted_within")


def _filters(kwargs):
    return {name: kwargs.get(name) for name in FILTER_PARAMS if kwargs.get(name)}


def _locale(site_ref, locale):
    return locale or (site_ref or {}).get("locale")


def _page(site, query, applied, offset, locale, limit=PAGE_SIZE):
    return cxs_jobs(site, shared.search_body(query, applied, offset, limit), locale)


# ---- details -----------------------------------------------------------------------

def _path_for(site, ref, locale):
    """A resolved job ref -> the CXS job path (a requisition id goes through
    a one-result search; the site echoes it in bulletFields)."""
    if ref.get("job_path"):
        return ref["job_path"]
    if ref.get("posting_id"):
        return ref["posting_id"]
    req_id = ref["req_id"]
    body = _page(site, req_id, {}, 0, locale)
    wanted = req_id.lower()
    for row in body.get("jobPostings") or []:
        bullets = [str(b).lower() for b in row.get("bulletFields") or []]
        path = P.job_path_of(row.get("externalPath"))
        if path and (wanted in bullets or path.lower().endswith("_" + wanted)):
            return path
    raise WorkdayNotFound(f"no posting with requisition id '{req_id}' on {site['tenant']}/{site['site']}")


def _details(site, ref, locale):
    path = _path_for(site, ref, locale)
    body = cxs_job(site, path, locale)
    if not isinstance(body.get("jobPostingInfo"), dict):
        raise WorkdayNotFound(f"job '{path}' not found on {site['tenant']}/{site['site']}")
    out = P.job_details(body, site, locale)
    out["site"] = shared.site_summary(site, locale)
    return out


def _site_for_job(ref, site):
    """The site a job ref belongs to: the link's own, else `site`."""
    if ref.get("site"):
        return shared.resolve_site(ref["site"])
    if not site:
        raise ValueError("site is required when job is not a posting link")
    return shared.resolve_site(site)


def details(job, site=None, locale=None):
    """One posting in full: plain-text and HTML description, requisition id,
    posting and closing dates, country and every location, remote and time
    type, apply flags, the hiring organization and similar jobs."""
    resolved = _site_for_job(job, site)
    return {"job": _details(resolved, job, _locale(job.get("site") or site, locale))}


def similar(job, site=None, locale=None):
    """The jobs the career site recommends next to one posting."""
    resolved = _site_for_job(job, site)
    full = _details(resolved, job, _locale(job.get("site") or site, locale))
    rows = full.pop("similar_jobs") or []
    return {
        "job": {k: full.get(k) for k in ("id", "requisition_id", "title", "link")},
        "site": full["site"],
        "count": len(rows),
        "similar_jobs": rows,
    }


def batch(jobs, site=None, locale=None):
    """Up to 20 postings in one call. Refs that resolve to nothing are
    reported under `not_found` with the input that named them."""
    bare = [ref["input"] for ref in jobs if not ref.get("site")]
    if bare and not site:
        raise ValueError(f"site is required for jobs that are not posting links ({', '.join(bare[:5])})")
    def one(ref):
        try:
            resolved = _site_for_job(ref, site)
            return _details(resolved, ref, _locale(ref.get("site") or site, locale))
        except (WorkdayNotFound, ValueError):
            return None

    results = map_parallel(one, jobs, workers=DETAIL_WORKERS)
    found = [r for r in results if r]
    return {
        "count": len(found),
        "not_found": [ref["input"] for ref, r in zip(jobs, results) if r is None],
        "jobs": found,
    }


def _hydrate(site, rows, locale):
    """Replace listing rows with their full details (a failed detail keeps
    the listing row)."""
    def one(row):
        if not row.get("path"):
            return row
        full = soft(lambda: _details(site, {"job_path": row["path"]}, locale))()
        if not full:
            return row
        full.pop("similar_jobs", None)
        full.pop("site", None)
        return full
    return map_parallel(one, rows, workers=DETAIL_WORKERS)


# ---- search / filters ---------------------------------------------------------------

def search(site, query=None, page=1, locale=None, facets=None, include_details=None, **filters):
    """Live job search on one career site, 20 a page, with the site's own
    filters (country, region, location, category, job type, time type,
    remote type, posting age) and every facet with counts."""
    resolved = shared.resolve_site(site)
    locale = _locale(site, locale)
    applied = shared.build_applied(resolved, _filters(filters), locale, raw_facets=facets)
    body = _page(resolved, query, applied, (page - 1) * PAGE_SIZE, locale)
    rows = [P.job(r, resolved, locale) for r in body.get("jobPostings") or [] if isinstance(r, dict)]
    if include_details and rows:
        rows = _hydrate(resolved, rows, locale)
    total = P.to_int(body.get("total"))
    return {
        "query": query,
        "site": shared.site_summary(resolved, locale),
        "is_total_capped": total == RESULT_CAP,
        "pagination": P.pagination(page, PAGE_SIZE, total),
        "filters": P.facets(shared.flatten_facets(body.get("facets")), applied),
        "jobs": rows,
    }


def filters(site, query=None, locale=None, facets=None, **public_filters):
    """The site's filter vocabulary (every facet with its value ids and
    counts) for a query, so callers can build exact `facets=` filters."""
    resolved = shared.resolve_site(site)
    locale = _locale(site, locale)
    applied = shared.build_applied(resolved, _filters(public_filters), locale, raw_facets=facets)
    body = _page(resolved, query, applied, 0, locale, limit=1)
    flat = shared.flatten_facets(body.get("facets"))
    total = P.to_int(body.get("total"))
    return {
        "query": query,
        "site": shared.site_summary(resolved, locale),
        "total_jobs": total,
        "is_total_capped": total == RESULT_CAP,
        "count": len(flat),
        "filters": P.facets(flat, applied),
    }


# ---- latest (siteMap.xml) -----------------------------------------------------------

def latest(site, locale=None, limit=20, include_details=None):
    """The newest postings from the site's siteMap.xml (the 100 most recent,
    newest first), hydrated with details for the first `limit`."""
    resolved = shared.resolve_site(site)
    locale = _locale(site, locale)
    status, xml = get_text(f"{site_base(resolved)}/siteMap.xml", label=f"{resolved['tenant']}/{resolved['site']}/siteMap.xml")
    if status != 200 or "<urlset" not in xml:
        raise WorkdayNotFound(f"career site '{resolved['site']}' publishes no site map on tenant '{resolved['tenant']}'")
    entries = P.sitemap(xml)
    rows = []
    for entry in entries[:limit]:
        path = entry["link"].split("/job/", 1)[1].strip("/") if "/job/" in entry["link"] else None
        posting_id = P.posting_id_of(path)
        rows.append({
            "id": posting_id,
            "requisition_id": posting_id.rsplit("_", 1)[-1] if posting_id and "_" in posting_id else None,
            "title": None,
            "link": entry["link"],
            "path": path,
            "locales": entry["locales"],
            "last_modified": entry["last_modified"],
        })
    if include_details and rows:
        rows = _hydrate(resolved, rows, locale)
    return {
        "site": shared.site_summary(resolved, locale),
        "sitemap_count": len(entries),
        "count": len(rows),
        "jobs": rows,
    }


# ---- all (full dump) -------------------------------------------------------------------

def _enumerate(site, query, applied, locale, max_jobs, first_page=None):
    """Every posting of one query up to max_jobs (20 a page, pages fetched
    in parallel). Returns (rows, reported total)."""
    first = first_page or _page(site, query, applied, 0, locale)
    total = P.to_int(first.get("total")) or 0
    wanted = min(total, max_jobs)
    rows = [P.job(r, site, locale) for r in first.get("jobPostings") or [] if isinstance(r, dict)]
    offsets = list(range(PAGE_SIZE, wanted, PAGE_SIZE))
    pages = map_parallel(lambda off: _page(site, query, applied, off, locale), offsets, workers=ALL_WORKERS)
    for body in pages:
        rows.extend(P.job(r, site, locale) for r in body.get("jobPostings") or [] if isinstance(r, dict))
    return rows[:wanted], total


def _slice_facet(flat):
    """The facet to split a capped query on: the one with the most values
    among the ones whose counts cover the listing."""
    best = None
    for facet in flat:
        if facet["param"].lower() in ("locationmaingroup",) or not facet["values"]:
            continue
        if best is None or len(facet["values"]) > len(best["values"]):
            best = facet
    return best


def all(site, query=None, locale=None, facets=None, max_jobs=500, **filters):
    """Every posting matching the query, deduplicated by posting id. A query
    Workday caps at 2000 is split by its widest facet and each slice
    enumerated, so sites with more than 2000 openings dump completely."""
    resolved = shared.resolve_site(site)
    locale = _locale(site, locale)
    applied = shared.build_applied(resolved, _filters(filters), locale, raw_facets=facets)
    first = _page(resolved, query, applied, 0, locale)
    total = P.to_int(first.get("total")) or 0
    capped = total >= RESULT_CAP
    rows, seen = [], set()

    def take(batch):
        for row in batch:
            key = row.get("id") or row.get("path")
            if key and key not in seen:
                seen.add(key)
                rows.append(row)

    if capped and max_jobs > RESULT_CAP:
        flat = shared.flatten_facets(first.get("facets"))
        facet = _slice_facet(flat)
        if facet:
            for value in facet["values"]:
                if len(rows) >= max_jobs:
                    break
                sliced = dict(applied)
                sliced[facet["param"]] = list(dict.fromkeys(applied.get(facet["param"], []) + [value["id"]]))
                part, _ = _enumerate(resolved, query, sliced, locale, max_jobs - len(rows))
                take(part)
    if not rows:
        part, _ = _enumerate(resolved, query, applied, locale, max_jobs, first_page=first)
        take(part)
    return {
        "query": query,
        "site": shared.site_summary(resolved, locale),
        "total_count": total,
        "is_total_capped": capped,
        "count": len(rows),
        "jobs": rows[:max_jobs],
    }
