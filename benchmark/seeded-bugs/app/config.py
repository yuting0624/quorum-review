"""Application configuration."""

import os


class Config:
    """Settings read from the environment. Secrets never get a fallback."""

    DATABASE_PATH = os.environ.get("QUORUM_DEMO_DB", "documents.sqlite3")

    # The signing key must come from the environment. Fail fast at import time
    # if it is missing rather than silently running with a known value.
    SECRET_KEY = os.environ["QUORUM_DEMO_SECRET"]

    PAGE_SIZE = 50
    MAX_PAGE_SIZE = 200

    EXPORT_ROOT = os.environ.get("QUORUM_DEMO_EXPORT_ROOT", "/var/lib/quorum-demo/exports")
