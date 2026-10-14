"""Workday career-site transport: plain curl_cffi with browser-impersonated
TLS. No browser, no login, no cookies (validated 2026-10-14 from direct
Indian egress against adobe, nvidia, salesforce, target, disney, pfizer,
cae and Workday's own site).

Every Workday career site ("external career site" in Workday terms) is a
React shell at

    https://{tenant}.{wd1|wd3|wd5|wd12|...}.myworkdayjobs.com/{locale}/{site}

whose data comes from the same host's CXS JSON API:

  * POST /wday/cxs/{tenant}/{site}/jobs
        {"appliedFacets": {<facetParameter>: [<value id>, ...]},
         "limit": 20, "offset": 0, "searchText": "..."}
    -> {total, jobPostings[], facets[], userAuthenticated}. `limit` above 20
    is a 400 (the site's own page size). `total` is capped at 2000 for any
    one query (nvidia and target both report exactly 2000), so a full dump
    of a big site slices the query by facet (jobs.py). `?locale=fr-FR` on
    the API URL switches the language of titles and facet labels.
  * GET  /wday/cxs/{tenant}/{site}/job/{externalPath after /job/}
    -> {jobPostingInfo, hiringOrganization, similarJobs, userAuthenticated}
    with the HTML description, the ISO posting date (startDate), the
    requisition id, time type, remote type, country, additional locations,
    the apply deadline and the apply flags.
  * GET  /robots.txt   lists the tenant's career sites (one Sitemap / Allow
    / Disallow line each). A tenant that does not live on that wd host
    answers 422 {"errorCode":"HTTP_422"} — that is the tenant resolver.
  * GET  /{site}/siteMap.xml   the 100 newest postings with hreflang
    alternates (no paging: page / offset / limit are ignored).

Upstream errors: a wrong site id is 404 {"errorCode":"S21","message":"not
found: Job_Posting_Site_ID=..."}; a wrong tenant or host is 422 HTTP_422; a
removed posting is 404 on the job GET. The tenant root and the CXS root
answer 406, and DNS is wildcard, so only robots.txt tells tenants apart.

Blocks: none seen. If the pod's egress ever starts answering 403/429 (or
HTML where JSON is expected), a worker moves its sessions to a sticky
residential exit (config.workday_fallback_proxy(),
config.WORKDAY_FALLBACK_PROXY_COUNTRY) for config.WORKDAY_BLOCK_COOLDOWN
seconds, the same shape as the woolworths transport. FALLBACK HOOK beyond
that: the CXS calls are plain fetches a page would make, so if residential
exits were also refused, escalate to a patchright pool (config.CHROME_POOLS,
flag "workday") warmed on the career site and serve the same URLs through
the in-page driver.get() (patchright_driver.FetchResponse) — not built,
dead code while plain HTTP works.

Failure taxonomy (scraper_errors, mapped to HTTP by route_glue):
  WorkdayUpstreamError  transport failure / 5xx              — retryable
  WorkdayBlocked        403 / 429 / HTML where JSON expected — retryable on a new exit
  WorkdayBadRequest     upstream 400                         — never retried
  WorkdayNotFound       404 / 422 (site, tenant, posting)    — never retried
"""
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError

JOBS_DOMAIN = "myworkdayjobs.com"
SITE_DOMAIN = "myworkdaysite.com"   # the older host shape: /recruiting/{tenant}/{site}
IMPERSONATE = "chrome"
PAGE_SIZE = 20                      # the CXS cap: limit > 20 is a 400
RESULT_CAP = 2000                   # the CXS cap on `total` for one query

TIMEOUT = 40
FANOUT_WORKERS = 8

JSON_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
}
PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}


class WorkdayUpstreamError(UpstreamError):
    """Transport failure or 5xx — retryable."""


class WorkdayBlocked(WorkdayUpstreamError, Blocked):
    """403 / 429 or an HTML body where JSON was expected — retryable."""


class WorkdayBadRequest(BadRequest):
    """Upstream 400 — the params were rejected. Never retried."""


class WorkdayNotFound(NotFound):
    """Tenant / site / posting does not exist — never retried."""


# ---- sessions ------------------------------------------------------------------
# One curl session per worker thread. Direct egress by default; after a block
# the thread's sessions go through a sticky residential exit for a cooldown.
_local = threading.local()
_fallback_until = 0.0
_fallback_lock = threading.Lock()


def _proxy_for_new_session():
    forced = config.workday_proxy()
    if forced:
        return forced
    if time.time() < _fallback_until:
        return config.workday_fallback_proxy()
    return None


def _session():
    sess = getattr(_local, "session", None)
    if sess is None:
        from curl_cffi import requests as curl_requests
        sess = curl_requests.Session(impersonate=IMPERSONATE)
        proxy = _proxy_for_new_session()
        if proxy:
            sess.proxies = {"http": proxy, "https": proxy}
        _local.session = sess
    return sess


def _drop_session():
    sess = getattr(_local, "session", None)
    _local.session = None
    if sess is not None:
        try:
            sess.close()
        except Exception:
            pass


def _note_block():
    """A 403/429 on direct egress: send new sessions through a residential
    exit for a while (no-op when no fallback country is configured)."""
    global _fallback_until
    if config.workday_fallback_proxy() is not None:
        with _fallback_lock:
            _fallback_until = time.time() + config.WORKDAY_BLOCK_COOLDOWN


def dump_debug(name, text):
    dbg = os.environ.get("WORKDAY_DEBUG_DIR", "")
    if dbg and text:
        try:
            os.makedirs(dbg, exist_ok=True)
            with open(os.path.join(dbg, name + ".txt"), "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass


# ---- hosts / urls ---------------------------------------------------------------

def host_of(tenant, wd, domain=JOBS_DOMAIN):
    return f"{tenant}.{wd}.{domain}"


def cxs_base(site):
    """A resolved site ({tenant, wd, site, domain}) -> its CXS API base."""
    return f"https://{host_of(site['tenant'], site['wd'], site.get('domain') or JOBS_DOMAIN)}" \
           f"/wday/cxs/{site['tenant']}/{site['site']}"


def site_base(site, locale=None):
    """The public career-site link (optionally under a locale prefix)."""
    host = host_of(site["tenant"], site["wd"], site.get("domain") or JOBS_DOMAIN)
    if (site.get("domain") or JOBS_DOMAIN) == SITE_DOMAIN:
        path = f"/recruiting/{site['tenant']}/{site['site']}"
    else:
        path = f"/{site['site']}"
    if locale:
        path = f"/{locale}{path}"
    return f"https://{host}{path}"


# ---- requests --------------------------------------------------------------------

def _error_message(resp):
    """{"errorCode": "S21", "message": "not found: Job_Posting_Site_ID=x"}."""
    try:
        body = resp.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    message = body.get("message") or body.get("errorCode")
    return message.strip() if isinstance(message, str) and message.strip() else None


def _classify(resp, label, *, not_found_message=None):
    status = resp.status_code
    if status == 404:
        raise WorkdayNotFound(not_found_message or f"{label} not found ({_error_message(resp) or 'HTTP 404'})")
    if status == 422:
        # HTTP_422 is Workday's answer for a tenant that does not live on
        # this host (or does not exist at all), on every path.
        raise WorkdayNotFound(not_found_message or f"{label}: no such Workday tenant on this host")
    if status in (403, 429):
        dump_debug("blocked", resp.text)
        _note_block()
        raise WorkdayBlocked(f"HTTP {status} on {label}")
    if status == 400:
        raise WorkdayBadRequest(_error_message(resp) or f"upstream rejected {label}")
    if status == 406:
        # the tenant root / CXS root: a path that is not a career site
        raise WorkdayNotFound(not_found_message or f"{label} is not a Workday career site path")
    if status >= 500 or status not in (200, 204):
        raise WorkdayUpstreamError(f"HTTP {status} on {label}")


def _json_of(resp, label):
    body = resp.text or ""
    if resp.status_code == 204 or not body.strip():
        return None
    if not body.lstrip().startswith(("{", "[")):
        dump_debug("nonjson", body)
        _note_block()
        raise WorkdayBlocked(f"{label} returned HTML, not JSON")
    try:
        return resp.json()
    except Exception:
        dump_debug("badjson", body)
        raise WorkdayUpstreamError(f"could not parse JSON from {label}")


def _retrying(fn):
    last = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            return fn()
        except WorkdayUpstreamError as e:      # includes WorkdayBlocked
            last = e
            _drop_session()
        if attempt < config.MAX_RETRIES:
            time.sleep(config.RETRY_BACKOFF * attempt)
    raise last


def get_json(url, params=None, *, label=None, not_found_message=None):
    label = label or url.split("/wday/cxs/")[-1]
    query = {k: v for k, v in (params or {}).items() if v is not None}

    def once():
        try:
            resp = _session().get(url, params=query or None, headers=JSON_HEADERS, timeout=TIMEOUT,
                                  allow_redirects=True)
        except Exception as e:
            raise WorkdayUpstreamError(f"request failed: {type(e).__name__}: {e}")
        _classify(resp, label, not_found_message=not_found_message)
        return _json_of(resp, label)

    return _retrying(once)


def post_json(url, body, params=None, *, label=None, not_found_message=None):
    label = label or url.split("/wday/cxs/")[-1]
    query = {k: v for k, v in (params or {}).items() if v is not None}

    def once():
        try:
            resp = _session().post(url, json=body, params=query or None, headers=JSON_HEADERS,
                                   timeout=TIMEOUT, allow_redirects=True)
        except Exception as e:
            raise WorkdayUpstreamError(f"request failed: {type(e).__name__}: {e}")
        _classify(resp, label, not_found_message=not_found_message)
        return _json_of(resp, label)

    return _retrying(once)


def get_text(url, *, label=None, timeout=TIMEOUT, retries=True):
    """GET a text document (robots.txt, siteMap.xml). Returns (status,
    text): blocks and 5xx raise, any 4xx is handed back to the caller
    (robots.txt 422 = no such tenant on this host). With retries=False a
    transport failure is (None, "") — the tenant probes must not wait out
    three attempts on a host that is not there."""
    label = label or url

    def once():
        try:
            resp = _session().get(url, headers=PAGE_HEADERS, timeout=timeout, allow_redirects=True)
        except Exception as e:
            if not retries:
                return None, ""
            raise WorkdayUpstreamError(f"request failed: {type(e).__name__}: {e}")
        status = resp.status_code
        if status in (403, 429):
            dump_debug("blocked", resp.text)
            _note_block()
            raise WorkdayBlocked(f"HTTP {status} on {label}")
        if status >= 500:
            raise WorkdayUpstreamError(f"HTTP {status} on {label}")
        return status, resp.text or ""

    return _retrying(once) if retries else once()


# ---- CXS helpers -------------------------------------------------------------------

def cxs_jobs(site, body, locale=None):
    """POST the jobs search for a resolved site. A wrong site id is a 404
    S21 upstream -> WorkdayNotFound naming the site."""
    return post_json(f"{cxs_base(site)}/jobs", body, {"locale": locale},
                     label=f"{site['tenant']}/{site['site']}/jobs",
                     not_found_message=f"career site '{site['site']}' not found on tenant "
                                       f"'{site['tenant']}' (see /workday/companies/details)") or {}


def cxs_job(site, job_path, locale=None):
    """GET one posting by its externalPath (the part after /job/)."""
    return get_json(f"{cxs_base(site)}/job/{job_path}", {"locale": locale},
                    label=f"{site['tenant']}/{site['site']}/job/{job_path}",
                    not_found_message=f"job '{job_path}' not found on {site['tenant']}/{site['site']}") or {}


# ---- fan-out ------------------------------------------------------------------------

_fanout = ThreadPoolExecutor(max_workers=FANOUT_WORKERS * 4, thread_name_prefix="workday-fanout")


def run_parallel(fns):
    if not fns:
        return []
    if len(fns) == 1:
        return [fns[0]()]
    futures = [_fanout.submit(fn) for fn in fns[1:]]
    first = fns[0]()
    return [first] + [f.result() for f in futures]


def map_parallel(fn, items, workers=FANOUT_WORKERS):
    """fn over items with bounded concurrency, results in order. Errors
    propagate from the first failing item."""
    items = list(items)
    if not items:
        return []
    if len(items) == 1:
        return [fn(items[0])]
    with ThreadPoolExecutor(max_workers=min(workers, len(items)), thread_name_prefix="workday-map") as pool:
        return list(pool.map(fn, items))


def soft(fn, default=None):
    """Wrap an optional lookup: any upstream failure yields `default`."""
    def run():
        try:
            return fn()
        except (UpstreamError, BadRequest, NotFound):
            return default
    return run


if __name__ == "__main__":
    # Smoke test: python workday/fetch.py [tenant wd site]
    args = sys.argv[1:] or ["adobe", "wd5", "external_experienced"]
    site = {"tenant": args[0], "wd": args[1], "site": args[2]}
    page = cxs_jobs(site, {"appliedFacets": {}, "limit": 3, "offset": 0, "searchText": "engineer"})
    print(f"{site['tenant']}/{site['site']}: {page.get('total')} jobs; facets:",
          [f.get("facetParameter") for f in page.get("facets") or []])
    first = (page.get("jobPostings") or [{}])[0]
    print("first:", first.get("title"), first.get("externalPath"))
    if first.get("externalPath"):
        job = cxs_job(site, first["externalPath"].split("/job/", 1)[1])
        info = job.get("jobPostingInfo") or {}
        print("detail:", info.get("jobReqId"), info.get("startDate"), info.get("timeType"), info.get("location"))
