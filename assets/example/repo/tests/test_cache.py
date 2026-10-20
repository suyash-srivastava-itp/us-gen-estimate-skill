import os

os.environ["CACHE_ENABLED"] = "1"

from shop import cache  # noqa: E402


def test_set_then_get():
    assert cache.cache_set("a", 1) is True
    assert cache.cache_get("a") == 1


def test_invalidate_by_prefix():
    cache.cache_set("report:1", 1)
    cache.cache_set("other:1", 2)
    assert cache.cache_invalidate("report:") == 1
    assert cache.cache_get("other:1") == 2
