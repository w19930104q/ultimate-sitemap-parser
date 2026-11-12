Get Started
===========

Ultimate Sitemap Parser can be installed from PyPI or conda-forge:

.. tab-set::

    .. tab-item:: pip

        .. code-block:: shell-session

            $ pip install ultimate-sitemap-parser

    .. tab-item:: conda

        .. code-block:: shell-session

            $ conda install -c conda-forge ultimate-sitemap-parser


Traversing a website's sitemaps and retrieving all webpages requires just a single line of code:

.. code-block:: python

    from usp.tree import sitemap_tree_for_homepage

    tree = sitemap_tree_for_homepage('https://example.org/')

This will return a tree representing the structure of the sitemaps. To iterate through the pages, use :func:`tree.all_pages() <usp.objects.sitemap.AbstractSitemap.all_pages>`.

.. code-block:: python

    for page in tree.all_pages():
        print(page.url)

This will output the URL of each page in the sitemap, loading the parsed representations of sitemaps `lazily to reduce memory usage <performance_page_generator>`_ in very large sitemaps.

Each page is an instance of :class:`~usp.objects.page.SitemapPage`, which will always have at least a URL and priority, and may have other attributes if present.

.. _local parse:

Local Parsing
-------------

USP is primarily designed to fetch live sitemaps from the web, but does support local parsing too:

.. code-block::

    from usp.tree import sitemap_from_str

    # Load your sitemap and parse it in
    parsed_sitemap = sitemap_from_str("...")

    for page in parsed_sitemap.all_pages():
        print(page.url)

The returned object will be the appropriate child class of :class:`~.AbstractSitemap`. Page sitemaps will have their pages as above, but in index sitemaps each sub-sitemap will be an :class:`~usp.objects.sitemap.InvalidSitemap` (as it's unable to make a request to fetch them).

.. _relative-url-resolution:

Resolving Relative URLs
-----------------------

By default, relative URLs found in XML sitemaps (page links, image links,
``hreflang`` alternates and index sub-sitemap links) are not crawled and are
exported exactly as written. Opt in to resolving them with
``resolve_relative_urls=True``; they are then resolved against the document's
base URL and any in-scope `XML Base <https://www.w3.org/TR/xmlbase/>`_
(``xml:base``) attributes, so resolved URLs flow through the existing
filtering, deduplication and recursion-loop protection:

.. code-block:: python

    tree = sitemap_tree_for_homepage(
        'https://example.org/',
        resolve_relative_urls=True,
    )

The default base URL is the document's URL after any HTTP redirects.
``xml:base`` attributes on the root element, nested elements and sibling
scopes are handled per the XML Base specification. Links that resolve to a
non-HTTP(S) scheme (e.g. ``mailto:``) are skipped and logged. robots.txt and
plain text sitemap semantics are unaffected by this option.

When parsing a string, a valid HTTP(S) ``base_url`` must be supplied for the
option to work:

.. code-block:: python

    parsed_sitemap = sitemap_from_str(
        sitemap_string,
        resolve_relative_urls=True,
        base_url='https://example.org/sitemap.xml',
    )

The equivalent CLI flag is ``usp ls --resolve-relative-urls``.