"""Workday reference parsing: ONE param per input that auto-detects its
forms (tripadvisor QueryOrIdField convention — never a sibling `url`/`id`
pair). This module only normalises spelling; anything that needs the
network (which wd host a tenant lives on, a tenant's default site, a
requisition id -> posting) is done by shared.py inside the endpoint.

  site     a career-site link in any shape
               https://adobe.wd5.myworkdayjobs.com/external_experienced
               https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/Seattle/X_R1
               https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external_experienced/jobs
               https://acme.wd1.myworkdaysite.com/recruiting/acme/External
           | tenant/site         adobe/external_experienced
           | tenant.wd5/site     adobe.wd5/external_experienced
           | tenant              adobe   (the tenant's first career site)
  job      a posting link (…/{site}/job/{location}/{slug}_{req} or …/{site}/details/{slug}_{req})
           | {slug}_{req}        Research-Engineer_R166886   (the posting id, needs `site`)
           | {location}/{slug}_{req}                           (the externalPath, needs `site`)
           | {req}               R166886                       (the requisition id, needs `site`)
  company  a company name (Capital One), a domain (capitalone.com), a tenant
           slug (capitalone) or a career-site link
"""
import re
from urllib.parse import unquote, urlparse

from workday.fetch import JOBS_DOMAIN, SITE_DOMAIN

_LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2,4})?$")
_WD_RE = re.compile(r"^wd\d{1,3}(?:-impl)?$", re.I)
_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,62}$", re.I)
_SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_.]{0,120}$")
_REQ_RE = re.compile(r"^[A-Za-z]{0,6}[-_]?\d{2,12}(?:[-_][A-Za-z0-9]{1,8})?$")
_POSTING_ID_RE = re.compile(r"^[^/\s]+_[^/\s_]+$")
_SITE_RESERVED = {"job", "details", "login", "wday", "recruiting", "sitemap.xml", "sitemap.xml", "refreshfacet",
                  "talentcommunity", "robots.txt"}
MAX_BATCH = 20


def _is_url(value):
    low = value.lower()
    return low.startswith(("http://", "https://", "//")) or f".{JOBS_DOMAIN}" in low or f".{SITE_DOMAIN}" in low


def _parse_url(value):
    """-> (host labels, path segments, query) of a Workday career link.
    Raises ValueError for another host."""
    url = value.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif not url.lower().startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    domain = next((d for d in (JOBS_DOMAIN, SITE_DOMAIN) if host.endswith("." + d)), None)
    if domain is None:
        raise ValueError(f"not a {JOBS_DOMAIN} link")
    labels = host[: -len(domain) - 1].split(".")
    if len(labels) != 2 or not _WD_RE.match(labels[1]) or not _TENANT_RE.match(labels[0]):
        raise ValueError("a Workday career link looks like https://<tenant>.wd5.myworkdayjobs.com/<site>")
    segments = [unquote(s) for s in parsed.path.split("/") if s]
    return {"tenant": labels[0], "wd": labels[1].lower(), "domain": domain}, segments


def _site_from_url(value):
    """A career link -> {tenant, wd, domain, site|None, locale|None,
    job_path|None, posting_id|None}."""
    host, segments = _parse_url(value)
    out = dict(host, site=None, locale=None, job_path=None, posting_id=None)
    if not segments:
        return out
    if segments[0].lower() == "wday" and len(segments) >= 4 and segments[1].lower() == "cxs":
        # /wday/cxs/{tenant}/{site}/jobs | /job/{path}
        out["site"] = segments[3]
        rest = segments[4:]
    else:
        if _LOCALE_RE.match(segments[0]) and len(segments) > 1:
            out["locale"] = segments[0]
            segments = segments[1:]
        if segments and segments[0].lower() == "recruiting" and len(segments) >= 3:
            # myworkdaysite.com: /recruiting/{tenant}/{site}/...
            out["site"] = segments[2]
            rest = segments[3:]
        else:
            if not segments or segments[0].lower() in _SITE_RESERVED:
                return out
            out["site"] = segments[0]
            rest = segments[1:]
    if rest:
        kind = rest[0].lower()
        tail = [s for s in rest[1:] if s]
        lowered = [s.lower() for s in tail]
        if "apply" in lowered:   # .../job/{path}/apply[/applyManually|autofillWithResume|...]
            tail = tail[:lowered.index("apply")]
        if kind == "job" and tail:
            out["job_path"] = "/".join(tail)
            out["posting_id"] = tail[-1]
        elif kind == "details" and tail:
            out["posting_id"] = tail[-1]
    if out["site"] and not _SITE_RE.match(out["site"]):
        raise ValueError(f"'{out['site']}' is not a Workday career-site id")
    return out


# ---- links ------------------------------------------------------------------------

def site_link(site, locale=None):
    from workday.fetch import site_base
    return site_base(site, locale)


def job_link(site, job_path, locale=None):
    return f"{site_link(site, locale)}/job/{job_path}" if job_path else None


# ---- resolvers ----------------------------------------------------------------------

def resolve_site(value):
    """-> {tenant, wd|None, domain, site|None, locale|None}. `wd` None means
    the host is still to be found (shared.locate_tenant); `site` None means
    the tenant's first career site."""
    value = str(value or "").strip()
    if not value:
        raise ValueError("site is required")
    if _is_url(value):
        try:
            parsed = _site_from_url(value)
        except ValueError as e:
            raise ValueError(f"site must be a Workday career-site link, tenant/site or a tenant ({e})")
        return {k: parsed[k] for k in ("tenant", "wd", "domain", "site", "locale")}
    head, _, site = value.strip("/").partition("/")
    tenant, _, wd = head.partition(".")
    tenant = tenant.strip().lower()
    wd = wd.strip().lower() or None
    site = site.strip().strip("/") or None
    if not _TENANT_RE.match(tenant):
        raise ValueError("site must be a Workday tenant (adobe), tenant/site (adobe/external_experienced) "
                         "or a myworkdayjobs.com career-site link")
    if wd and not _WD_RE.match(wd):
        raise ValueError("the host part must look like wd5 (adobe.wd5/external_experienced)")
    if site and (not _SITE_RE.match(site.split("/")[0]) or site.split("/")[0].lower() in _SITE_RESERVED):
        raise ValueError(f"'{site}' is not a Workday career-site id")
    return {"tenant": tenant, "wd": wd, "domain": JOBS_DOMAIN, "site": site.split("/")[0] if site else None,
            "locale": None}


def resolve_job(value):
    """-> {site|None, job_path|None, posting_id|None, req_id|None, locale|None}.
    Exactly one of job_path / posting_id / req_id is set; `site` is set only
    when the value was a link."""
    value = str(value or "").strip()
    if not value:
        raise ValueError("job is required")
    out = {"input": value, "site": None, "job_path": None, "posting_id": None, "req_id": None, "locale": None}
    if _is_url(value):
        try:
            parsed = _site_from_url(value)
        except ValueError as e:
            raise ValueError(f"job must be a Workday posting link, a posting id or a requisition id ({e})")
        if not parsed["site"] or not parsed["posting_id"]:
            raise ValueError("job link must point at one posting "
                             "(https://adobe.wd5.myworkdayjobs.com/external_experienced/job/Seattle/Research-Engineer_R166886)")
        out["site"] = {k: parsed[k] for k in ("tenant", "wd", "domain", "site", "locale")}
        out["job_path"] = parsed["job_path"]
        out["posting_id"] = None if parsed["job_path"] else parsed["posting_id"]
        out["locale"] = parsed["locale"]
        return out
    value = value.strip("/")
    if value.lower().startswith("job/"):
        value = value[4:]
    if "/" in value:
        tail = value.rsplit("/", 1)[-1]
        if not _POSTING_ID_RE.match(tail):
            raise ValueError("job path must end in the posting id (Seattle/Research-Engineer_R166886)")
        return dict(out, job_path=value)
    if _POSTING_ID_RE.match(value) and not _REQ_RE.match(value):
        return dict(out, posting_id=value)
    if _REQ_RE.match(value):
        return dict(out, req_id=value)
    raise ValueError("job must be a posting link, a posting id (Research-Engineer_R166886), "
                     "a job path (Seattle/Research-Engineer_R166886) or a requisition id (R166886)")


def resolve_jobs(value):
    """Comma-separated job refs -> [resolve_job(...)] (deduped, order kept,
    at most MAX_BATCH)."""
    out, seen = [], set()
    for raw in str(value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        ref = resolve_job(raw)
        key = (ref["job_path"] or ref["posting_id"] or ref["req_id"] or "").lower(), \
            (ref["site"] or {}).get("tenant"), (ref["site"] or {}).get("site")
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    if not out:
        raise ValueError("jobs is required")
    if len(out) > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} jobs per call")
    return out


_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9\-]+(?:\.[a-z0-9\-]+)+)/?$", re.I)


def resolve_company(value):
    """Company name, domain, tenant slug or career link -> {"site": resolved
    site} | {"name": name, "domain": registrable domain|None}."""
    value = " ".join(str(value or "").split())
    if not value:
        raise ValueError("company is required")
    if _is_url(value):
        return {"site": resolve_site(value), "name": None, "domain": None}
    match = _DOMAIN_RE.match(value)
    if match:
        domain = match.group(1).lower()
        return {"site": None, "name": domain.split(".")[0], "domain": domain}
    if len(value) > 80:
        raise ValueError("company name is too long")
    return {"site": None, "name": value, "domain": None}


def slug_candidates(name):
    """'Capital One' -> ['capitalone', 'capital-one', 'capital_one', 'capital'];
    the spellings Workday tenants use, most likely first."""
    base = re.sub(r"[^a-z0-9 \-_]", "", str(name or "").lower().replace("&", " and ")).strip()
    words = [w for w in re.split(r"[\s\-_]+", base) if w]
    out = []
    for cand in ("".join(words), "-".join(words), "_".join(words), words[0] if words else ""):
        if cand and _TENANT_RE.match(cand) and cand not in out:
            out.append(cand)
    for stop in ("inc", "corp", "corporation", "co", "llc", "ltd", "plc", "group", "company", "the"):
        if stop in words and len(words) > 1:
            kept = [w for w in words if w != stop]
            for cand in ("".join(kept), "-".join(kept)):
                if cand and cand not in out:
                    out.append(cand)
    return out
