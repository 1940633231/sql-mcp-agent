"""Real-MySQL multi-principal permission integration tests.

Run with:
    $env:RUN_DB_TESTS=1; python -m pytest tests/test_permission_integration.py -q
"""
import os

import pytest

from mcp_server.auth.models import Principal, RequestContext
from mcp_server.authorization.policy import load_permission_policy, permission_policy_to_dict
from mcp_server.authorization.manager import MySqlPolicyStore, PolicyManager
from mcp_server.authorization.service import AuthorizationService
from mcp_server.security.validator import SqlValidator
from mcp_server.services.query_service import QueryService


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="需设置 RUN_DB_TESTS=1 且 MySQL 可用",
)


def _context(subject: str) -> RequestContext:
    policy = load_permission_policy()
    raw = policy.principals[subject]
    return RequestContext(
        principal=Principal(raw.subject, raw.roles, raw.attributes),
        source="db-integration",
        auth_method="test",
    )


def _service() -> QueryService:
    policy = load_permission_policy()
    return QueryService(
        validator=SqlValidator(),
        authorizer=AuthorizationService(policy),
    )


def test_admin_sees_all_permission_fixtures():
    result = _service().run(
        "SELECT * FROM company WHERE company_id BETWEEN 9001 AND 9006 ORDER BY company_id",
        _context("local-dev"),
    )
    assert "error" not in result, result
    assert [row["company_id"] for row in result["rows"]] == [9001, 9002, 9003, 9004, 9005, 9006]
    assert "employees" in result["columns"]


def test_alice_company_rows_and_columns_are_filtered():
    result = _service().run(
        "SELECT * FROM company WHERE company_id BETWEEN 9001 AND 9006 ORDER BY company_id",
        _context("alice"),
    )
    assert "error" not in result, result
    assert [row["company_id"] for row in result["rows"]] == [9001, 9002]
    assert "employees" not in result["columns"]


def test_bob_company_rows_and_columns_are_filtered():
    result = _service().run(
        "SELECT * FROM company WHERE company_id BETWEEN 9001 AND 9006 ORDER BY company_id",
        _context("bob"),
    )
    assert "error" not in result, result
    assert [row["company_id"] for row in result["rows"]] == [9003, 9004]
    assert "employees" not in result["columns"]


def test_alice_sales_are_scoped_to_fixed_company_ids():
    result = _service().run(
        "SELECT * FROM sale_records WHERE company_id BETWEEN 9001 AND 9006",
        _context("alice"),
    )
    assert "error" not in result, result
    assert {row["company_id"] for row in result["rows"]} == {9001, 9002}
    assert result["row_count"] == 6


def test_bob_sales_are_scoped_to_fixed_company_ids():
    result = _service().run(
        "SELECT * FROM sale_records WHERE company_id BETWEEN 9001 AND 9006",
        _context("bob"),
    )
    assert "error" not in result, result
    assert {row["company_id"] for row in result["rows"]} == {9003, 9004}
    assert result["row_count"] == 6


def test_cte_and_derived_table_lineage_with_real_database():
    service = _service()
    cte = service.run(
        "WITH scoped AS (SELECT * FROM company) "
        "SELECT * FROM scoped WHERE company_id BETWEEN 9001 AND 9006",
        _context("alice"),
    )
    derived = service.run(
        "SELECT * FROM (SELECT * FROM company) x "
        "WHERE company_id BETWEEN 9001 AND 9006",
        _context("alice"),
    )
    for result in (cte, derived):
        assert "error" not in result, result
        assert [row["company_id"] for row in result["rows"]] == [9001, 9002]
        assert "employees" not in result["columns"]


def test_viewer_cannot_run_query():
    result = _service().run("SELECT name FROM company", _context("auditor"))
    assert result["code"] == "permission_denied"


def test_mysql_policy_store_versioning_and_hot_reload():
    policy = load_permission_policy()
    store = MySqlPolicyStore()
    manager = PolicyManager(store, reload_seconds=0)
    record = manager.publish(
        permission_policy_to_dict(policy),
        actor="integration-test",
        reason="v0.3 integration",
    )
    assert record.version.startswith("policy-")
    assert manager.status()["source"] == "mysql"
