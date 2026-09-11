"""tests/test_sql_validator.py — 校验流水线单元测试（不依赖数据库）。"""
import pytest

from mcp_server.security.models import ValidationContext
from mcp_server.security.validator import SqlValidator


@pytest.fixture
def validator():
    return SqlValidator()


class TestAccepts:
    @pytest.mark.parametrize("sql", [
        "SELECT * FROM company",
        "select count(*) from company;",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        (
            "SELECT co.name FROM company co "
            "JOIN sale_records s ON co.company_id = s.company_id "
            "WHERE YEAR(s.record_date) = 2026 ORDER BY co.name LIMIT 10"
        ),
    ])
    def test_readonly_accepted(self, validator, sql):
        result = validator.validate(sql)
        assert result.allowed, result.reason
        # 放行的 SQL 已去掉结尾分号，便于后续追加 LIMIT
        assert not result.normalized_sql.endswith(";")
        assert result.reason is None
        assert result.statement_type == "SELECT"
        assert result.warnings == []


class TestRejects:
    @pytest.mark.parametrize("sql,code", [
        ("", "empty"),
        ("   ", "empty"),
        ("DELETE FROM company", "not_readonly"),
        ("UPDATE company SET name='x'", "not_readonly"),
        ("INSERT INTO company VALUES (1)", "not_readonly"),
        ("SELECT * FROM company; DROP TABLE sale_records;", "multi_statement"),
        ("SELECT LOAD_FILE('/etc/passwd')", "forbidden_keyword"),
        ("SELECT SLEEP(5)", "forbidden_keyword"),
        ("SELECT @@version", "forbidden_pattern"),
        ("SELECT * FROM a UNION SELECT * FROM b", "forbidden_pattern"),
        ("SELECT * FROM information_schema.tables", "blocked_schema"),
        ("SELECT * FROM mysql.user", "blocked_schema"),
        ("SELECT * FROM com-pany", "parse_error"),
        ("SELECT * FROM `bad-name`", "table_not_allowed"),
    ])
    def test_rejected_with_code(self, validator, sql, code):
        result = validator.validate(sql)
        assert not result.allowed
        assert result.code == code
        assert result.reason


def test_rejection_does_not_leak_sql(validator):
    """被拒绝时不应把未校验的 SQL 当作可执行语句返回。"""
    result = validator.validate("DELETE FROM company")
    assert result.normalized_sql == ""


class TestContract:
    """V0.2 接口契约：字段齐备、context 可选、结构化信息随结论带出。"""

    REQUIRED_FIELDS = (
        "allowed", "reason", "normalized_sql",
        "statement_type", "tables", "columns", "warnings",
    )

    def test_result_has_all_contract_fields(self, validator):
        result = validator.validate("SELECT * FROM company")
        for field in self.REQUIRED_FIELDS:
            assert hasattr(result, field), "缺少契约字段 %s" % field

    def test_context_is_optional(self, validator):
        without = validator.validate("SELECT 1")
        with_ctx = validator.validate("SELECT 1", ValidationContext(source="test"))
        assert without.allowed == with_ctx.allowed

    def test_approve_carries_structured_info(self, validator):
        result = validator.validate(
            "SELECT co.name FROM company co JOIN sale_records s ON co.company_id = s.id"
        )
        assert result.allowed
        assert result.statement_type == "SELECT"
        assert result.tables == ["company", "sale_records"]

    def test_reject_carries_statement_type(self, validator):
        result = validator.validate("DELETE FROM company")
        assert not result.allowed
        assert result.statement_type == "DELETE"


class TestTableAllowlist:
    """表级 Allowlist（security.yaml 已配置 tables.allowed/denied）。"""

    @pytest.fixture
    def validator(self):
        return SqlValidator()

    def test_allowed_table_ok(self, validator):
        assert validator.validate("SELECT * FROM company").allowed
        assert validator.validate("SELECT * FROM sale_records").allowed

    def test_denied_table_rejected(self, validator):
        result = validator.validate("SELECT * FROM user")
        assert result.code == "table_denied"

    def test_unallowed_table_rejected(self, validator):
        # orders 未在 allowed、也未在 denied —— 走到 allowed 兜底，拒绝
        result = validator.validate("SELECT * FROM orders")
        assert result.code == "table_not_allowed"

    def test_denied_table_hidden_in_subquery_rejected(self, validator):
        # Allowlist 比对的是 AST 真实表引用，子查询里藏的 user 也会被揪出
        result = validator.validate("SELECT * FROM (SELECT * FROM user) t")
        assert result.code == "table_denied"

    def test_denied_table_hidden_in_cte_rejected(self, validator):
        result = validator.validate("WITH x AS (SELECT * FROM employee) SELECT * FROM x")
        assert result.code == "table_denied"

    def test_allowed_via_qualified_name(self, validator):
        # sales_demo.company 归一化为裸表名 company，应放行
        result = validator.validate("SELECT * FROM sales_demo.company")
        assert result.allowed

    def test_denied_precedence_over_allowed(self, validator):
        # denied 判定先于 allowed 兜底：命中的优先给出精确的 table_denied 分类
        result = validator.validate("SELECT * FROM employee")
        assert result.code == "table_denied"


class TestColumnAllowlist:
    """列级 ACL（security.yaml 已配置 company 的 allowed/denied 列）。"""

    @pytest.fixture
    def validator(self):
        return SqlValidator()

    def test_denied_column_rejected(self, validator):
        result = validator.validate("SELECT employees FROM company")
        assert not result.allowed
        assert result.code == "column_not_allowed"

    def test_allowed_column_ok(self, validator):
        assert validator.validate("SELECT name FROM company").allowed

    def test_mixed_allowed_and_denied_rejected(self, validator):
        result = validator.validate("SELECT name, employees FROM company")
        assert result.code == "column_not_allowed"

    def test_qualified_denied_column_rejected(self, validator):
        # 用别名限定仍能归属到 company.employees
        result = validator.validate("SELECT c.employees FROM company c")
        assert result.code == "column_not_allowed"

    def test_listed_non_denied_column_ok(self, validator):
        # headquarters 在 allowed 内、未在 denied，放行
        assert validator.validate("SELECT headquarters FROM company").allowed

    def test_star_bypasses_column_acl(self, validator):
        # SELECT * 无列引用，ACL 不覆盖（已知边界，放行）
        assert validator.validate("SELECT * FROM company").allowed

    def test_table_without_acl_rule_unaffected(self, validator):
        # sale_records 未配 ACL，任意列均放行
        assert validator.validate("SELECT amount FROM sale_records").allowed


class TestColumnAllowlistPrecedence:
    """列 ACL 与表 allowlist 的先后：表级先拦（denied/not_allowed），列级后拦。"""

    @pytest.fixture
    def validator(self):
        return SqlValidator()

    def test_table_denied_beats_column_acl(self, validator):
        # employee 表级 denied 先拦，即便它也可能有列 ACL
        result = validator.validate("SELECT * FROM employee")
        assert result.code == "table_denied"


class TestJoinControl:
    """JOIN 数量上限（security.yaml max_joins: 3）。"""

    def _joins_sql(self, n: int) -> str:
        joins = "".join(
            " JOIN company c%d ON company.company_id = c%d.company_id" % (i, i)
            for i in range(1, n + 1)
        )
        return "SELECT COUNT(*) FROM company" + joins

    def test_rejects_exceeding_join_limit(self):
        v = SqlValidator()
        result = v.validate(self._joins_sql(4))
        assert not result.allowed
        assert result.code == "too_many_joins"

    def test_accepts_within_join_limit(self):
        v = SqlValidator()
        assert v.validate(self._joins_sql(3)).allowed

    def test_accepts_no_join(self):
        assert SqlValidator().validate("SELECT * FROM company").allowed

    def test_subquery_joins_also_counted(self):
        # find_all 遍历整棵树，子查询内的 JOIN 也会被计入
        v = SqlValidator()
        deep = ("SELECT * FROM (SELECT COUNT(*) FROM company"
                " JOIN sale_records s ON company.company_id=s.company_id"
                " JOIN company c2 ON company.company_id=c2.company_id"
                " JOIN company c3 ON company.company_id=c3.company_id"
                " JOIN company c4 ON company.company_id=c4.company_id) t")
        assert v.validate(deep).code == "too_many_joins"


class TestSqlLengthLimit:
    """单条 SQL 长度上限（security.yaml max_sql_length: 10000）。"""

    def test_short_sql_ok(self):
        assert SqlValidator().validate("SELECT * FROM company").allowed

    def test_overlong_sql_rejected(self):
        v = SqlValidator()
        sql = "SELECT * FROM company WHERE name = '%s'" % ("x" * v.policy.max_sql_length)
        result = v.validate(sql)
        assert not result.allowed
        assert result.code == "sql_too_long"
