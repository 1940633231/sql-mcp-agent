"""初始化权限策略专用数据库和数据表。"""
import sys
from pathlib import Path

import pymysql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server import config
from mcp_server.authorization.manager import MySqlPolicyStore


def main() -> None:
    params = config.get_policy_connection()
    database = params.pop("database")
    conn = pymysql.connect(**params, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE DATABASE IF NOT EXISTS `%s` DEFAULT CHARACTER SET utf8mb4" % database
            )
    finally:
        conn.close()

    store = MySqlPolicyStore()
    store.ensure_schema()
    print("policy database ready: %s.%s" % (params["host"], database))


if __name__ == "__main__":
    main()
