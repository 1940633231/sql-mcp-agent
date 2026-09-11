"""数据库连接层：创建 pymysql 连接，提供游标上下文管理器。"""
import contextlib

import pymysql

from .. import config


def connect(database: str | None = None):
    """创建一个业务库连接（DictCursor，autocommit）。"""
    return pymysql.connect(
        **{**config.get_connection(), "database": database or config.TARGET_DATABASE},
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


@contextlib.contextmanager
def db_cursor(database: str | None = None):
    """游标上下文：进入时建连/开游标，退出时确保连接关闭。"""
    conn = connect(database)
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()
