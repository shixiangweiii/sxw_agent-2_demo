from __future__ import annotations

import random
import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...
    def monotonic(self) -> float: ...


class RandomSource(Protocol):
    def uniform(self, low: float, high: float) -> float: ...


class SystemClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def monotonic(self) -> float:
        return time.monotonic()


class SystemRandomSource:
    def uniform(self, low: float, high: float) -> float:
        return random.uniform(low, high)

