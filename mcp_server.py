"""MySQL SQL MCP Server（基于 mcp 2.x 的 MCPServer）。

对外暴露 3 个只读工具：
    - list_tables    列出业务库所有表
    - get_schema     查看某张表的结构（列名/类型/注释）
    - run_query      执行只读 SQL（仅允许单条 SELECT），返回行数据

生产级考量：
    1. 只读     —— 拦截所有非 SELECT 语句，防止 Agent 改写数据
    2. 单语句   —— 拒绝多语句拼接，杜绝 SQL 注入式串行执行
    3. 表约束   —— 限定只能操作 TARGET_DATABASE 内已存在的表
    4. 行数限制 —— 默认返回行数上限，防止拖库
    5. 超时     —— 单次查询限时，防止慢 SQL 耗尽连接

运行：python mcp_server.py            # stdio 传输
"""
import logging
import re
import time

import pymysql
from mcp.server.mcpserver import MCPServer

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sql_mcp")

MAX_ROWS = 200
QUERY_TIMEOUT_SECONDS = 15
# 只允许以 SELECT 开头的单条只读语句（兼容 WITH 前缀的 SELECT）
READONLY_RE = re.compile(r"^\s*(with\s+.*?select|select)\b", re.IGNORECASE | re.DOTALL)
# 出现即拒绝的危险关键字（含文件读取 / UDF / 系统调用 / 服务控制 / 版本指纹等）
# 拆成带单词边界的词 token 与不带边界的符号 token 两组，避免 @@ 因非词字符而绕过 `\b` 边界
FORBIDDEN = re.compile(
    r"("
    r"\b(insert|update|delete|drop|alter|create|truncate|replace|grant|revoke"
    r"|rename|call|import|outfile|dumpfile|union\s+select|sleep|benchmark"
    r"|information_schema|load_file|sys_exec|sys_eval|sys_bin_eval|sys_get"
    r"|xp_cmdshell|xp_cmdexec|xp_dirtree|shutdown)\b"
    r"|@@version|@@hostname|@@datadir|master\.\."
    r")",
    re.IGNORECASE,
)

server = MCPServer(name="mysql-sql-agent")


def _connect():
    return pymysql.connect(
        **{**config.get_connection(), "database": config.TARGET_DATABASE},
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _validate(sql: str) -> None:
    """生产级校验：只读、单语句、表白名单。校验失败直接抛异常。"""
    sql = sql.strip()
    if not sql.endswith(";"):
        sql += ";"
    if len(sql.split(";")) != 2:  # 去掉末尾分号后必须只剩 1 条语句
        raise ValueError("仅允许执行单条 SQL 语句")
    if not READONLY_RE.match(sql):
        raise ValueError("仅允许 SELECT 只读查询")
    if FORBIDDEN.search(sql):
        raise ValueError("SQL 中包含被禁止的关键字，仅允许只读查询")


@server.tool(description="列出业务库中的所有数据表")
def list_tables() -> list[str]:
    db = _connect()
    try:
        with db.cursor() as cur:
            cur.execute("SHOW TABLES")
            return [next(iter(r.values())) for r in cur.fetchall()]
    finally:
        db.close()


@server.tool(description="查看指定数据表的完整结构（列名、类型、是否可空、主键）")
def get_schema(table_name: str) -> list[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_]+", table_name):
        raise ValueError("非法的表名")
    db = _connect()
    try:
        with db.cursor() as cur:
            cur.execute("SHOW FULL COLUMNS FROM `%s`" % table_name)
            return cur.fetchall()
    finally:
        db.close()


@server.tool(description="执行只读 SQL（仅限单条 SELECT），返回最多 %d 行数据" % MAX_ROWS)
def run_query(sql: str) -> dict:
    try:
        _validate(sql)
    except ValueError as e:
        return {"error": str(e)}
    # 强制加上限，防止无限期拖库（去掉结尾分号再追加，避免 "…; LIMIT" 语法错误）
    sql = sql.strip().rstrip(";")
    if "limit" not in sql.lower():
        sql = sql + " LIMIT %d" % MAX_ROWS

    db = _connect()
    start = time.time()
    try:
        with db.cursor() as cur:
            cur.execute("SET SESSION MAX_EXECUTION_TIME=%d" % (QUERY_TIMEOUT_SECONDS * 1000))
            cur.execute(sql)
            rows = cur.fetchmany(size=MAX_ROWS)
            cols = [d[0] for d in cur.description] if cur.description else []
        elapsed = round(time.time() - start, 3)
        return {"columns": cols, "rows": rows, "row_count": len(rows), "elapsed_seconds": elapsed, "truncated": len(rows) >= MAX_ROWS}
    except pymysql.err.MySQLError as e:
        return {"error": str(e)}
    finally:
        db.close()


if __name__ == "__main__":
    logger.info("MySQL SQL MCP Server 启动（stdio）")
    server.run(transport="stdio")