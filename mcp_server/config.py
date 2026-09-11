"""MCP Server 配置：数据库连接 + 传输参数。

通过 .env 覆盖默认值，代码零硬编码。数据库连接与 LLM 配置分离：
本文件只关心「服务端怎么连库、怎么起服务」。
"""
import os

from dotenv import load_dotenv

load_dotenv()


def get_connection() -> dict:
    """pymysql 连接参数（database 留空，由调用方按需指定）。"""
    return {
        "host": os.getenv("MYSQL_HOST", "172.23.97.35"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", "root"),
        "database": "",
        "charset": "utf8mb4",
        "connect_timeout": int(os.getenv("MYSQL_CONNECT_TIMEOUT", "10")),
    }


# 独立的业务库，与其它库隔离
TARGET_DATABASE = os.getenv("MYSQL_DATABASE", "sales_demo")

# MCP 传输：http（Streamable HTTP，常驻服务）或 stdio（子进程管道）
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "http")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
MCP_PATH = os.getenv("MCP_PATH", "/mcp")
