"""Helpers for streaming sitemap parsing.

The helpers in this module make it possible to parse an XML sitemap without
keeping its contents (or the pages derived from it) in memory:

* :func:`iter_response_content` streams and decompresses an HTTP response in
  bounded chunks;
* :class:`StreamingPagesStore` appends pickled :class:`~usp.objects.page.SitemapPage`
  records to a temporary file and keeps the duplicate-detection index on disk
  (using SQLite).
"""

import gzip as gzip_lib
import logging
import os
import pickle
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator

from .helpers import response_is_gzipped_data
from .objects.page import SitemapPage

log = logging.getLogger(__name__)

DEFAULT_STREAM_CHUNK_SIZE = 64 * 1024
"""Default number of bytes read from an HTTP response at a time."""

MISSING_URL_DEDUP_KEY = "\x00<usp-missing-url>"
"""Deduplication key used when a sitemap entry has no URL at all.

``None`` cannot be used as a SQLite primary key value in a meaningful way, so
a sentinel is used instead. All entries without a URL therefore collapse into
the first one, which matches the in-memory implementation.
"""


def _close_iterator(iterator) -> None:
    """Close an iterator if it supports closing, ignoring any errors."""
    close = getattr(iterator, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # pragma: no cover - defensive cleanup
            log.debug("Unable to close iterator", exc_info=True)


def _truncate_chunks(
    chunks: Iterable[bytes], max_output_bytes: int | None
) -> Iterator[bytes]:
    """Yield chunks, limiting their total size to ``max_output_bytes``."""
    total = 0
    for chunk in chunks:
        if not chunk:
            continue

        if max_output_bytes is not None:
            remaining = max_output_bytes - total
            if remaining <= 0:
                return
            if len(chunk) > remaining:
                chunk = chunk[:remaining]

        total += len(chunk)
        yield chunk


def _replay_chunk(first: bytes, rest: Iterable[bytes]) -> Iterator[bytes]:
    """Yield ``first`` and then the remaining chunks, closing ``rest``."""
    try:
        if first:
            yield first
        yield from rest
    finally:
        _close_iterator(rest)


class _ChunkReader:
    """Minimal file-like reader on top of an iterator of byte chunks.

    :class:`gzip.GzipFile` requires an object with ``read``/``readinto``.
    Reads may return fewer bytes than requested, so a small buffer is kept to
    satisfy the exact-length reads performed while parsing the gzip header.
    """

    __slots__ = ["_chunks", "_buffer"]

    def __init__(self, chunks: Iterable[bytes]):
        self._chunks = iter(chunks)
        self._buffer = b""

    def _fill(self, size: int) -> None:
        while len(self._buffer) < size:
            try:
                self._buffer += next(self._chunks)
            except StopIteration:
                return

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            # Not used by the streaming code path, but implement it correctly.
            try:
                for chunk in self._chunks:
                    self._buffer += chunk
            finally:
                _close_iterator(self._chunks)
            data, self._buffer = self._buffer, b""
            return data

        self._fill(size)
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def readable(self) -> bool:
        return True


def _iter_gunzipped(
    chunks: Iterable[bytes],
    max_output_bytes: int | None,
    chunk_size: int,
) -> Iterator[bytes]:
    """Incrementally gunzip ``chunks``, limiting decompressed output size."""
    reader = _ChunkReader(chunks)
    gz = gzip_lib.GzipFile(fileobj=reader)
    total = 0
    try:
        while True:
            chunk = gz.read(chunk_size)
            if not chunk:
                return

            if max_output_bytes is not None:
                remaining = max_output_bytes - total
                if remaining <= 0:
                    return
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]

            total += len(chunk)
            yield chunk
    except (gzip_lib.BadGzipFile, EOFError, OSError, ValueError) as ex:
        # The sitemap might end prematurely (or be mislabelled as gzip).
        # Keep everything that has been parsed so far.
        log.warning(f"Unable to stream-gunzip response: {ex}")
    finally:
        try:
            gz.close()
        except Exception:  # pragma: no cover - defensive cleanup
            log.debug("Unable to close gzip reader", exc_info=True)


def iter_response_content(
    response,
    url: str,
    max_uncompressed_bytes: int | None = None,
    chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE,
) -> Iterator[bytes]:
    """Yield a response's content in bounded, decompressed byte chunks.

    The response is read through ``response.iter_content`` instead of
    ``response.raw_data``, so the whole body is never materialized in memory.
    Transport-level gzip compression is handled by the web client (Requests
    transparently decodes ``Content-Encoding``); gzip payloads discovered from
    the URL extension or ``Content-Type`` are decompressed here.

    :param response: Successful web client response.
    :param url: URL the response was fetched from.
    :param max_uncompressed_bytes: Maximum number of content bytes to yield.
    :param chunk_size: Number of bytes to request from the response at a time.
    """
    raw_chunks = response.iter_content(chunk_size=chunk_size)
    try:
        # Find the first non-empty chunk without materializing the body.
        first = None
        for chunk in raw_chunks:
            if chunk:
                first = chunk
                break

        if first is None:
            return

        if response_is_gzipped_data(url=url, response=response):
            if first[:2] == b"\x1f\x8b":
                yield from _iter_gunzipped(
                    _replay_chunk(first, raw_chunks),
                    max_output_bytes=max_uncompressed_bytes,
                    chunk_size=chunk_size,
                )
                return

            # The response was expected to be gzipped but does not even have
            # a gzip magic header (e.g. a plain XML file served with a ".gz"
            # extension). Parse it as plain data, just like the non-streaming
            # implementation does after a failed decompression attempt.
            log.warning(
                f"Unable to gunzip response for {url}, maybe it's a non-gzipped sitemap: "
                "data does not start with the gzip magic header"
            )

        yield from _truncate_chunks(
            _replay_chunk(first, raw_chunks),
            max_output_bytes=max_uncompressed_bytes,
        )
    finally:
        _close_iterator(raw_chunks)


class StreamingPagesStore:
    """Disk-backed storage for pages parsed from a single URL set.

    Pages are appended to a temporary file as individual pickle records and
    can be iterated over any number of times. Duplicate detection uses a
    temporary SQLite database, so neither the page data nor the seen-URL
    index has to stay in memory.
    """

    __slots__ = [
        "_directory",
        "_page_path",
        "_dedup_path",
        "_connection",
        "_writer",
        "_finalized",
        "_closed",
    ]

    def __init__(self):
        self._directory = tempfile.mkdtemp(prefix="usp-stream-")
        self._page_path = os.path.join(self._directory, "pages.bin")
        self._dedup_path = os.path.join(self._directory, "dedup.sqlite3")

        self._connection = sqlite3.connect(self._dedup_path, isolation_level=None)
        # Keep SQLite's memory footprint bounded and constant. All actual data
        # lives in the temporary database file.
        self._connection.execute("PRAGMA journal_mode=OFF")
        self._connection.execute("PRAGMA synchronous=OFF")
        self._connection.execute("PRAGMA cache_size=-256")
        self._connection.execute("PRAGMA temp_store=FILE")
        self._connection.execute("CREATE TABLE seen (key TEXT PRIMARY KEY)")

        self._writer = open(self._page_path, "wb")
        self._finalized = False
        self._closed = False

    @property
    def page_path(self) -> str:
        """Path of the file containing the pickled page records."""
        return self._page_path

    @property
    def directory(self) -> str:
        """Temporary directory holding all store files."""
        return self._directory

    def add_if_new(self, key: str | None) -> bool:
        """Register a raw sitemap URL and report whether it was new.

        :param key: Raw URL string as it appeared in the sitemap, or ``None``
            if the entry had no ``<loc>``.
        :return: ``True`` if this is the first time the key was seen.
        """
        if self._connection is None:
            raise RuntimeError("Page store has already been finalized.")

        if key is None:
            key = MISSING_URL_DEDUP_KEY

        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO seen (key) VALUES (?)", (key,)
        )
        return cursor.rowcount == 1

    def append(self, page: SitemapPage) -> None:
        """Append a completed page as a single pickle record."""
        if self._writer is None:
            raise RuntimeError("Page store has already been finalized.")
        pickle.dump(page, self._writer, protocol=pickle.HIGHEST_PROTOCOL)

    def finalize(self) -> None:
        """Flush and close the writers so that pages can be iterated over."""
        if self._finalized:
            return
        self._finalized = True

        if self._writer is not None:
            try:
                self._writer.flush()
            finally:
                self._writer.close()
                self._writer = None

        if self._connection is not None:
            try:
                self._connection.commit()
            finally:
                self._connection.close()
                self._connection = None

    def iter_pages(self) -> Iterator[SitemapPage]:
        """Iterate over the stored pages in their original order.

        Every call opens an independent file handle, which makes iteration
        repeatable and safe to interleave with other calls.
        """
        # Make sure buffered records have reached the file if iteration is
        # requested before the store was explicitly finalized.
        self.finalize()

        with open(self._page_path, "rb") as file:
            while True:
                try:
                    yield pickle.load(file)
                except EOFError:
                    return

    def close(self) -> None:
        """Finalize and remove all temporary files."""
        self.finalize()

        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self._directory, ignore_errors=True)

    def __del__(self):
        try:
            self.close()
        except Exception:  # pragma: no cover - defensive cleanup
            pass
