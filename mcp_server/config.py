"""MCP Server 配置：数据库连接 + 传输参数。

通过 .env 覆盖默认值，代码零硬编码。数据库连接与 LLM 配置分离：
本文件只关心「服务端怎么连库、怎么起服务」。
"""
import json
import os

from dotenv import load_dotenv

load_dotenv()


def _json_dict(name: str) -> dict:
    """读取 JSON object 环境变量；空值返回空字典。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("%s 必须是 JSON object" % name)
    return {str(k): str(v) for k, v in value.items()}


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

# ===== Authentication / RBAC =====
# disabled: 本地开发身份；static: 使用 AUTH_TOKENS_JSON 的 Bearer Token。
AUTH_MODE = os.getenv("AUTH_MODE", "disabled").strip().lower()
AUTH_TOKENS = _json_dict("AUTH_TOKENS_JSON")
AUTH_ISSUER_URL = os.getenv("AUTH_ISSUER_URL", "http://localhost:8000")
AUTH_RESOURCE_URL = os.getenv(
    "AUTH_RESOURCE_URL",
    "http://%s:%d%s" % (MCP_HOST, MCP_PORT, MCP_PATH),
)
AUTH_REQUIRED_SCOPES = [
    scope.strip()
    for scope in os.getenv("AUTH_REQUIRED_SCOPES", "").split(",")
    if scope.strip()
]

# ===== External OAuth 2.1 / JWT Resource Server =====
AUTH_JWT_ISSUER = os.getenv("AUTH_JWT_ISSUER", "")
AUTH_JWT_AUDIENCE = os.getenv("AUTH_JWT_AUDIENCE", AUTH_RESOURCE_URL)
AUTH_JWT_JWKS_URL = os.getenv("AUTH_JWT_JWKS_URL", "")
AUTH_JWT_PUBLIC_KEY = os.getenv("AUTH_JWT_PUBLIC_KEY", "").replace("\\n", "\n")
AUTH_JWT_SECRET = os.getenv("AUTH_JWT_SECRET", "")
AUTH_JWT_ALGORITHMS = [
    item.strip().upper()
    for item in os.getenv("AUTH_JWT_ALGORITHMS", "RS256").split(",")
    if item.strip()
]
AUTH_JWT_LEEWAY = int(os.getenv("AUTH_JWT_LEEWAY", "30"))
AUTH_JWT_SUBJECT_CLAIM = os.getenv("AUTH_JWT_SUBJECT_CLAIM", "sub")
AUTH_JWT_ROLES_CLAIM = os.getenv("AUTH_JWT_ROLES_CLAIM", "roles")
AUTH_JWT_SCOPES_CLAIM = os.getenv("AUTH_JWT_SCOPES_CLAIM", "scope")
AUTH_JWT_ATTRIBUTES = _json_dict("AUTH_JWT_ATTRIBUTES_JSON")
AUTH_JWT_ROLE_MAP = _json_dict("AUTH_JWT_ROLE_MAP_JSON")
AUTH_ALLOW_RAW_ROLE_CLAIMS = os.getenv(
    "AUTH_ALLOW_RAW_ROLE_CLAIMS", "false"
).strip().lower() in {"1", "true", "yes", "on"}
# policy: 仅使用服务端 principal；claims: 使用服务端角色映射后的 JWT claims。
AUTH_PRINCIPAL_MODE = os.getenv("AUTH_PRINCIPAL_MODE", "policy").strip().lower()

# ===== Permission Policy Store / Hot Reload =====
# file: configs/permissions.yaml；mysql: 版本表 + active 指针，支持热更新。
POLICY_STORE = os.getenv("POLICY_STORE", "file").strip().lower()
POLICY_RELOAD_SECONDS = int(os.getenv("POLICY_RELOAD_SECONDS", "5"))
POLICY_VERSION_TABLE = "permission_policy_versions"
POLICY_ACTIVE_TABLE = "permission_policy_active"
POLICY_DB_HOST = os.getenv("POLICY_DB_HOST", os.getenv("MYSQL_HOST", "127.0.0.1"))
POLICY_DB_PORT = int(os.getenv("POLICY_DB_PORT", os.getenv("MYSQL_PORT", "3306")))
POLICY_DB_USER = os.getenv("POLICY_DB_USER", "")
POLICY_DB_PASSWORD = os.getenv("POLICY_DB_PASSWORD", "")
POLICY_DB_DATABASE = os.getenv("POLICY_DB_DATABASE", "")
POLICY_DB_CHARSET = os.getenv("POLICY_DB_CHARSET", "utf8mb4")
POLICY_DB_CONNECT_TIMEOUT = int(os.getenv("POLICY_DB_CONNECT_TIMEOUT", "10"))
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
DB_POOL_TIMEOUT_SECONDS = float(os.getenv("DB_POOL_TIMEOUT_SECONDS", "5"))
DB_POOL_MAX_USAGE = int(os.getenv("DB_POOL_MAX_USAGE", "0"))
POLICY_DB_POOL_SIZE = int(os.getenv("POLICY_DB_POOL_SIZE", "2"))
DB_POOL_PING = os.getenv("DB_POOL_PING", "true").strip().lower() in {"1", "true", "yes", "on"}
SCHEMA_CACHE_TTL_SECONDS = int(os.getenv("SCHEMA_CACHE_TTL_SECONDS", "60"))
SCHEMA_CACHE_MAX_ENTRIES = int(os.getenv("SCHEMA_CACHE_MAX_ENTRIES", "256"))
AUDIT_STORE = os.getenv("AUDIT_STORE", "log").strip().lower()
AUDIT_REQUIRED = os.getenv("AUDIT_REQUIRED", "false").strip().lower() in {"1", "true", "yes", "on"}
AUDIT_TABLE = "request_audit_events"
SHUTDOWN_GRACE_SECONDS = float(os.getenv("SHUTDOWN_GRACE_SECONDS", "20"))
POLICY_AUDIT_TABLE = "permission_policy_events"


def get_policy_connection() -> dict:
    """返回策略库专用连接参数。"""
    if not POLICY_DB_DATABASE or not POLICY_DB_USER:
        raise ValueError(
            "POLICY_STORE=mysql 时必须配置独立的 POLICY_DB_DATABASE 与 POLICY_DB_USER"
        )
    return {
        "host": POLICY_DB_HOST,
        "port": POLICY_DB_PORT,
        "user": POLICY_DB_USER,
        "password": POLICY_DB_PASSWORD,
        "database": POLICY_DB_DATABASE,
        "charset": POLICY_DB_CHARSET,
        "connect_timeout": POLICY_DB_CONNECT_TIMEOUT,
    }
