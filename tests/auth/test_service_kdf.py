"""Concurrent password hashing is bounded (roadmap F14, memory safety of scrypt)."""

from __future__ import annotations

import asyncio
import threading
import time

from greenkube.auth import service


def test_kdf_runs_are_bounded_per_event_loop():
    lock = threading.Lock()
    running = 0
    peak = 0

    def slow_kdf(value: int) -> int:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.02)
        with lock:
            running -= 1
        return value

    async def burst() -> list[int]:
        return await asyncio.gather(*(service._run_kdf(slow_kdf, index) for index in range(12)))

    results = asyncio.run(burst())
    # A second event loop gets its own semaphore (the TestClient portal runs one per test).
    again = asyncio.run(burst())

    assert results == list(range(12)) and again == results
    assert 1 <= peak <= service.MAX_CONCURRENT_KDF
