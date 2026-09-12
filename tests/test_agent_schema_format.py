"""Agent 端 schema 预取格式化测试（Gap5：不丢弃表级与键信息）。"""
from agent.agent import _format_table


def test_format_table_includes_full_metadata():
    t = {
        "table": "sale_records",
        "label": "销售记录",
        "description": "每笔销售流水",
        "primary_keys": ["id"],
        "foreign_keys": [
            {"COLUMN_NAME": "company_id", "REFERENCED_TABLE_NAME": "company", "REFERENCED_COLUMN_NAME": "company_id"}
        ],
        "indexes": [{"name": "idx", "column": "company_id"}],
        "columns": [{"name": "amount", "label": "销售金额", "synonyms": ["销售额"]}],
    }
    text = _format_table(t)
    assert "销售记录" in text          # 表 label
    assert "每笔销售流水" in text      # 表 description
    assert "主键: id" in text          # 主键
    assert "company.company_id" in text  # 外键引用
    assert "idx(company_id" in text     # 索引
    assert '"name": "amount"' in text   # 列