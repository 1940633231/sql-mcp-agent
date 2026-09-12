"""数据库连接层：创建 pymysql 连接，提供游标上下文管理器。"""
import contextlib

import pymysql

from .pool import ConnectionPool

from .. import config


def connect(database: str | None = None):
    """创建一个业务库连接（DictCursor，autocommit）。"""
    return pymysql.connect(
        **{**config.get_connection(), "database": database or config.TARGET_DATABASE},
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def policy_connect():
    """创建策略库专用连接，事务由调用方显式提交或回滚。"""
    return pymysql.connect(
        **config.get_policy_connection(),
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


business_pool = ConnectionPool(
    factory=connect,
    max_size=config.DB_POOL_SIZE,
    timeout_seconds=config.DB_POOL_TIMEOUT_SECONDS,
    ping=config.DB_POOL_PING,
    max_usage=config.DB_POOL_MAX_USAGE,
)

policy_pool = ConnectionPool(
    factory=policy_connect,
    max_size=config.POLICY_DB_POOL_SIZE,
    timeout_seconds=config.DB_POOL_TIMEOUT_SECONDS,
    ping=config.DB_POOL_PING,
    max_usage=config.DB_POOL_MAX_USAGE,
)


@contextlib.contextmanager
def db_cursor(database: str | None = None):
    """游标上下文：进入时建连/开游标，退出时确保连接关闭。"""
    if database and database != config.TARGET_DATABASE:
        conn = connect(database)
        try:
            with conn.cursor() as cur:
                yield cur
        finally:
            conn.close()
        return
    with business_pool.connection() as conn:
        with conn.cursor() as cur:
            yield cur


@contextlib.contextmanager
def policy_db_cursor():
    with policy_pool.connection() as conn:
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def pool_stats() -> dict:
    return {
        "business": business_pool.stats().__dict__,
        "policy": policy_pool.stats().__dict__,
    }


def close_pools() -> None:
    business_pool.close()
    policy_pool.close()


@contextlib.contextmanager
def policy_db_transaction():
    """在单个策略发布事务内提供游标。"""
    with policy_pool.connection() as conn:
        try:
            conn.begin()
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
