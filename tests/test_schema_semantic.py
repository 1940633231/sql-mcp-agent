"""Schema Intelligence：语义加载 / 轻量检索 / 模型融合 / search_schema ACL 测试。"""
import contextlib

import yaml

from mcp_server.authorization.manager import PolicyContext
from mcp_server.authorization.service import AuthorizationService
from mcp_server.catalog.models import ColumnSchema, TableSchema
from mcp_server.catalog.semantic import (
    ColumnDesc,
    SemanticHit,
    SemanticMetadata,
    TableDesc,
    load_semantics,
    search_schema,
)
from mcp_server.catalog.service import SchemaCatalog
from mcp_server.auth.models import Principal, RequestContext
from mcp_server.authorization.policy import load_permission_policy


# ---------- 加载器 ----------


class TestLoadSemantics:
    def test_missing_file_returns_empty(self, tmp_path):
        meta = load_semantics(tmp_path / "nope.yaml")
        assert meta.tables == {}
        assert meta.columns == {}

    def test_load_normalizes_and_parses(self, tmp_path):
        path = tmp_path / "schema_desc.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "tables": {
                        "sale_records": {"label": "销售记录", "description": "销售流水"},
                    },
                    "columns": {
                        "sale_records": {
                            "amount": {
                                "label": "销售金额",
                                "description": "单笔销售额",
                                "synonyms": ["销售额", "金额"],
                                "enum_values": [{"value": 1, "label": "x"}],
                            }
                        }
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        meta = load_semantics(path)
        assert set(meta.tables) == {"sale_records"}
        assert meta.columns["sale_records"]["amount"].label == "销售金额"
        assert meta.columns["sale_records"]["amount"].synonyms == ("销售额", "金额")
        assert meta.columns["sale_records"]["amount"].enum_values == ({"value": 1, "label": "x"},)


# ---------- 检索 ----------

_META = SemanticMetadata(
    tables={"sale_records": TableDesc(label="销售记录", description="每笔销售流水")},
    columns={
        "sale_records": {
            "amount": ColumnDesc(label="销售金额", synonyms=("销售额", "金额"), description="单笔销售额"),
            "record_date": ColumnDesc(label="销售日期", synonyms=("日期",)),
        }
    },
)


class TestSearchSchema:
    def test_empty_keyword_returns_nothing(self):
        assert search_schema(_META, "") == []
        assert search_schema(_META, "   ") == []

    def test_label_hit_and_ordering(self):
        hits = search_schema(_META, "销售")
        assert any(h.kind == "table" and h.matched_on == "销售记录" for h in hits)
        assert any(h.column == "amount" for h in hits)
        # 同档内按 表 -> 列名 字典序：表级（column=""）在“amount”“record_date”之前
        assert [h.matched_on for h in hits] == ["销售记录", "销售金额", "销售日期"]

    def test_synonym_hit(self):
        hits = search_schema(_META, "销售额")
        assert any(h.column == "amount" and h.matched_on == "销售额" for h in hits)

    def test_synonym_loses_to_label_rank(self):
        # "金额" 既命中 label 销售金额（档2），又命中 synonym 金额（档3），应取 label 档
        hits = search_schema(_META, "金额")
        amount = [h for h in hits if h.column == "amount"]
        assert amount and amount[0].matched_on == "销售金额"

    def test_exact_name_rank(self):
        hits = search_schema(_META, "amount")
        assert hits and hits[0].column == "amount" and hits[0].matched_on == "amount"


# ---------- 数据模型融合 ----------


class TestModels:
    def test_column_carries_semantic_fields(self):
        col = ColumnSchema(name="amount", label="销售金额", synonyms=("销售额",))
        table = TableSchema(table="sale_records", columns=(col,), label="销售记录", description="每笔销售流水")
        row = table.to_legacy_rows()[0]
        assert row["Label"] == "销售金额"
        assert row["Synonyms"] == ["销售额"]
        assert table.label == "销售记录"
        assert col.label == "销售金额"

    def test_load_table_merges_semantics(self, monkeypatch):
        from mcp_server import config as cfg
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", encoding="utf-8", delete=False
        ) as fh:
            fh.write(
                yaml.safe_dump(
                    {
                        "tables": {"company": {"label": "公司档案"}},
                        "columns": {
                            "company": {"employees": {"label": "员工规模", "synonyms": ["员工数"]}}
                        },
                    },
                    allow_unicode=True,
                )
            )
            path = fh.name
        monkeypatch.setattr(cfg, "SCHEMA_DESC_PATH", path)

        class FakeCursor:
            def execute(self, sql, params=None):
                if sql.startswith("SHOW FULL COLUMNS"):
                    self._rows = [
                        {"Field": "company_id", "Type": "int", "Null": "NO", "Key": "PRI", "Default": None, "Comment": ""},
                        {"Field": "employees", "Type": "int", "Null": "NO", "Key": "", "Default": None, "Comment": ""},
                    ]
                elif sql.startswith("SHOW INDEX"):
                    self._rows = [{"Key_name": "PRIMARY", "Column_name": "company_id", "Non_unique": 0, "Seq_in_index": 1}]
                else:
                    self._rows = []
                return None

            def fetchall(self):
                return list(self._rows)

        @contextlib.contextmanager
        def fake_db_cursor():
            yield FakeCursor()

        monkeypatch.setattr("mcp_server.catalog.service.db_cursor", fake_db_cursor)
        catalog = SchemaCatalog(ttl_seconds=60)
        table = catalog.get_table("company")
        assert table.label == "公司档案"
        emp = next(c for c in table.columns if c.name == "employees")
        assert emp.label == "员工规模"
        assert emp.synonyms == ("员工数",)


# ---------- search_schema Tool 的 ACL 过滤 ----------

_POLICY = load_permission_policy()


def _policy_context() -> PolicyContext:
    return PolicyContext(policy=_POLICY, version="test", policy_hash="h", source="test")


def _make_hits():
    return [
        SemanticHit(table="company", column=None, kind="table", matched_on="公司", label="公司档案", description=""),
        SemanticHit(table="company", column="name", kind="column", matched_on="公司名", label="公司名称", description=""),
        SemanticHit(table="company", column="employees", kind="column", matched_on="员工数", label="员工规模", description=""),
    ]


class TestSearchSchemaAcl:
    def test_alice_gets_table_and_allowed_column_only(self, monkeypatch):
        import mcp_server.tools.schema as schema_tools

        principal = Principal(
            subject="alice",
            roles=frozenset(_POLICY.principals["alice"].roles),
            attributes=_POLICY.principals["alice"].attributes,
        )

        def fake_current_request_context(source="schema", policy_context=None):
            return RequestContext(principal=principal, source=source, auth_method="test", policy_version="test_ver")

        monkeypatch.setattr(schema_tools, "current_request_context", fake_current_request_context)
        monkeypatch.setattr(schema_tools, "_authorizer", AuthorizationService(_POLICY))
        monkeypatch.setattr(schema_tools._catalog, "search", lambda kw: _make_hits())

        schema_tools.get_policy_manager  # noqa 触发真实 manager 提供上下文
        # 覆盖 manager.get_context（本测试直接用构造的 policy）
        monkeypatch.setattr(
            schema_tools.get_policy_manager(), "get_context", lambda force=False: _policy_context()
        )

        result = schema_tools.search_schema("公司")
        columns = [r["column"] for r in result if r["kind"] == "column"]
        # name 可见，employees 对 alice 不可见（列 ACL）
        assert columns == ["name"]
        assert any(r["kind"] == "table" for r in result)

    def test_permission_gate_denies(self, monkeypatch):
        import mcp_server.tools.schema as schema_tools

        # policy-admin 只具备 policy:* 权限，无 schema:read
        principal = Principal(
            subject="policy-admin",
            roles=frozenset(_POLICY.principals["policy-admin"].roles),
        )

        def fake_current_request_context(source="schema", policy_context=None):
            return RequestContext(principal=principal, source=source, auth_method="test")

        monkeypatch.setattr(schema_tools, "current_request_context", fake_current_request_context)
        monkeypatch.setattr(schema_tools, "_authorizer", AuthorizationService(_POLICY))
        monkeypatch.setattr(
            schema_tools.get_policy_manager(), "get_context", lambda force=False: _policy_context()
        )

        try:
            schema_tools.search_schema("公司")
        except PermissionError:
            return
        raise AssertionError("auditor 不应有 schema:read 权限，应抛出 PermissionError")