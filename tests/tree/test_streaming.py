"""Offline tests for the opt-in streaming sitemap parser."""

import gc
import os
import pickle
import textwrap
from decimal import Decimal

from tests.helpers import gzip
from tests.tree.base import TreeTestBase
from usp.fetch_parse import SitemapFetcher
from usp.objects.page import SitemapPage
from usp.objects.sitemap import (
    InvalidSitemap,
    PagesXMLSitemap,
)
from usp.streaming import DEFAULT_STREAM_CHUNK_SIZE
from usp.tree import sitemap_from_str, sitemap_tree_for_homepage
from usp.web_client.abstract_client import (
    AbstractWebClient,
    AbstractWebClientResponse,
    AbstractWebClientSuccessResponse,
)


def _sitemap_xml(base_url: str, bodies: str) -> str:
    return textwrap.dedent(
        f"""
        <?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        {bodies}
        </urlset>
        """
    ).strip()


def _page(url: str) -> str:
    return f"<url><loc>{url}</loc></url>"


class TestStreamingOutput(TreeTestBase):
    def _serve_tree(self, requests_mock):
        self.init_basic_sitemap(requests_mock)

    def test_output_equivalence_with_non_streaming(self, requests_mock):
        self._serve_tree(requests_mock)

        buffered_tree = sitemap_tree_for_homepage(self.TEST_BASE_URL)
        streaming_tree = sitemap_tree_for_homepage(self.TEST_BASE_URL, streaming=True)

        assert buffered_tree == streaming_tree
        assert list(buffered_tree.all_pages()) == list(streaming_tree.all_pages())
        assert list(buffered_tree.all_sitemaps()) == list(streaming_tree.all_sitemaps())
        assert buffered_tree.to_dict() == streaming_tree.to_dict()

        # The streaming tree stores its pages on disk, but exposes the same
        # public class and interfaces.
        sitemap = streaming_tree.sub_sitemaps[0].sub_sitemaps[0]
        assert isinstance(sitemap, PagesXMLSitemap)
        assert len(sitemap.pages) == 2

    def test_streaming_off_does_not_use_chunked_reads(self, requests_mock, mocker):
        self._serve_tree(requests_mock)

        called = []
        original = __import__(
            "usp.web_client.requests_client",
            fromlist=["RequestsWebClientSuccessResponse"],
        ).RequestsWebClientSuccessResponse.iter_content

        def _record(self, chunk_size=DEFAULT_STREAM_CHUNK_SIZE):
            called.append(chunk_size)
            return original(self, chunk_size=chunk_size)

        mocker.patch(
            "usp.web_client.requests_client.RequestsWebClientSuccessResponse.iter_content",
            _record,
        )

        tree = sitemap_tree_for_homepage(self.TEST_BASE_URL)
        list(tree.all_pages())
        assert called == []

    def test_index_children_are_streamed(self, requests_mock, mocker):
        self._serve_tree(requests_mock)

        chunk_sizes = []
        original = __import__(
            "usp.web_client.requests_client",
            fromlist=["RequestsWebClientSuccessResponse"],
        ).RequestsWebClientSuccessResponse.iter_content

        def _record(self, chunk_size=DEFAULT_STREAM_CHUNK_SIZE):
            chunk_sizes.append(chunk_size)
            return original(self, chunk_size=chunk_size)

        mocker.patch(
            "usp.web_client.requests_client.RequestsWebClientSuccessResponse.iter_content",
            _record,
        )

        tree = sitemap_tree_for_homepage(self.TEST_BASE_URL, streaming=True)
        list(tree.all_pages())

        # robots.txt, the two page-level URL sets and the index itself are all
        # read through the streaming interface. Chunks requested by the parser
        # are bounded.
        assert chunk_sizes
        assert all(size == DEFAULT_STREAM_CHUNK_SIZE for size in chunk_sizes)


class TestStreamingFormats(TreeTestBase):
    def test_gzip_file(self, requests_mock):
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)
        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=f"Sitemap: {self.TEST_BASE_URL}/sitemap.xml.gz",
        )
        xml = _sitemap_xml(
            self.TEST_BASE_URL,
            _page(f"{self.TEST_BASE_URL}/a.html"),
        )
        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap.xml.gz",
            headers={"Content-Type": "application/x-gzip"},
            content=gzip(xml),
        )

        tree = sitemap_tree_for_homepage(self.TEST_BASE_URL, streaming=True)
        assert [p.url for p in tree.all_pages()] == [f"{self.TEST_BASE_URL}/a.html"]

    def test_transport_gzip(self, requests_mock):
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)
        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=f"Sitemap: {self.TEST_BASE_URL}/sitemap.xml",
        )
        xml = _sitemap_xml(
            self.TEST_BASE_URL,
            _page(f"{self.TEST_BASE_URL}/a.html"),
        )
        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap.xml",
            headers={"Content-Type": "application/xml", "Content-Encoding": "gzip"},
            content=gzip(xml),
        )

        tree = sitemap_tree_for_homepage(self.TEST_BASE_URL, streaming=True)
        assert [p.url for p in tree.all_pages()] == [f"{self.TEST_BASE_URL}/a.html"]

    def test_mislabelled_gzip_is_parsed_as_plain(self, requests_mock, caplog):
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)
        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=f"Sitemap: {self.TEST_BASE_URL}/sitemap.xml.gz",
        )
        xml = _sitemap_xml(
            self.TEST_BASE_URL,
            _page(f"{self.TEST_BASE_URL}/a.html"),
        )
        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap.xml.gz",
            headers={"Content-Type": "application/x-gzip"},
            text=xml,
        )

        tree = sitemap_tree_for_homepage(self.TEST_BASE_URL, streaming=True)
        assert [p.url for p in tree.all_pages()] == [f"{self.TEST_BASE_URL}/a.html"]
        assert "Unable to gunzip response" in caplog.text

    def test_client_without_streaming_support_uses_buffered_path(self):
        xml = _sitemap_xml(
            "http://example.org",
            _page("http://example.org/a.html") + _page("http://example.org/b.html"),
        )

        class _BufferedResponse(AbstractWebClientSuccessResponse):
            closed = False

            def status_code(self) -> int:
                return 200

            def status_message(self) -> str:
                return "OK"

            def header(self, case_insensitive_name: str) -> str | None:
                if case_insensitive_name.lower() == "content-type":
                    return "application/xml"
                return None

            def raw_data(self) -> bytes:
                return xml.encode()

            def url(self) -> str:
                return "http://example.org/sitemap.xml"

            def iter_content(self, chunk_size=DEFAULT_STREAM_CHUNK_SIZE):
                raise AssertionError(
                    "Clients without streaming support must be buffered"
                )

            def close(self) -> None:
                self.closed = True

        class _BufferedClient(AbstractWebClient):
            def __init__(self):
                self.response = _BufferedResponse()

            def set_max_response_data_length(
                self, max_response_data_length: int | None
            ) -> None:
                pass

            def get(self, url: str) -> AbstractWebClientResponse:
                return self.response

        client = _BufferedClient()
        sitemap = SitemapFetcher(
            url="http://example.org/sitemap.xml",
            recursion_level=0,
            web_client=client,
            streaming=True,
        ).sitemap()

        assert isinstance(sitemap, PagesXMLSitemap)
        assert [page.url for page in sitemap.all_pages()] == [
            "http://example.org/a.html",
            "http://example.org/b.html",
        ]
        assert client.response.closed

    def test_sitemap_from_str_uses_buffered_path(self):
        xml = _sitemap_xml(
            "http://example.org",
            _page("http://example.org/a.html") + _page("http://example.org/b.html"),
        )

        sitemap = sitemap_from_str(xml, streaming=True)

        assert isinstance(sitemap, PagesXMLSitemap)
        assert [page.url for page in sitemap.all_pages()] == [
            "http://example.org/a.html",
            "http://example.org/b.html",
        ]

    def test_rss_atom_use_original_parser(self, requests_mock):
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)
        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=textwrap.dedent(
                f"""
                Sitemap: {self.TEST_BASE_URL}/sitemap_rss.xml
                Sitemap: {self.TEST_BASE_URL}/sitemap_atom.xml
                """
            ).strip(),
        )
        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap_rss.xml",
            headers={"Content-Type": "application/rss+xml"},
            text=textwrap.dedent(
                f"""
                <?xml version="1.0" encoding="UTF-8"?>
                <rss version="2.0"><channel>
                    <title>Feed</title>
                    <item>
                        <title>Story</title>
                        <link>{self.TEST_BASE_URL}/rss.html</link>
                        <pubDate>{self.TEST_DATE_STR_RFC2822}</pubDate>
                    </item>
                </channel></rss>
                """
            ).strip(),
        )
        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap_atom.xml",
            headers={"Content-Type": "application/atom+xml"},
            text=textwrap.dedent(
                f"""
                <?xml version="1.0" encoding="UTF-8"?>
                <feed xmlns="http://www.w3.org/2005/Atom">
                    <entry>
                        <title>Story</title>
                        <link rel="alternate" href="{self.TEST_BASE_URL}/atom.html"/>
                        <updated>{self.TEST_DATE_STR_ISO8601}</updated>
                    </entry>
                </feed>
                """
            ).strip(),
        )

        tree = sitemap_tree_for_homepage(self.TEST_BASE_URL, streaming=True)
        assert [p.url for p in tree.all_pages()] == [
            f"{self.TEST_BASE_URL}/rss.html",
            f"{self.TEST_BASE_URL}/atom.html",
        ]


class TestStreamingDedupAndTruncation(TreeTestBase):
    def _single_sitemap_tree(self, requests_mock, xml):
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)
        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=f"Sitemap: {self.TEST_BASE_URL}/sitemap.xml",
        )
        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap.xml",
            headers={"Content-Type": "application/xml"},
            text=xml.strip(),
        )
        return sitemap_tree_for_homepage(self.TEST_BASE_URL, streaming=True)

    def test_first_duplicate_wins_and_extension_fields_are_kept(self, requests_mock):
        bodies = textwrap.dedent(
            f"""
            <url>
                <loc>{self.TEST_BASE_URL}/page.html</loc>
                <priority>0.9</priority>
                <lastmod>{self.TEST_DATE_STR_ISO8601}</lastmod>
                <xhtml:link rel="alternate" hreflang="fr"
                            href="{self.TEST_BASE_URL}/fr/page.html"/>
            </url>
            <url>
                <loc>{self.TEST_BASE_URL}/page.html</loc>
                <priority>0.1</priority>
                <lastmod>2001-01-01T00:00:00Z</lastmod>
            </url>
            <url>
                <loc>{self.TEST_BASE_URL}/other.html</loc>
            </url>
            """
        )
        xml = textwrap.dedent(
            f"""
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                    xmlns:xhtml="http://www.w3.org/1999/xhtml">
            {bodies}
            </urlset>
            """
        ).strip()

        tree = self._single_sitemap_tree(requests_mock, xml)
        pages = list(tree.all_pages())

        assert len(pages) == 2
        assert pages[0].url == f"{self.TEST_BASE_URL}/page.html"
        assert pages[0].priority == Decimal("0.9")
        assert pages[0].alternates == [("fr", f"{self.TEST_BASE_URL}/fr/page.html")]

        buffered_tree = sitemap_tree_for_homepage(self.TEST_BASE_URL)
        assert list(buffered_tree.all_pages()) == pages

    def test_truncated_xml_keeps_complete_prefix(self, requests_mock):
        prefix = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            """
        )
        for index in range(49):
            prefix += _page(f"{self.TEST_BASE_URL}/page_{index}.html")
        prefix += f"<url><loc>{self.TEST_BASE_URL}/page_"

        tree = self._single_sitemap_tree(requests_mock, prefix)
        pages = list(tree.all_pages())

        assert len(pages) == 49
        assert pages[-1].url.endswith("page_48.html")

        # The buffered parser preserves exactly the same prefix.
        buffered_tree = sitemap_tree_for_homepage(self.TEST_BASE_URL)
        assert list(buffered_tree.all_pages()) == pages

    def test_truncated_mid_url_discards_incomplete_entry(self, requests_mock):
        xml = textwrap.dedent(
            f"""
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <url>
                    <loc>{self.TEST_BASE_URL}/first.html</loc>
                </url>
                <url>
                    <loc>{self.TEST_BASE_URL}/second
            """
        )

        tree = self._single_sitemap_tree(requests_mock, xml)
        assert [p.url for p in tree.all_pages()] == [f"{self.TEST_BASE_URL}/first.html"]

        buffered_tree = sitemap_tree_for_homepage(self.TEST_BASE_URL)
        assert list(buffered_tree.all_pages()) == list(tree.all_pages())

    def test_doctype_is_rejected(self, requests_mock):
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)
        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=f"Sitemap: {self.TEST_BASE_URL}/sitemap.xml",
        )
        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap.xml",
            headers={"Content-Type": "application/xml"},
            text=textwrap.dedent(
                f"""
                <?xml version="1.0" encoding="UTF-8"?>
                <!DOCTYPE lolz [<!ENTITY lol "lol">]>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                    <url><loc>{self.TEST_BASE_URL}/page.html</loc></url>
                </urlset>
                """
            ).strip(),
        )

        tree = sitemap_tree_for_homepage(self.TEST_BASE_URL, streaming=True)
        assert type(list(tree.all_sitemaps())[-1]) is InvalidSitemap

    def test_missing_loc_entries_are_deduplicated(self, requests_mock):
        xml = _sitemap_xml(
            self.TEST_BASE_URL,
            "<url><lastmod>2020-01-01</lastmod></url>"
            "<url><lastmod>2021-01-01</lastmod></url>",
        )

        tree = self._single_sitemap_tree(requests_mock, xml)
        assert list(tree.all_pages()) == []


class TestStreamingIterationAndCleanup(TreeTestBase):
    def _tree(self, requests_mock):
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)
        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=f"Sitemap: {self.TEST_BASE_URL}/sitemap.xml",
        )
        bodies = "".join(
            _page(f"{self.TEST_BASE_URL}/page_{index}.html") for index in range(10)
        )
        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap.xml",
            headers={"Content-Type": "application/xml"},
            text=_sitemap_xml(self.TEST_BASE_URL, bodies),
        )
        return sitemap_tree_for_homepage(self.TEST_BASE_URL, streaming=True)

    def test_all_pages_is_repeatable(self, requests_mock):
        tree = self._tree(requests_mock)
        expected = list(tree.all_pages())
        assert len(expected) == 10
        assert list(tree.all_pages()) == expected
        assert list(tree.all_pages())[:5] == expected[:5]

    def test_all_pages_iterators_are_isolated(self, requests_mock):
        tree = self._tree(requests_mock)
        expected = list(tree.all_pages())

        first = tree.all_pages()
        second = tree.all_pages()

        assert next(first) == expected[0]
        assert next(second) == expected[0]
        assert next(first) == expected[1]
        assert next(second) == expected[1]

        # Closing one reader must not disturb the other one.
        first.close()
        assert list(second) == expected[2:]
        assert list(tree.all_pages()) == expected

    def test_pages_to_dict_and_pickle_are_compatible(self, requests_mock):
        tree = self._tree(requests_mock)
        expected_pages = list(tree.all_pages())

        assert tree.to_dict()["sub_sitemaps"][0]["sub_sitemaps"][0]["pages"] == [
            page.to_dict() for page in expected_pages
        ]

        loaded = pickle.loads(pickle.dumps(tree))
        assert list(loaded.all_pages()) == expected_pages
        assert loaded.to_dict() == tree.to_dict()

    def test_temp_files_are_removed_on_release(self, requests_mock):
        tree = self._tree(requests_mock)
        pages_sitemap = tree.sub_sitemaps[0].sub_sitemaps[0]
        directory = pages_sitemap._AbstractPagesSitemap__streaming_store.directory
        page_path = pages_sitemap._AbstractPagesSitemap__pages_temp_file_path
        assert os.path.isdir(directory)
        assert os.path.exists(page_path)

        del pages_sitemap
        del tree
        gc.collect()

        assert not os.path.isdir(directory)
        assert not os.path.exists(page_path)

    def test_closed_readers_do_not_leak_file_handles(self, requests_mock):
        tree = self._tree(requests_mock)
        expected = list(tree.all_pages())
        for _ in range(5):
            iterator = tree.all_pages()
            assert next(iterator) == expected[0]
            iterator.close()
        assert list(tree.all_pages()) == expected


class _ChunkedFakeResponse(AbstractWebClientSuccessResponse):
    """In-memory streaming response used to exercise the parser offline."""

    def __init__(self, url: str, xml_parts: list[bytes], chunk_size: int):
        self._url = url
        self._xml_parts = xml_parts
        self._chunk_size = chunk_size
        self.closed = False
        self.raw_data_called = False
        self.requested_chunk_sizes: list[int] = []
        self.max_live_pages = 0
        self.baseline_live_pages = 0

    def status_code(self) -> int:
        return 200

    def status_message(self) -> str:
        return "OK"

    def header(self, case_insensitive_name: str) -> str | None:
        return None

    def raw_data(self) -> bytes:
        self.raw_data_called = True
        raise AssertionError("Streaming mode must not buffer raw_data")

    def url(self) -> str:
        return self._url

    def supports_streaming(self) -> bool:
        return True

    def iter_content(self, chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE):
        self.requested_chunk_sizes.append(chunk_size)
        self._chunk_size = chunk_size
        self.baseline_live_pages = _count_live_pages()
        for part in self._xml_parts:
            yield part
            # Runs after the parser has consumed the previous part.
            self.max_live_pages = max(
                self.max_live_pages, _count_live_pages() - self.baseline_live_pages
            )

    def close(self) -> None:
        self.closed = True


class _ChunkedFakeClient(AbstractWebClient):
    def __init__(self, response: _ChunkedFakeResponse):
        self.response = response

    def set_max_response_data_length(
        self, max_response_data_length: int | None
    ) -> None:
        pass

    def get(self, url: str) -> AbstractWebClientResponse:
        return self.response


def _count_live_pages() -> int:
    return sum(1 for obj in gc.get_objects() if isinstance(obj, SitemapPage))


class TestStreamingMemoryBound:
    def test_read_blocks_are_bounded_and_pages_are_not_retained(self):
        page_count = 5000
        chunks = [
            b'<?xml version="1.0" encoding="UTF-8"?>',
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        chunks.extend(
            f"<url><loc>http://example.org/page_{index}.html</loc></url>".encode()
            for index in range(page_count)
        )
        chunks.append(b"</urlset>")

        response = _ChunkedFakeResponse(
            url="http://example.org/sitemap.xml",
            xml_parts=chunks,
            chunk_size=DEFAULT_STREAM_CHUNK_SIZE,
        )
        client = _ChunkedFakeClient(response)

        gc.collect()
        baseline = _count_live_pages()
        response.baseline_live_pages = baseline

        sitemap = SitemapFetcher(
            url="http://example.org/sitemap.xml",
            recursion_level=0,
            web_client=client,
            streaming=True,
        ).sitemap()

        assert isinstance(sitemap, PagesXMLSitemap)
        assert response.closed
        assert not response.raw_data_called

        # The parser asks for bounded read blocks and processes them one at a
        # time.
        assert response.requested_chunk_sizes == [DEFAULT_STREAM_CHUNK_SIZE]
        assert all(len(part) <= DEFAULT_STREAM_CHUNK_SIZE for part in chunks)

        assert len(list(sitemap.all_pages())) == page_count

        # While parsing, no growing collection of live SitemapPage objects is
        # kept; at most the page currently being completed may be alive.
        assert response.max_live_pages <= 1

    def test_dedup_index_is_on_disk(self):
        page_count = 2000
        chunks = [b"<urlset>"]
        for index in range(page_count):
            # Half the entries are duplicates of the first one.
            url = (
                "http://example.org/duplicate.html"
                if index % 2
                else f"http://example.org/{index}.html"
            )
            chunks.append(f"<url><loc>{url}</loc></url>".encode())
        chunks.append(b"</urlset>")

        response = _ChunkedFakeResponse(
            url="http://example.org/sitemap.xml",
            xml_parts=chunks,
            chunk_size=DEFAULT_STREAM_CHUNK_SIZE,
        )
        client = _ChunkedFakeClient(response)

        sitemap = SitemapFetcher(
            url="http://example.org/sitemap.xml",
            recursion_level=0,
            web_client=client,
            streaming=True,
        ).sitemap()

        urls = [page.url for page in sitemap.all_pages()]
        assert len(urls) == page_count // 2 + 1
        assert urls.count("http://example.org/duplicate.html") == 1
        # Order of first occurrences is preserved.
        assert urls[0] == "http://example.org/0.html"
        assert urls[1] == "http://example.org/duplicate.html"
        assert urls[-1] == f"http://example.org/{page_count - 2}.html"

        store = sitemap._AbstractPagesSitemap__streaming_store
        assert os.path.getsize(store.page_path) > 0
        assert any(name.endswith(".sqlite3") for name in os.listdir(store.directory))

    def test_response_is_closed_when_streaming_raises(self):
        class _BrokenResponse(_ChunkedFakeResponse):
            def iter_content(self, chunk_size=DEFAULT_STREAM_CHUNK_SIZE):
                self.requested_chunk_sizes.append(chunk_size)
                self.closed = False
                yield b"<urlset>"
                raise RuntimeError("connection dropped")

        response = _BrokenResponse(
            url="http://example.org/sitemap.xml",
            xml_parts=[],
            chunk_size=DEFAULT_STREAM_CHUNK_SIZE,
        )
        client = _ChunkedFakeClient(response)

        # Like the non-streaming implementation, a failed read keeps whatever
        # prefix was parsed and does not propagate the transport error.
        sitemap = SitemapFetcher(
            url="http://example.org/sitemap.xml",
            recursion_level=0,
            web_client=client,
            streaming=True,
        ).sitemap()

        assert isinstance(sitemap, PagesXMLSitemap)
        assert list(sitemap.all_pages()) == []
        assert response.closed
