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

# LLM：默认走 OpenAI 兼容协议，可替换为任意兼容厂商（DeepSeek / Qwen / 硅基流动等）
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen-plus")
LLM_MAX_TOOL_ITERATIONS = int(os.getenv("LLM_MAX_TOOL_ITERATIONS", "10"))