"""Workday normalizers: the CXS API's objects -> one clean snake_case shape
per entity.

Output conventions (shared with the other scrapers here): `link` for
canonical page URLs (always absolute), `*_count` for counters, `is_*` /
`has_*` / `can_*` for booleans, ISO dates ("2026-09-18"), numbers as
numbers, null for missing.

One job shape everywhere (search, latest, all, similar, batch); the details
endpoint adds the long-form blocks to the same object:

  job      {id (posting id), requisition_id, title, link, path,
            location {text, count}, remote_type, time_type,
            posted {text, date, days_ago}, bullets}
           + details: workday_id, description, description_html,
             location.country / additional, closes {date, text},
             can_apply, is_posted, has_resume_parsing, company, site,
             similar_jobs

Deliberately dropped as noise:
  * userAuthenticated (viewer state — always false for us);
  * questionnaireId / secondaryQuestionnaireId / includeResumeParsing's
    apply-flow ids (the apply form is out of scope; the resume flag is kept
    as has_resume_parsing), jobPostingSiteId (= the site block);
  * jobPostingEndDateAsText ("End Date: October 16, 2026" — the date is
    kept as closes.date, the countdown as closes.text);
  * hiringOrganization.url (always empty on every tenant seen);
  * the Workday internal ids of facet groups without values.
"""
import html
import re
from datetime import date, timedelta

from workday import refs

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*(?:br\s*/?|/p|/div|/h[1-6]|/tr|/ul|/ol)\s*>", re.I)
_LI_END_RE = re.compile(r"<\s*/li\s*>", re.I)
_LI_RE = re.compile(r"<\s*li[^>]*>", re.I)
_SPACE_RE = re.compile(r"[ \t ]+")
_BLANK_RE = re.compile(r"\n{3,}")
_POSTED_RE = re.compile(r"(\d+)\+?\s*(day|week|month|year)s?\s*ago", re.I)
_LOCATIONS_RE = re.compile(r"^\s*(\d+)\s+locations?\s*$", re.I)
# a leading Workday legal-entity code: "TW07 ", "2100 ", "7Z4 ", "ADUS-" (a
# two-character token is kept unless all digits: "3M Company" stays)
_ENTITY_CODE_RE = re.compile(r"^(?:(?=[A-Z0-9]*\d)(?:[A-Z0-9]{3,5}|\d{1,2})\s+|[A-Z]{2,6}-(?=[A-Z]))")


# ---- value helpers ------------------------------------------------------------

def text(value):
    if isinstance(value, str):
        value = " ".join(value.split())
        return value or None
    return None


def to_int(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def clean_entity(name):
    """'2100 NVIDIA USA' -> 'NVIDIA USA'; '414 salesforce.com France S.A.S.'
    -> 'salesforce.com France S.A.S.'; 'ADUS-Adobe Inc.' -> 'Adobe Inc.'
    (Workday prefixes legal entities with an internal code)."""
    name = text(name)
    if not name:
        return None
    return text(_ENTITY_CODE_RE.sub("", name)) or name


def bool_or_none(value):
    return value if isinstance(value, bool) else None


def html_to_text(raw):
    """Workday's description HTML -> readable plain text (paragraphs kept
    as blank lines, list items as "- " bullets)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = _LI_END_RE.sub("", raw)
    value = _LI_RE.sub("\n- ", value)
    value = _BREAK_RE.sub("\n", value)
    value = _TAG_RE.sub("", value)
    value = html.unescape(value)
    value = _SPACE_RE.sub(" ", value)
    lines = [line.strip() for line in value.split("\n")]
    value = _BLANK_RE.sub("\n\n", "\n".join(lines)).strip()
    return value or None


def posted(posted_on, start_date=None, today=None):
    """"Posted 26 Days Ago" (+ the exact startDate when the detail has it)
    -> {text, date, days_ago}. Without startDate the date is derived from
    the relative text ("30+ Days Ago" -> null date, days_ago 30)."""
    label = text(posted_on)
    today = today or date.today()
    days = None
    exact = text(start_date)
    if label:
        low = label.lower()
        if "today" in low or "just" in low:
            days = 0
        elif "yesterday" in low:
            days = 1
        else:
            match = _POSTED_RE.search(low)
            if match:
                n, unit = int(match.group(1)), match.group(2)
                days = n * {"day": 1, "week": 7, "month": 30, "year": 365}[unit]
    if exact:
        try:
            days = (today - date.fromisoformat(exact[:10])).days
        except ValueError:
            exact = None
    when = exact or ((today - timedelta(days=days)).isoformat() if days is not None and "+" not in (label or "") else None)
    return {"text": label, "date": when, "days_ago": days}


def _location_block(locations_text):
    """"Seattle" | "3 Locations" -> {text, count}."""
    label = text(locations_text)
    match = _LOCATIONS_RE.match(label or "")
    if match:
        return {"text": label, "count": int(match.group(1))}
    return {"text": label, "count": 1 if label else None}


def posting_id_of(external_path):
    path = text(external_path) or ""
    return path.rsplit("/", 1)[-1] or None


def job_path_of(external_path):
    """"/job/Seattle/Research-Engineer_R166886" -> "Seattle/Research-Engineer_R166886"."""
    path = (text(external_path) or "").strip("/")
    if path.lower().startswith("job/"):
        path = path[4:]
    return path or None


# ---- jobs -------------------------------------------------------------------------

def job(row, site, locale=None):
    """One search / similar-jobs row -> the job shape."""
    row = row if isinstance(row, dict) else {}
    path = job_path_of(row.get("externalPath"))
    bullets = [b for b in (text(x) for x in row.get("bulletFields") or []) if b]
    posting_id = posting_id_of(path)
    return {
        "id": posting_id,
        "requisition_id": bullets[0] if bullets else (posting_id.rsplit("_", 1)[-1] if posting_id and "_" in posting_id else None),
        "title": text(row.get("title")),
        "link": refs.job_link(site, path, locale),
        "path": path,
        "location": _location_block(row.get("locationsText")),
        "remote_type": text(row.get("remoteType")),
        "time_type": text(row.get("timeType")),
        "posted": posted(row.get("postedOn"), row.get("startDate")),
        "bullets": bullets,
    }


def job_details(body, site, locale=None):
    """The CXS job document -> the job shape with the long-form blocks."""
    body = body if isinstance(body, dict) else {}
    info = body.get("jobPostingInfo") if isinstance(body.get("jobPostingInfo"), dict) else {}
    org = body.get("hiringOrganization") if isinstance(body.get("hiringOrganization"), dict) else {}
    from workday.shared import country_of
    posting_id = text(info.get("jobPostingId"))
    external = text(info.get("externalUrl"))
    path = None
    if external and "/job/" in external:
        path = external.split("/job/", 1)[1].split("?")[0].strip("/") or None
    if not path and posting_id:
        path = posting_id
    country_raw = info.get("country") if isinstance(info.get("country"), dict) else {}
    req_loc = info.get("jobRequisitionLocation") if isinstance(info.get("jobRequisitionLocation"), dict) else {}
    req_country = req_loc.get("country") if isinstance(req_loc.get("country"), dict) else {}
    country = country_of(text(country_raw.get("descriptor")) or text(req_country.get("descriptor")),
                         text(country_raw.get("id")) or text(req_country.get("id")))
    if country and not country.get("code") and text(req_country.get("alpha2Code")):
        country["code"] = text(req_country.get("alpha2Code")).upper()
    additional = [a for a in (text(x) for x in info.get("additionalLocations") or []) if a]
    main_location = text(info.get("location"))
    closes_date = text(info.get("endDate"))
    closes_text = text(info.get("timeLeftToApply"))
    similar = [job(r, site, locale) for r in body.get("similarJobs") or [] if isinstance(r, dict)]
    req_id = text(info.get("jobReqId"))
    description_html = info.get("jobDescription") if isinstance(info.get("jobDescription"), str) else None
    return {
        "id": posting_id,
        "requisition_id": req_id or (posting_id.rsplit("_", 1)[-1] if posting_id and "_" in posting_id else None),
        "workday_id": text(info.get("id")),
        "title": text(info.get("title")),
        "link": external or refs.job_link(site, path, locale),
        "path": path,
        "description": html_to_text(description_html),
        "description_html": description_html.strip() if description_html and description_html.strip() else None,
        "location": {
            "text": main_location,
            "count": 1 + len(additional) if main_location else (len(additional) or None),
            "country": country,
            "additional": additional,
        },
        "remote_type": text(info.get("remoteType")),
        "time_type": text(info.get("timeType")),
        "posted": posted(info.get("postedOn"), info.get("startDate")),
        "closes": {"date": closes_date, "text": closes_text} if (closes_date or closes_text) else None,
        "can_apply": bool_or_none(info.get("canApply")),
        "is_posted": bool_or_none(info.get("posted")),
        "has_resume_parsing": bool_or_none(info.get("includeResumeParsing")),
        "company": {"name": clean_entity(org.get("name")), "link": text(org.get("url")) or refs.site_link(site)},
        "similar_jobs": similar,
    }


# ---- facets ----------------------------------------------------------------------

def facets(flat, applied=None):
    """shared.flatten_facets() output -> the public filters block:
    [{param, label, values: [{id, label, count, is_selected}]}]."""
    applied = applied or {}
    out = []
    for facet in flat or []:
        chosen = {i for i in applied.get(facet["param"], [])}
        out.append({
            "param": facet["param"],
            "label": facet["label"],
            "count": len(facet["values"]),
            "values": [dict(v, is_selected=v["id"] in chosen) for v in facet["values"]],
        })
    return out


# ---- sitemap -------------------------------------------------------------------------

_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S | re.I)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_ALT_RE = re.compile(r'<xhtml:link[^>]*hreflang="([^"]+)"[^>]*href="([^"]+)"', re.I)
_LASTMOD_RE = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.I)


def sitemap(xml):
    """/{site}/siteMap.xml -> [{link, locales: {hreflang: link}, last_modified}]
    in document order (newest first on every site seen)."""
    out = []
    for block in _URL_BLOCK_RE.findall(xml or ""):
        loc = _LOC_RE.search(block)
        if not loc:
            continue
        link = html.unescape(loc.group(1))
        alternates = {hl: html.unescape(href) for hl, href in _ALT_RE.findall(block)}
        mod = _LASTMOD_RE.search(block)
        out.append({"link": link, "locales": alternates, "last_modified": mod.group(1)[:10] if mod else None})
    return out


def pagination(page, per_page, total):
    total = to_int(total)
    pages = (total + per_page - 1) // per_page if total else 0
    return {"page": page, "items_per_page": per_page, "total_pages": pages, "total_count": total}
