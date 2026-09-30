import asyncio

from app.auth.throttle import LoginThrottle


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_per_ip_limit_and_window_expiry():
    clock = FakeClock()
    throttle = LoginThrottle(clock=clock)
    for _ in range(4):
        throttle.record_failure("a")
    assert throttle.retry_after("a") is None
    throttle.record_failure("a")
    assert throttle.retry_after("a") == 900
    assert throttle.retry_after("b") is None
    clock.now += 600
    assert throttle.retry_after("a") == 300
    clock.now += 300
    assert throttle.retry_after("a") is None


def test_success_clears_ip_failures():
    throttle = LoginThrottle(clock=FakeClock())
    for _ in range(4):
        throttle.record_failure("a")
    throttle.record_success("a")
    throttle.record_failure("a")
    assert throttle.retry_after("a") is None


def test_global_delay_formula_and_cap():
    clock = FakeClock()
    throttle = LoginThrottle(clock=clock)
    for i in range(49):
        throttle.record_failure(f"ip{i}")
    assert throttle.delay_seconds() == 0.0  # the 50th failure is not delayed
    throttle.record_failure("ip49")
    assert throttle.delay_seconds() == 2.0  # the 51st is
    throttle.record_failure("x")
    assert throttle.delay_seconds() == 4.0
    for i in range(100):
        throttle.record_failure(f"y{i}")
    assert throttle.delay_seconds() == 30.0  # capped
    clock.now += 901
    assert throttle.delay_seconds() == 0.0  # window passed


def test_wait_uses_injected_sleep_and_defaults_to_asyncio():
    assert LoginThrottle().sleep is asyncio.sleep
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    throttle = LoginThrottle(clock=FakeClock(), sleep=fake_sleep)
    assert asyncio.run(throttle.wait()) == 0.0
    for i in range(51):
        throttle.record_failure(f"ip{i}")
    assert asyncio.run(throttle.wait()) == 4.0
    assert slept == [4.0]


def test_stale_ip_entries_are_swept():
    clock = FakeClock()
    throttle = LoginThrottle(clock=clock)
    for i in range(1100):
        throttle.record_failure(f"ip{i}")
    clock.now += 901
    throttle.record_failure("new")
    assert throttle._per_ip.keys() == {"new"}
