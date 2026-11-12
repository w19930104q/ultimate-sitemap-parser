.. _cli:

CLI Reference
-------------

The CLI provides a simple command-line interface to retrieve sitemap data.

``usp``
=======

.. code-block:: none

    usage: usp [-h] [-v]  ...

    Ultimate Sitemap Parser

    options:
      -h, --help     show this help message and exit
      -v, --version  show program's version number and exit

    commands:

        ls           List sitemap pages

``usp ls``
==========

.. code-block:: none

    usage: usp ls [-h] [-f FORMAT] [-r] [-k] [-u] [-v/-vv] [-l LOG_FILE] [--cache-dir CACHE_DIR] [--cache-namespace CACHE_NAMESPACE] url

    download, parse and list the sitemap structure

    positional arguments:
      url              URL of the site including protocol

    options:
      -h, --help       show this help message and exit
      -f FORMAT, --format FORMAT
                            set output format (default: tabtree)
                            choices:
                              tabtree: Sitemaps and pages, nested with tab indentation
                              pages: Flat list of pages, one per line
      -r, --no-robots  don't discover sitemaps through robots.txt
      -k, --no-known   don't discover sitemaps through well-known URLs
      -u, --strip-url  strip the supplied URL from each page and sitemap URL
      -v, --verbose         increase output verbosity (-v=INFO, -vv=DEBUG)
      -l LOG_FILE, --log-file LOG_FILE
                            write log to this file and suppress console output
      --cache-dir CACHE_DIR
                            enable the on-disk conditional XML sitemap cache and store
                            entries in this directory
      --cache-namespace CACHE_NAMESPACE
                            namespace for the cache; use a distinct value for every
                            representation variant of the same URLs (default: default)

.. rubric:: Examples

.. code-block:: shell-session

    $ usp ls https://example.org/
    https://example.org/
        https://example.org/robots.txt
            https://example.org/sitemap.xml
                https://example.org/page1.html


.. code-block:: shell-session

    $ usp ls https://example.org/ --strip-url
    https://example.org/
        /robots.txt
            /sitemap.xml
                /page1.html

.. code-block:: shell-session

    $ usp ls https://example.org/ --format pages
    https://example.org/page1.html

.. code-block:: shell-session

    $ usp ls https://example.org/ --cache-dir /var/cache/usp \\
        --cache-namespace example.org-default

See :doc:`/guides/disk-cache` for details on the cache.