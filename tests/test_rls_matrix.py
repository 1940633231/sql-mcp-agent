"""RLS 与 JOIN、CTE、子查询和聚合的组合测试。"""
from mcp_server.authorization.policy import load_permission_policy
from mcp_server.authorization.service import AuthorizationService
from mcp_server.auth.models import Principal, RequestContext


class FakeCatalog:
    def get_schema(self, table_name):
        columns = {
            "company": ["company_id", "name", "industry", "headquarters", "founded_year", "employees"],
            "sale_records": ["id", "company_id", "amount", "record_date"],
        }
        return [{"Field": column} for column in columns.get(table_name, [])]


def _alice():
    policy = load_permission_policy()
    raw = policy.principals["alice"]
    context = RequestContext(Principal(raw.subject, raw.roles, raw.attributes))
    return AuthorizationService(policy), context


def test_rls_join_filters_each_source():
    authz, context = _alice()
    result = authz.authorize_sql(
        "SELECT c.name, s.amount FROM company c "
        "JOIN sale_records s ON c.company_id = s.company_id",
        context,
        FakeCatalog(),
    )
    assert result.allowed
    assert "FROM company AS c WHERE c.industry" in result.sql
    assert "FROM sale_records AS s WHERE s.company_id IN (9001, 9002)" in result.sql


def test_rls_left_join_preserves_right_source_filter():
    authz, context = _alice()
    result = authz.authorize_sql(
        "SELECT c.name, s.amount FROM company c "
        "LEFT JOIN sale_records s ON c.company_id = s.company_id",
        context,
        FakeCatalog(),
    )
    assert result.allowed
    assert "LEFT JOIN (SELECT * FROM sale_records AS s WHERE s.company_id IN (9001, 9002)) AS s" in result.sql
    assert "WHERE s.company_id" not in result.sql.split("LEFT JOIN")[-1].split("ON")[-1]


def test_rls_cte_filters_inside_definition():
    authz, context = _alice()
    result = authz.authorize_sql(
        "WITH scoped AS (SELECT * FROM company) SELECT * FROM scoped",
        context,
        FakeCatalog(),
    )
    assert result.allowed
    assert "FROM (SELECT * FROM company WHERE company.industry" in result.sql


def test_rls_subquery_filters_inner_select():
    authz, context = _alice()
    result = authz.authorize_sql(
        "SELECT x.name FROM (SELECT * FROM company) x",
        context,
        FakeCatalog(),
    )
    assert result.allowed
    assert "FROM (SELECT * FROM company WHERE company.industry" in result.sql


def test_rls_union_filters_each_branch():
    authz, context = _alice()
    result = authz.authorize_sql(
        "SELECT name FROM company UNION ALL SELECT name FROM company",
        context,
        FakeCatalog(),
    )
    assert result.allowed
    assert result.sql.count("company.industry") == 2


def test_rls_aggregation_filters_before_aggregate():
    authz, context = _alice()
    result = authz.authorize_sql(
        "SELECT SUM(amount) FROM sale_records",
        context,
        FakeCatalog(),
    )
    assert result.allowed
    assert "SELECT SUM(amount) FROM (SELECT * FROM sale_records WHERE sale_records.company_id IN (9001, 9002)) AS sale_records" in result.sql


def test_rls_window_function_filters_before_window():
    authz, context = _alice()
    result = authz.authorize_sql(
        "SELECT ROW_NUMBER() OVER (ORDER BY amount) AS rn FROM sale_records",
        context,
        FakeCatalog(),
    )
    assert result.allowed
    assert "FROM (SELECT * FROM sale_records WHERE sale_records.company_id IN (9001, 9002)) AS sale_records" in result.sql
