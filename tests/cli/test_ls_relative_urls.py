"""End-to-end CLI tests for the --resolve-relative-urls flag.

Unlike test_ls.py, these exercise the real parsing pipeline with a mocked
HTTP layer so the rendered CLI output is verified.
"""

import textwrap

from tests.tree.base import TreeTestBase


class TestCliRelativeUrls(TreeTestBase):
    def _setup_mock(self, requests_mock):
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
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                        xml:base="{self.TEST_BASE_URL}/pages/">
                    <url><loc>about.html</loc></url>
                    <url><loc>contact.html</loc></url>
                </urlset>
                """
            ).strip(),
        )

    def test_flag_resolves_urls_in_tabtree_output(self, requests_mock, run_cmd):
        self._setup_mock(requests_mock)

        out, _ = run_cmd(
            f"ls --resolve-relative-urls -k {self.TEST_BASE_URL}",
        )

        lines = out.splitlines()
        assert self.TEST_BASE_URL + "/" in lines
        assert "\t\t" + self.TEST_BASE_URL + "/sitemap.xml" in lines
        assert "\t\t\t" + self.TEST_BASE_URL + "/pages/about.html" in lines
        assert "\t\t\t" + self.TEST_BASE_URL + "/pages/contact.html" in lines

    def test_flag_resolves_urls_in_pages_output(self, requests_mock, run_cmd):
        self._setup_mock(requests_mock)

        out, _ = run_cmd(
            f"ls --resolve-relative-urls -k -f pages {self.TEST_BASE_URL}",
        )

        assert out.splitlines() == [
            self.TEST_BASE_URL + "/pages/about.html",
            self.TEST_BASE_URL + "/pages/contact.html",
        ]

    def test_without_flag_keeps_relative_urls(self, requests_mock, run_cmd):
        """Default CLI behaviour still exports the relative URLs verbatim."""
        self._setup_mock(requests_mock)

        out, _ = run_cmd(f"ls -k -f pages {self.TEST_BASE_URL}")

        assert out.splitlines() == ["about.html", "contact.html"]
