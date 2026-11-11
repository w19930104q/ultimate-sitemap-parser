"""Offline tests for per-tree-crawl response de-duplication.

When the same sub-sitemap URL is referenced from more than one branch of a
sitemap tree (diamond-shaped references, shared indexes), the URL must be
fetched -- including the whole client-error retry flow -- at most once during
a single :func:`usp.tree.sitemap_tree_for_homepage` crawl. Both the successful
response and the final failure are reused by the other branches.

Path-specific semantics must remain untouched: every branch keeps its own
recursion level, ancestor URL set and filter-callback decisions, so parsing
results from the first path must never be stamped onto another path, filtered
references must not trigger any download, and one failing branch must not
swallow the other, valid branches. A new tree crawl gets a fresh cache and
re-fetches everything.
"""

import textwrap
from collections import Counter
from unittest import mock

from tests.tree.base import TreeTestBase
from usp.objects.sitemap import (
    AbstractIndexSitemap,
    AbstractSitemap,
    IndexRobotsTxtSitemap,
    IndexWebsiteSitemap,
    IndexXMLSitemap,
    InvalidSitemap,
    PagesXMLSitemap,
)
from usp.tree import sitemap_tree_for_homepage
from usp.web_client.abstract_client import (
    AbstractWebClient,
    AbstractWebClientResponse,
    AbstractWebClientSuccessResponse,
    WebClientErrorResponse,
)


def _index_xml(*sub_urls: str) -> str:
    """Build a sitemap index document referencing the given sub-sitemap URLs."""
    sitemap_entries = "\n".join(
        f"                    <sitemap><loc>{url}</loc></sitemap>" for url in sub_urls
    )
    return textwrap.dedent(
        f"""
        <?xml version="1.0" encoding="UTF-8"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{sitemap_entries}
        </sitemapindex>
        """
    ).strip()


def _pages_xml(*page_urls: str) -> str:
    """Build a urlset document containing the given page URLs."""
    url_entries = "\n".join(
        f"                    <url><loc>{url}</loc></url>" for url in page_urls
    )
    return textwrap.dedent(
        f"""
        <?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{url_entries}
        </urlset>
        """
    ).strip()


def _request_counts(requests_mock) -> Counter:
    """Count how many times each URL was actually requested from the client."""
    return Counter(request.url for request in requests_mock.request_history)


def _indexes_by_url(tree: AbstractSitemap) -> dict[str, AbstractIndexSitemap]:
    """Map every index sitemap in the tree (by URL) to its node."""
    return {
        node.url: node
        for node in tree.all_sitemaps()
        if isinstance(node, AbstractIndexSitemap)
    }


def _nodes_by_url(tree: AbstractSitemap) -> dict[str, list[AbstractSitemap]]:
    """Group every sitemap node in the tree by URL."""
    nodes: dict[str, list[AbstractSitemap]] = {}
    for node in tree.all_sitemaps():
        nodes.setdefault(node.url, []).append(node)
    return nodes


class _CountingWebClient(AbstractWebClient):
    """Web client wrapper counting the raw ``get()`` calls per URL.

    Retries are performed by the library's retry helper, which calls
    :meth:`get` repeatedly, so the counter observes every HTTP attempt rather
    than only cache lookups.
    """

    def __init__(self, wrapped: AbstractWebClient):
        self._wrapped = wrapped
        self.get_counts: Counter = Counter()

    def set_max_response_data_length(
        self, max_response_data_length: int | None
    ) -> None:
        self._wrapped.set_max_response_data_length(max_response_data_length)

    def get(self, url: str) -> AbstractWebClientResponse:
        self.get_counts[url] += 1
        return self._wrapped.get(url)


class _ScriptedResponse(AbstractWebClientSuccessResponse):
    """In-memory successful response used by the scripted web client."""

    def __init__(self, url: str, data: str):
        self._url = url
        self._data = data.encode("utf-8")

    def status_code(self) -> int:
        return 200

    def status_message(self) -> str:
        return "OK"

    def header(self, case_insensitive_name: str) -> str | None:
        if case_insensitive_name.lower() == "content-type":
            return "application/xml"
        return None

    def raw_data(self) -> bytes:
        return self._data

    def url(self) -> str:
        return self._url


class _ScriptedWebClient(AbstractWebClient):
    """Minimal offline web client serving scripted documents and counting calls."""

    def __init__(self, documents: dict[str, str], errors: dict[str, str] | None = None):
        self._documents = documents
        self._errors = errors or {}
        self.get_counts: Counter = Counter()
        self._max_response_data_length = None

    def set_max_response_data_length(
        self, max_response_data_length: int | None
    ) -> None:
        self._max_response_data_length = max_response_data_length

    def get(self, url: str) -> AbstractWebClientResponse:
        self.get_counts[url] += 1
        if url in self._errors:
            # Non-retryable: the retry helper gives up after the first attempt.
            return WebClientErrorResponse(message=self._errors[url], retryable=False)
        if url not in self._documents:
            # Emulate the fallback 404 matcher used by the requests-mock tests.
            return WebClientErrorResponse(message="404 Not Found", retryable=False)
        return _ScriptedResponse(url=url, data=self._documents[url])


class TestSitemapFetchDedup(TreeTestBase):
    BASE = TreeTestBase.TEST_BASE_URL

    def _register_robots(self, requests_mock, *sitemap_urls: str):
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)
        robots_body = "\n".join(f"Sitemap: {url}" for url in sitemap_urls)
        requests_mock.get(
            self.BASE + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=robots_body,
        )

    def test_diamond_reference_fetched_once_tree_preserved(self, requests_mock):
        """A shared sub-sitemap is downloaded once but appears under both parents."""
        robots_url = self.BASE + "/robots.txt"
        left_index = self.BASE + "/index_left.xml"
        right_index = self.BASE + "/index_right.xml"
        shared_index = self.BASE + "/shared_index.xml"
        shared_pages = self.BASE + "/shared_pages.xml"

        self._register_robots(requests_mock, left_index, right_index)
        requests_mock.get(left_index, text=_index_xml(shared_index))
        requests_mock.get(right_index, text=_index_xml(shared_index))
        requests_mock.get(shared_index, text=_index_xml(shared_pages))
        requests_mock.get(shared_pages, text=_pages_xml(self.BASE + "/page_a.html"))

        tree = sitemap_tree_for_homepage(self.BASE)

        counts = _request_counts(requests_mock)
        # Every sitemap document is requested exactly once despite two paths
        for url in (
            robots_url,
            left_index,
            right_index,
            shared_index,
            shared_pages,
        ):
            assert counts[url] == 1, f"{url} requested {counts[url]} times"

        # The full reference tree is retained: both parents keep their own
        # child node for the shared URLs (distinct objects, same URL/type).
        indexes = _indexes_by_url(tree)
        assert set(indexes) == {
            robots_url,
            left_index,
            right_index,
            shared_index,
        }
        left_child = indexes[left_index].sub_sitemaps
        right_child = indexes[right_index].sub_sitemaps
        assert len(left_child) == 1
        assert len(right_child) == 1
        assert left_child[0] == right_child[0]
        assert left_child[0] is not right_child[0]
        assert isinstance(left_child[0], IndexXMLSitemap)

        shared_nodes = _nodes_by_url(tree)[shared_pages]
        assert len(shared_nodes) == 2
        assert all(isinstance(node, PagesXMLSitemap) for node in shared_nodes)

        # Pages are reachable through both branches (tree is not collapsed).
        page_urls = [page.url for page in tree.all_pages()]
        assert page_urls == [self.BASE + "/page_a.html"] * 2

    def test_shared_index_path_specific_recurse_callback(self, requests_mock):
        """A shared index allows different descendants on different paths."""
        robots_url = self.BASE + "/robots.txt"
        left_index = self.BASE + "/index_left.xml"
        right_index = self.BASE + "/index_right.xml"
        shared_index = self.BASE + "/shared_index.xml"
        child_allowed = self.BASE + "/child_allowed.xml"
        child_blocked = self.BASE + "/child_blocked.xml"

        self._register_robots(requests_mock, left_index, right_index)
        requests_mock.get(left_index, text=_index_xml(shared_index))
        requests_mock.get(right_index, text=_index_xml(shared_index))
        requests_mock.get(shared_index, text=_index_xml(child_allowed, child_blocked))
        requests_mock.get(child_allowed, text=_pages_xml(self.BASE + "/allowed.html"))
        requests_mock.get(child_blocked, text=_pages_xml(self.BASE + "/blocked.html"))

        seen_calls: list[tuple[str, int, tuple[str, ...]]] = []

        def recurse_callback(url: str, recursion_level: int, parent_urls: set[str]):
            seen_calls.append((url, recursion_level, tuple(sorted(parent_urls))))
            if url == child_blocked:
                # Blocked only when reached through the right-hand index.
                return right_index not in parent_urls
            return True

        tree = sitemap_tree_for_homepage(self.BASE, recurse_callback=recurse_callback)

        counts = _request_counts(requests_mock)
        # The shared index itself is still downloaded only once.
        assert counts[shared_index] == 1
        assert counts[robots_url] == 1
        # The filtered reference is downloaded only on the path that allows it
        # (once), and never on the path whose callback filters it out.
        assert counts[child_blocked] == 1
        assert counts[child_allowed] == 1

        indexes = _indexes_by_url(tree)
        left_shared = indexes[left_index].sub_sitemaps[0]
        right_shared = indexes[right_index].sub_sitemaps[0]
        # Same URL, independently parsed nodes with path-specific children.
        assert left_shared is not right_shared
        assert [node.url for node in left_shared.sub_sitemaps] == [
            child_allowed,
            child_blocked,
        ]
        assert [node.url for node in right_shared.sub_sitemaps] == [child_allowed]
        assert isinstance(right_shared.sub_sitemaps[0], PagesXMLSitemap)

        # The callback was evaluated per path with that path's level and
        # ancestor set, never decided once and stamped onto both branches.
        blocked_calls = [call for call in seen_calls if call[0] == child_blocked]
        assert len(blocked_calls) == 2
        for _, level, ancestors in blocked_calls:
            assert level == 2
            assert shared_index in ancestors
        assert {right_index in ancestors for _, _, ancestors in blocked_calls} == {
            False,
            True,
        }

        # Only the left-hand branch exposes the blocked page.
        assert {page.url for page in tree.all_pages()} == {
            self.BASE + "/allowed.html",
            self.BASE + "/blocked.html",
        }

    def test_shared_index_path_specific_recurse_list_callback(self, requests_mock):
        """The list filter is also applied independently per path to a shared index."""
        left_index = self.BASE + "/index_left.xml"
        right_index = self.BASE + "/index_right.xml"
        shared_index = self.BASE + "/shared_index.xml"
        child_allowed = self.BASE + "/child_allowed.xml"
        child_blocked = self.BASE + "/child_blocked.xml"

        self._register_robots(requests_mock, left_index, right_index)
        requests_mock.get(left_index, text=_index_xml(shared_index))
        requests_mock.get(right_index, text=_index_xml(shared_index))
        requests_mock.get(shared_index, text=_index_xml(child_allowed, child_blocked))
        requests_mock.get(child_allowed, text=_pages_xml(self.BASE + "/a.html"))
        requests_mock.get(child_blocked, text=_pages_xml(self.BASE + "/b.html"))

        list_call_paths: list[tuple[str, ...]] = []

        def recurse_list_callback(
            urls: list[str], recursion_level: int, parent_urls: set[str]
        ) -> list[str]:
            # Only inspect the calls made while parsing the shared index.
            if shared_index in parent_urls:
                list_call_paths.append(tuple(sorted(parent_urls)))
            if right_index in parent_urls:
                return [url for url in urls if url != child_blocked]
            return urls

        tree = sitemap_tree_for_homepage(
            self.BASE, recurse_list_callback=recurse_list_callback
        )

        counts = _request_counts(requests_mock)
        assert counts[shared_index] == 1
        # Downloaded only through the path whose list filter keeps it.
        assert counts[child_blocked] == 1

        indexes = _indexes_by_url(tree)
        left_shared = indexes[left_index].sub_sitemaps[0]
        right_shared = indexes[right_index].sub_sitemaps[0]
        assert [node.url for node in left_shared.sub_sitemaps] == [
            child_allowed,
            child_blocked,
        ]
        assert [node.url for node in right_shared.sub_sitemaps] == [child_allowed]
        # The list callback parsed the shared index once per path, each time
        # with that path's own ancestor set.
        assert len(list_call_paths) == 2
        assert len(set(list_call_paths)) == 2

    def test_recursion_level_still_path_specific(self, requests_mock):
        """The shared response is re-parsed with each path's recursion level."""
        shallow_index = self.BASE + "/index_shallow.xml"
        deep_index = self.BASE + "/index_deep.xml"
        shared_index = self.BASE + "/shared_index.xml"
        shared_pages = self.BASE + "/shared_pages.xml"

        self._register_robots(requests_mock, shallow_index, deep_index)
        requests_mock.get(shallow_index, text=_index_xml(shared_index))
        requests_mock.get(deep_index, text=_index_xml(shared_index))
        requests_mock.get(shared_index, text=_index_xml(shared_pages))
        requests_mock.get(shared_pages, text=_pages_xml(self.BASE + "/p.html"))

        levels_seen: list[tuple[str, int]] = []

        def recurse_callback(url: str, recursion_level: int, parent_urls: set[str]):
            levels_seen.append((url, recursion_level))
            return True

        tree = sitemap_tree_for_homepage(self.BASE, recurse_callback=recurse_callback)

        counts = _request_counts(requests_mock)
        assert counts[shared_index] == 1
        assert counts[shared_pages] == 1

        # Both paths evaluate the shared URL, each at its own depth.
        shared_index_levels = {
            level for url, level in levels_seen if url == shared_index
        }
        assert shared_index_levels == {1}
        shared_pages_levels = {
            level for url, level in levels_seen if url == shared_pages
        }
        assert shared_pages_levels == {2}

        indexes = _indexes_by_url(tree)
        shallow_shared = indexes[shallow_index].sub_sitemaps[0]
        deep_shared = indexes[deep_index].sub_sitemaps[0]
        assert shallow_shared is not deep_shared
        assert shallow_shared == deep_shared

    def test_self_loop_through_shared_diamond(self, requests_mock):
        """A shared index pointing at itself fails per path without extra downloads."""
        robots_url = self.BASE + "/robots.txt"
        left_index = self.BASE + "/index_left.xml"
        right_index = self.BASE + "/index_right.xml"
        shared_index = self.BASE + "/shared_index.xml"

        self._register_robots(requests_mock, left_index, right_index)
        requests_mock.get(left_index, text=_index_xml(shared_index))
        requests_mock.get(right_index, text=_index_xml(shared_index))
        # The shared index references itself.
        requests_mock.get(shared_index, text=_index_xml(shared_index))

        tree = sitemap_tree_for_homepage(self.BASE)

        counts = _request_counts(requests_mock)
        assert counts[robots_url] == 1
        assert counts[shared_index] == 1

        # Each branch gets its own InvalidSitemap node; valid nodes survive.
        shared_invalid_nodes = [
            node
            for node in _nodes_by_url(tree).get(shared_index, [])
            if isinstance(node, InvalidSitemap)
        ]
        assert len(shared_invalid_nodes) == 2
        assert all(
            f"Recursion detected in URL {shared_index}" in node.reason
            for node in shared_invalid_nodes
        )

        indexes = _indexes_by_url(tree)
        assert isinstance(indexes[left_index], IndexXMLSitemap)
        assert isinstance(indexes[right_index], IndexXMLSitemap)
        assert isinstance(tree, IndexWebsiteSitemap)
        robots_index = tree.sub_sitemaps[0]
        assert isinstance(robots_index, IndexRobotsTxtSitemap)

    def test_redirect_back_to_ancestor_reuses_response(self, requests_mock):
        """A redirect to an ancestor URL is path-detected while fetching once."""
        robots_url = self.BASE + "/robots.txt"
        left_index = self.BASE + "/index_left.xml"
        right_index = self.BASE + "/index_right.xml"
        shared_index = self.BASE + "/shared_index.xml"
        redirecting_sitemap = self.BASE + "/redirecting.xml"

        self._register_robots(requests_mock, left_index, right_index)
        requests_mock.get(left_index, text=_index_xml(shared_index))
        requests_mock.get(right_index, text=_index_xml(shared_index))
        requests_mock.get(shared_index, text=_index_xml(redirecting_sitemap))
        # Both paths end at a response whose final URL is the shared ancestor.
        requests_mock.get(
            redirecting_sitemap,
            status_code=301,
            headers={"Location": shared_index},
        )

        tree = sitemap_tree_for_homepage(self.BASE)

        counts = _request_counts(requests_mock)
        # The dedup guarantee is on the requested URL: redirecting.xml goes
        # through the client (and thus the whole redirect chain) exactly once.
        # The extra GET of shared_index is the HTTP client following the 301
        # inside that single call, not a second usp-level fetch.
        assert counts[robots_url] == 1
        assert counts[redirecting_sitemap] == 1
        assert counts[shared_index] == 2

        invalid_nodes = [
            node
            for node in _nodes_by_url(tree).get(redirecting_sitemap, [])
            if isinstance(node, InvalidSitemap)
        ]
        assert len(invalid_nodes) == 2
        assert all(
            "Recursion detected" in node.reason and shared_index in node.reason
            for node in invalid_nodes
        )

        # The shared index node itself remains a valid node on both paths.
        shared_nodes = _nodes_by_url(tree)[shared_index]
        assert len(shared_nodes) == 2
        assert all(isinstance(node, IndexXMLSitemap) for node in shared_nodes)

    def test_failed_fetch_cached_but_other_branches_survive(self, requests_mock):
        """A final failure is reused too, without retrying or breaking siblings."""
        robots_url = self.BASE + "/robots.txt"
        left_index = self.BASE + "/index_left.xml"
        right_index = self.BASE + "/index_right.xml"
        broken_sitemap = self.BASE + "/broken.xml"
        healthy_sitemap = self.BASE + "/healthy.xml"

        self._register_robots(requests_mock, left_index, right_index)
        requests_mock.get(left_index, text=_index_xml(broken_sitemap, healthy_sitemap))
        requests_mock.get(right_index, text=_index_xml(broken_sitemap, healthy_sitemap))
        requests_mock.get(
            broken_sitemap,
            status_code=503,
            reason="Service Unavailable",
            text="unavailable",
        )
        requests_mock.get(healthy_sitemap, text=_pages_xml(self.BASE + "/healthy.html"))

        # Speed up the retry flow; the URL still goes through all retry rounds.
        with mock.patch("usp.helpers.time.sleep"):
            tree = sitemap_tree_for_homepage(self.BASE)

        counts = _request_counts(requests_mock)
        # Five attempts (default retry flow) in total, not five per branch.
        assert counts[robots_url] == 1
        assert counts[broken_sitemap] == 5
        assert counts[healthy_sitemap] == 1

        broken_nodes = _nodes_by_url(tree)[broken_sitemap]
        assert len(broken_nodes) == 2
        assert all(isinstance(node, InvalidSitemap) for node in broken_nodes)
        assert all("503" in node.reason for node in broken_nodes)

        healthy_nodes = _nodes_by_url(tree)[healthy_sitemap]
        assert len(healthy_nodes) == 2
        assert all(isinstance(node, PagesXMLSitemap) for node in healthy_nodes)
        # Sibling branches are not swallowed by the failure.
        assert [page.url for page in tree.all_pages()] == [
            self.BASE + "/healthy.html"
        ] * 2

    def test_repeated_runs_are_isolated(self, requests_mock):
        """Each tree crawl creates a fresh cache and re-fetches shared URLs."""
        robots_url = self.BASE + "/robots.txt"
        left_index = self.BASE + "/index_left.xml"
        right_index = self.BASE + "/index_right.xml"
        shared_pages = self.BASE + "/shared_pages.xml"

        self._register_robots(requests_mock, left_index, right_index)
        requests_mock.get(left_index, text=_index_xml(shared_pages))
        requests_mock.get(right_index, text=_index_xml(shared_pages))
        requests_mock.get(shared_pages, text=_pages_xml(self.BASE + "/page.html"))

        first_tree = sitemap_tree_for_homepage(self.BASE)
        counts_after_first = _request_counts(requests_mock)
        assert counts_after_first[shared_pages] == 1
        assert len(_nodes_by_url(first_tree)[shared_pages]) == 2

        second_tree = sitemap_tree_for_homepage(self.BASE)
        counts_after_second = _request_counts(requests_mock)
        # The second crawl re-fetched everything, including the shared URL.
        assert counts_after_second[robots_url] == 2
        assert counts_after_second[left_index] == 2
        assert counts_after_second[right_index] == 2
        assert counts_after_second[shared_pages] == 2

        # Both trees are independently usable with the same full structure.
        for tree in (first_tree, second_tree):
            assert len(_nodes_by_url(tree)[shared_pages]) == 2
            assert [page.url for page in tree.all_pages()] == [
                self.BASE + "/page.html"
            ] * 2

    def test_custom_web_client_dedup_and_isolation(self):
        """Deduplication works with a custom AbstractWebClient implementation."""
        base = "http://custom-client.example.org"
        robots_url = base + "/robots.txt"
        left_index = base + "/index_left.xml"
        right_index = base + "/index_right.xml"
        shared_pages = base + "/shared_pages.xml"

        documents = {
            robots_url: f"Sitemap: {left_index}\nSitemap: {right_index}\n",
            left_index: _index_xml(shared_pages),
            right_index: _index_xml(shared_pages),
            shared_pages: _pages_xml(base + "/page.html"),
        }
        client = _ScriptedWebClient(documents=documents)

        first_tree = sitemap_tree_for_homepage(base, web_client=client)
        assert client.get_counts[shared_pages] == 1
        assert client.get_counts[left_index] == 1
        assert client.get_counts[right_index] == 1
        assert len(_nodes_by_url(first_tree)[shared_pages]) == 2

        second_tree = sitemap_tree_for_homepage(base, web_client=client)
        # Fresh crawl, fresh cache: every URL is requested once more.
        assert client.get_counts[shared_pages] == 2
        assert client.get_counts[robots_url] == 2
        assert len(_nodes_by_url(second_tree)[shared_pages]) == 2

    def test_custom_web_client_failure_shared_across_branches(self):
        """A final failure from a custom client is reused without retries on branches."""
        base = "http://custom-client-fail.example.org"
        robots_url = base + "/robots.txt"
        left_index = base + "/index_left.xml"
        right_index = base + "/index_right.xml"
        broken_sitemap = base + "/broken.xml"

        documents = {
            robots_url: f"Sitemap: {left_index}\nSitemap: {right_index}\n",
            left_index: _index_xml(broken_sitemap),
            right_index: _index_xml(broken_sitemap),
        }
        client = _ScriptedWebClient(
            documents=documents, errors={broken_sitemap: "404 Not Found"}
        )

        tree = sitemap_tree_for_homepage(base, web_client=client)

        # Non-retryable error: one client call in total, shared by both branches.
        assert client.get_counts[broken_sitemap] == 1
        broken_nodes = _nodes_by_url(tree)[broken_sitemap]
        assert len(broken_nodes) == 2
        assert all(isinstance(node, InvalidSitemap) for node in broken_nodes)
        assert all("404 Not Found" in node.reason for node in broken_nodes)

    def test_default_web_client_wrapper_counts_single_fetch(self):
        """End-to-end retry flow runs once for a shared retryable error response."""
        base = "http://retry-flow.example.org"
        robots_url = base + "/robots.txt"
        left_index = base + "/index_left.xml"
        right_index = base + "/index_right.xml"
        flaky_sitemap = base + "/flaky.xml"
        healthy_sitemap = base + "/healthy.xml"

        import requests_mock as rq_mock

        with rq_mock.Mocker() as requests_mock:
            requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)
            requests_mock.get(
                robots_url,
                headers={"Content-Type": "text/plain"},
                text=f"Sitemap: {left_index}\nSitemap: {right_index}\n",
            )
            requests_mock.get(left_index, text=_index_xml(flaky_sitemap))
            requests_mock.get(
                right_index, text=_index_xml(flaky_sitemap, healthy_sitemap)
            )
            requests_mock.get(
                flaky_sitemap,
                status_code=500,
                reason="Internal Server Error",
                text="error",
            )
            requests_mock.get(healthy_sitemap, text=_pages_xml(base + "/healthy.html"))

            from usp.web_client.requests_client import RequestsWebClient

            counting_client = _CountingWebClient(RequestsWebClient())
            with mock.patch("usp.helpers.time.sleep"):
                tree = sitemap_tree_for_homepage(base, web_client=counting_client)

        # The complete retry flow (5 attempts) happens once for the shared URL.
        assert counting_client.get_counts[flaky_sitemap] == 5
        assert counting_client.get_counts[healthy_sitemap] == 1

        flaky_nodes = _nodes_by_url(tree)[flaky_sitemap]
        assert len(flaky_nodes) == 2
        assert all(isinstance(node, InvalidSitemap) for node in flaky_nodes)
        healthy_nodes = _nodes_by_url(tree)[healthy_sitemap]
        assert len(healthy_nodes) == 1
        assert isinstance(healthy_nodes[0], PagesXMLSitemap)
