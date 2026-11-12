"""Offline tests for relative URL resolution in XML sitemap indexes.

Covers the default base being the post-redirect document URL, nested
``xml:base`` in indexes, relative sub-sitemap recursion, index loop
protection on resolved absolute URLs, and unchanged robots/plain-text
semantics.
"""

import textwrap

from tests.tree.base import TreeTestBase
from usp.objects.sitemap import (
    IndexXMLSitemap,
    InvalidSitemap,
    PagesTextSitemap,
    PagesXMLSitemap,
)
from usp.tree import sitemap_from_str, sitemap_tree_for_homepage

BASE = "http://test_ultimate-sitemap-parser.com"


class TestRelativeIndexFromStr(TreeTestBase):
    def test_relative_sub_sitemap_loc_resolved_but_unfetched(self):
        """Relative <loc> entries resolve against base_url offline and end up
        as InvalidSitemap (the local parser cannot fetch them)."""
        content = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                          xml:base="https://example.com/indexes/">
                <sitemap><loc>nested/sitemap-1.xml</loc></sitemap>
                <sitemap><loc>/top/sitemap-2.xml</loc></sitemap>
                <sitemap><loc>https://other.example.net/sitemap-3.xml</loc></sitemap>
            </sitemapindex>
            """
        ).strip()

        parsed = sitemap_from_str(
            content,
            resolve_relative_urls=True,
            base_url=f"{BASE}/root-index.xml",
        )

        assert isinstance(parsed, IndexXMLSitemap)
        assert [sub.url for sub in parsed.sub_sitemaps] == [
            "https://example.com/indexes/nested/sitemap-1.xml",
            "https://example.com/top/sitemap-2.xml",
            "https://other.example.net/sitemap-3.xml",
        ]
        assert all(isinstance(sub, InvalidSitemap) for sub in parsed.sub_sitemaps)

    def test_non_http_sub_sitemap_loc_skipped(self, caplog):
        content = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <sitemap><loc>ftp://example.com/sitemap.xml</loc></sitemap>
                <sitemap><loc>sitemap.xml</loc></sitemap>
            </sitemapindex>
            """
        ).strip()

        parsed = sitemap_from_str(
            content,
            resolve_relative_urls=True,
            base_url=f"{BASE}/index.xml",
        )

        assert [sub.url for sub in parsed.sub_sitemaps] == [f"{BASE}/sitemap.xml"]
        assert "Sub-sitemap URL does not look like one" in caplog.text

    def test_relative_sub_sitemap_dedup(self):
        """Equivalent relative sub-sitemap addresses are deduplicated."""
        content = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <sitemap><loc>dir/../sitemap.xml</loc></sitemap>
                <sitemap><loc>./sitemap.xml</loc></sitemap>
                <sitemap><loc>http://test_ultimate-sitemap-parser.com/sitemap.xml</loc></sitemap>
            </sitemapindex>
            """
        ).strip()

        parsed = sitemap_from_str(
            content,
            resolve_relative_urls=True,
            base_url=f"{BASE}/index.xml",
        )
        assert [sub.url for sub in parsed.sub_sitemaps] == [f"{BASE}/sitemap.xml"]


class TestRelativeIndexFetched(TreeTestBase):
    def test_redirected_document_url_is_default_base(self, requests_mock):
        """The document URL after redirects becomes the default base URL."""
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)

        requests_mock.get(
            f"{BASE}/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=f"Sitemap: {BASE}/old/sitemap.xml",
        )

        # The index is fetched from /old/ but redirects to /new/ directory
        requests_mock.get(
            f"{BASE}/old/sitemap.xml",
            headers={"Location": f"{BASE}/new/sitemap.xml"},
            status_code=301,
        )
        requests_mock.get(
            f"{BASE}/new/sitemap.xml",
            headers={"Content-Type": "application/xml"},
            text=textwrap.dedent(
                """
                <?xml version="1.0" encoding="UTF-8"?>
                <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                    <sitemap><loc>pages.xml</loc></sitemap>
                </sitemapindex>
                """
            ).strip(),
        )
        requests_mock.get(
            f"{BASE}/new/pages.xml",
            headers={"Content-Type": "application/xml"},
            text=textwrap.dedent(
                """
                <?xml version="1.0" encoding="UTF-8"?>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                    <url><loc>page-1.html</loc></url>
                </urlset>
                """
            ).strip(),
        )

        tree = sitemap_tree_for_homepage(
            BASE, use_known_paths=False, resolve_relative_urls=True
        )

        sitemaps = list(tree.all_sitemaps())
        index = next(s for s in sitemaps if isinstance(s, IndexXMLSitemap))
        assert index.url == f"{BASE}/new/sitemap.xml"

        pages_sitemap = next(s for s in sitemaps if isinstance(s, PagesXMLSitemap))
        assert pages_sitemap.url == f"{BASE}/new/pages.xml"
        assert [page.url for page in tree.all_pages()] == [f"{BASE}/new/page-1.html"]

    def test_nested_xml_base_in_fetched_index(self, requests_mock):
        """xml:base in a fetched index controls where relative children resolve."""
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)

        requests_mock.get(
            f"{BASE}/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=f"Sitemap: {BASE}/sitemap.xml",
        )
        requests_mock.get(
            f"{BASE}/sitemap.xml",
            headers={"Content-Type": "application/xml"},
            text=textwrap.dedent(
                f"""
                <?xml version="1.0" encoding="UTF-8"?>
                <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                              xml:base="{BASE}/nested/dir/">
                    <sitemap xml:base="children/">
                        <loc>pages-a.xml</loc>
                    </sitemap>
                    <sitemap>
                        <!-- Sibling: "children/" from the previous entry is out of scope -->
                        <loc>pages-b.xml</loc>
                    </sitemap>
                </sitemapindex>
                """
            ).strip(),
        )
        for name in ("pages-a.xml", "pages-b.xml"):
            requests_mock.get(
                f"{BASE}/nested/dir/{'children/' if name == 'pages-a.xml' else ''}{name}",
                headers={"Content-Type": "application/xml"},
                text=textwrap.dedent(
                    f"""
                    <?xml version="1.0" encoding="UTF-8"?>
                    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                        <url><loc>{name.replace(".xml", ".html")}</loc></url>
                    </urlset>
                    """
                ).strip(),
            )

        tree = sitemap_tree_for_homepage(
            BASE, use_known_paths=False, resolve_relative_urls=True
        )

        page_urls = {page.url for page in tree.all_pages()}
        assert page_urls == {
            f"{BASE}/nested/dir/children/pages-a.html",
            f"{BASE}/nested/dir/pages-b.html",
        }

    def test_index_loop_via_relative_loc(self, requests_mock):
        """A relative <loc> resolving back to an ancestor index must be stopped
        by the existing recursion protection."""
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)

        requests_mock.get(
            f"{BASE}/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=f"Sitemap: {BASE}/sitemap.xml",
        )
        # Index points at itself through a relative reference
        requests_mock.get(
            f"{BASE}/sitemap.xml",
            headers={"Content-Type": "application/xml"},
            text=textwrap.dedent(
                """
                <?xml version="1.0" encoding="UTF-8"?>
                <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                    <sitemap><loc>sitemap.xml</loc></sitemap>
                </sitemapindex>
                """
            ).strip(),
        )

        tree = sitemap_tree_for_homepage(
            BASE, use_known_paths=False, resolve_relative_urls=True
        )

        sub_sitemaps = list(tree.all_sitemaps())
        assert type(sub_sitemaps[-1]) is InvalidSitemap
        assert "Recursion detected" in str(sub_sitemaps[-1])

    def test_resolution_disabled_relative_loc_is_invalid(self, requests_mock):
        """With the option off, a relative <loc> is not a valid URL and the
        child is simply absent (historical behaviour)."""
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)

        requests_mock.get(
            f"{BASE}/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=f"Sitemap: {BASE}/sitemap.xml",
        )
        requests_mock.get(
            f"{BASE}/sitemap.xml",
            headers={"Content-Type": "application/xml"},
            text=textwrap.dedent(
                """
                <?xml version="1.0" encoding="UTF-8"?>
                <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                    <sitemap><loc>relative/sitemap.xml</loc></sitemap>
                </sitemapindex>
                """
            ).strip(),
        )

        tree = sitemap_tree_for_homepage(BASE, use_known_paths=False)

        index = next(s for s in tree.all_sitemaps() if isinstance(s, IndexXMLSitemap))
        assert index.sub_sitemaps == []


class TestRobotsAndPlainTextSemantics(TreeTestBase):
    def test_plain_text_relative_lines_unchanged_with_flag(self, requests_mock):
        """Plain text sitemaps have no xml:base: relative lines keep being
        skipped even when the option is enabled."""
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)

        requests_mock.get(
            f"{BASE}/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=f"Sitemap: {BASE}/sitemap.txt",
        )
        requests_mock.get(
            f"{BASE}/sitemap.txt",
            text=textwrap.dedent(
                f"""
                {BASE}/news/absolute.html
                relative/page.html
                """
            ).strip(),
        )

        tree = sitemap_tree_for_homepage(
            BASE, use_known_paths=False, resolve_relative_urls=True
        )

        text_sitemap = next(
            s for s in tree.all_sitemaps() if isinstance(s, PagesTextSitemap)
        )
        assert [page.url for page in text_sitemap.pages] == [
            f"{BASE}/news/absolute.html"
        ]

    def test_robots_relative_sitemap_line_unchanged_with_flag(
        self, requests_mock, caplog
    ):
        """robots.txt Sitemap: lines must remain absolute-only even with the
        option enabled."""
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)

        requests_mock.get(
            f"{BASE}/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=textwrap.dedent(
                f"""
                Sitemap: relative/sitemap.xml
                Sitemap: {BASE}/sitemap.xml
                """
            ).strip(),
        )
        requests_mock.get(
            f"{BASE}/sitemap.xml",
            headers={"Content-Type": "application/xml"},
            text=textwrap.dedent(
                f"""
                <?xml version="1.0" encoding="UTF-8"?>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                    <url><loc>{BASE}/page.html</loc></url>
                </urlset>
                """
            ).strip(),
        )

        tree = sitemap_tree_for_homepage(
            BASE, use_known_paths=False, resolve_relative_urls=True
        )

        # Only the absolute robots.txt entry was followed
        assert len(list(tree.all_sitemaps())) == 2  # robots index + xml sitemap
        assert "doesn't look like an URL, skipping" in caplog.text
