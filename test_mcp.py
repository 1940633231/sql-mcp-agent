"""端到端验证 MCP Server：用 MCP 客户端连接 stdio，发现工具并直接调用。"""
import asyncio

from mcp import ClientSession, StdioServerParameters, stdio_client
import sys

sys.path.insert(0, ".")
from mcp.client.stdio import stdio_client as sc  # noqa: F401  # 已被上面 mcp import 覆盖


async def render(res):
    """mcp 2.x 中 list 类型返回值会拆成多个 text，统一用 structured_content（完整结果）。"""
    if res.structured_content is not None:
        return res.structured_content.get("result", res.structured_content)
    return "\n".join(c.text for c in res.content if getattr(c, "text", None))


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=["mcp_server.py"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("发现工具:")
            for t in tools.tools:
                print("  -", t.name)
            print()

            print("== list_tables ==")
            print(await render(await session.call_tool("list_tables", {})))

            print("== get_schema(company) ==")
            print(await render(await session.call_tool("get_schema", {"table_name": "company"})))

            print("== run_query(2026 top10) ==")
            sql = (
                "SELECT co.name, ROUND(SUM(s.amount),0) AS total FROM sale_records s "
                "JOIN company co ON co.company_id=s.company_id "
                "WHERE YEAR(s.record_date)=2026 "
                "GROUP BY co.company_id, co.name ORDER BY total DESC LIMIT 10"
            )
            print(await render(await session.call_tool("run_query", {"sql": sql})))

            print("== 安全校验测试（应返回 error，不执行） ==")
            print(await render(await session.call_tool("run_query", {"sql": "DELETE FROM company"})))


if __name__ == "__main__":
    asyncio.run(main())