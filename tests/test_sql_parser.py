"""tests/test_sql_parser.py — 结构化解析单元测试（不依赖数据库/网络）。"""
import pytest

from mcp_server.security import parser
from mcp_server.security.models import StatementKind


class TestNormalize:
    def test_strip_whitespace(self):
        assert parser.normalize("  SELECT 1  ") == "SELECT 1"

    def test_none_becomes_empty(self):
        assert parser.normalize(None) == ""

    def test_strip_trailing_semicolon(self):
        assert parser.strip_trailing_semicolon("SELECT 1;") == "SELECT 1"
        assert parser.strip_trailing_semicolon("SELECT 1") == "SELECT 1"
        assert parser.strip_trailing_semicolon("  SELECT 1 ;;  ") == "SELECT 1"


class TestSplitStatements:
    def test_single(self):
        assert parser.split_statements("SELECT 1") == ["SELECT 1"]

    def test_trailing_semicolon_still_single(self):
        assert len(parser.split_statements("SELECT * FROM company;")) == 1

    def test_multi(self):
        stmts = parser.split_statements("SELECT 1; DELETE FROM t;")
        assert len(stmts) == 2

    def test_empty(self):
        assert parser.split_statements("") == []


class TestClassify:
    @pytest.mark.parametrize("sql,keyword,kind", [
        ("SELECT * FROM company", "SELECT", StatementKind.SELECT),
        ("select 1", "SELECT", StatementKind.SELECT),
        # CTE 也应识别为 SELECT（而不是 WITH）
        ("WITH x AS (SELECT 1) SELECT * FROM x", "SELECT", StatementKind.SELECT),
        ("DELETE FROM company", "DELETE", StatementKind.OTHER),
        ("INSERT INTO company VALUES (1)", "INSERT", StatementKind.OTHER),
        ("UPDATE company SET name='x'", "UPDATE", StatementKind.OTHER),
    ])
    def test_classify(self, sql, keyword, kind):
        kind_out, keyword_out = parser.classify(sql)
        assert keyword_out == keyword
        assert kind_out is kind


class TestExtractTables:
    @pytest.mark.parametrize("sql,tables", [
        ("SELECT * FROM company", ["company"]),
        ("SELECT * FROM company co", ["company"]),
        ("SELECT * FROM a, b", ["a", "b"]),
        ("SELECT * FROM `company`", ["company"]),
        ("SELECT * FROM information_schema.tables", ["information_schema.tables"]),
        (
            "SELECT co.name FROM company co JOIN sale_records s ON co.company_id = s.company_id",
            ["company", "sale_records"],
        ),
        ("UPDATE company SET name='x'", ["company"]),
        ("INSERT INTO company VALUES (1)", ["company"]),
        # 子查询的表名在顶层不可见，应跳过而不误报
        ("SELECT * FROM (SELECT 1) t", []),
    ])
    def test_extract(self, sql, tables):
        assert parser.extract_tables(sql) == tables


class TestIsValidIdentifier:
    def test_valid(self):
        pattern = r"^[A-Za-z0-9_]+$"
        assert parser.is_valid_identifier("company_1", pattern)

    @pytest.mark.parametrize("name", ["company;DROP", "..%2Fetc", "bad-name", "", "a b"])
    def test_invalid(self, name):
        assert not parser.is_valid_identifier(name, r"^[A-Za-z0-9_]+$")


class TestParse:
    def test_aggregates_single(self):
        parsed = parser.parse("SELECT * FROM company;")
        assert parsed.statement_count == 1
        assert parsed.keyword == "SELECT"
        assert parsed.kind is StatementKind.SELECT
        assert parsed.tables == ["company"]

    def test_multi_statement_count(self):
        parsed = parser.parse("SELECT 1; DELETE FROM t")
        assert parsed.statement_count == 2
        assert parsed.keyword == "SELECT"

    def test_empty(self):
        parsed = parser.parse("   ")
        assert parsed.statement_count == 0
        assert parsed.tables == []


class TestAstCapabilities:
    """sqlglot AST 相较旧 sqlparse 新增的结构抽取能力。"""

    def test_columns_extracted(self):
        parsed = parser.parse("SELECT co.name, s.amount FROM company co JOIN sale_records s ON co.id = s.id")
        assert "co.name" in parsed.columns
        assert "s.amount" in parsed.columns

    def test_functions_extracted(self):
        parsed = parser.parse("SELECT COUNT(*), ROUND(x, 2) FROM t")
        assert "COUNT" in parsed.functions
        assert "ROUND" in parsed.functions

    def test_anonymous_function_name(self):
        # LOAD_FILE / SLEEP 在 sqlglot 里是 Anonymous，取真实函数名
        assert "LOAD_FILE" in parser.extract_functions("SELECT LOAD_FILE('/etc/passwd')")
        assert "SLEEP" in parser.extract_functions("SELECT SLEEP(5)")

    def test_join_count(self):
        sql = "SELECT 1 FROM a JOIN b ON a.x=b.x JOIN c ON b.y=c.y"
        assert parser.count_joins(sql) == 2

    def test_subquery_depth(self):
        assert parser.subquery_depth("SELECT * FROM t") == 0
        assert parser.subquery_depth("SELECT * FROM (SELECT 1 FROM t) a") == 1
        assert parser.subquery_depth("SELECT * FROM (SELECT * FROM (SELECT 1 FROM t) b) a") == 2

    def test_limit_and_star_flags(self):
        parsed = parser.parse("SELECT * FROM company LIMIT 10")
        assert parsed.has_limit is True
        assert parsed.has_star is True
        assert parser.parse("SELECT name FROM company").has_limit is False

    def test_cte_alias_excluded_but_inner_table_kept(self):
        # CTE 名 x 不算真实表；其定义体内引用的 real_t 必须被抽到
        parsed = parser.parse("WITH x AS (SELECT 1 FROM real_t) SELECT * FROM x")
        assert "x" not in parsed.tables
        assert "real_t" in parsed.tables


class TestBypassesClosed:
    """旧实现（sqlparse）会漏判、AST 已修复的绕过面。"""

    def test_table_inside_subquery_is_visible(self):
        # 旧 sqlparse 遇到子查询直接跳过返回 []，导致表级 allowlist 会误放行
        assert parser.extract_tables("SELECT * FROM (SELECT * FROM user) t") == ["user"]

    def test_table_inside_cte_body_is_visible(self):
        assert parser.extract_tables("WITH x AS (SELECT * FROM mysql.user) SELECT * FROM x") == ["mysql.user"]

    def test_qualified_schema_survives_subquery(self):
        sql = "SELECT * FROM (SELECT * FROM information_schema.tables) t"
        assert "information_schema.tables" in parser.extract_tables(sql)


class TestColumnResolution:
    """列归属真实表的解析（供列级 ACL 使用）。"""

    def test_bare_column_single_table(self):
        assert parser.extract_column_refs("SELECT name FROM company") == [("company", "name")]

    def test_aliased_column_resolved_to_table(self):
        assert parser.extract_column_refs("SELECT c.employees FROM company c") == [("company", "employees")]

    def test_qualified_by_table_name(self):
        assert parser.extract_column_refs("SELECT company.name FROM company") == [("company", "name")]

    def test_join_resolves_each_table(self):
        refs = parser.extract_column_refs(
            "SELECT c.name, s.amount FROM company c JOIN sale_records s ON c.company_id = s.company_id"
        )
        assert ("company", "name") in refs
        assert ("sale_records", "amount") in refs
        assert ("sale_records", "company_id") in refs

    def test_star_produces_no_refs(self):
        assert parser.extract_column_refs("SELECT * FROM company") == []

