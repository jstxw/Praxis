"""Retry a flaky callable."""


def call_with_retry(fn, attempts=3, retry_on=(ConnectionError,), backoff=0.5, sleep=None):
    """Call fn() until it succeeds, retrying on the given exception types.

    - attempts < 1 -> ValueError, fn is never called.
    - fn is called at most `attempts` times.
    - Only exceptions that are instances of `retry_on` are retried; anything
      else propagates immediately.
    - Between attempts (not after the last), sleep(backoff * 2**i) is called
      with i = 0, 1, 2, ...
    - When all attempts fail, the last exception is re-raised as-is.
    - sleep=None means "do not wait".
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    retry_on = tuple(retry_on)
    for i in range(attempts):
        try:
            return fn()
        except retry_on:
            if i == attempts - 1:
                raise
            if sleep is not None:
                sleep(backoff * 2**i)
