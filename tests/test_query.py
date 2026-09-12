"""tests/test_query.py — QueryService 编排层单元测试。

默认只跑「不依赖数据库」的分支（校验拦截、LIMIT 注入、错误收敛）；
真实连库用例默认跳过，需设置环境变量 RUN_DB_TESTS=1 且 MySQL 可用时执行：

    $env:RUN_DB_TESTS=1; python -m pytest tests/test_query.py
"""
import os

import pymysql
import pytest

from mcp_server.security.models import QueryResult
from mcp_server.auth.models import Principal, RequestContext
from mcp_server.authorization.policy import load_permission_policy
from mcp_server.security.validator import SqlValidator
from mcp_server.services import query_service as query_service_module
from mcp_server.services.query_service import QueryService


def _ok_query_result(rows=None, truncated=False):
    rows = rows or []
    return QueryResult(
        columns=["a"],
        rows=rows,
        row_count=len(rows),
        elapsed_seconds=0.01,
        truncated=truncated,
    )


@pytest.fixture
def service(monkeypatch):
    # 用真实 validator（真实规则），executor 不落地、靠 monkeypatch 注入
    policy = load_permission_policy()
    raw = policy.principals["query-admin"]
    context = RequestContext(Principal(raw.subject, raw.roles, raw.attributes))
    monkeypatch.setattr(
        query_service_module,
        "current_request_context",
        lambda source="mcp": context,
    )
    return QueryService(validator=SqlValidator())


class TestValidationGate:
    def test_malicious_sql_rejected_without_execution(self, service, monkeypatch):
        called = {"value": False}

        def fake_run(*args, **kwargs):
            called["value"] = True
            raise AssertionError("校验失败时不应执行查询")

        monkeypatch.setattr(service.executor, "run", fake_run)
        result = service.run("DELETE FROM company")
        assert "error" in result
        assert called["value"] is False

    def test_rejection_carries_code(self, service):
        result = service.run("SELECT * FROM information_schema.tables")
        assert result.get("code") == "blocked_schema"


class TestLimitEnforcement:
    def test_appends_limit_when_missing(self, service, monkeypatch):
        captured = {}

        def fake_run(sql, max_rows, timeout_seconds, max_result_bytes=None):
            captured["sql"] = sql
            captured["max_rows"] = max_rows
            return _ok_query_result()

        monkeypatch.setattr(service.executor, "run", fake_run)
        service.run("SELECT * FROM company")
        assert captured["sql"].endswith("LIMIT %d" % service.policy.max_rows)
        assert captured["max_rows"] == service.policy.max_rows

    def test_keeps_existing_limit(self, service, monkeypatch):
        captured = {}

        def fake_run(sql, *args, **kwargs):
            captured["sql"] = sql
            return _ok_query_result()

        monkeypatch.setattr(service.executor, "run", fake_run)
        service.run("SELECT * FROM company LIMIT 5")
        assert captured["sql"].endswith("LIMIT 5")

    def test_limit_keyword_in_string_literal_not_confused(self, service, monkeypatch):
        """AST 判定 LIMIT：字符串字面量里出现 limit 不应误判为已有限制。"""
        captured = {}

        def fake_run(sql, *args, **kwargs):
            captured["sql"] = sql
            return _ok_query_result()

        monkeypatch.setattr(service.executor, "run", fake_run)
        service.run("SELECT 'this has limit word' AS msg FROM company")
        assert captured["sql"].endswith("LIMIT %d" % service.policy.max_rows)

    def test_trailing_semicolon_stripped_before_limit(self, service, monkeypatch):
        captured = {}

        def fake_run(sql, *args, **kwargs):
            captured["sql"] = sql
            return _ok_query_result()

        monkeypatch.setattr(service.executor, "run", fake_run)
        service.run("SELECT * FROM company;")
        assert "; LIMIT" not in captured["sql"].upper()
        assert captured["sql"].endswith("LIMIT %d" % service.policy.max_rows)


class TestResultShape:
    def test_returns_executor_result_as_dict(self, service, monkeypatch):
        def fake_run(sql, max_rows, timeout_seconds, max_result_bytes=None):
            return _ok_query_result(rows=[{"a": 1}])

        monkeypatch.setattr(service.executor, "run", fake_run)
        result = service.run("SELECT 1 AS a")
        assert result["columns"] == ["a"]
        assert result["rows"] == [{"a": 1}]
        assert result["row_count"] == 1
        assert result["truncated"] is False

    def test_database_error_is_captured(self, service, monkeypatch):
        def fake_run(sql, max_rows, timeout_seconds, max_result_bytes=None):
            raise pymysql.err.ProgrammingError(1054, "Unknown column 'foo'")

        monkeypatch.setattr(service.executor, "run", fake_run)
        result = service.run("SELECT foo FROM company")
        assert "error" in result


class TestToolThin:
    """Tool 层只做协议转换：直接委托 QueryService，不含安全/执行逻辑。"""

    def test_tool_delegates_to_service(self, service, monkeypatch):
        from mcp_server.tools import query as query_tools

        def fake_run(sql):
            return {"error": "仅允许 SELECT 只读查询", "code": "not_readonly"}

        monkeypatch.setattr(query_tools._service, "run", fake_run)
        assert query_tools.run_query("DELETE FROM company")["code"] == "not_readonly"


@pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="需设置 RUN_DB_TESTS=1 且 MySQL 可用")
def test_run_query_against_real_database():
    from mcp_server.tools import query as query_tools
    result = query_tools.run_query("SELECT 1 AS n")
    assert result["rows"] == [{"n": 1}]


class TestByteLimit:
    """结果字节上限（max_result_bytes）：行数未超但大字段超限时也需截断。"""

    def test_row_bytes_estimates_utf8(self):
        from mcp_server.database.executor import _row_bytes
        assert _row_bytes({"a": "abc"}) == 3
        # 中文按 UTF-8 计 3 字节
        assert _row_bytes({"a": "中", "b": 123}) >= 3

    def test_captures_result_bytes_on_result(self, service, monkeypatch):
        captured = {}
        big_rows = [{"a": "x" * 1000}] * 5

        def fake_run(sql, max_rows, timeout_seconds, max_result_bytes):
            captured["max_result_bytes"] = max_result_bytes
            return _ok_query_result(rows=big_rows)

        monkeypatch.setattr(service.executor, "run", fake_run)
        result = service.run("SELECT * FROM company")
        assert captured["max_result_bytes"] == service.policy.max_result_bytes


class TestConcurrency:
    def test_rejects_when_concurrency_exhausted(self, service, monkeypatch):
        import threading

        # 耗尽信号量
        for _ in range(service.policy.max_concurrent_queries):
            assert service._semaphore.acquire(blocking=False)
        try:
            result = service.run("SELECT * FROM company")
        finally:
            # 释放，避免影响其它用例
            for _ in range(service.policy.max_concurrent_queries):
                service._semaphore.release()
        assert "error" in result

    def test_normal_query_releases_semaphore(self, service, monkeypatch):
        def fake_run(sql, max_rows, timeout_seconds, max_result_bytes=None):
            return _ok_query_result()

        monkeypatch.setattr(service.executor, "run", fake_run)
        assert service.run("SELECT * FROM company")["truncated"] is False
        # 结束后信号量应释放回满额
        assert service._semaphore.acquire(blocking=False)
        service._semaphore.release()
