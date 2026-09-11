"""MCP 客户端层：建立会话，并在 MCP 与 OpenAI function calling 之间做适配。

- mcp_session     连接 MCP Server 并完成握手，产出可用的 ClientSession
- to_openai_tool  MCP 工具定义 -> OpenAI tool schema
- extract_result  从 MCP 调用结果中取出可喂回 LLM 的文本
"""
import json
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from . import config


@asynccontextmanager
async def mcp_session(url: str | None = None):
    """连接 MCP Server（默认 Streamable HTTP）并完成初始化握手。"""
    async with streamable_http_client(url or config.mcp_url()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def to_openai_tool(tool) -> dict:
    """把 MCP 工具定义转换为 OpenAI function-calling 的 tool schema。"""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema or {"type": "object", "properties": {}},
        },
    }


def extract_result(res) -> str:
    """从 MCP 调用结果中取出结构化内容（mcp 2.x 列表型结果在 structured_content）。"""
    if res.structured_content is not None:
        return json.dumps(
            res.structured_content.get("result", res.structured_content),
            ensure_ascii=False,
        )
    texts = [c.text for c in res.content if getattr(c, "text", None)]
    return "\n".join(texts)
