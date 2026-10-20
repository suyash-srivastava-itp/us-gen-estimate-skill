"""Tiny in-process cache used by the shop app.

If CACHE_ENABLED is not "1" every operation is a no-op, so the app still works (just slower).
"""
import os
import time

_ENABLED = os.getenv("CACHE_ENABLED", "") == "1"
_STORE = {}


def cache_get(key):
    """Return the cached value, or None on a miss or when the cache is disabled."""
    if not _ENABLED:
        return None
    hit = _STORE.get(key)
    if hit is None or hit[1] < time.time():
        return None
    return hit[0]


def cache_set(key, value, ttl=300):
    """Store a value for ttl seconds. Returns False when the cache is disabled."""
    if not _ENABLED:
        return False
    _STORE[key] = (value, time.time() + ttl)
    return True


def cache_invalidate(prefix):
    """Remove every key that starts with prefix. Returns how many were removed."""
    doomed = [k for k in _STORE if k.startswith(prefix)]
    for k in doomed:
        del _STORE[k]
    return len(doomed)
