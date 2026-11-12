"""On-disk conditional cache for XML sitemaps.

The cache stores the *decoded* sitemap body together with its HTTP validators
(``ETag`` and ``Last-Modified``).  Each entry is kept in a single JSON file and
replaced atomically, so two processes sharing the same directory can never observe
a body from one response paired with validators from another response: readers see
either the complete old entry or the complete new one.

The cache key is ``namespace + URL``.  The namespace is supplied by the caller and
is not guessed from request headers.  Callers must therefore use a distinct
namespace for every representation that may be returned for the same URL (for
example a different ``Accept-Language``, an authenticated variant, or a different
sitemap style selected through content negotiation).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CACHE_FORMAT_VERSION = 1
"""Version of the on-disk JSON format."""


class DiskCacheError(Exception):
    """Error reading from or writing to the disk cache."""

    pass


def _optional_header(value: str | None) -> str | None:
    """Normalise an HTTP header value, treating empty values as missing."""
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class CacheEntry:
    """A cached sitemap body and the validators that describe it."""

    body: bytes
    """Decoded (decompressed and UTF-8 decoded-then-re-encoded) sitemap body."""

    etag: str | None = None
    """HTTP ``ETag`` validator, if one was returned."""

    last_modified: str | None = None
    """HTTP ``Last-Modified`` validator, if one was returned."""

    @property
    def has_validators(self) -> bool:
        """Return whether the entry can be used for a conditional request."""
        return self.etag is not None or self.last_modified is not None

    def conditional_headers(self) -> dict[str, str]:
        """Return request headers to revalidate this entry."""
        headers: dict[str, str] = {}
        if self.etag is not None:
            headers["If-None-Match"] = self.etag
        if self.last_modified is not None:
            headers["If-Modified-Since"] = self.last_modified
        return headers

    def decodable_body(self) -> bool:
        """Return whether the cached body is non-empty and valid UTF-8."""
        if not self.body:
            return False
        try:
            self.body.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True

    def merged_with(self, etag: str | None, last_modified: str | None) -> CacheEntry:
        """Merge validators received in a ``304 Not Modified`` response.

        Validators absent from the response keep their cached value; this
        implements the HTTP semantics of a ``304`` response which only needs to
        include validators that changed.
        """
        return CacheEntry(
            body=self.body,
            etag=_optional_header(etag) or self.etag,
            last_modified=_optional_header(last_modified) or self.last_modified,
        )


class DiskConditionalCache:
    """A namespace-scoped, process-safe on-disk conditional sitemap cache."""

    __slots__ = ["_directory", "_namespace"]

    def __init__(self, directory: str | os.PathLike[str], namespace: str):
        """
        :param directory: Directory in which cache entries are stored. It is
            created if it does not exist. The directory may be shared by multiple
            processes.
        :param namespace: Cache namespace. Entries for the same URL in different
            namespaces do not collide. The caller is responsible for choosing a
            namespace that keeps a URL's representation stable for the lifetime
            of the entries in it.
        """
        if not namespace:
            raise ValueError("Cache namespace must not be empty.")

        self._directory = Path(directory)
        self._namespace = namespace

        try:
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError as ex:
            raise DiskCacheError(
                f"Unable to create sitemap cache directory {self._directory}: {ex}"
            ) from ex

    @property
    def directory(self) -> Path:
        """Return the directory used by this cache."""
        return self._directory

    @property
    def namespace(self) -> str:
        """Return the namespace used by this cache."""
        return self._namespace

    def key(self, url: str) -> str:
        """Return the opaque filename-safe cache key for a URL."""
        material = f"{self._namespace}\0{url}".encode()
        return hashlib.sha256(material).hexdigest()

    def _path(self, url: str) -> Path:
        return self._directory / f"{self.key(url)}.json"

    def read(self, url: str) -> CacheEntry | None:
        """Read a cache entry, or return ``None`` if there is no usable entry.

        A missing entry is normal. A malformed or inconsistent entry is logged as
        an error and treated as a miss; this lets a damaged cache be overwritten
        by the next successful parse instead of breaking the fetch loop.
        """
        path = self._path(url)

        try:
            with path.open("rb") as entry_file:
                payload: Any = json.load(entry_file)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError) as ex:
            log.error(f"Ignoring unreadable sitemap cache entry {path}: {ex}")
            return None

        try:
            if not isinstance(payload, dict):
                raise ValueError("cache entry is not an object")
            if payload.get("version") != CACHE_FORMAT_VERSION:
                raise ValueError(
                    f"unsupported cache format version {payload.get('version')!r}"
                )
            if payload.get("namespace") != self._namespace:
                raise ValueError("cache namespace does not match")
            if payload.get("url") != url:
                raise ValueError("cache URL does not match")

            body_b64 = payload["body"]
            if not isinstance(body_b64, str):
                raise ValueError("cache body is not a string")
            body = base64.b64decode(body_b64, validate=True)
            if not body:
                raise ValueError("cache body is empty")
            body.decode("utf-8")

            etag = payload.get("etag")
            last_modified = payload.get("last_modified")
            if etag is not None and not isinstance(etag, str):
                raise ValueError("cache ETag is not a string")
            if last_modified is not None and not isinstance(last_modified, str):
                raise ValueError("cache Last-Modified is not a string")

        except (KeyError, TypeError, ValueError, binascii.Error) as ex:
            log.error(f"Ignoring corrupt sitemap cache entry {path}: {ex}")
            return None

        entry = CacheEntry(
            body=body,
            etag=_optional_header(etag),
            last_modified=_optional_header(last_modified),
        )
        if not entry.has_validators:
            # Without a validator the entry cannot be revalidated, so a conditional
            # request could never use it. Treat it as a miss.
            return None
        return entry

    def write(
        self,
        url: str,
        body: bytes,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        """Atomically store a cache entry.

        The body and validators are written to one temporary file, flushed to
        disk and then atomically renamed over the previous entry. If the write is
        interrupted at any point, the previous complete entry remains in place.

        :raises DiskCacheError: If the entry cannot be written. The previous
            entry is left untouched.
        """
        if not body:
            raise DiskCacheError("Refusing to cache an empty sitemap body.")

        path = self._path(url)
        payload = {
            "version": CACHE_FORMAT_VERSION,
            "namespace": self._namespace,
            "url": url,
            "etag": _optional_header(etag),
            "last_modified": _optional_header(last_modified),
            "body": base64.b64encode(body).decode("ascii"),
        }

        temp_name: str | None = None
        try:
            # a unique temporary file per writer makes the rename atomic even when
            # several processes share the directory
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self._directory,
                prefix=".sitemap-cache-",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_name = temp_file.name
                temp_file.write(
                    json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                )
                temp_file.flush()
                os.fsync(temp_file.fileno())

            os.replace(temp_name, path)
            temp_name = None

            # Best-effort durability of the rename itself. Failure here does not
            # affect the atomicity guarantee for readers.
            try:
                directory_fd = os.open(self._directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass

        except OSError as ex:
            raise DiskCacheError(
                f"Unable to write sitemap cache entry {path}: {ex}"
            ) from ex
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    def delete(self, url: str) -> None:
        """Remove an entry.

        Removing an entry is atomic: readers see either the complete old entry or
        no entry at all.

        :raises DiskCacheError: If an existing entry cannot be removed.
        """
        path = self._path(url)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as ex:
            raise DiskCacheError(
                f"Unable to remove sitemap cache entry {path}: {ex}"
            ) from ex
