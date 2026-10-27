"""One thread-safe request/deadline counter shared by transports and all retries."""

import threading
import time

from stock_analyser.massive import MassiveError


class QualificationStop(MassiveError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class RequestBudget:
    def __init__(self, ceilings, overall, seconds, *, clock=time.monotonic):
        if (
            not ceilings
            or any(type(n) is not int or n < 0 for n in ceilings.values())
            or type(overall) is not int
            or overall < 0
            or not 0 < seconds <= 3600
        ):
            raise ValueError("Invalid qualification bounds")
        self.ceilings, self.overall = dict(ceilings), overall
        self.counts = dict.fromkeys(ceilings, 0)
        self.clock, self.started, self.seconds = clock, clock(), seconds
        self.lock = threading.Lock()
        self.stop_reason = None
        self.retries = 0
        self.cooldown_seconds = 0.0

    def remaining(self):
        remaining = self.seconds - (self.clock() - self.started)
        if remaining <= 0:
            self.stop_reason = "DEADLINE"
            raise QualificationStop("DEADLINE")
        return remaining

    def dispatch(self, source):
        with self.lock:
            self.remaining()
            if (
                source not in self.counts
                or self.counts[source] >= self.ceilings[source]
                or sum(self.counts.values()) >= self.overall
            ):
                self.stop_reason = "REQUEST_LIMIT"
                raise QualificationStop("REQUEST_LIMIT")
            self.counts[source] += 1

    def record_retry(self):
        with self.lock:
            self.retries += 1

    def record_cooldown(self, seconds):
        # Total scheduled provider cooldown, not an assertion of time actually slept.
        with self.lock:
            self.cooldown_seconds += seconds

    def transport(self, source, transport):
        def bounded(target, key, timeout):
            self.dispatch(source)
            result = transport(target, key, min(timeout, self.remaining()))
            self.remaining()
            return result

        return bounded
