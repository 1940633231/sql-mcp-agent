"""端到端验证 MCP Server：用 MCP 客户端经 HTTP 连接，发现工具并直接调用。

先启动 Server：python mcp_server.py
再运行本脚本：python test_mcp.py
"""
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import config


async def render(res):
    """mcp 2.x 中 list 类型返回值会拆成多个 text，统一用 structured_content（完整结果）。"""
    if res.structured_content is not None:
        return res.structured_content.get("result", res.structured_content)
    return "\n".join(c.text for c in res.content if getattr(c, "text", None))


async def main():
    async with streamable_http_client(config.mcp_url()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("发现工具:")
            for t in tools.tools:
                print("  -", t.name)
            print()

            print("== resources/list ==")
            res_list = await session.list_resources()
            for r in res_list.resources:
                print("  -", r.uri, "(%s)" % r.name)
            tpl_list = await session.list_resource_templates()
            for t in tpl_list.resource_templates:
                print("  ~", t.uri_template, "(%s)" % t.name)
            print()

            print("== read database://schema ==")
            schema_res = await session.read_resource("database://schema")
            print(schema_res.contents[0].text[:400], "...")
            print()

            print("== read database://table/company（URI 模板资源） ==")
            tbl_res = await session.read_resource("database://table/company")
            print(tbl_res.contents[0].text[:400], "...")
            print()

            print("== 非法表名（应报错，不执行） ==")
            try:
                await session.read_resource("database://table/..%2Fetc")
                print("  未拦截！（不应出现）")
            except Exception as e:
                print("  已拦截:", type(e).__name__)
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