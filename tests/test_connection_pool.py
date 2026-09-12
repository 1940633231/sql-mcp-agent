"""连接池单元测试。"""
import pytest

from mcp_server.database.pool import ConnectionPool, PoolExhausted


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.pings = 0

    def ping(self, reconnect=False):
        self.pings += 1
        return True

    def close(self):
        self.closed = True


def test_pool_reuses_connection():
    created = []

    def factory():
        conn = FakeConnection()
        created.append(conn)
        return conn

    pool = ConnectionPool(factory, max_size=1, timeout_seconds=0.1)
    with pool.connection() as first:
        pass
    with pool.connection() as second:
        pass

    assert first is second
    assert len(created) == 1
    assert pool.stats().created == 1


def test_pool_exhaustion():
    pool = ConnectionPool(FakeConnection, max_size=1, timeout_seconds=0.01)
    first = pool.acquire()
    try:
        with pytest.raises(PoolExhausted):
            pool.acquire()
    finally:
        pool.release(first)


def test_pool_discards_invalid_connection():
    class BadConnection(FakeConnection):
        def ping(self, reconnect=False):
            raise RuntimeError("dead")

    created = []

    def factory():
        conn = BadConnection()
        created.append(conn)
        return conn

    pool = ConnectionPool(factory, max_size=1, timeout_seconds=0.01)
    first = pool.acquire()
    pool.release(first)
    second = pool.acquire()
    pool.release(second)

    assert first is not second
    assert first.closed
    assert len(created) == 2
