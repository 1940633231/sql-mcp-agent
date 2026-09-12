"""V0.5 对外契约锁定测试。

冻结 MCP Tool 清单；任何增删工具都须先走版本演进（README 记录）并同步更新此处。
"""
import asyncio

import pytest

# 基线冻结的 13 个 MCP Tool（V0.5 新增 3 个领域查询工具，与 README「稳定 API 契约」保持一致）
EXPECTED_TOOLS = sorted(
    {
        "list_tables",
        "get_schema",
        "search_schema",
        "run_query",
        "sales_summary",
        "company_ranking",
        "industry_analysis",
        "get_policy_status",
        "reload_permission_policy",
        "validate_permission_policy",
        "publish_permission_policy",
        "list_permission_policy_versions",
        "export_permission_policy",
    }
)


def _registered_tool_names():
    import mcp_server.server as server_module

    async def _list():
        tools = await server_module.server.list_tools()
        return sorted(t.name for t in tools)

    return asyncio.run(_list())


def test_tool_inventory_is_locked():
    """对外 Tool 清单为精确集合：增删任意工具都会使本测试失败。"""
    assert _registered_tool_names() == EXPECTED_TOOLS


def test_core_tools_present():
    """核心数据工具始终可用（防御性冗余断言，便于精确定位）。"""
    registered = set(_registered_tool_names())
    for tool in ("list_tables", "get_schema", "search_schema", "run_query"):
        assert tool in registered


@pytest.mark.parametrize("name", EXPECTED_TOOLS)
def test_each_locked_tool_registered(name):
    assert name in set(_registered_tool_names())