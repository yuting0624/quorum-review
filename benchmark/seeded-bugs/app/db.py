"""Thin SQLite wrapper."""

import sqlite3
from contextlib import contextmanager

from .config import Config


@contextmanager
def connect():
    conn = sqlite3.connect(Config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def query(sql, params=()):
    """Run a parameterised query and return every row."""
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def execute(sql, params=()):
    """Run a write and return the number of affected rows."""
    with connect() as conn:
        cursor = conn.execute(sql, params)
        return cursor.rowcount
