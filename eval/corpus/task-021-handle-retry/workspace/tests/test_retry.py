import pytest

from retry import call_with_retry


class Flaky:
    """Raises the queued exceptions in order, then returns 'ok'."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


def test_success_first_try():
    fn = Flaky()
    sleeps = Sleeps()
    assert call_with_retry(fn, sleep=sleeps) == "ok"
    assert fn.calls == 1
    assert sleeps == []


def test_success_after_retries():
    fn = Flaky(ConnectionError("a"), ConnectionError("b"))
    sleeps = Sleeps()
    assert call_with_retry(fn, attempts=3, sleep=sleeps) == "ok"
    assert fn.calls == 3


def test_exact_number_of_calls_when_all_fail():
    fn = Flaky(*[ConnectionError(str(i)) for i in range(10)])
    with pytest.raises(ConnectionError):
        call_with_retry(fn, attempts=4)
    assert fn.calls == 4


def test_last_exception_reraised_as_same_object():
    last = ConnectionError("third")
    fn = Flaky(ConnectionError("first"), ConnectionError("second"), last)
    with pytest.raises(ConnectionError) as info:
        call_with_retry(fn, attempts=3)
    assert info.value is last


def test_exponential_backoff_between_attempts_only():
    fn = Flaky(*[ConnectionError() for _ in range(10)])
    sleeps = Sleeps()
    with pytest.raises(ConnectionError):
        call_with_retry(fn, attempts=4, backoff=0.5, sleep=sleeps)
    assert sleeps == [0.5, 1.0, 2.0]


def test_backoff_on_eventual_success():
    fn = Flaky(TimeoutError(), TimeoutError())
    sleeps = Sleeps()
    assert call_with_retry(fn, attempts=5, retry_on=(TimeoutError,), backoff=1, sleep=sleeps) == "ok"
    assert sleeps == [1, 2]


def test_non_retryable_exception_propagates_immediately():
    fn = Flaky(KeyError("boom"), ConnectionError())
    sleeps = Sleeps()
    with pytest.raises(KeyError):
        call_with_retry(fn, attempts=5, sleep=sleeps)
    assert fn.calls == 1
    assert sleeps == []


def test_non_retryable_after_retryable():
    fn = Flaky(ConnectionError(), ValueError("bad"))
    sleeps = Sleeps()
    with pytest.raises(ValueError, match="bad"):
        call_with_retry(fn, attempts=5, sleep=sleeps)
    assert fn.calls == 2
    assert sleeps == [0.5]


def test_subclasses_of_retry_on_are_retried():
    fn = Flaky(ConnectionResetError(), ConnectionRefusedError())
    assert call_with_retry(fn, attempts=3) == "ok"


def test_multiple_retry_types():
    fn = Flaky(TimeoutError(), ConnectionError())
    assert call_with_retry(fn, attempts=3, retry_on=(TimeoutError, ConnectionError)) == "ok"


def test_single_attempt_never_sleeps():
    fn = Flaky(ConnectionError("only"))
    sleeps = Sleeps()
    with pytest.raises(ConnectionError, match="only"):
        call_with_retry(fn, attempts=1, sleep=sleeps)
    assert fn.calls == 1 and sleeps == []


@pytest.mark.parametrize("attempts", [0, -1])
def test_invalid_attempts(attempts):
    fn = Flaky()
    with pytest.raises(ValueError):
        call_with_retry(fn, attempts=attempts)
    assert fn.calls == 0


def test_keyboard_interrupt_not_swallowed():
    def fn():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        call_with_retry(fn, attempts=3, retry_on=(ConnectionError,))
