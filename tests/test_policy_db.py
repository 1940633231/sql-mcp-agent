"""Tests for dedicated policy database connection separation."""
import pytest
import contextlib

from mcp_server.authorization import manager as manager_module
from mcp_server.authorization.manager import MySqlPolicyStore, PolicyConflictError
from mcp_server.authorization.policy import load_permission_policy, permission_policy_to_dict

from mcp_server import config
from mcp_server.database import connection


def test_policy_connection_uses_dedicated_configuration(monkeypatch):
    captured = {}

    class FakeConnection:
        pass

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return FakeConnection()

    monkeypatch.setattr(connection.pymysql, "connect", fake_connect)
    monkeypatch.setattr(config, "POLICY_DB_HOST", "policy-db")
    monkeypatch.setattr(config, "POLICY_DB_PORT", 3307)
    monkeypatch.setattr(config, "POLICY_DB_USER", "policy_user")
    monkeypatch.setattr(config, "POLICY_DB_PASSWORD", "secret")
    monkeypatch.setattr(config, "POLICY_DB_DATABASE", "mcp_policy")

    connection.policy_connect()

    assert captured["host"] == "policy-db"
    assert captured["port"] == 3307
    assert captured["user"] == "policy_user"
    assert captured["database"] == "mcp_policy"
    assert captured["autocommit"] is False


class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConnection:
    def __init__(self):
        self.events = []

    def begin(self):
        self.events.append("begin")

    def cursor(self):
        return FakeCursor()

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")


def test_mysql_publish_rejects_stale_expected_version(monkeypatch):
    class Cursor:
        def execute(self, sql, params=None):
            self.sql = sql

        def fetchone(self):
            return {"version": "current-version"}

    @contextlib.contextmanager
    def fake_transaction():
        yield Cursor()

    store = MySqlPolicyStore()
    monkeypatch.setattr(store, "ensure_schema", lambda: None)
    monkeypatch.setattr(manager_module, "policy_db_transaction", fake_transaction)
    document = permission_policy_to_dict(load_permission_policy())

    with pytest.raises(PolicyConflictError):
        store.publish(document, expected_version="stale-version")


def test_policy_transaction_commits_and_rolls_back(monkeypatch):
    connection_state = {"conn": FakeConnection()}
    monkeypatch.setattr(connection, "policy_connect", lambda: connection_state["conn"])
    with connection.policy_db_transaction():
        pass
    assert connection_state["conn"].events == ["begin", "commit", "close"]

    connection_state["conn"] = FakeConnection()
    with pytest.raises(RuntimeError):
        with connection.policy_db_transaction():
            raise RuntimeError("fail")
    assert connection_state["conn"].events == ["begin", "rollback", "close"]


def test_policy_connection_requires_dedicated_database(monkeypatch):
    monkeypatch.setattr(config, "POLICY_DB_DATABASE", "")
    monkeypatch.setattr(config, "POLICY_DB_USER", "")
    with pytest.raises(ValueError):
        config.get_policy_connection()
