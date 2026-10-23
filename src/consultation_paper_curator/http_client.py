"""HTTP session and file download helpers shared by every source."""

import os

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "ConsultationPaperCurator/1.0"
RETRY_STATUSES = (429, 500, 502, 503, 504)
DOWNLOAD_TIMEOUT_SECONDS = 120
DOWNLOAD_CHUNK_BYTES = 1 << 20


def create_session(retries: int = 3, backoff_factor: float = 1.0) -> requests.Session:
    """Create an HTTP session that retries transient failures on every method."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=None,  # Source filters are POSTs; retry them too
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def download_file(session: requests.Session, url: str, path: str) -> bool:
    """Stream url to path; on failure log it, remove any partial file and return False."""
    try:
        with session.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS, stream=True) as response:
            response.raise_for_status()
            with open(path, "wb") as file:
                file.writelines(response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES))
        return True
    except (requests.RequestException, OSError) as exc:
        logger.warning(f"Failed to download {url}: {exc}")
        if os.path.exists(path):
            os.remove(path)
        return False
