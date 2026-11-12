from importlib.metadata import version

from .disk_cache import CacheEntry, DiskCacheError, DiskConditionalCache

__version__ = version("ultimate-sitemap-parser")

__all__ = [
    "tree",
    "__version__",
    "CacheEntry",
    "DiskCacheError",
    "DiskConditionalCache",
]
