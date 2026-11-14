"""Kiosk state: ONLINE, DEGRADED or LOCKED.

StaticState is a fixed state for tests and for a pipeline that has no
state manager. It defaults to LOCKED: without a state manager the kiosk
cannot vouch for its data, so it never fails open.
"""
from .decision import LOCKED


class StaticState:
    def __init__(self, state=LOCKED, snapshot_version=None, reason="no state manager"):
        self.state = state
        self.snapshot_version = snapshot_version
        self.reason = reason

    def current(self, now=None):
        return self.state, self.snapshot_version

    def info(self, now=None):
        return {"state": self.state, "reason": self.reason,
                "snapshot_version": self.snapshot_version}
