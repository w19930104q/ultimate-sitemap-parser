"""Offline tests for the opt-in relative URL resolution (``resolve_relative_urls``).

Covers XML urlset, RSS and Atom feeds: default base URL, root/nested
``xml:base`` overrides, sibling scope isolation, image and ``hreflang``
alternate links, equivalent relative address deduplication and non-HTTP(S)
link skipping.
"""

import textwrap

import pytest

from tests.tree.base import TreeTestBase
from usp.exceptions import SitemapException
from usp.objects.page import SitemapImage
from usp.objects.sitemap import (
    PagesAtomSitemap,
    PagesRSSSitemap,
    PagesXMLSitemap,
)
from usp.tree import sitemap_from_str, sitemap_tree_for_homepage

BASE = "http://test_ultimate-sitemap-parser.com"


class TestRelativeUrlResolutionFromStr(TreeTestBase):
    def test_requires_http_base_url(self):
        """Enabling resolution through sitemap_from_str() needs a HTTP(S) base."""
        content = "<urlset/>"

        with pytest.raises(SitemapException):
            sitemap_from_str(content, resolve_relative_urls=True)

        with pytest.raises(SitemapException):
            sitemap_from_str(content, resolve_relative_urls=True, base_url="not-a-url")

        with pytest.raises(SitemapException):
            sitemap_from_str(
                content, resolve_relative_urls=True, base_url="ftp://example.com/"
            )

        # A valid base URL is accepted
        sitemap_from_str(
            content, resolve_relative_urls=True, base_url="https://example.com/"
        )

    def test_disabled_keeps_relative_urls_verbatim(self):
        """With the option off (the default), relative URLs are exported untouched."""
        content = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <url><loc>about.html</loc></url>
            </urlset>
            """
        ).strip()

        parsed = sitemap_from_str(content)
        assert isinstance(parsed, PagesXMLSitemap)
        assert parsed.pages[0].url == "about.html"

    def test_default_base_resolves_relative_loc(self):
        """Without any xml:base, links resolve against the supplied base URL."""
        content = textwrap.dedent(
            f"""
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <url><loc>about.html</loc></url>
                <url><loc>/contact.html</loc></url>
                <url><loc>{BASE}/absolute.html</loc></url>
            </urlset>
            """
        ).strip()

        parsed = sitemap_from_str(
            content,
            resolve_relative_urls=True,
            base_url=f"{BASE}/sitemap/sitemap.xml",
        )

        urls = [page.url for page in parsed.pages]
        assert urls == [
            f"{BASE}/sitemap/about.html",
            f"{BASE}/contact.html",
            f"{BASE}/absolute.html",
        ]

    def test_root_xml_base_applies_to_everything(self):
        """An xml:base on the root element establishes the document-wide base."""
        content = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                    xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"
                    xmlns:xhtml="http://www.w3.org/1999/xhtml"
                    xml:base="https://cdn.example.com/root/">
                <url>
                    <loc>page.html</loc>
                    <image:image><image:loc>img/pic.jpg</image:loc></image:image>
                    <xhtml:link rel="alternate" hreflang="de" href="de/page.html"/>
                </url>
            </urlset>
            """
        ).strip()

        parsed = sitemap_from_str(
            content,
            resolve_relative_urls=True,
            base_url=f"{BASE}/sitemap.xml",
        )

        page = parsed.pages[0]
        assert page.url == "https://cdn.example.com/root/page.html"
        assert page.images == [
            SitemapImage(loc="https://cdn.example.com/root/img/pic.jpg")
        ]
        assert page.alternates == [("de", "https://cdn.example.com/root/de/page.html")]

    def test_nested_xml_base_overrides_and_sibling_isolation(self):
        """Nested xml:base resolves against the inherited base, and siblings
        must not see each other's overrides."""
        content = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                    xml:base="https://example.com/root/">
                <url xml:base="sub/">
                    <loc>a.html</loc>
                </url>
                <url>
                    <!-- Sibling of the previous <url>: "sub/" must be out of scope -->
                    <loc>b.html</loc>
                </url>
                <url xml:base="https://other.example.net/deep/">
                    <loc>c.html</loc>
                </url>
                <url>
                    <!-- Another sibling: the absolute override above is gone too -->
                    <loc>d.html</loc>
                </url>
            </urlset>
            """
        ).strip()

        parsed = sitemap_from_str(
            content,
            resolve_relative_urls=True,
            base_url=f"{BASE}/sitemap.xml",
        )

        assert [page.url for page in parsed.pages] == [
            "https://example.com/root/sub/a.html",
            "https://example.com/root/b.html",
            "https://other.example.net/deep/c.html",
            "https://example.com/root/d.html",
        ]

    def test_nested_base_on_inner_elements(self):
        """xml:base on an inner element applies to descendants only."""
        content = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                    xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"
                    xml:base="https://example.com/root/">
                <url>
                    <loc>page.html</loc>
                    <image:image xml:base="images/">
                        <image:loc>pic.jpg</image:loc>
                    </image:image>
                </url>
            </urlset>
            """
        ).strip()

        parsed = sitemap_from_str(
            content,
            resolve_relative_urls=True,
            base_url=f"{BASE}/sitemap.xml",
        )

        page = parsed.pages[0]
        assert page.url == "https://example.com/root/page.html"
        assert page.images[0].loc == "https://example.com/root/images/pic.jpg"

    def test_equivalent_relative_addresses_are_deduplicated(self):
        """Different relative spellings of the same absolute URL collapse."""
        content = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <url><loc>dir/../page.html</loc></url>
                <url><loc>./page.html</loc></url>
                <url><loc>https://example.com/base/page.html</loc></url>
            </urlset>
            """
        ).strip()

        parsed = sitemap_from_str(
            content,
            resolve_relative_urls=True,
            base_url="https://example.com/base/sitemap.xml",
        )

        assert [page.url for page in parsed.pages] == [
            "https://example.com/base/page.html"
        ]

    def test_non_http_links_are_skipped_and_logged(self, caplog):
        """Non-HTTP(S) resolved links are skipped with a logged reason."""
        content = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                    xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"
                    xmlns:xhtml="http://www.w3.org/1999/xhtml">
                <url><loc>mailto:nobody@example.com</loc></url>
                <url>
                    <loc>page.html</loc>
                    <image:image>
                        <image:loc>ftp://example.com/pic.jpg</image:loc>
                    </image:image>
                    <image:image>
                        <image:loc>img/pic.jpg</image:loc>
                    </image:image>
                    <xhtml:link rel="alternate" hreflang="x"
                               href="javascript:alert(1)"/>
                </url>
            </urlset>
            """
        ).strip()

        parsed = sitemap_from_str(
            content,
            resolve_relative_urls=True,
            base_url=f"{BASE}/sitemap.xml",
        )

        # The mailto page is dropped entirely; the second page survives
        assert [page.url for page in parsed.pages] == [f"{BASE}/page.html"]
        page = parsed.pages[0]
        assert page.alternates is None
        # Only the HTTP(S) image survives; the ftp one was skipped
        assert [image.loc for image in page.images] == [f"{BASE}/img/pic.jpg"]
        assert "not an HTTP(S) URL, skipping" in caplog.text

    def test_resolved_urls_feed_existing_filters(self, requests_mock):
        """Resolved sub-sitemap URLs flow through the existing recurse
        callbacks and recursion protection like any other absolute URL."""
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
                    <sitemap><loc>keep.xml</loc></sitemap>
                    <sitemap><loc>drop.xml</loc></sitemap>
                </sitemapindex>
                """
            ).strip(),
        )
        for name in ("keep", "drop"):
            requests_mock.get(
                f"{BASE}/{name}.xml",
                headers={"Content-Type": "application/xml"},
                text=textwrap.dedent(
                    f"""
                    <?xml version="1.0" encoding="UTF-8"?>
                    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                        <url><loc>{name}.html</loc></url>
                    </urlset>
                    """
                ).strip(),
            )

        tree = sitemap_tree_for_homepage(
            BASE,
            use_known_paths=False,
            resolve_relative_urls=True,
            recurse_callback=lambda url, level, parents: "drop" not in url,
        )

        page_urls = {page.url for page in tree.all_pages()}
        assert page_urls == {f"{BASE}/keep.html"}

        # The list callback receives the resolved absolute URLs as well
        filtered_tree = sitemap_tree_for_homepage(
            BASE,
            use_known_paths=False,
            resolve_relative_urls=True,
            recurse_list_callback=lambda urls, level, parents: [
                url for url in urls if "drop" not in url
            ],
        )
        assert {page.url for page in filtered_tree.all_pages()} == {f"{BASE}/keep.html"}


class TestRelativeRssAtomFromStr(TreeTestBase):
    def test_rss_relative_links(self):
        content = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0" xml:base="https://example.com/feed/">
                <channel>
                    <title>Feed</title>
                    <description>Desc</description>
                    <item>
                        <title>Story 1</title>
                        <description>Desc 1</description>
                        <link>story-1.html</link>
                    </item>
                    <item xml:base="https://cdn.example.net/">
                        <title>Story 2</title>
                        <description>Desc 2</description>
                        <link>story-2.html</link>
                    </item>
                </channel>
            </rss>
            """
        ).strip()

        parsed = sitemap_from_str(
            content,
            resolve_relative_urls=True,
            base_url=f"{BASE}/rss.xml",
        )
        assert isinstance(parsed, PagesRSSSitemap)
        assert [page.url for page in parsed.pages] == [
            "https://example.com/feed/story-1.html",
            "https://cdn.example.net/story-2.html",
        ]

    def test_rss_dedup_equivalent_relative_links(self):
        content = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0">
                <channel>
                    <title>Feed</title>
                    <description>Desc</description>
                    <item>
                        <title>Story 1</title>
                        <description>Desc 1</description>
                        <link>./story.html</link>
                    </item>
                    <item>
                        <title>Story 2</title>
                        <description>Desc 2</description>
                        <link>https://example.com/feed/story.html</link>
                    </item>
                </channel>
            </rss>
            """
        ).strip()

        parsed = sitemap_from_str(
            content,
            resolve_relative_urls=True,
            base_url="https://example.com/feed/rss.xml",
        )
        assert [page.url for page in parsed.pages] == [
            "https://example.com/feed/story.html"
        ]

    def test_atom_relative_links_and_sibling_isolation(self):
        content = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <feed xmlns="http://www.w3.org/2005/Atom"
                  xml:base="https://example.com/feed/">
                <title>Feed</title>
                <entry>
                    <title>Story 1</title>
                    <summary>Desc 1</summary>
                    <link href="entry-1.html"/>
                </entry>
                <entry xml:base="archive/2024/">
                    <title>Story 2</title>
                    <summary>Desc 2</summary>
                    <link href="entry-2.html"/>
                </entry>
                <entry>
                    <title>Story 3</title>
                    <summary>Desc 3</summary>
                    <link href="entry-3.html"/>
                </entry>
            </feed>
            """
        ).strip()

        parsed = sitemap_from_str(
            content,
            resolve_relative_urls=True,
            base_url=f"{BASE}/atom.xml",
        )
        assert isinstance(parsed, PagesAtomSitemap)
        assert [page.url for page in parsed.pages] == [
            "https://example.com/feed/entry-1.html",
            "https://example.com/feed/archive/2024/entry-2.html",
            "https://example.com/feed/entry-3.html",
        ]

    def test_feed_non_http_link_skipped(self, caplog):
        content = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0">
                <channel>
                    <title>Feed</title>
                    <description>Desc</description>
                    <item>
                        <title>Story</title>
                        <description>Desc</description>
                        <link>news:42</link>
                    </item>
                </channel>
            </rss>
            """
        ).strip()

        parsed = sitemap_from_str(
            content,
            resolve_relative_urls=True,
            base_url=f"{BASE}/rss.xml",
        )
        assert parsed.pages == []
        assert "not an HTTP(S) URL, skipping" in caplog.text
