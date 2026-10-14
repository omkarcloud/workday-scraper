"""Cache TTL per /workday/* endpoint (cache.py, keyed on the validated
params).

Career sites change through the working day: postings open and close in
batches (Workday re-indexes its search every few minutes), so listings are
kept short; one posting's text almost never changes after it is published;
a tenant's host and site list change only when HR reconfigures Workday.
"""
from datetime import timedelta

# --- jobs ----------------------------------------------------------------------
SEARCH_CACHE = timedelta(minutes=30)
FILTERS_CACHE = timedelta(hours=1)
LATEST_CACHE = timedelta(minutes=15)
# A full dump is 25-250 upstream calls; serve it for a while.
ALL_CACHE = timedelta(hours=2)
DETAILS_CACHE = timedelta(hours=6)
BATCH_CACHE = timedelta(hours=2)
SIMILAR_CACHE = timedelta(hours=6)

# --- companies -------------------------------------------------------------------
RESOLVE_CACHE = timedelta(days=1)
COMPANY_CACHE = timedelta(hours=6)
DIRECTORY_CACHE = timedelta(days=1)
COUNTRIES_CACHE = timedelta(days=7)
