"""HTTP handlers for the shop app."""
from shop.cache import cache_get, cache_set


def product_page(product_id):
    key = f"product:{product_id}"
    page = cache_get(key)
    if page is None:
        page = f"<h1>Product {product_id}</h1>"
        cache_set(key, page, ttl=60)
    return page


def search_page(query):
    key = f"search:{query}"
    page = cache_get(key)
    if page is None:
        page = f"<h1>Results for {query}</h1>"
        cache_set(key, page)
    return page
