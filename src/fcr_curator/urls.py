"""URL helpers every source shares, so one file gets the same URL, domain and id wherever it was found."""

import hashlib
from urllib.parse import urlsplit, urlunsplit


def normalize_url(url: str) -> str:
    """Lower-case the scheme and host and drop any #fragment, so one file always has one URL."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def url_document_id(url: str, source: str) -> str:
    """Return a stable id for a file found by a source: '<source>_' plus a short hash of its URL."""
    return f"{source}_{hashlib.sha256(normalize_url(url).encode()).hexdigest()[:16]}"


def domain(url: str) -> str:
    """Return the URL's host without a leading 'www.'."""
    return urlsplit(url).netloc.lower().removeprefix("www.")
