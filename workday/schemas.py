"""Marshmallow request schemas for every /workday/* route. Generic fields
come from the shared schema_fields.py; this module adds the Workday
resolvers. Every schema's load() output is the kwargs dict its endpoint
function takes.

ONE param per input (tripadvisor QueryOrIdField convention, never a sibling
`url`/`id` pair):

    site      a career-site link in any shape, tenant/site, tenant.wd5/site
              OR a bare tenant (adobe -> its first career site)
    job       a posting link, a posting id (Research-Engineer_R166886), a job
              path (Seattle/Research-Engineer_R166886) OR a requisition id
              (R166886); `site` is needed unless it is a link
    jobs      comma-separated job refs (max 20)
    company   a company name, a domain, a tenant slug OR a career link
    facets    raw Workday facet filters as param:id pairs (from /jobs/filters),
              e.g. jobFamilyGroup:591af8…,locationCountry:bc33aa…

The friendly filters (country, region, location, location_type, category,
job_type, time_type, remote_type, posted_within) take a facet value id, its
label (case-insensitive; a unique substring is enough) or, for country, an
ISO code. They resolve against the site's own facet list in the endpoint.
"""
import re

from marshmallow import ValidationError, fields

from schema_fields import (BaseSchema, CommaListField, Flag, LimitField, PageField, PositiveInt,
                           RefField, StrippedString)
from workday import refs

_FACET_PAIR_RE = re.compile(r"^([A-Za-z0-9_\-]+)\s*[:=]\s*([0-9a-fA-F]{32})$")
_LOCALE_RE = re.compile(r"^[a-z]{2}(?:[-_][A-Za-z]{2,4})?$")


def _max_length(n):
    """validate.Length(max=n) that tolerates the None StrippedString returns
    for an empty value (marshmallow runs validators on it; len(None) is a 500)."""
    def check(value):
        if value is not None and len(value) > n:
            raise ValidationError(f"Longer than maximum length {n}.")
    return check


# ---- id-or-link fields -------------------------------------------------------------

class SiteField(RefField):
    resolver = staticmethod(refs.resolve_site)


class JobField(RefField):
    resolver = staticmethod(refs.resolve_job)


class JobListField(RefField):
    resolver = staticmethod(refs.resolve_jobs)


class CompanyField(RefField):
    resolver = staticmethod(refs.resolve_company)


class LocaleField(StrippedString):
    """A Workday locale tag (en-US, fr-FR, ja-JP, de-DE). A bare language is
    accepted and passed through as given — tenants that do not publish
    that language answer in their default one."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", None)
        super().__init__(**kwargs)

    def _deserialize(self, value, attr, data, **kwargs):
        value = super()._deserialize(value, attr, data, **kwargs)
        if value is None:
            return None
        if not _LOCALE_RE.match(value):
            raise ValidationError("Must be a locale tag such as en-US, fr-FR or ja-JP.")
        lang, _, region = value.replace("_", "-").partition("-")
        return f"{lang.lower()}-{region.upper()}" if region else lang.lower()


class FacetsField(fields.Field):
    """'param:id,param:id' -> {param: [id, ...]} (ids are the 32-hex facet
    value ids /workday/jobs/filters lists)."""

    def __init__(self, max_items=10, **kwargs):
        kwargs.setdefault("load_default", None)
        super().__init__(**kwargs)
        self.max_items = max_items

    def _deserialize(self, value, attr, data, **kwargs):
        out = {}
        pairs = [p.strip() for p in str(value).split(",") if p.strip()]
        if len(pairs) > self.max_items:
            raise ValidationError(f"At most {self.max_items} facet filters.")
        for pair in pairs:
            match = _FACET_PAIR_RE.match(pair)
            if not match:
                raise ValidationError("Each facet must be param:id with the 32-character value id from "
                                      "/workday/jobs/filters (jobFamilyGroup:591af8b812fa10737af39db3d96eed9f).")
            out.setdefault(match.group(1), [])
            if match.group(2).lower() not in out[match.group(1)]:
                out[match.group(1)].append(match.group(2).lower())
        return out or None


def _values(max_items=10):
    return CommaListField(upper=False, max_items=max_items)


# ---- schemas ---------------------------------------------------------------------------

class _FilterSchema(BaseSchema):
    """The friendly facet filters shared by search / filters / all."""
    country = _values()
    region = _values()
    location = _values()
    location_type = _values(3)
    category = _values()
    job_type = _values(5)
    time_type = _values(3)
    remote_type = _values(3)
    posted_within = _values(1)
    facets = FacetsField()


class EmptySchema(BaseSchema):
    pass


class SearchSchema(_FilterSchema):
    site = SiteField()
    query = StrippedString(required=False, load_default=None, validate=_max_length(200))
    page = PageField(max_page=100)
    locale = LocaleField()
    include_details = Flag()


class FiltersSchema(_FilterSchema):
    site = SiteField()
    query = StrippedString(required=False, load_default=None, validate=_max_length(200))
    locale = LocaleField()


class AllSchema(_FilterSchema):
    site = SiteField()
    query = StrippedString(required=False, load_default=None, validate=_max_length(200))
    locale = LocaleField()
    max_jobs = PositiveInt(max_value=5000, load_default=500)


class DetailsSchema(BaseSchema):
    job = JobField()
    site = SiteField(required=False, load_default=None)
    locale = LocaleField()


class BatchSchema(BaseSchema):
    jobs = JobListField()
    site = SiteField(required=False, load_default=None)
    locale = LocaleField()


class LatestSchema(BaseSchema):
    site = SiteField()
    locale = LocaleField()
    limit = LimitField(default=20, max_size=100)
    include_details = Flag()


class CompanySchema(BaseSchema):
    company = CompanyField()


class CompanyDetailsSchema(CompanySchema):
    locale = LocaleField()


class DirectorySchema(BaseSchema):
    query = StrippedString(required=False, load_default=None, validate=_max_length(80))
    page = PageField(max_page=100)
    limit = LimitField(default=25, max_size=100)
