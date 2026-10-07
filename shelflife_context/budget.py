"""Wall-clock budget shared by everything a hook does in one turn."""

import time


class Budget:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())

    def allows(self, seconds):
        return self.remaining() >= seconds

    def timeout(self, cap):
        """A per-call timeout that never outlives the turn."""
        return max(0.0, min(cap, self.remaining()))


UNBOUNDED = Budget(10 ** 9)
