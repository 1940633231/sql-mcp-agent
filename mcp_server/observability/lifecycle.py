"""进程生命周期状态与优雅排空协调。"""
from __future__ import annotations

import asyncio
import threading
import time
from enum import Enum


class LifecycleState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    STOPPED = "stopped"


class Lifecycle:
    def __init__(self):
        self._state = LifecycleState.STARTING
        self._inflight = 0
        self._started_at = time.time()
        self._lock = threading.Lock()

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def set_ready(self) -> None:
        self._state = LifecycleState.READY

    def set_draining(self) -> None:
        self._state = LifecycleState.DRAINING

    def set_stopped(self) -> None:
        self._state = LifecycleState.STOPPED

    def begin_request(self) -> None:
        with self._lock:
            self._inflight += 1

    def end_request(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    async def wait_for_drain(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.inflight == 0:
                return True
            await asyncio.sleep(0.05)
        return self.inflight == 0

    def snapshot(self) -> dict:
        return {
            "state": self._state.value,
            "inflight": self.inflight,
            "uptime_seconds": round(time.time() - self._started_at, 3),
        }


lifecycle = Lifecycle()
