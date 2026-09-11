"""Agent 配置：LLM 参数 + MCP Server 端点。通过 .env 覆盖默认值。

与 mcp_server/config.py 分离：本文件只关心「客户端怎么连服务、怎么调模型」。
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ===== LLM（OpenAI 兼容协议，默认为通义千问 / DashScope）=====
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen-plus")
# 单个问题最多允许的「LLM 调工具」轮数，防止死循环
LLM_MAX_TOOL_ITERATIONS = int(os.getenv("LLM_MAX_TOOL_ITERATIONS", "10"))

# ===== MCP Server 端点（与 mcp_server/config.py 读取同一组环境变量）=====
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
MCP_PATH = os.getenv("MCP_PATH", "/mcp")


def mcp_url() -> str:
    """MCP Server 的 HTTP 端点地址（streamable-http 客户端用）。"""
    return f"http://{MCP_HOST}:{MCP_PORT}{MCP_PATH}"
