"""Agent 配置：LLM 参数 + MCP Server 端点。通过 .env 覆盖默认值。

与 mcp_server/config.py 分离：本文件只关心「客户端怎么连服务、怎么调模型」。
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _parse_price_map(raw: str) -> dict:
    """按 'model=in,out;model2=in,out' 解析模型单价覆盖；空串返回 {}。"""
    mapping: dict = {}
    if not raw:
        return mapping
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, prices = chunk.split("=", 1)
        parts = [p.strip() for p in prices.split(",")]
        if len(parts) == 2:
            try:
                mapping[name.strip()] = (float(parts[0]), float(parts[1]))
            except ValueError:
                continue
    return mapping


def _list_env(name: str, default: str) -> list[str]:
    """读取逗号分隔的环境变量为字符串列表（空串返回空列表）。"""
    raw = os.getenv(name, default)
    return [part.strip() for part in raw.split(",") if part.strip()]


# ===== LLM（OpenAI 兼容协议，默认为通义千问 / DashScope）=====
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen-plus")
# 单个问题最多允许的「LLM 调工具」轮数（V0.6 作为迭代预算上限）
LLM_MAX_TOOL_ITERATIONS = int(os.getenv("LLM_MAX_TOOL_ITERATIONS", "10"))

# V0.6 可靠性：LLM 调用超时（连接 / 读 / 写 / 连接池，单位秒，OpenAI 侧）
LLM_CONNECT_TIMEOUT_SECONDS = float(os.getenv("LLM_CONNECT_TIMEOUT_SECONDS", "5"))
LLM_READ_TIMEOUT_SECONDS = float(os.getenv("LLM_READ_TIMEOUT_SECONDS", "60"))
LLM_WRITE_TIMEOUT_SECONDS = float(os.getenv("LLM_WRITE_TIMEOUT_SECONDS", "60"))
LLM_POOL_TIMEOUT_SECONDS = float(os.getenv("LLM_POOL_TIMEOUT_SECONDS", "5"))
# 单次 LLM 调用额外落地的超时（asyncio.wait_for 兜底，单位秒）
LLM_MAX_CALL_SECONDS = float(os.getenv("LLM_MAX_CALL_SECONDS", "90"))
# LLM 临时错误（网络/限流/5xx）最大重试次数；永久错误（认证/4xx）不重试
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))

# V0.6 可靠性：全局请求截止时间（超时后停止继续修复/调用，单位秒）
REQUEST_DEADLINE_SECONDS = float(os.getenv("REQUEST_DEADLINE_SECONDS", "120"))

# V0.6 可靠性：工具调用超时与临时错误重试（仅只读调用可重试）
MCP_CONNECT_TIMEOUT_SECONDS = float(os.getenv("MCP_CONNECT_TIMEOUT_SECONDS", "5"))
MCP_CALL_TIMEOUT_SECONDS = float(os.getenv("MCP_CALL_TIMEOUT_SECONDS", "30"))
MCP_MAX_RETRIES = int(os.getenv("MCP_MAX_RETRIES", "2"))
RETRY_BASE_DELAY_SECONDS = float(os.getenv("RETRY_BASE_DELAY_SECONDS", "0.5"))
RETRY_MAX_DELAY_SECONDS = float(os.getenv("RETRY_MAX_DELAY_SECONDS", "4.0"))

# V0.6 可靠性：统一预算
MAX_TOOL_CALLS = int(os.getenv("MAX_TOOL_CALLS", "40"))         # 单问题工具调用总数上限
MAX_SQL_REPAIRS = int(os.getenv("MAX_SQL_REPAIRS", "3"))        # SQL 修复次数上限
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "0"))                  # Token 上限；0=不限制
MAX_COST = float(os.getenv("MAX_COST", "0"))                    # 费用上限（美元）；0=不限制

# V0.6 可靠性：工具暴露与重试边界
# BLOCKED_TOOLS：Agent 一律不得调用（有副作用 / 服务端管理操作），从发现的工具清单中剔除。
BLOCKED_TOOLS = _list_env(
    "BLOCKED_TOOLS",
    "publish_permission_policy,reload_permission_policy",
)
# RETRY_SAFE_TOOLS：可安全重试的只读工具白名单。仅白名单内的工具在临时错误时退避重试；
# 不在白名单内的工具至多执行一次（失败即终止），从根上杜绝写操作的重复副作用。
RETRY_SAFE_TOOLS = _list_env(
    "RETRY_SAFE_TOOLS",
    "list_tables,get_schema,search_schema,run_query,"
    "sales_summary,company_ranking,industry_analysis",
)

# 模型单价（美元 / 百万 token），用于 MAX_COST 费用预算与指标
# 键为模型名，值为 (输入单价, 输出单价)；未收录模型按默认 (0, 0) 记 0 费用
LLM_INPUT_PRICE = float(os.getenv("LLM_INPUT_PRICE", "0.002"))
LLM_OUTPUT_PRICE = float(os.getenv("LLM_OUTPUT_PRICE", "0.008"))
MODEL_PRICES_OVERRIDE = _parse_price_map(os.getenv("MODEL_PRICES", ""))

# ===== MCP Server 端点（与 mcp_server/config.py 读取同一组环境变量）=====
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
MCP_PATH = os.getenv("MCP_PATH", "/mcp")

# v0.3：服务端 AUTH_MODE=static 时，Agent 用该 Bearer Token 代表某个 principal。
MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "")


def model_prices(model: str) -> tuple[float, float]:
    """返回 (输入单价, 输出单价)，单位美元 / 百万 token。

    优先取 MODEL_PRICES 显式覆盖，其次内置的 qwen-plus 参考价，最后用默认单价。
    未收录的模型也能有个笼统价格，保证费用预算至少是「有界」的。
    """
    if model in MODEL_PRICES_OVERRIDE:
        return MODEL_PRICES_OVERRIDE[model]
    if model == "qwen-plus":
        return (0.002, 0.008)  # 通义千问 plus 参考价（非精确计费）
    return (LLM_INPUT_PRICE, LLM_OUTPUT_PRICE)


def mcp_url() -> str:
    """MCP Server 的 HTTP 端点地址（streamable-http 客户端用）。"""
    return f"http://{MCP_HOST}:{MCP_PORT}{MCP_PATH}"
