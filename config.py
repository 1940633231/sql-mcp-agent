"""全局配置：数据库连接 + LLM 参数。
通过 .env 环境变量覆盖默认值，便于本地开发与生产部署分离。
"""
import os

from dotenv import load_dotenv

load_dotenv()


def get_connection():
    return {
        "host": os.getenv("MYSQL_HOST", "172.23.97.35"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", "root"),
        "database": "",
        "charset": "utf8mb4",
    }


# 独立的业务库，与其它库隔离，符合“单独建一个库”的要求
TARGET_DATABASE = os.getenv("MYSQL_DATABASE", "sales_demo")

# MCP 传输方式：http（Streamable HTTP，Server 作为独立进程常驻）或 stdio（子进程管道）
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "http")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
MCP_PATH = os.getenv("MCP_PATH", "/mcp")

def mcp_url() -> str:
    """MCP Server 的 HTTP 端点地址（streamable-http 客户端用）。"""
    return f"http://{MCP_HOST}:{MCP_PORT}{MCP_PATH}"

# LLM：默认走 OpenAI 兼容协议，可替换为任意兼容厂商（DeepSeek / Qwen / 硅基流动等）
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen-plus")
LLM_MAX_TOOL_ITERATIONS = int(os.getenv("LLM_MAX_TOOL_ITERATIONS", "10"))