"""MCP 客户端层：建立会话，并在 MCP 与 OpenAI function calling 之间做适配。

- mcp_session     连接 MCP Server 并完成握手，产出可用的 ClientSession
- to_openai_tool  MCP 工具定义 -> OpenAI tool schema
- extract_result  从 MCP 调用结果中取出可喂回 LLM 的文本

V0.6：连接 / 读取超时由 http_client 的 httpx 超时施加（连接、读、写、连接池）。
"""
import json
from contextlib import asynccontextmanager

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from . import config


def _http_timeout() -> httpx2.Timeout:
    """把连接 / 读 / 写 / 连接池超时包装为 httpx 超时对象。"""
    return httpx2.Timeout(
        connect=config.MCP_CONNECT_TIMEOUT_SECONDS,
        read=config.MCP_CALL_TIMEOUT_SECONDS,
        write=config.MCP_CALL_TIMEOUT_SECONDS,
        pool=config.MCP_CONNECT_TIMEOUT_SECONDS,
    )


@asynccontextmanager
async def mcp_session(url: str | None = None):
    """连接 MCP Server（默认 Streamable HTTP）并完成初始化握手。"""
    headers = {}
    if config.MCP_AUTH_TOKEN:
        headers["Authorization"] = "Bearer %s" % config.MCP_AUTH_TOKEN
    http_client = create_mcp_http_client(
        headers=headers or None,
        timeout=_http_timeout(),
    )
    async with http_client:
        async with streamable_http_client(
            url or config.mcp_url(), http_client=http_client
        ) as (read, write):
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
