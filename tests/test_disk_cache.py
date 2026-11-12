import json
import logging

import pytest

from usp.disk_cache import (
    CacheEntry,
    DiskCacheError,
    DiskConditionalCache,
)


def _cache_file(cache: DiskConditionalCache, url: str):
    return cache.directory / f"{cache.key(url)}.json"


class TestDiskConditionalCache:
    URL = "http://cache.example/sitemap.xml"

    def test_round_trip(self, tmp_path):
        cache = DiskConditionalCache(tmp_path, "namespace")
        body = b"<?xml version='1.0'?><urlset/>"

        cache.write(
            self.URL,
            body=body,
            etag='"v1"',
            last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
        )

        entry = cache.read(self.URL)
        assert entry is not None
        assert entry.body == body
        assert entry.etag == '"v1"'
        assert entry.last_modified == "Wed, 21 Oct 2015 07:28:00 GMT"
        assert entry.conditional_headers() == {
            "If-None-Match": '"v1"',
            "If-Modified-Since": "Wed, 21 Oct 2015 07:28:00 GMT",
        }

    def test_namespace_is_part_of_key(self, tmp_path):
        cache_a = DiskConditionalCache(tmp_path, "namespace-a")
        cache_b = DiskConditionalCache(tmp_path, "namespace-b")

        cache_a.write(self.URL, body=b"body-a", etag='"a"')
        cache_b.write(self.URL, body=b"body-b", etag='"b"')

        assert cache_a.key(self.URL) != cache_b.key(self.URL)
        assert cache_a.read(self.URL).body == b"body-a"  # type: ignore[union-attr]
        assert cache_b.read(self.URL).body == b"body-b"  # type: ignore[union-attr]

    def test_missing_entry(self, tmp_path):
        assert DiskConditionalCache(tmp_path, "ns").read(self.URL) is None

    def test_entry_without_validator_is_a_miss(self, tmp_path):
        cache = DiskConditionalCache(tmp_path, "ns")
        cache.write(self.URL, body=b"body", etag=None, last_modified=None)
        assert cache.read(self.URL) is None

    @pytest.mark.parametrize(
        "contents",
        [
            b"not json",
            b"[]",
            b'{"version": 999}',
            b'{"version": 1, "namespace": "ns", "url": "http://cache.example/sitemap.xml", "body": ""}',
            b'{"version": 1, "namespace": "ns", "url": "http://cache.example/sitemap.xml", "body": "not base64!"}',
        ],
    )
    def test_corrupt_entries_are_misses(self, tmp_path, caplog, contents):
        cache = DiskConditionalCache(tmp_path, "ns")
        _cache_file(cache, self.URL).write_bytes(contents)

        with caplog.at_level(logging.ERROR):
            assert cache.read(self.URL) is None

        assert "sitemap cache entry" in caplog.text
        assert "corrupt" in caplog.text or "unreadable" in caplog.text

    def test_merge_validators_keeps_missing_ones(self):
        entry = CacheEntry(body=b"body", etag='"old"', last_modified="old-date")
        merged = entry.merged_with(etag='"new"', last_modified=None)
        assert merged.body == b"body"
        assert merged.etag == '"new"'
        assert merged.last_modified == "old-date"

        merged = entry.merged_with(etag=None, last_modified="new-date")
        assert merged.etag == '"old"'
        assert merged.last_modified == "new-date"

        merged = entry.merged_with(etag="", last_modified="  ")
        assert merged.etag == '"old"'
        assert merged.last_modified == "old-date"

    def test_interrupted_write_keeps_complete_old_entry(self, tmp_path, mocker):
        cache = DiskConditionalCache(tmp_path, "ns")
        cache.write(self.URL, body=b"old-body", etag='"old"')

        mocker.patch("usp.disk_cache.os.replace", side_effect=OSError("interrupted"))

        with pytest.raises(DiskCacheError):
            cache.write(self.URL, body=b"new-body", etag='"new"')

        entry = cache.read(self.URL)
        assert entry is not None
        # body and validator are from the same (old) version
        assert entry.body == b"old-body"
        assert entry.etag == '"old"'
        # no partially-written file is visible to readers
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            f"{cache.key(self.URL)}.json"
        ]

    def test_concurrent_writers_never_mix_versions(self, tmp_path):
        cache_a = DiskConditionalCache(tmp_path, "ns")
        cache_b = DiskConditionalCache(tmp_path, "ns")

        cache_a.write(self.URL, body=b"body-a", etag='"a"')
        cache_b.write(self.URL, body=b"body-b", etag='"b"')

        entry = cache_a.read(self.URL)
        assert entry is not None
        assert (entry.body, entry.etag) == (b"body-b", '"b"')

        cache_a.write(self.URL, body=b"body-a", etag='"a"')
        entry = cache_b.read(self.URL)
        assert entry is not None
        assert (entry.body, entry.etag) == (b"body-a", '"a"')

    def test_delete(self, tmp_path):
        cache = DiskConditionalCache(tmp_path, "ns")
        cache.write(self.URL, body=b"body", etag='"v1"')
        cache.delete(self.URL)
        assert cache.read(self.URL) is None

        # Deleting a missing entry is a no-op.
        cache.delete(self.URL)

    def test_written_file_is_one_atomic_document(self, tmp_path):
        cache = DiskConditionalCache(tmp_path, "ns")
        cache.write(self.URL, body=b"body", etag='"v1"', last_modified="date")

        payload = json.loads(_cache_file(cache, self.URL).read_text())
        assert payload["version"] == 1
        assert payload["namespace"] == "ns"
        assert payload["url"] == self.URL
        assert payload["etag"] == '"v1"'
        assert payload["last_modified"] == "date"
        assert payload["body"]
