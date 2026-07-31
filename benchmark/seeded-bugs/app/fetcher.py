"""Import a document from a URL the user supplies."""

import random
import time

import requests

MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 0.5
REQUEST_TIMEOUT = 10


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter.

    The jitter only spreads retries out so a burst of clients does not
    synchronise on the same schedule. It is not used for anything
    security-bearing, so the default RNG is the right tool.
    """
    return BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 0.25)


def fetch_remote_document(user: dict, url: str) -> dict:
    """Download a document the user asked us to import."""
    last_error = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return {
                "title": url.rsplit("/", 1)[-1] or "imported",
                "body": response.text,
                "source_url": url,
                "owner_id": user["id"],
            }
        except requests.RequestException as error:
            last_error = error
            time.sleep(_backoff(attempt))

    raise RuntimeError(f"could not fetch {url}: {last_error}")
