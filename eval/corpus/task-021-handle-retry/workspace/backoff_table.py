"""Pure helper that previews a backoff schedule (unrelated to retry logic)."""


def schedule(base: float, factor: float, steps: int, cap: float | None = None) -> list[float]:
    delays = []
    delay = base
    for _ in range(steps):
        delays.append(min(delay, cap) if cap is not None else delay)
        delay *= factor
    return delays
