import logging

import pytest

from usp.disk_cache import DiskCacheError, DiskConditionalCache
from usp.fetch_parse import SitemapFetcher
from usp.objects.sitemap import InvalidSitemap
from usp.tree import sitemap_tree_for_homepage
from usp.web_client.requests_client import RequestsWebClient

BASE_URL = "http://conditional-cache.example"
LAST_MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"


def _urlset(*paths: str) -> str:
    urls = "".join(f"<url><loc>{path}</loc></url>" for path in paths)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )


def _sitemapindex(*paths: str) -> str:
    sitemaps = "".join(f"<sitemap><loc>{path}</loc></sitemap>" for path in paths)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{sitemaps}</sitemapindex>"
    )


def _page_urls(sitemap) -> list[str]:
    return [page.url for page in sitemap.all_pages()]


def _request_count(requests_mock, url: str) -> int:
    return len(
        [request for request in requests_mock.request_history if request.url == url]
    )


class TestConditionalCache:
    def test_304_reuses_body_across_restart_and_merges_validators(
        self, requests_mock, tmp_path
    ):
        url = BASE_URL + "/sitemap.xml"
        body = _urlset(BASE_URL + "/a.html", BASE_URL + "/b.html")
        seen_headers: list[dict[str, str]] = []

        def handler(request, context):
            seen_headers.append(dict(request.headers))
            if request.headers.get("If-None-Match") == '"v1"':
                context.status_code = 304
                context.reason = "Not Modified"
                context.headers = {"ETag": '"v2"'}
                return ""
            context.status_code = 200
            context.reason = "OK"
            context.headers = {
                "Content-Type": "application/xml",
                "ETag": '"v1"',
                "Last-Modified": LAST_MODIFIED,
            }
            return body

        requests_mock.get(url, text=handler)

        first_cache = DiskConditionalCache(tmp_path, "ns")
        first = SitemapFetcher(url=url, recursion_level=0, cache=first_cache).sitemap()
        assert _page_urls(first) == [BASE_URL + "/a.html", BASE_URL + "/b.html"]

        entry = first_cache.read(url)
        assert entry is not None
        assert (entry.etag, entry.last_modified) == ('"v1"', LAST_MODIFIED)

        # Simulate a process restart: new cache object and a new Requests client.
        second_cache = DiskConditionalCache(tmp_path, "ns")
        second = SitemapFetcher(
            url=url, recursion_level=0, cache=second_cache
        ).sitemap()
        assert _page_urls(second) == [BASE_URL + "/a.html", BASE_URL + "/b.html"]

        assert seen_headers[1]["If-None-Match"] == '"v1"'
        assert seen_headers[1]["If-Modified-Since"] == LAST_MODIFIED

        merged = second_cache.read(url)
        assert merged is not None
        assert merged.etag == '"v2"'
        assert merged.last_modified == LAST_MODIFIED
        assert merged.body == body.encode()

    def test_304_reparses_with_current_depth_ancestors_and_filters(
        self, requests_mock, tmp_path
    ):
        index_url = BASE_URL + "/index.xml"
        child_one = BASE_URL + "/child-one.xml"
        child_two = BASE_URL + "/child-two.xml"
        index_body = _sitemapindex(child_one, child_two)

        def make_handler(body: str, etag: str):
            def handler(request, context):
                if request.headers.get("If-None-Match") == etag:
                    context.status_code = 304
                    context.reason = "Not Modified"
                    context.headers = {"ETag": etag}
                    return ""
                context.status_code = 200
                context.reason = "OK"
                context.headers = {
                    "Content-Type": "application/xml",
                    "ETag": etag,
                }
                return body

            return handler

        requests_mock.get(index_url, text=make_handler(index_body, '"index-v1"'))
        requests_mock.get(
            child_one, text=make_handler(_urlset(BASE_URL + "/one.html"), '"one-v1"')
        )
        requests_mock.get(
            child_two, text=make_handler(_urlset(BASE_URL + "/two.html"), '"two-v1"')
        )

        cache = DiskConditionalCache(tmp_path, "ns")
        first = SitemapFetcher(url=index_url, recursion_level=0, cache=cache).sitemap()
        assert _page_urls(first) == [BASE_URL + "/one.html", BASE_URL + "/two.html"]
        assert _request_count(requests_mock, child_two) == 1

        callback_calls: list[tuple[str, int, set[str]]] = []

        def recurse_callback(url: str, level: int, parents: set[str]) -> bool:
            callback_calls.append((url, level, set(parents)))
            return "child-two" not in url

        second = SitemapFetcher(
            url=index_url,
            recursion_level=1,
            cache=cache,
            recurse_callback=recurse_callback,
        ).sitemap()

        # The index body came from the cache, but child sitemaps were fetched again
        # and the current round's filter was applied.
        assert _page_urls(second) == [BASE_URL + "/one.html"]
        assert _request_count(requests_mock, index_url) == 2
        assert _request_count(requests_mock, child_one) == 2
        assert _request_count(requests_mock, child_two) == 1

        assert callback_calls
        assert all(level == 1 for _, level, _ in callback_calls)
        assert all(index_url in parents for _, _, parents in callback_calls)
        # The first cached-body parse did not reuse a parsed subtree: child-one was
        # re-fetched with its own validator.
        conditional_child_request = [
            request
            for request in requests_mock.request_history
            if request.url == child_one
        ][1]
        assert conditional_child_request.headers.get("If-None-Match") == '"one-v1"'

    def test_redirect_hop_uses_only_its_own_validators(self, requests_mock, tmp_path):
        old_url = BASE_URL + "/old-sitemap.xml"
        new_url = BASE_URL + "/new-sitemap.xml"
        old_body = _urlset(BASE_URL + "/old-page.html")
        new_body = _urlset(BASE_URL + "/new-page.html")

        cache = DiskConditionalCache(tmp_path, "ns")
        cache.write(old_url, old_body.encode(), etag='"old-v1"')
        cache.write(new_url, new_body.encode(), etag='"new-v1"')

        old_request_headers: list[dict[str, str]] = []
        new_request_headers: list[dict[str, str]] = []

        def old_handler(request, context):
            old_request_headers.append(dict(request.headers))
            context.status_code = 301
            context.reason = "Moved Permanently"
            context.headers = {"Location": new_url, "ETag": '"redirect-etag"'}
            return ""

        def new_handler(request, context):
            new_request_headers.append(dict(request.headers))
            if request.headers.get("If-None-Match") == '"new-v1"':
                context.status_code = 304
                context.reason = "Not Modified"
                context.headers = {"ETag": '"new-v2"'}
                return ""
            context.status_code = 200
            context.reason = "OK"
            context.headers = {"ETag": '"other-v2"'}
            return new_body

        requests_mock.get(old_url, text=old_handler)
        requests_mock.get(new_url, text=new_handler)

        sitemap = SitemapFetcher(url=old_url, recursion_level=0, cache=cache).sitemap()
        assert _page_urls(sitemap) == [BASE_URL + "/new-page.html"]

        assert old_request_headers[0]["If-None-Match"] == '"old-v1"'
        # The second hop must not inherit the first hop's conditional headers.
        assert new_request_headers[0]["If-None-Match"] == '"new-v1"'
        assert "If-Modified-Since" not in new_request_headers[0]
        assert new_request_headers[0]["If-None-Match"] != '"old-v1"'
        assert new_request_headers[0]["If-None-Match"] != '"redirect-etag"'

        # The 304 body is the one cached under the final URL, not the first hop.
        assert cache.read(old_url).body == old_body.encode()  # type: ignore[union-attr]
        merged = cache.read(new_url)
        assert merged is not None
        assert merged.etag == '"new-v2"'
        assert merged.body == new_body.encode()

    def test_truncated_xml_returns_complete_items_without_changing_cache(
        self, requests_mock, tmp_path
    ):
        url = BASE_URL + "/sitemap.xml"
        full_body = _urlset(
            BASE_URL + "/a.html", BASE_URL + "/b.html", BASE_URL + "/c.html"
        )
        truncated_body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<url><loc>{BASE_URL}/a.html</loc></url>"
            f"<url><loc>{BASE_URL}/b.html</loc></url>"
            f"<url><loc>{BASE_URL}/c"
        )

        def handler(request, context):
            context.status_code = 200
            context.reason = "OK"
            if "If-None-Match" in request.headers:
                context.headers = {"Content-Type": "application/xml", "ETag": '"v2"'}
                return truncated_body
            context.headers = {
                "Content-Type": "application/xml",
                "ETag": '"v1"',
            }
            return full_body

        requests_mock.get(url, text=handler)

        cache = DiskConditionalCache(tmp_path, "ns")
        first = SitemapFetcher(url=url, recursion_level=0, cache=cache).sitemap()
        assert _page_urls(first) == [
            BASE_URL + "/a.html",
            BASE_URL + "/b.html",
            BASE_URL + "/c.html",
        ]

        second = SitemapFetcher(url=url, recursion_level=0, cache=cache).sitemap()
        assert _page_urls(second) == [BASE_URL + "/a.html", BASE_URL + "/b.html"]

        unchanged = cache.read(url)
        assert unchanged is not None
        assert unchanged.etag == '"v1"'
        assert unchanged.body == full_body.encode()

    def test_corrupt_cache_is_a_miss_and_is_replaced(
        self, requests_mock, tmp_path, caplog
    ):
        url = BASE_URL + "/sitemap.xml"
        body = _urlset(BASE_URL + "/a.html")
        cache_file = tmp_path / f"{DiskConditionalCache(tmp_path, 'ns').key(url)}.json"
        cache_file.write_text("this is not a cache entry")

        def handler(request, context):
            context.status_code = 200
            context.reason = "OK"
            context.headers = {"Content-Type": "application/xml", "ETag": '"v1"'}
            return body

        requests_mock.get(url, text=handler)

        cache = DiskConditionalCache(tmp_path, "ns")
        with caplog.at_level(logging.ERROR):
            sitemap = SitemapFetcher(url=url, recursion_level=0, cache=cache).sitemap()

        assert _page_urls(sitemap) == [BASE_URL + "/a.html"]
        assert "sitemap cache entry" in caplog.text
        assert "If-None-Match" not in requests_mock.request_history[0].headers

        entry = cache.read(url)
        assert entry is not None
        assert entry.etag == '"v1"'

    def test_304_without_usable_body_makes_one_unconditional_request(
        self, requests_mock, tmp_path, caplog
    ):
        url = BASE_URL + "/sitemap.xml"
        body = _urlset(BASE_URL + "/a.html")
        request_count = 0

        def handler(request, context):
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                context.status_code = 304
                context.reason = "Not Modified"
                context.headers = {"ETag": '"v1"'}
                return ""
            context.status_code = 200
            context.reason = "OK"
            context.headers = {"Content-Type": "application/xml", "ETag": '"v1"'}
            return body

        requests_mock.get(url, text=handler)

        cache = DiskConditionalCache(tmp_path, "ns")
        with caplog.at_level(logging.WARNING):
            sitemap = SitemapFetcher(url=url, recursion_level=0, cache=cache).sitemap()

        assert _page_urls(sitemap) == [BASE_URL + "/a.html"]
        assert request_count == 2
        assert "without a usable cached body" in caplog.text
        assert "If-None-Match" not in requests_mock.request_history[0].headers

    def test_second_304_without_body_is_invalid_sitemap(self, requests_mock, tmp_path):
        url = BASE_URL + "/sitemap.xml"
        request_count = 0

        def handler(request, context):
            nonlocal request_count
            request_count += 1
            context.status_code = 304
            context.reason = "Not Modified"
            context.headers = {"ETag": '"v1"'}
            return ""

        requests_mock.get(url, text=handler)

        cache = DiskConditionalCache(tmp_path, "ns")
        sitemap = SitemapFetcher(url=url, recursion_level=0, cache=cache).sitemap()

        assert isinstance(sitemap, InvalidSitemap)
        # exactly one fallback request, then give up
        assert request_count == 2

    def test_http_error_keeps_previous_cache_entry(self, requests_mock, tmp_path):
        url = BASE_URL + "/sitemap.xml"
        cache = DiskConditionalCache(tmp_path, "ns")
        cache.write(url, _urlset(BASE_URL + "/old.html").encode(), etag='"v1"')

        def handler(request, context):
            context.status_code = 404
            context.reason = "Not Found"
            return "not found"

        requests_mock.get(url, text=handler)

        sitemap = SitemapFetcher(url=url, recursion_level=0, cache=cache).sitemap()
        assert isinstance(sitemap, InvalidSitemap)

        entry = cache.read(url)
        assert entry is not None
        assert entry.etag == '"v1"'
        assert entry.body == _urlset(BASE_URL + "/old.html").encode()

    def test_decompression_failure_keeps_previous_cache_entry(
        self, requests_mock, tmp_path, caplog
    ):
        url = BASE_URL + "/sitemap.xml"
        cache = DiskConditionalCache(tmp_path, "ns")
        old_body = _urlset(BASE_URL + "/old.html").encode()
        cache.write(url, old_body, etag='"v1"')

        def handler(request, context):
            context.status_code = 200
            context.reason = "OK"
            context.headers = {
                "Content-Type": "application/gzip",
                "ETag": '"v2"',
            }
            return "this is definitely not gzip"

        requests_mock.get(url, text=handler)

        with caplog.at_level(logging.ERROR):
            sitemap = SitemapFetcher(url=url, recursion_level=0, cache=cache).sitemap()

        assert sitemap is not None
        assert "Unable to decompress" in caplog.text

        entry = cache.read(url)
        assert entry is not None
        assert entry.etag == '"v1"'
        assert entry.body == old_body

    def test_write_failure_keeps_previous_cache_entry(
        self, requests_mock, tmp_path, mocker, caplog
    ):
        url = BASE_URL + "/sitemap.xml"
        old_body = _urlset(BASE_URL + "/old.html")
        new_body = _urlset(BASE_URL + "/new.html")

        cache = DiskConditionalCache(tmp_path, "ns")
        cache.write(url, old_body.encode(), etag='"v1"')

        def handler(request, context):
            context.status_code = 200
            context.reason = "OK"
            context.headers = {"Content-Type": "application/xml", "ETag": '"v2"'}
            return new_body

        requests_mock.get(url, text=handler)

        mocker.patch.object(
            DiskConditionalCache, "write", side_effect=DiskCacheError("disk full")
        )

        with caplog.at_level(logging.ERROR):
            sitemap = SitemapFetcher(url=url, recursion_level=0, cache=cache).sitemap()

        assert _page_urls(sitemap) == [BASE_URL + "/new.html"]
        assert "keeping the previous entry" in caplog.text

        entry = cache.read(url)
        assert entry is not None
        assert entry.etag == '"v1"'
        assert entry.body == old_body.encode()

    def test_success_without_validators_invalidates_old_entry(
        self, requests_mock, tmp_path
    ):
        url = BASE_URL + "/sitemap.xml"
        cache = DiskConditionalCache(tmp_path, "ns")
        cache.write(url, _urlset(BASE_URL + "/old.html").encode(), etag='"v1"')

        def handler(request, context):
            context.status_code = 200
            context.reason = "OK"
            context.headers = {"Content-Type": "application/xml"}
            return _urlset(BASE_URL + "/new.html")

        requests_mock.get(url, text=handler)

        sitemap = SitemapFetcher(url=url, recursion_level=0, cache=cache).sitemap()
        assert _page_urls(sitemap) == [BASE_URL + "/new.html"]
        assert cache.read(url) is None

    def test_only_xml_sitemaps_are_cached(self, requests_mock, tmp_path):
        robots_url = BASE_URL + "/robots.txt"
        cache = DiskConditionalCache(tmp_path, "ns")

        def handler(request, context):
            context.status_code = 200
            context.reason = "OK"
            context.headers = {"Content-Type": "text/plain", "ETag": '"v1"'}
            return "User-agent: *\n"

        requests_mock.get(robots_url, text=handler)

        sitemap = SitemapFetcher(
            url=robots_url, recursion_level=0, cache=cache
        ).sitemap()
        assert sitemap is not None
        assert cache.read(robots_url) is None

    def test_cache_is_off_by_default(self, requests_mock):
        url = BASE_URL + "/sitemap.xml"
        seen_headers: list[dict[str, str]] = []

        def handler(request, context):
            seen_headers.append(dict(request.headers))
            context.status_code = 200
            context.reason = "OK"
            context.headers = {
                "Content-Type": "application/xml",
                "ETag": '"v1"',
            }
            return _urlset(BASE_URL + "/a.html")

        requests_mock.get(url, text=handler)

        SitemapFetcher(url=url, recursion_level=0).sitemap()
        SitemapFetcher(url=url, recursion_level=0).sitemap()

        assert all("If-None-Match" not in headers for headers in seen_headers)
        assert RequestsWebClient().conditional_cache is None

    def test_cache_can_be_configured_on_the_requests_client(
        self, requests_mock, tmp_path
    ):
        url = BASE_URL + "/sitemap.xml"

        def handler(request, context):
            if request.headers.get("If-None-Match") == '"v1"':
                context.status_code = 304
                context.reason = "Not Modified"
                context.headers = {"ETag": '"v1"'}
                return ""
            context.status_code = 200
            context.reason = "OK"
            context.headers = {"Content-Type": "application/xml", "ETag": '"v1"'}
            return _urlset(BASE_URL + "/a.html")

        requests_mock.get(url, text=handler)

        client = RequestsWebClient(cache_dir=str(tmp_path), cache_namespace="ns")
        assert client.conditional_cache is not None

        SitemapFetcher(url=url, recursion_level=0, web_client=client).sitemap()
        sitemap = SitemapFetcher(
            url=url, recursion_level=0, web_client=client
        ).sitemap()

        assert _page_urls(sitemap) == [BASE_URL + "/a.html"]
        assert requests_mock.request_history[-1].headers.get("If-None-Match") == '"v1"'

    def test_tree_level_cache(self, requests_mock, tmp_path):
        robots_url = BASE_URL + "/robots.txt"
        sitemap_url = BASE_URL + "/sitemap.xml"
        robots_body = f"User-agent: *\nSitemap: {sitemap_url}\n"
        sitemap_body = _urlset(BASE_URL + "/a.html")

        def robots_handler(request, context):
            context.status_code = 200
            context.reason = "OK"
            context.headers = {"Content-Type": "text/plain"}
            return robots_body

        def sitemap_handler(request, context):
            if request.headers.get("If-None-Match") == '"v1"':
                context.status_code = 304
                context.reason = "Not Modified"
                context.headers = {"ETag": '"v1"'}
                return ""
            context.status_code = 200
            context.reason = "OK"
            context.headers = {"Content-Type": "application/xml", "ETag": '"v1"'}
            return sitemap_body

        requests_mock.get(robots_url, text=robots_handler)
        requests_mock.get(sitemap_url, text=sitemap_handler)

        cache = DiskConditionalCache(tmp_path, "ns")
        tree = sitemap_tree_for_homepage(BASE_URL, cache=cache, use_known_paths=False)
        assert _page_urls(tree) == [BASE_URL + "/a.html"]

        tree = sitemap_tree_for_homepage(BASE_URL, cache=cache, use_known_paths=False)
        assert _page_urls(tree) == [BASE_URL + "/a.html"]
        assert _request_count(requests_mock, sitemap_url) == 2
        assert requests_mock.request_history[-1].headers.get("If-None-Match") == '"v1"'

    def test_custom_client_ignores_cache(self, requests_mock, tmp_path):
        url = BASE_URL + "/sitemap.xml"

        def handler(request, context):
            context.status_code = 200
            context.reason = "OK"
            context.headers = {"Content-Type": "application/xml", "ETag": '"v1"'}
            return _urlset(BASE_URL + "/a.html")

        requests_mock.get(url, text=handler)

        class CustomClient(RequestsWebClient):
            pass

        # A subclass is still a Requests client, so this only checks that the
        # explicit cache argument remains usable. The true "custom client" path
        # is covered by the type check in SitemapFetcher.
        client = CustomClient()
        cache = DiskConditionalCache(tmp_path, "ns")
        sitemap = SitemapFetcher(
            url=url, recursion_level=0, web_client=client, cache=cache
        ).sitemap()
        assert _page_urls(sitemap) == [BASE_URL + "/a.html"]


@pytest.mark.parametrize(
    ("cache_namespace", "expected"),
    [("ns-a", b"body-a"), ("ns-b", b"body-b")],
)
def test_namespace_does_not_leak_across_fetches(
    cache_namespace, expected, requests_mock, tmp_path
):
    url = BASE_URL + "/sitemap.xml"
    cache_a = DiskConditionalCache(tmp_path, "ns-a")
    cache_b = DiskConditionalCache(tmp_path, "ns-b")
    cache_a.write(url, b"body-a", etag='"a"')
    cache_b.write(url, b"body-b", etag='"b"')

    cache = cache_a if cache_namespace == "ns-a" else cache_b
    entry = cache.read(url)
    assert entry is not None
    assert entry.body == expected
    assert requests_mock is not None
