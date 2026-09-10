"""SQL Agent：通过 MCP 发现并调用 MySQL 只读工具，解答自然语言数据库问题。

工作流程（ReAct + Function Calling）：
    1. 用 MCP Streamable HTTP 客户端连接 mcp_server.py（需先单独启动），动态发现工具（list_tools）
    2. 把 MCP 工具 schema 转换为 OpenAI 兼容的 function-calling schema
    3. 进入循环：LLM 决定调用哪些工具 → agent 经 MCP 执行 → 结果喂回 LLM
    4. 直到 LLM 给出最终答案

示例：python mcp_server.py &  python sql_agent.py "今年销售额最高的10家公司"
"""
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from openai import OpenAI

import config


class SQLAgent:
    """基于 MCP 工具 + LLM function calling 的 SQL 查询 Agent。"""

    def __init__(self, verbose: bool = False):
        if not config.LLM_API_KEY or "填入" in config.LLM_API_KEY:
            raise SystemExit(
                "未配置 LLM_API_KEY：请先在 .env 里填入有效的 API Key，"
                "或设置环境变量 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL。"
            )
        self.client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)
        self.verbose = verbose

    def _trace(self, msg: str):
        if self.verbose:
            print("  [trace] " + msg, flush=True)

    @staticmethod
    def _to_openai_schema(tool) -> dict:
        """把 MCP 工具定义转换为 OpenAI function-calling 的 tool schema。"""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema or {"type": "object", "properties": {}},
            },
        }

    @staticmethod
    def _extract_result(res) -> str:
        """从 MCP 调用结果中取出结构化内容（mcp 2.x 里列表型结果在 structured_content）。"""
        if res.structured_content is not None:
            return json.dumps(
                res.structured_content.get("result", res.structured_content),
                ensure_ascii=False,
            )
        texts = [c.text for c in res.content if getattr(c, "text", None)]
        return "\n".join(texts)

    @staticmethod
    def _build_system_prompt(tables_desc: str, schema_desc: str) -> str:
        return (
            "你是一个只读 SQL 数据分析助手，通过调用 MCP 工具查询 MySQL 数据库。\n\n"
            "可用工具：\n"
            "  - list_tables       列举所有数据表\n"
            "  - get_schema(tbl)   查看某表字段结构\n"
            "  - run_query(sql)    执行只读 SELECT 查询（数据库会强制只读、单语句、限行）\n\n"
            "当前业务库中的数据表：\n"
            f"{tables_desc}\n\n"
            "各表结构：\n"
            f"{schema_desc}\n\n"
            "规则：\n"
            "1. 回答用户问题时，先判断需要哪些数据，必要时先 list_tables / get_schema 了解结构。\n"
            "2. 用 run_query 编写 SQL，只做只读聚合查询；不要编造数据，结果以真实查询为准。\n"
            "3. '今年'/'今年内' 等相对时间词，按当前年度 2026 处理（数据含 2023~2026）。\n"
            "4. 得到结果后，用自然语言组织成清晰答案给用户。\n"
        )

    async def _gather_context(self, session: ClientSession) -> tuple[str, str, list[dict]]:
        """发现工具；库结构走 Resource（database://schema）一次性预取，不再逐表调 Tool。

        表结构是静态参考数据、由应用而非模型决定加载，用 read_resource 语义更准确，
        且省去 list_tables + N 次 get_schema 的工具调用轮次。
        """
        tools = await session.list_tools()
        openai_schemas = [self._to_openai_schema(t) for t in tools.tools]

        res = await session.read_resource("database://schema")
        schema_data = json.loads(res.contents[0].text)
        tables_desc = json.dumps([t["table"] for t in schema_data], ensure_ascii=False)
        schema_desc = "\n".join(
            f"表 {t['table']}: {json.dumps(t['columns'], ensure_ascii=False)}"
            for t in schema_data
        )

        return tables_desc, schema_desc, openai_schemas

    async def run(self, question: str) -> str:
        """主入口：连接 MCP（HTTP），循环调用 LLM 直到得到最终答案。"""
        url = config.mcp_url()
        async with streamable_http_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tables_desc, schema_desc, openai_schemas = await self._gather_context(session)

                messages = [
                    {"role": "system",
                     "content": self._build_system_prompt(tables_desc, schema_desc)},
                    {"role": "user", "content": question},
                ]
                pending = config.LLM_MAX_TOOL_ITERATIONS
                while pending > 0:
                    resp = self.client.chat.completions.create(
                        model=config.LLM_MODEL,
                        messages=messages,
                        tools=openai_schemas,
                        tool_choice="auto",
                    )
                    msg = resp.choices[0].message
                    tool_calls = msg.tool_calls
                    if not tool_calls:
                        return msg.content or "（模型未返回内容）"

                    # 执行本轮所有工具调用
                    messages.append({
                        "role": "assistant",
                        "content": msg.content,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.function.name,
                                             "arguments": tc.function.arguments},
                            }
                            for tc in tool_calls
                        ],
                    })
                    for tc in tool_calls:
                        name = tc.function.name
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        self._trace("%s(%s)" % (name, json.dumps(args, ensure_ascii=False)))
                        result = await session.call_tool(name, args)
                        text = self._extract_result(result)
                        self._trace("=> " + (text if len(text) < 300 else text[:300] + "...[截断]"))
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": text,
                        })
                    pending -= 1
                return "达到最大工具调用次数，未能得出最终答案。"


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    verbose = "--trace" in sys.argv[1:]
    if len(args) < 1:
        print("用法: python sql_agent.py \"你想查询的问题\" [--trace]")
        return
    agent = SQLAgent(verbose=verbose)
    answer = await agent.run(args[0])
    print("\n===== 最终答案 =====\n%s" % answer)


if __name__ == "__main__":
    asyncio.run(main())