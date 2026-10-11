"""全局连接速率限制。

部署到服务器上时，优选扫描会发起大量 TCP 连接（每个 IP 一次 TLS 握手），
如果放任并发跑满，会把这台机器（以及同一网络里的其他人）的出口带宽和
NAT 会话表打爆。因此这里做一个**全平台共享**的令牌桶限速：

    --rate 100      表示所有平台加起来，每秒最多发起 100 个 TCP 连接

实现要点：
  * 令牌桶提前预支（burstable=1 时每个请求间隔严格为 1/rate 秒），
    因此可以保证「任意 1 秒窗口内 <= rate 个连接」，而不是只在整数秒边界限流；
  * 线程安全，多个平台的线程池共用同一个桶，所以是全局上限而非每平台上限；
  * rate <= 0 表示不限速。
"""

from __future__ import annotations

import threading
import time


class NullRateLimiter:
    """不限速时的空实现，避免在热路径上做无谓的锁操作。"""

    enabled = False
    rate = 0.0

    def acquire(self) -> None:
        return None

    def __repr__(self) -> str:
        return "NullRateLimiter(不限速)"


class RateLimiter:
    """令牌桶限速器：保证任意 1 秒内 acquire() 不超过 rate 次。"""

    enabled = True

    def __init__(self, rate: float, burst: float = 1.0) -> None:
        if rate <= 0:
            raise ValueError("rate 必须大于 0")
        self.rate = float(rate)
        self.burst = max(1.0, float(burst))
        self._interval = 1.0 / self.rate
        self._lock = threading.Lock()
        self._next_at = time.monotonic()
        self._acquired = 0

    def acquire(self) -> None:
        """取一个令牌，必要时睡眠等待（在锁外睡眠，不阻塞其他线程）。"""
        while True:
            with self._lock:
                now = time.monotonic()
                if self._next_at <= now:
                    # 桶里还有令牌（或已回填）：立即放行，并把下次放行时间往后推
                    self._next_at = max(self._next_at, now) + self._interval
                    self._acquired += 1
                    return
                wait = self._next_at - now
            time.sleep(min(wait, 0.05))

    @property
    def acquired(self) -> int:
        return self._acquired

    def stats(self, started_at: float) -> str:
        elapsed = max(1e-6, time.monotonic() - started_at)
        return f"已发起 {self._acquired} 个连接，平均 {self._acquired / elapsed:.1f} 连接/秒"

    def __repr__(self) -> str:
        return f"RateLimiter({self.rate:g}/秒)"


def make_rate_limiter(rate: float | None) -> NullRateLimiter | RateLimiter:
    """rate 为 None 或 <= 0 时返回不限速的空实现。"""
    if rate is None or rate <= 0:
        return NullRateLimiter()
    return RateLimiter(rate)
