"""Tests for dashboard DB initialization race handling."""

import importlib

import pytest
from sqlalchemy.exc import OperationalError


def test_init_db_ignores_sqlite_table_exists_race(monkeypatch):
    import dashboard.db as db_module

    db_module = importlib.reload(db_module)

    def _raise_already_exists(*args, **kwargs):
        raise OperationalError(
            "CREATE TABLE users (...)",
            {},
            Exception("sqlite3.OperationalError: table users already exists"),
        )

    monkeypatch.setattr(db_module.Base.metadata, "create_all", _raise_already_exists)

    # Should not raise for the known concurrent-startup race.
    db_module.init_db()


def test_init_db_raises_other_operational_errors(monkeypatch):
    import dashboard.db as db_module

    db_module = importlib.reload(db_module)

    def _raise_other(*args, **kwargs):
        raise OperationalError(
            "CREATE TABLE users (...)",
            {},
            Exception("sqlite3.OperationalError: disk I/O error"),
        )

    monkeypatch.setattr(db_module.Base.metadata, "create_all", _raise_other)

    with pytest.raises(OperationalError):
        db_module.init_db()
