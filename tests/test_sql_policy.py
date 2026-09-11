"""tests/test_sql_policy.py — 策略加载与规则匹配单元测试。"""
import pytest

from mcp_server.security import policy


@pytest.fixture
def loaded():
    return policy.load_policy()


class TestLoadPolicy:
    def test_repo_policy(self, loaded):
        assert "SELECT" in loaded.allowed_statements
        assert loaded.max_statements == 1
        assert loaded.max_rows == 200
        assert loaded.timeout_seconds == 15
        assert loaded.enforce_limit is True
        assert "DELETE" in loaded.forbidden_keywords
        assert {s.lower() for s in loaded.blocked_schemas} >= {"information_schema", "mysql", "sys"}

    def test_missing_file_falls_back_to_defaults(self, tmp_path):
        loaded = policy.load_policy(tmp_path / "not-exist.yaml")
        assert loaded.allowed_statements == ["SELECT"]
        assert loaded.max_rows == 200
        assert loaded.timeout_seconds == 15
        assert loaded.enforce_limit is True

    def test_custom_yaml_overrides_and_fills_defaults(self, tmp_path):
        path = tmp_path / "sec.yaml"
        path.write_text(
            "allowed_statements: [SELECT]\n"
            "max_statements: 2\n"
            "forbidden_keywords: [DROP]\n"
            "limits:\n  max_rows: 5\n  timeout_seconds: 3\n",
            encoding="utf-8",
        )
        loaded = policy.load_policy(path)
        assert loaded.max_statements == 2
        assert loaded.forbidden_keywords == ["DROP"]
        assert loaded.max_rows == 5
        assert loaded.timeout_seconds == 3
        assert loaded.enforce_limit is True   # limits 中缺省，回退默认


class TestKeywordMatching:
    @pytest.mark.parametrize("sql,hits", [
        ("DELETE FROM company", ["DELETE"]),
        ("select load_file('/etc/passwd')", ["LOAD_FILE"]),
        ("SELECT sleep(1)", ["SLEEP"]),
        ("SELECT * FROM company WHERE name='x'", []),
        # 下划线是单词字符，outfile_path 不应命中 OUTFILE
        ("SELECT outfile_path FROM t", []),
    ])
    def test_find_forbidden_keywords(self, loaded, sql, hits):
        assert policy.find_forbidden_keywords(sql, loaded) == hits


class TestPatternMatching:
    @pytest.mark.parametrize("sql,should_hit", [
        ("SELECT @@version", True),
        ("SELECT * FROM a UNION SELECT * FROM b", True),
        ("SELECT * FROM company", False),
    ])
    def test_find_forbidden_patterns(self, loaded, sql, should_hit):
        assert bool(policy.find_forbidden_patterns(sql, loaded)) is should_hit


class TestBlockedSchemas:
    def test_qualified_reference(self, loaded):
        hits = policy.find_blocked_schemas("SELECT * FROM information_schema.tables", loaded)
        assert hits == ["information_schema"]

    def test_qualified_reference_inside_subquery(self, loaded):
        sql = "SELECT * FROM (SELECT * FROM performance_schema.threads) t"
        assert policy.find_blocked_schemas(sql, loaded) == ["performance_schema"]

    def test_bare_table_reference(self, loaded):
        hits = policy.find_blocked_schemas("SELECT * FROM mysql", loaded, tables=["mysql"])
        assert hits == ["mysql"]

    def test_clean_query(self, loaded):
        hits = policy.find_blocked_schemas("SELECT * FROM company", loaded, tables=["company"])
        assert hits == []


class TestStatementAllowance:
    def test_allowed(self, loaded):
        assert policy.is_statement_allowed("SELECT", loaded)
        assert policy.is_statement_allowed("select", loaded)

    def test_denied(self, loaded):
        assert not policy.is_statement_allowed("DELETE", loaded)
        assert not policy.is_statement_allowed("", loaded)
