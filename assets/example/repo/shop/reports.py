"""Nightly reports for the shop app."""
from shop.cache import cache_get, cache_invalidate, cache_set


def sales_report(day):
    key = f"report:sales:{day}"
    rows = cache_get(key)
    if rows is None:
        rows = [("total", 0)]
        cache_set(key, rows, ttl=3600)
    return rows


def refresh_reports():
    """Called after the nightly import: drop every cached report."""
    return cache_invalidate("report:")
