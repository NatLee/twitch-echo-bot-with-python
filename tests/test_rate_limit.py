"""STEP 8: RateLimiter, with a fake clock so the tests run instantly."""

from twitch_chat import DEFAULT_RATE_LIMITS, RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def limiter(rules, clock):
    return RateLimiter(rules, clock=clock, sleep=clock.sleep)


def test_first_messages_go_out_at_once():
    clock = FakeClock()
    limit = limiter([(3, 10.0)], clock)

    assert [limit.wait() for _ in range(3)] == [0.0, 0.0, 0.0]


def test_one_per_second():
    clock = FakeClock()
    limit = limiter([(1, 1.0)], clock)

    limit.wait()
    clock.now += 0.25

    assert limit.wait() == 0.75


def test_twitch_defaults_twenty_one_messages_take_thirty_seconds():
    # DEFAULT_RATE_LIMITS = 20 per 30 s and 1 per s (Twitch's numbers for a normal bot).
    # Sends 1-20 go out one second apart (t = 0..19). Send 21 must wait until
    # send 1 is 30 s old, so it leaves at t = 30 after an 11 s wait.
    clock = FakeClock()
    limit = limiter(DEFAULT_RATE_LIMITS, clock)

    for _ in range(21):
        limit.wait()

    assert clock.slept == [1.0] * 19 + [11.0]
    assert clock.now == 30.0


def test_messages_spread_out_never_wait():
    clock = FakeClock()
    limit = limiter(DEFAULT_RATE_LIMITS, clock)

    for _ in range(50):
        assert limit.wait() == 0.0
        clock.now += 2.0  # slower than 20 per 30 s


def test_no_rules_means_no_limit():
    clock = FakeClock()
    limit = limiter([], clock)

    assert sum(limit.wait() for _ in range(100)) == 0.0
