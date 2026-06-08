"""secretservice-fetcher: fetch config files and env secrets (CLI: ss-fetcher)."""

from .__version__ import version as __version__
from .config import RcSecret, load

__all__ = ["RcSecret", "load", "__version__"]
