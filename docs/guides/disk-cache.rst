Conditional Disk Cache
======================

USP can keep a per-URL on-disk cache of already parsed XML sitemap bodies and use
HTTP conditional requests (``If-None-Match`` and ``If-Modified-Since``) to avoid
downloading them again. The cache is disabled by default; enabling it is entirely
opt-in and does not change the behaviour of the existing API.

Scope
-----

The cache is deliberately narrow:

* It is used only with the built-in
  :class:`~usp.web_client.requests_client.RequestsWebClient`. Custom web client
  implementations do not participate in conditional requests, so a cache passed
  together with a custom client is ignored.
* Only documents parsed as XML sitemaps are stored: XML sitemap indexes, XML URL
  sets, and RSS 2.0 / Atom sitemaps. ``robots.txt`` and plain-text sitemaps are
  never cached.
* Cache entries require at least one of ``ETag`` or ``Last-Modified``. Responses
  without a validator cannot be revalidated and are not cached; if an older entry
  exists, it is invalidated after a successful parse.

Enabling the cache
------------------

Create a :class:`~usp.disk_cache.DiskConditionalCache` and pass it either to
:func:`~usp.tree.sitemap_tree_for_homepage` or directly to
:class:`~usp.fetch_parse.SitemapFetcher`:

.. code-block:: python

    from usp.disk_cache import DiskConditionalCache
    from usp.tree import sitemap_tree_for_homepage

    cache = DiskConditionalCache(
        directory='/var/cache/usp',
        namespace='example.org-default',
    )
    tree = sitemap_tree_for_homepage('https://www.example.org/', cache=cache)

For convenience, the cache can also be configured on the Requests client:

.. code-block:: python

    from usp.web_client.requests_client import RequestsWebClient
    from usp.tree import sitemap_tree_for_homepage

    client = RequestsWebClient(
        cache_dir='/var/cache/usp',
        cache_namespace='example.org-default',
    )
    tree = sitemap_tree_for_homepage(
        'https://www.example.org/',
        web_client=client,
    )

The CLI exposes the same feature through ``--cache-dir`` and
``--cache-namespace``:

.. code-block:: shell-session

    $ usp ls https://www.example.org/ --cache-dir /var/cache/usp \
        --cache-namespace example.org-default

Namespaces
----------

The cache key is the pair ``(namespace, URL)``. The namespace is not derived from
request headers; the caller owns it. A single namespace must contain at most one
representation for each URL. If the same URL can return different sitemaps
depending on, for example, ``Accept-Language``, authentication, or a different
``User-Agent``, use a separate namespace for each variant. Reusing a namespace
across variants can make one variant's cached body appear as a valid ``304``
response for another variant.

Cache semantics
---------------

When a URL is requested again, USP sends the validators stored for that URL. If
the server answers ``304 Not Modified``:

* the cached decoded body is reused;
* validators present in the ``304`` response are merged with the cached ones,
  keeping any validator the response omitted;
* the body is parsed again from scratch. The current recursion level, ancestor
  URL set, and ``recurse_callback`` / ``recurse_list_callback`` filters are
  applied, and child sitemaps are fetched again. Parsed subtrees are never
  reused from the cache.

Redirects are followed one hop at a time. Every hop looks up and sends only the
validators stored for that hop's URL; conditional headers from an earlier hop are
never carried into the next hop. The final URL is the URL under which a successful
response is cached.

What is committed, and when
---------------------------

The cache is updated only after the whole XML document for the URL in question has
parsed successfully. In particular:

* A truncated XML document still returns all complete items parsed so far, but the
  cache entry for that document is left unchanged.
* The commit happens before child sitemaps are fetched. A failure in a child
  subgraph therefore cannot prevent the parent XML document from being committed.
* A ``304`` with no usable cached body causes exactly one unconditional retry. If
  that request also returns ``304``, the URL is reported as an
  :class:`~usp.objects.sitemap.InvalidSitemap`.
* HTTP errors, decompression errors, and cache write errors leave the previous
  entry in place and are logged. A successfully parsed response is still returned
  to the caller even if writing the cache entry failed.

Concurrency and crash safety
----------------------------

Every cache entry is stored in a single JSON file containing the body and its
validators. Writes go to a uniquely named temporary file in the cache directory
which is flushed, synchronised, and then atomically renamed over the old entry.
Readers therefore always see a complete version of the entry: either the old body
with the old validators, or the new body with the new validators, never a mixture.
If a write or the process is interrupted before the rename, the previous entry
remains valid; if it happens after the rename, the new entry is complete. Multiple
processes can safely share the same directory.

Corrupt or unreadable entries are logged as errors and treated as cache misses, so
the next successful fetch can replace them. Stale temporary files from interrupted
writes are ignored by readers and may be removed by normal directory maintenance.

.. note::

    The cache does not impose a freshness lifetime of its own. It relies on the
    origin server's validators to report whether a cached representation is still
    current. A server that returns a changed representation with ``200 OK`` causes
    the entry to be replaced; a server that answers ``304`` confirms that the
    cached body is current.
