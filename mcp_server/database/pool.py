"""面向 PyMySQL 的轻量线程安全连接池。"""
from __future__ import annotations

import contextlib
import queue
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class PoolStats:
    size: int
    available: int
    in_use: int
    created: int
    waits: int
    timeouts: int
    usage_limit: int


class PoolExhausted(TimeoutError):
    pass


class ConnectionPool:
    """按需建连、LIFO 复用，并在取用前执行健康检查。"""

    def __init__(
        self,
        factory,
        *,
        max_size: int = 10,
        timeout_seconds: float = 5.0,
        ping: bool = True,
        max_usage: int = 0,
    ):
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._factory = factory
        self._max_size = int(max_size)
        self._timeout = float(timeout_seconds)
        self._ping = bool(ping)
        self._max_usage = int(max_usage or 0)
        self._available: queue.LifoQueue = queue.LifoQueue(maxsize=self._max_size)
        self._created = 0
        self._in_use = 0
        self._waits = 0
        self._timeouts = 0
        self._closed = False
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def connection(self):
        conn = self.acquire()
        try:
            yield conn
        finally:
            self.release(conn)

    def acquire(self):
        deadline = time.monotonic() + self._timeout
        while True:
            with self._lock:
                if self._closed:
                    raise RuntimeError("connection pool is closed")
                can_create = self._created < self._max_size
                if can_create:
                    self._created += 1
                    self._in_use += 1
                    create = True
                else:
                    create = False
                    self._waits += 1
            if create:
                try:
                    return self._create()
                except Exception:
                    with self._lock:
                        self._created -= 1
                        self._in_use -= 1
                    raise

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    self._timeouts += 1
                raise PoolExhausted("database connection pool exhausted")
            try:
                entry = self._available.get(timeout=remaining)
            except queue.Empty:
                with self._lock:
                    self._timeouts += 1
                raise PoolExhausted("database connection pool exhausted")
            conn, usage = entry
            if self._valid(conn):
                with self._lock:
                    self._in_use += 1
                return conn
            self._discard(conn)

    def release(self, conn) -> None:
        with self._lock:
            if self._closed:
                close = True
                self._in_use -= 1
            else:
                close = False
            usage = getattr(conn, "_sql_mcp_pool_usage", 0) + 1
            conn._sql_mcp_pool_usage = usage
            discard = close or (self._max_usage and usage >= self._max_usage)
            if not close:
                self._in_use -= 1
        if discard:
            self._discard(conn)
            return
        self._available.put((conn, usage))

    def close(self) -> None:
        with self._lock:
            self._closed = True
        while True:
            try:
                conn, _ = self._available.get_nowait()
            except queue.Empty:
                break
            self._discard(conn)

    def stats(self) -> PoolStats:
        with self._lock:
            return PoolStats(
                size=self._max_size,
                available=self._available.qsize(),
                in_use=self._in_use,
                created=self._created,
                waits=self._waits,
                timeouts=self._timeouts,
                usage_limit=self._max_usage,
            )

    def probe(self) -> bool:
        try:
            with self.connection():
                return True
        except Exception:
            return False


    def _create(self):
        conn = self._factory()
        conn._sql_mcp_pool_usage = 0
        return conn

    def _valid(self, conn) -> bool:
        if not self._ping:
            return True
        try:
            conn.ping()
            return True
        except Exception:
            return False

    def _discard(self, conn) -> None:
        with self._lock:
            self._created -= 1
        try:
            conn.close()
        except Exception:
            pass
