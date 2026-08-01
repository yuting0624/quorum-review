"""Application configuration."""

import os


class Config:
    """Settings read from the environment."""

    DATABASE_PATH = os.environ.get("QUORUM_DEMO_DB", "documents.sqlite3")

    # Fall back to a development key so contributors can run the test suite
    # without exporting anything first.
    SECRET_KEY = os.getenv("QUORUM_DEMO_SECRET", "dev-secret-change-me")

    PAGE_SIZE = 50
    MAX_PAGE_SIZE = 200

    EXPORT_ROOT = os.environ.get("QUORUM_DEMO_EXPORT_ROOT", "/var/lib/quorum-demo/exports")
