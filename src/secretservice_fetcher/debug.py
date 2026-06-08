"""Opt-in verbose debug tracing to stderr.

Enabled by ``SSFETCHER_DEBUG=1`` in the environment or ``ss-fetcher -v``. Traces
go to *stderr* (stdout stays clean for `load`/`get-env`/`env-export` output) and
**never include secret values** -- only metadata (attributes, labels, byte
counts) useful for figuring out why a lookup misses.
"""

from __future__ import annotations

import os
import sys

_TRUTHY = {"1", "true", "yes", "on", "debug"}
_enabled = os.environ.get("SSFETCHER_DEBUG", "").strip().lower() in _TRUTHY


def enable(on: bool = True) -> None:
    """Turn tracing on (e.g. from the ``-v`` CLI flag). Never turns it off."""

    global _enabled
    if on:
        _enabled = True


def enabled() -> bool:
    return _enabled


def log(msg: str) -> None:
    if _enabled:
        sys.stderr.write(f"ss-fetcher[debug] {msg}\n")
        sys.stderr.flush()
