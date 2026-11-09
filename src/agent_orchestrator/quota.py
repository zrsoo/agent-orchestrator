"""Admission uses fresh provider readings; elapsed resets alone never grant admission."""
import time
from .backends.base import Quota


DEFAULT_POLL_SECONDS = 60


def admission(quota: Quota):
    return quota.allowed and all(b.remaining is None or (b.remaining > 0 and b.remaining >= b.minimum)
                                 for b in quota.buckets)


def wake_delay(quota: Quota, now=None):
    now = time.time() if now is None else now
    resets = [b.resets_at for b in quota.buckets if b.resets_at and b.resets_at > now
              and b.remaining is not None and (b.remaining <= 0 or b.remaining < b.minimum)]
    return min(DEFAULT_POLL_SECONDS, max(.1, min(resets) - now)) if resets else DEFAULT_POLL_SECONDS
