"""The 11 Workday endpoints. Every path is served with and without the
`/workday` prefix, so code generated against the hosted API on RapidAPI
(paths like /jobs/search) runs unchanged against this server.

Params are validated by the same marshmallow schemas the hosted API uses
(workday/schemas.py): one param per input — `site` takes a tenant, tenant/site
or a career-site link, `job` a posting link, posting id or requisition id,
`company` a name, domain, tenant or link.
"""
import json
from urllib.parse import urlencode

from bottle import request, response, route

from schema_fields import load_query
from scraper_errors import BadRequest, NotFound
from workday import companies, jobs, schemas


def json_response(data, status=200):
    response.status = status
    response.content_type = "application/json"
    return json.dumps(data, ensure_ascii=False)


def query_dict():
    """The request query as unicode strings (bottle 0.12's .get() hands back
    latin-1 decoded bytes, so a UTF-8 "Zürich" would arrive as "ZÃ¼rich")."""
    return {key: request.query.getunicode(key) for key in request.query.keys()}


def _page_link(params, page):
    if not page:
        return None
    query = {k: v for k, v in params.items() if v not in (None, "")}
    query["page"] = page
    parts = request.urlparts
    return f"{parts.scheme}://{parts.netloc}{parts.path}?{urlencode(query)}"


def paginate(result, params):
    """The scraper's `pagination` block -> the flat shape the hosted API
    returns (count / per_page / current_page / total_pages / next / previous)."""
    pagination = result.pop("pagination", None) or {}
    page = int(pagination.get("page") or params.get("page") or 1)
    total_pages = int(pagination.get("total_pages") or 0)
    out = {
        "count": pagination.get("total_count"),
        "per_page": pagination.get("items_per_page"),
        "current_page": page,
        "total_pages": total_pages,
        "next": _page_link(params, page + 1 if page < total_pages else None),
        "previous": _page_link(params, page - 1 if page > 1 else None),
    }
    out.update(result)
    return out


def call(label, schema, fn, paginated=False):
    """Validate the query, run the scraper and map errors: bad params -> 400,
    missing site / job / company -> 404, upstream failures -> 500."""
    raw = query_dict()
    data, error = load_query(schema, raw)
    if error:
        return json_response(error, 400)
    try:
        result = fn(**data)
    except ValueError as e:                # e.g. a filter value the site does not have
        return json_response({"error": str(e)}, 400)
    except BadRequest as e:                # upstream rejected the request
        return json_response({"error": f"workday rejected the request: {e}"}, 400)
    except NotFound as e:
        return json_response({"error": str(e) or "not found"}, 404)
    except Exception as e:                 # retries exhausted / blocked
        return json_response({"error": f"workday {label} failed: {e}"}, 500)
    if paginated:
        result = paginate(result, raw)
    return json_response(result)


ENDPOINTS = [
    ("/jobs/details", schemas.DetailsSchema, jobs.details, False),
    ("/jobs/search", schemas.SearchSchema, jobs.search, True),
    ("/jobs/all", schemas.AllSchema, jobs.all, False),
    ("/jobs/latest", schemas.LatestSchema, jobs.latest, False),
    ("/jobs/batch", schemas.BatchSchema, jobs.batch, False),
    ("/jobs/similar", schemas.DetailsSchema, jobs.similar, False),
    ("/jobs/filters", schemas.FiltersSchema, jobs.filters, False),
    ("/companies/details", schemas.CompanyDetailsSchema, companies.details, False),
    ("/companies/resolve", schemas.CompanySchema, companies.resolve, False),
    ("/companies/search", schemas.DirectorySchema, companies.search, True),
    ("/countries", schemas.EmptySchema, companies.countries, False),
]


def mount(path, schema, fn, paginated):
    """Serve one endpoint at /path and /workday/path."""
    label = path.strip("/")

    def handler():
        return call(label, schema, fn, paginated)

    handler.__name__ = "workday_" + label.replace("/", "_").replace("-", "_")
    route(path, method="GET")(handler)
    route("/workday" + path, method="GET")(handler)


for _path, _schema, _fn, _paginated in ENDPOINTS:
    mount(_path, _schema, _fn, _paginated)


@route("/", method="GET")
@route("/health", method="GET")
def health():
    return json_response({"status": "ok", "endpoints": [p for p, *_ in ENDPOINTS]})
