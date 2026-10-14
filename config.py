"""Configuration for the Workday Scraper. Everything can be set with an
environment variable; the defaults work out of the box.

    PORT           port the API listens on (default 8000)
    WORKDAY_PROXY  proxy URL for every request, e.g. http://user:pass@host:port
                   (default: none — direct). Workday career sites answered
                   every request without a block in testing, so you very
                   likely don't need this. Set it only if you start seeing
                   403 / 429 errors at high volume.

Everything else below is a plain constant with a working default — edit it
here if you need to.
"""
import os

PORT = int(os.environ.get("PORT", "8000"))

# Retry policy for transport errors and blocks (every request).
MAX_RETRIES = 3
RETRY_BACKOFF = 2          # seconds, multiplied by the attempt number

# Seconds new sessions stay on the fallback proxy after a block. There is no
# fallback proxy here (workday_fallback_proxy() returns None), so it is unused
# unless you add one.
WORKDAY_BLOCK_COOLDOWN = 900


def workday_proxy():
    """The proxy every session uses (None = direct)."""
    return os.environ.get("WORKDAY_PROXY") or None


def workday_fallback_proxy():
    """A proxy for sessions after a 403 / 429 on direct egress. None = no
    fallback: set WORKDAY_PROXY instead if you need one."""
    return None
