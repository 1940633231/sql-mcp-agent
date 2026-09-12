"""tests/test_domain_query.py — V0.5 领域查询层测试。

覆盖验收标准：
  - 领域工具不直接访问数据库（只经 QueryService，可离线断言其安全链路）
  - 各主体下 ACL / RLS / 审计与 run_query 一致（RLS 改写离线可断言 + DB 集成验证）
  - 参数 / 排序字段 / 分组字段 / limit 不能绕过白名单
  - 每个工具：正常 / 越权 / 超时 / 截断 / 非法参数测试
  - 最小 CI（离线用例）覆盖领域工具安全契约

单元用例无需数据库；DB 集成用例默认跳过，需 RUN_DB_TESTS=1 且 MySQL 可用。
"""
import os
import shutil
from pathlib import Path

import pymysql
import pytest

from mcp_server.auth.models import Principal, RequestContext
from mcp_server.authorization.policy import load_permission_policy
from mcp_server.authorization.service import AuthorizationService
from mcp_server.database.executor import _bind_params
from mcp_server.domain.binder import InvalidParameter, render
from mcp_server.domain.registry import DomainRegistry
from mcp_server.domain.service import DomainQueryService
from mcp_server.security.models import QueryResult
from mcp_server.security.validator import SqlValidator
from mcp_server.services.query_service import QueryService

ROOT = Path(__file__).resolve().parents[1]
DOMAIN_YAML = ROOT / "configs" / "domain_queries.yaml"

SALES_ARGS = {"start_date": "2026-01-01", "end_date": "2026-12-31"}
RANK_ARGS = {"start_date": "2026-01-01", "end_date": "2026-12-31"}


class FakeCatalog:
    """离线列血缘解析用的假 Catalog（避免域工具测试触碰数据库）。"""

    COLUMNS = {
        "company": ["company_id", "name", "industry", "headquarters", "founded_year", "employees"],
        "sale_records": ["id", "company_id", "amount", "record_date"],
    }

    def get_schema(self, table_name):
        return [{"Field": column} for column in self.COLUMNS[table_name]]


def _context(subject: str) -> RequestContext:
    policy = load_permission_policy()
    raw = policy.principals[subject]
    return RequestContext(
        principal=Principal(raw.subject, raw.roles, raw.attributes),
        source="test",
        auth_method="test",
    )


def _offline_service(captured: dict, tool: str = "sales_summary") -> DomainQueryService:
    """真实 validator + authorizer + 假 catalog + 假 executor 的领域服务。

    假 executor 返回契约声明的列（结果契约校验要求实际列与声明一致）。
    """
    policy = load_permission_policy()
    contract = list(DomainRegistry.shared().get(tool).template.result_columns)

    def fake_run(sql, max_rows, timeout_seconds, max_result_bytes=None, params=None):
        captured["sql"] = sql
        captured["params"] = params
        captured["max_rows"] = max_rows
        return QueryResult(
            columns=contract, rows=[], row_count=0, elapsed_seconds=0.01, truncated=False
        )

    qs = QueryService(
        validator=SqlValidator(),
        authorizer=AuthorizationService(policy),
        catalog=FakeCatalog(),
    )
    qs.executor.run = fake_run
    return DomainQueryService(query_service=qs)


def _dto_service(
    tool: str = "sales_summary",
    rows=None,
    columns: list[str] | None = None,
    truncated: bool = False,
    truncated_reason: str = "",
):
    policy = load_permission_policy()
    contract = list(DomainRegistry.shared().get(tool).template.result_columns)

    def fake_run(sql, max_rows, timeout_seconds, max_result_bytes=None, params=None):
        return QueryResult(
            columns=columns if columns is not None else contract,
            rows=rows or [],
            row_count=len(rows or []),
            elapsed_seconds=0.01,
            truncated=truncated,
            truncated_reason=truncated_reason,
        )

    qs = QueryService(
        validator=SqlValidator(),
        authorizer=AuthorizationService(policy),
        catalog=FakeCatalog(),
    )
    qs.executor.run = fake_run
    return DomainQueryService(query_service=qs)


@pytest.fixture
def registry(tmp_path) -> DomainRegistry:
    target = tmp_path / "domain_queries.yaml"
    shutil.copyfile(DOMAIN_YAML, target)
    return DomainRegistry(path=target, reload_seconds=0)


# ================= 注册表 / 定义契约 =================

class TestRegistry:
    def test_loads_three_tools_with_contracts(self, registry):
        assert registry.list() == ["company_ranking", "industry_analysis", "sales_summary"]
        for name in registry.list():
            spec = registry.get(name)
            assert spec.definition_version.startswith("domain-")
            assert spec.template.result_columns
            assert spec.template.semantics, "领域口径必须显式定义"
            assert spec.template.sql

    def test_semantics_are_explicit(self, registry):
        sales = registry.get("sales_summary").template.semantics
        assert "timezone" in sales and "refunds_included" in sales and "sales_caliber" in sales
        rank = registry.get("company_ranking").template.semantics
        assert "tie_breaking" in rank and "max_limit" in rank and "time_window" in rank
        industry = registry.get("industry_analysis").template.semantics
        assert "unknown_industry" in industry and "denominator" in industry and "coverage" in industry

    def test_unknown_tool_raises(self, registry):
        with pytest.raises(KeyError):
            registry.get("not_a_tool")

    def test_reload_picks_up_new_definition(self, registry, tmp_path):
        old = registry.definition_version()
        target = tmp_path / "domain_queries.yaml"
        content = target.read_text(encoding="utf-8")
        target.write_text(
            content.replace("version: 1", "version: 2"), encoding="utf-8"
        )
        registry.reload()
        assert registry.definition_version() != old
        assert registry.definition_version().startswith("domain-v2")


# ================= 参数绑定 / 白名单 =================

class TestBinder:
    def test_sales_summary_renders_placeholders_and_allowlisted_identifiers(self, registry):
        spec = registry.get("sales_summary")
        sql, params, effective = render(spec.template, {"start_date": "2026-01-01", "end_date": "2026-12-31"})
        assert "DATE_FORMAT(record_date, '%Y-%m')" in sql        # 默认 month 内联
        assert "company_id = COALESCE(?, company_id)" in sql
        assert sql.count("?") == 4
        assert params == ("2026-01-01", "2026-12-31", None, 200)
        assert effective["granularity"] == "month" and effective["limit"] == 200

    def test_company_ranking_sorts_by_allowlisted_fragment(self, registry):
        spec = registry.get("company_ranking")
        sql, params, _ = render(spec.template, dict(RANK_ARGS, sort_by="order_count", order_dir="asc"))
        assert "ORDER BY COUNT(*) ASC, c.company_id ASC" in sql
        assert sql.count("?") == 4
        assert params == ("2026-01-01", "2026-12-31", None, 10)

    def test_industry_filter_keeps_denominator_shape(self, registry):
        """行业过滤不改变占比分母：分母由独立派生表计算，与过滤无关。"""
        spec = registry.get("industry_analysis")
        sql, params, _ = render(spec.template, dict(RANK_ARGS, industry="电子"))
        assert "SUM(r.amount) AS total" in sql                # 独立分母（派生表）
        assert "AND c.industry = COALESCE(?, c.industry)" in sql   # 过滤仍在 WHERE
        assert sql.count("?") == 6                            # 派生表 2 + 外层 3 + LIMIT 1
        assert params == (
            "2026-01-01", "2026-12-31", "2026-01-01", "2026-12-31", "电子", 50,
        )

    def test_value_text_never_enters_sql(self, registry):
        spec = registry.get("sales_summary")
        sql, params, _ = render(
            spec.template,
            {"start_date": "2026-06-15", "end_date": "2026-06-30", "company_id": 42},
        )
        assert "2026-06-15" not in sql
        assert "42" not in sql
        assert params == ("2026-06-15", "2026-06-30", 42, 200)

    @pytest.mark.parametrize(
        "tool,args",
        [
            ("sales_summary", dict(SALES_ARGS, granularity="hourly")),
            ("sales_summary", dict(SALES_ARGS, limit=0)),
            ("sales_summary", dict(SALES_ARGS, limit=201)),
            ("sales_summary", dict(SALES_ARGS, limit="abc")),
            ("sales_summary", dict(SALES_ARGS, company_id=-1)),
            ("sales_summary", {"end_date": "2026-12-31"}),              # 缺必填 start_date
            ("sales_summary", dict(SALES_ARGS, start_date="2026-13-01")),  # 非法日期
            ("sales_summary", dict(SALES_ARGS, start_date="2026-01-01' OR '1'='1")),
            ("sales_summary", dict(SALES_ARGS, start_date="2026-02-01", end_date="2026-01-01")),
            ("sales_summary", dict(SALES_ARGS, unknown_key=1)),         # 未知参数
            ("company_ranking", dict(RANK_ARGS, sort_by="employees")),  # 排序字段逃逸白名单
            ("company_ranking", dict(RANK_ARGS, sort_by="name")),
            ("company_ranking", dict(RANK_ARGS, order_dir="random")),
            ("company_ranking", dict(RANK_ARGS, industry="不存在行业")),
            ("company_ranking", dict(RANK_ARGS, limit=51)),
            ("industry_analysis", dict(RANK_ARGS, industry="其他")),
        ],
    )
    def test_invalid_parameters_rejected(self, registry, tool, args):
        spec = registry.get(tool)
        with pytest.raises(InvalidParameter):
            render(spec.template, args)

    def test_limit_hard_cap(self, registry):
        rank = registry.get("company_ranking")
        _, _, effective = render(rank.template, dict(RANK_ARGS, limit=50))
        assert effective["limit"] == 50  # 白名单上限恰好可用


# ================= 驱动绑定（Executor 层） =================

class TestBindParams:
    def test_question_mark_to_percent_s(self):
        sql, params = _bind_params("SELECT * FROM t WHERE a = ? AND b = ?", (1, "x"))
        assert sql == "SELECT * FROM t WHERE a = %s AND b = %s"
        assert params == (1, "x")

    def test_question_inside_string_literal_not_converted(self):
        sql, _ = _bind_params("SELECT 'a?b' AS x, v FROM t WHERE v = ?", (1,))
        assert sql == "SELECT 'a?b' AS x, v FROM t WHERE v = %s"

    def test_percent_escaped_inside_and_outside_strings(self):
        sql, _ = _bind_params(
            "SELECT DATE_FORMAT(d, '%Y-%m') AS p FROM t WHERE a = ?", (1,)
        )
        assert sql == "SELECT DATE_FORMAT(d, '%%Y-%%m') AS p FROM t WHERE a = %s"

    def test_placeholder_count_mismatch_rejected(self):
        with pytest.raises(ValueError):
            _bind_params("SELECT * FROM t WHERE a = ? AND b = ?", (1,))

    def test_stray_percent_s_rejected(self):
        with pytest.raises(ValueError):
            _bind_params("SELECT '%s' AS x FROM t WHERE a = ?", (1,))


# ================= 安全链路（离线：RLS / ACL / RBAC） =================

class TestSecurityPipeline:
    def test_alice_company_ranking_rls_applied(self):
        captured = {}
        svc = _offline_service(captured, "company_ranking")
        result = svc.run("company_ranking", dict(RANK_ARGS, limit=10), context=_context("alice"))
        assert "error" not in result, result
        assert "'电子'" in captured["sql"]              # company RLS
        assert "9001" in captured["sql"] and "9002" in captured["sql"]  # sale_records RLS
        assert captured["sql"].count("?") == 4          # 占位符与参数一一对应
        assert captured["params"] == ("2026-01-01", "2026-12-31", None, 10)

    def test_alice_sales_summary_rls_applied(self):
        captured = {}
        svc = _offline_service(captured, "sales_summary")
        result = svc.run("sales_summary", SALES_ARGS, context=_context("alice"))
        assert "error" not in result, result
        assert "9001" in captured["sql"] and "9002" in captured["sql"]
        assert "'电子'" not in captured["sql"]           # 单表无 company 连接
        assert captured["sql"].count("?") == 4

    def test_bob_rls_uses_his_industry(self):
        captured = {}
        svc = _offline_service(captured, "company_ranking")
        result = svc.run("company_ranking", RANK_ARGS, context=_context("bob"))
        assert "error" not in result, result
        assert "'医药'" in captured["sql"]
        assert "9003" in captured["sql"] and "9004" in captured["sql"]

    def test_admin_bypasses_rls(self):
        captured = {}
        svc = _offline_service(captured, "company_ranking")
        result = svc.run("company_ranking", RANK_ARGS, context=_context("query-admin"))
        assert "error" not in result, result
        assert "'电子'" not in captured["sql"]
        assert "9001" not in captured["sql"]

    def test_viewer_denied_same_as_run_query(self):
        captured = {}
        svc = _offline_service(captured, "sales_summary")
        result = svc.run("sales_summary", SALES_ARGS, context=_context("auditor"))
        assert result["code"] == "permission_denied"
        assert captured == {}                            # 未触达执行

    def test_rendered_sql_passes_ast_guard_for_every_tool(self, registry):
        validator = SqlValidator()
        for name, args in [
            ("sales_summary", SALES_ARGS),
            ("company_ranking", RANK_ARGS),
            ("industry_analysis", RANK_ARGS),
        ]:
            sql, _, _ = render(registry.get(name).template, args)
            validated = validator.validate(sql)
            assert validated.allowed, "%s: %s" % (name, validated.reason)


# ================= DTO / 截断 / 超时 / 错误收敛 =================

class TestDto:
    def test_success_dto_shape(self):
        svc = _dto_service(
            rows=[{"period": "2026-01", "order_count": 2, "total_amount": 3200, "avg_amount": 1600}]
        )
        result = svc.run("sales_summary", SALES_ARGS, context=_context("query-admin"))
        assert set(result) == {
            "data", "columns", "meta", "definition_version", "policy_version", "truncated",
        }
        assert result["data"] == [
            {"period": "2026-01", "order_count": 2, "total_amount": 3200, "avg_amount": 1600}
        ]
        assert result["columns"] == ["period", "order_count", "total_amount", "avg_amount"]
        assert result["truncated"] is False
        assert result["definition_version"].startswith("domain-")
        assert result["policy_version"]
        assert result["meta"]["tool"] == "sales_summary"
        assert result["meta"]["semantics"]["timezone"]
        assert result["meta"]["parameters"]["start_date"] == "2026-01-01"

    def test_truncation_propagates_to_dto(self):
        svc = _dto_service(
            rows=[{"period": "2026-01", "order_count": 2, "total_amount": 3200, "avg_amount": 1600}],
            truncated=True,
            truncated_reason="rows",
        )
        result = svc.run("sales_summary", SALES_ARGS, context=_context("query-admin"))
        assert result["truncated"] is True
        assert result["meta"]["truncated_reason"] == "rows"

    def test_result_contract_mismatch_rejected(self):
        """结果契约真正执行：实际返回列与声明不一致时 fail loud，不静默返回。

        用契约列的子集构造：能通过 ACL 结果列守卫，但不符合完整契约。
        """
        svc = _dto_service(
            rows=[{"period": "2026-01", "order_count": 2, "total_amount": 3200}],
            columns=["period", "order_count", "total_amount"],   # 缺 avg_amount
        )
        result = svc.run("sales_summary", SALES_ARGS, context=_context("query-admin"))
        assert result["code"] == "result_contract_mismatch"
        assert "data" not in result
        assert "avg_amount" in result["error"]

    def test_timeout_error_converged_to_error_dict(self):
        policy = load_permission_policy()

        def timeout_run(sql, max_rows, timeout_seconds, max_result_bytes=None, params=None):
            raise pymysql.err.OperationalError(3024, "Statement exceeded execution time")

        qs = QueryService(
            validator=SqlValidator(),
            authorizer=AuthorizationService(policy),
            catalog=FakeCatalog(),
        )
        qs.executor.run = timeout_run
        svc = DomainQueryService(query_service=qs)
        result = svc.run("sales_summary", SALES_ARGS, context=_context("query-admin"))
        assert "error" in result
        assert "data" not in result                     # 错误不套 DTO

    def test_unknown_tool_error(self):
        svc = _dto_service()
        result = svc.run("not_a_tool", {}, context=_context("query-admin"))
        assert result["code"] == "unknown_domain_tool"

    def test_invalid_parameter_error_dict(self):
        svc = _dto_service()
        result = svc.run("sales_summary", {"start_date": "bad"}, context=_context("query-admin"))
        assert result["code"] == "invalid_parameter"


# ================= 工具层（薄转换） =================

class TestToolLayer:
    def test_tool_delegates_with_compacted_args(self, monkeypatch):
        from mcp_server.tools import domain as domain_tools

        captured = {}

        def fake_run(tool_name, args, context=None):
            captured["tool"] = tool_name
            captured["args"] = args
            return {"data": []}

        monkeypatch.setattr(domain_tools._service, "run", fake_run)
        domain_tools.sales_summary(start_date="2026-01-01", end_date="2026-12-31")
        assert captured["tool"] == "sales_summary"
        assert captured["args"] == {"start_date": "2026-01-01", "end_date": "2026-12-31"}

    def test_required_dates_are_required_in_tool_schema(self):
        """MCP inputSchema 由函数签名生成：必填日期不得带默认值，否则被标成可选。"""
        import inspect

        from mcp_server.tools import domain as domain_tools

        for fn in (
            domain_tools.sales_summary,
            domain_tools.company_ranking,
            domain_tools.industry_analysis,
        ):
            sig = inspect.signature(fn)
            assert sig.parameters["start_date"].default is inspect.Parameter.empty, fn.__name__
            assert sig.parameters["end_date"].default is inspect.Parameter.empty, fn.__name__
        # 可选参数仍保持可选（有默认值）
        assert (
            inspect.signature(domain_tools.sales_summary).parameters["company_id"].default
            is None
        )


# ================= 真实数据库集成 =================

@pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="需设置 RUN_DB_TESTS=1 且 MySQL 可用")
class TestDomainQueryRealDatabase:
    def test_sales_summary_admin(self):
        svc = DomainQueryService()
        result = svc.run(
            "sales_summary",
            {"start_date": "2026-01-01", "end_date": "2026-12-31"},
            context=_context("query-admin"),
        )
        assert "error" not in result, result
        assert result["columns"] == ["period", "order_count", "total_amount", "avg_amount"]
        assert result["data"]
        assert all(len(row["period"]) == 7 for row in result["data"])   # YYYY-MM

    def test_company_ranking_admin_ordered_and_limited(self):
        svc = DomainQueryService()
        result = svc.run(
            "company_ranking",
            {"start_date": "2026-01-01", "end_date": "2026-12-31", "limit": 5},
            context=_context("query-admin"),
        )
        assert "error" not in result, result
        assert len(result["data"]) == 5
        totals = [float(row["total_amount"]) for row in result["data"]]
        assert totals == sorted(totals, reverse=True)

    def test_industry_analysis_share_sums_to_100(self):
        svc = DomainQueryService()
        result = svc.run(
            "industry_analysis",
            {"start_date": "2026-01-01", "end_date": "2026-12-31"},
            context=_context("query-admin"),
        )
        assert "error" not in result, result
        assert result["columns"] == [
            "industry", "company_count", "order_count", "total_amount", "share_percent",
        ]
        assert result["data"]
        total = sum(float(row["share_percent"]) for row in result["data"])
        assert abs(total - 100) < 1.0

    def test_industry_filter_keeps_denominator(self):
        """行业过滤后占比不应变成 ~100%：分母必须仍是窗口内全部行业。"""
        svc = DomainQueryService()
        args = {"start_date": "2026-01-01", "end_date": "2026-12-31"}
        full = svc.run("industry_analysis", args, context=_context("query-admin"))
        filtered = svc.run(
            "industry_analysis", dict(args, industry="电子"), context=_context("query-admin")
        )
        assert "error" not in full and "error" not in filtered, (full, filtered)
        assert [row["industry"] for row in filtered["data"]] == ["电子"]
        full_electron = next(row for row in full["data"] if row["industry"] == "电子")
        share = float(filtered["data"][0]["share_percent"])
        assert share == float(full_electron["share_percent"])   # 分母不受过滤影响
        assert 0 < share < 100                                  # 电子不是窗口内全部行业

    def test_alice_rls_consistent_with_run_query(self):
        svc = DomainQueryService()
        rank = svc.run(
            "company_ranking",
            {"start_date": "2026-01-01", "end_date": "2026-12-31", "limit": 50},
            context=_context("alice"),
        )
        assert "error" not in rank, rank
        assert rank["data"] and all(row["industry"] == "电子" for row in rank["data"])

        # 与 run_query 同一安全链路：alice 可见行业仅电子
        direct = QueryService(
            validator=SqlValidator(),
            authorizer=AuthorizationService(load_permission_policy()),
        ).run("SELECT DISTINCT industry FROM company", _context("alice"))
        assert "error" not in direct, direct
        assert {row["industry"] for row in direct["rows"]} == {"电子"}

    def test_alice_sales_summary_scoped_to_fixture_companies(self):
        svc = DomainQueryService()
        result = svc.run(
            "sales_summary",
            {"start_date": "2026-01-01", "end_date": "2026-03-31", "granularity": "month"},
            context=_context("alice"),
        )
        assert "error" not in result, result
        # 权限夹具：alice 可见 9001/9002，2026-01 各 1 笔（1100 + 2100）
        jan = result["data"][0]
        assert float(jan["total_amount"]) == 3200.0
        assert jan["order_count"] == 2

    def test_company_id_binding_filters(self):
        svc = DomainQueryService()
        result = svc.run(
            "sales_summary",
            {"start_date": "2026-01-01", "end_date": "2026-01-31", "company_id": 9001},
            context=_context("query-admin"),
        )
        assert "error" not in result, result
        assert float(result["data"][0]["total_amount"]) == 1100.0
        assert result["data"][0]["order_count"] == 1

    def test_unknown_industry_rejected(self):
        svc = DomainQueryService()
        result = svc.run(
            "company_ranking",
            dict(RANK_ARGS, industry="不存在行业"),
            context=_context("query-admin"),
        )
        assert result["code"] == "invalid_parameter"

    def test_limit_above_cap_rejected(self):
        svc = DomainQueryService()
        result = svc.run(
            "company_ranking",
            dict(RANK_ARGS, limit=51),
            context=_context("query-admin"),
        )
        assert result["code"] == "invalid_parameter"

    def test_viewer_cannot_use_domain_tools(self):
        svc = DomainQueryService()
        result = svc.run("sales_summary", SALES_ARGS, context=_context("auditor"))
        assert result["code"] == "permission_denied"
