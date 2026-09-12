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
        monkeypatch.setattr(schema_tools._catalog, "search", lambda kw, scan_all=False: _make_hits())

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


# ---------- 防御性加载（Gap5）----------


class TestDefensiveLoad:
    def _write(self, path, content: str):
        path.write_text(content, encoding="utf-8")
        return path

    def test_malformed_yaml_returns_empty(self, tmp_path):
        path = self._write(tmp_path / "bad.yaml", "tables: [1, } not yaml")
        meta = load_semantics(path)
        assert meta.tables == {} and meta.columns == {}

    def test_root_not_dict_returns_empty(self, tmp_path):
        path = self._write(tmp_path / "list.yaml", "- a\n- b\n")
        meta = load_semantics(path)
        assert meta.tables == {} and meta.columns == {}

    def test_wrong_field_types_are_coerced(self, tmp_path):
        path = self._write(
            tmp_path / "types.yaml",
            "tables:\n  t:\n    label: 表\ncolumns:\n  t:\n    c:\n"
            "      label: 列\n      synonyms: 你好\n"
            "      enum_values:\n        - 1\n        - {value: x, label: X}\n",
        )
        meta = load_semantics(path)
        assert meta.columns["t"]["c"].synonyms == ()  # 非 list 降级为空
        assert meta.columns["t"]["c"].enum_values == ({"value": "x", "label": "X"},)  # 跳过非 dict

    def test_enum_values_non_iterable(self, tmp_path):
        path = self._write(
            tmp_path / "enum.yaml",
            "columns:\n  t:\n    c:\n      enum_values: 123\n",  # 标量 int，预判后不应迭代报错
        )
        meta = load_semantics(path)
        assert meta.columns["t"]["c"].enum_values == ()

    def test_invalid_utf8_returns_empty(self, tmp_path):
        path = tmp_path / "bad_utf8.yaml"
        path.write_bytes(b"tables: \xff\xfe bad utf8")
        meta = load_semantics(path)
        assert meta.tables == {} and meta.columns == {}


# ---------- 语义热重载（Gap4）----------


class TestSemanticReload:
    def test_invalidate_reloads_yaml(self, monkeypatch, tmp_path):
        from mcp_server import config as cfg

        path = tmp_path / "schema.yaml"
        path.write_text(yaml.safe_dump({"tables": {"company": {"label": "旧名"}}}, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(cfg, "SCHEMA_DESC_PATH", str(path))

        catalog = SchemaCatalog(ttl_seconds=60)
        assert catalog.semantic_metadata().tables["company"].label == "旧名"

        path.write_text(yaml.safe_dump({"tables": {"company": {"label": "新名"}}}, allow_unicode=True), encoding="utf-8")
        catalog.invalidate()
        assert catalog.semantic_metadata().tables["company"].label == "新名"

    def test_auto_reload_on_file_change(self, monkeypatch, tmp_path):
        from mcp_server import config as cfg

        path = tmp_path / "schema.yaml"
        path.write_text(yaml.safe_dump({"tables": {"company": {"label": "A"}}}, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(cfg, "SCHEMA_DESC_PATH", str(path))

        catalog = SchemaCatalog(ttl_seconds=60)
        assert catalog.semantic_metadata().tables["company"].label == "A"

        # 外部改文件后，下一次查询/检索触发的 mtime 检查会自动重载
        path.write_text(yaml.safe_dump({"tables": {"company": {"label": "B"}}}, allow_unicode=True), encoding="utf-8")
        catalog._semantic_checked = 0  # 确保允许本次 mtime 检查
        catalog._maybe_reload_semantics()
        assert catalog.semantic_metadata().tables["company"].label == "B"

    def test_deleting_file_clears_semantics(self, monkeypatch, tmp_path):
        from mcp_server import config as cfg

        path = tmp_path / "schema.yaml"
        path.write_text(yaml.safe_dump({"tables": {"company": {"label": "A"}}}, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(cfg, "SCHEMA_DESC_PATH", str(path))

        catalog = SchemaCatalog(ttl_seconds=60)
        assert catalog.semantic_metadata().tables["company"].label == "A"

        path.unlink()
        catalog._semantic_checked = 0
        catalog._maybe_reload_semantics()
        # 文件被删后按 load_semantics 的缺失语义返回空，而非沿用旧配置
        assert catalog.semantic_metadata().tables == {}


# ---------- 检索覆盖物理对象（Gap6）----------


class TestSearchPhysicalCoverage:
    def test_unconfigured_table_name_is_found(self, monkeypatch):
        # 没有会话语义的物理表：仅通过表名命中；get_table 只用于扫列名，mock 掉避免触库
        catalog = SchemaCatalog(ttl_seconds=60)
        monkeypatch.setattr(catalog, "_semantic", SemanticMetadata())

        def fake_tables():
            return ["company", "sale_records", "orders"]

        monkeypatch.setattr(catalog, "list_tables", fake_tables)
        monkeypatch.setattr(
            catalog, "get_table",
            lambda name: TableSchema(table=name, columns=()),
        )
        hits = catalog.search("orders")
        names = {(h.table, h.kind) for h in hits}
        assert ("orders", "table") in names

    def test_scan_all_finds_uncached_unmatched_column(self, monkeypatch):
        # 未缓存且表名未命中的物理列：只有 scan_all=True 才会被扫描到
        catalog = SchemaCatalog(ttl_seconds=60)
        monkeypatch.setattr(catalog, "_semantic", SemanticMetadata())
        monkeypatch.setattr(catalog, "_semantic_checked", 0)  # 让 TTL 检查放行
        monkeypatch.setattr(
            catalog, "list_tables", lambda: ["company", "orders"],
        )
        orders = TableSchema(
            table="orders",
            columns=(ColumnSchema(name="order_ts", label="下单时间"),),
        )
        monkeypatch.setattr(catalog, "get_table", lambda name: orders if name == "orders" else TableSchema(table=name, columns=()))

        # 默认不 scan_all：表名未命中、该表未缓存，物理列不被扫描
        assert catalog.search("order_ts") == []
        # scan_all=True：懒加载 orders 并扫到物理列名 order_ts
        assert any(h.column == "order_ts" for h in catalog.search("order_ts", scan_all=True))

    def test_scan_all_still_scans_columns_of_semantic_hit_table(self, monkeypatch):
        # 表因 label 命中语义（covered table 并非来自物理名），scan_all 仍须扫描其物理列
        catalog = SchemaCatalog(ttl_seconds=60)
        monkeypatch.setattr(
            catalog, "_semantic",
            SemanticMetadata(tables={"sales": TableDesc(label="purchase")}),
        )
        monkeypatch.setattr(catalog, "_semantic_checked", 0)
        monkeypatch.setattr(catalog, "list_tables", lambda: ["sales"])
        sales = TableSchema(
            table="sales",
            columns=(ColumnSchema(name="purchase_id"),),
        )
        monkeypatch.setattr(catalog, "get_table", lambda name: sales)

        default = catalog.search("purchase")
        assert ("sales", None) in {(h.table, h.column) for h in default}  # 表级语义命中
        assert all(h.column != "purchase_id" for h in default)  # 但默认不扫其列

        deep = catalog.search("purchase", scan_all=True)
        assert any(h.column == "purchase_id" for h in deep)  # scan_all 仍扫描该表列


# ---------- DTO 输出（Gap1/2）----------


class TestDto:
    def test_table_dict_carries_all_metadata(self):
        col = ColumnSchema(
            name="amount", data_type="decimal", nullable=False,
            label="金额", description="销售额", synonyms=("销售额",),
            enum_values=({"value": "1", "label": "电子"},),
        )
        table = TableSchema(
            table="sale_records", columns=(col,),
            primary_keys=("id",),
            foreign_keys=({"COLUMN_NAME": "company_id", "REFERENCED_TABLE_NAME": "company"},),
            indexes=({"name": "idx_company_date", "column": "company_id", "unique": False},),
            label="销售记录", description="每笔销售流水",
        )
        dto = table.to_dict()
        assert dto["table"] == "sale_records"
        assert dto["label"] == "销售记录" and dto["description"] == "每笔销售流水"
        assert dto["primary_keys"] == ["id"]
        assert dto["foreign_keys"][0]["COLUMN_NAME"] == "company_id"
        assert "column" in dto["indexes"][0]
        col_dto = dto["columns"][0]
        assert col_dto["name"] == "amount"
        assert col_dto["enum_values"] == [{"value": "1", "label": "电子"}]
        assert col_dto["synonyms"] == ["销售额"]

    def test_catalog_get_table_dict(self, monkeypatch, tmp_path):
        from mcp_server.catalog.service import SchemaCatalog as SC
        from mcp_server import config as cfg

        # 空的语义文件，避免依赖真实 configs/schema_desc.yaml
        path = tmp_path / "schema.yaml"
        path.write_text(yaml.safe_dump({"tables": {}}, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(cfg, "SCHEMA_DESC_PATH", str(path))

        class FakeCursor:
            def execute(self, sql, params=None):
                if sql.startswith("SHOW FULL COLUMNS"):
                    self._rows = [
                        {"Field": "company_id", "Type": "int", "Null": "NO", "Key": "PRI", "Default": None, "Comment": ""},
                        {"Field": "name", "Type": "varchar(100)", "Null": "NO", "Key": "", "Default": None, "Comment": ""},
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
        catalog = SC(ttl_seconds=60)
        dto = catalog.get_table_dict("company")
        assert dto["table"] == "company"
        assert {c["name"] for c in dto["columns"]} == {"company_id", "name"}
        assert "enum_values" in dto["columns"][1]