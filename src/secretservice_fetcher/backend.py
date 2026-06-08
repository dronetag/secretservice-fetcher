"""Secret storage backends.

Two backends are provided behind a common :class:`Backend` interface:

* :class:`SecretServiceBackend` -- the freedesktop Secret Service over D-Bus via
  ``secretstorage`` (GNOME Keyring, KWallet, KeePassXC, ...).
* :class:`VaultBackend` -- HashiCorp Vault's KV secrets engine, spoken over its
  HTTP API with the standard library (no extra dependency). The token is taken
  from an environment variable.

A *ref* is whatever a backend uses to address one secret. Callers never build
one directly: they ask the backend for ``config_ref(rc, entry)`` /
``env_ref(rc, entry)`` and pass the opaque result back to ``store`` / ``lookup``
/ ``clear`` / ``exists`` / ``describe``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import ConfigEntry, EnvEntry, RcSecret


class BackendError(RuntimeError):
    """Base class for backend failures."""


class SecretServiceError(BackendError):
    """Raised when the freedesktop Secret Service can't be reached or used."""


# Backwards-compatible alias (older name).
SecretToolError = SecretServiceError


class VaultError(BackendError):
    """Raised when a Vault request fails or is misconfigured."""


class Backend(ABC):
    """Common interface for secret storage backends."""

    @abstractmethod
    def config_ref(self, rc: RcSecret, entry: ConfigEntry) -> Any:
        """Return the backend ref addressing a config file's secret."""

    @abstractmethod
    def env_ref(self, rc: RcSecret, entry: EnvEntry) -> Any:
        """Return the backend ref addressing an env var's secret."""

    @abstractmethod
    def store(self, ref: Any, label: str, secret: bytes) -> None: ...

    @abstractmethod
    def lookup(self, ref: Any) -> bytes | None: ...

    def lookup_many(self, refs: list[Any]) -> list[bytes | None]:
        """Look up several secrets at once (order preserved).

        The default loops over :meth:`lookup`; backends override this to do it in
        a single round-trip (so the store is hit -- and, where applicable, an
        unlock prompt is shown -- only once).
        """

        return [self.lookup(ref) for ref in refs]

    @abstractmethod
    def clear(self, ref: Any) -> None: ...

    @abstractmethod
    def describe(self, ref: Any) -> str:
        """A short human-readable description of where a secret lives."""

    def exists(self, ref: Any) -> bool:
        return self.lookup(ref) is not None


# --------------------------------------------------------------------------
# Secret Service (freedesktop, via the secretstorage D-Bus client)
# --------------------------------------------------------------------------


class SecretServiceBackend(Backend):
    """Store/lookup/clear secrets via the Secret Service D-Bus API.

    Uses ``secretstorage`` (a required dependency) so everything happens over a
    single D-Bus connection. Items are *unlocked* before reading or deleting,
    which is what makes it work with providers -- notably KeePassXC -- that
    advertise items as locked even while their database is open (and is why
    ``clear``/``rm`` works here where the ``secret-tool`` CLI could not).
    """

    def __init__(self) -> None:
        try:
            import secretstorage
            from secretstorage.util import unlock_objects
        except ImportError as exc:  # pragma: no cover - dependency is required
            raise SecretServiceError(
                "the secret-service backend needs `secretstorage` "
                "(installed automatically with secretservice-fetcher)"
            ) from exc
        self._ss = secretstorage
        self._unlock_objects = unlock_objects
        try:
            self._conn = secretstorage.dbus_init()
        except Exception as exc:  # noqa: BLE001 - surface any D-Bus failure cleanly
            raise SecretServiceError(f"cannot reach the Secret Service over D-Bus: {exc}") from exc

    # --- ref derivation ---------------------------------------------------

    def config_ref(self, rc: RcSecret, entry: ConfigEntry) -> dict[str, str]:
        return rc.effective_attributes(entry)

    def env_ref(self, rc: RcSecret, entry: EnvEntry) -> dict[str, str]:
        return rc.effective_env_attributes(entry)

    def describe(self, ref: dict[str, str]) -> str:
        return " ".join(f"{k}={v}" for k, v in ref.items())

    # --- helpers ----------------------------------------------------------

    def _collection(self) -> Any:
        collection = self._ss.get_default_collection(self._conn)
        if collection.is_locked():
            collection.unlock()
        return collection

    def _unlock(self, items: list[Any]) -> None:
        paths = [it.item_path for it in items if it is not None and it.is_locked()]
        if paths:
            self._unlock_objects(self._conn, paths)  # one call for the whole batch

    # --- Backend interface ------------------------------------------------

    def store(self, ref: dict[str, str], label: str, secret: bytes) -> None:
        try:
            self._collection().create_item(label, ref, secret, replace=True)
        except Exception as exc:  # noqa: BLE001
            raise SecretServiceError(f"Secret Service store failed: {exc}") from exc

    def lookup(self, ref: dict[str, str]) -> bytes | None:
        return self.lookup_many([ref])[0]

    def lookup_many(self, refs: list[dict[str, str]]) -> list[bytes | None]:
        if not refs:
            return []
        try:
            collection = self._collection()
            items = [next(iter(collection.search_items(ref)), None) for ref in refs]
            self._unlock(items)  # unlock every found item in a single call
            return [bytes(it.get_secret()) if it is not None else None for it in items]
        except Exception as exc:  # noqa: BLE001
            raise SecretServiceError(f"Secret Service lookup failed: {exc}") from exc

    def clear(self, ref: dict[str, str]) -> None:
        try:
            items = list(self._collection().search_items(ref))
            self._unlock(items)
            for item in items:
                item.delete()
        except Exception as exc:  # noqa: BLE001
            raise SecretServiceError(f"Secret Service delete failed: {exc}") from exc

    def exists(self, ref: dict[str, str]) -> bool:
        # Cheap presence check: search only, no unlock/decrypt (avoids a prompt).
        try:
            return next(iter(self._collection().search_items(ref)), None) is not None
        except Exception as exc:  # noqa: BLE001
            raise SecretServiceError(f"Secret Service search failed: {exc}") from exc


# Backwards-compatible alias (older name).
SecretToolBackend = SecretServiceBackend


# --------------------------------------------------------------------------
# HashiCorp Vault (KV v1/v2)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VaultRef:
    """Addresses a single field inside a Vault KV secret path."""

    path: str
    field: str
    # If True, ``clear`` deletes the whole path; else just removes the field.
    whole_path: bool = False


class VaultBackend(Backend):
    """Talk to Vault's KV engine over HTTP using only the standard library."""

    def __init__(
        self,
        addr: str,
        token: str,
        *,
        mount: str = "secret",
        kv_version: int = 2,
        namespace: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.addr = addr.rstrip("/")
        self.token = token
        self.mount = mount.strip("/")
        self.kv_version = kv_version
        self.namespace = namespace
        self.timeout = timeout

    # --- ref derivation ---------------------------------------------------

    def config_ref(self, rc: RcSecret, entry: ConfigEntry) -> VaultRef:
        base = rc.vault_base_path()
        return VaultRef(path=f"{base}/{entry.name}", field="value", whole_path=True)

    def env_ref(self, rc: RcSecret, entry: EnvEntry) -> VaultRef:
        base = rc.vault_base_path()
        env_path = rc.vault.env_path if rc.vault else "env"
        return VaultRef(path=f"{base}/{env_path}", field=entry.logical_name())

    def describe(self, ref: VaultRef) -> str:
        return f"{self.mount}/{ref.path}#{ref.field}"

    # --- HTTP -------------------------------------------------------------

    def _data_url(self, path: str) -> str:
        seg = "data" if self.kv_version == 2 else None
        parts = [self.addr, "v1", self.mount]
        if seg:
            parts.append(seg)
        parts.append(path.strip("/"))
        return "/".join(parts)

    def _metadata_url(self, path: str) -> str:
        parts = [self.addr, "v1", self.mount, "metadata", path.strip("/")]
        return "/".join(parts)

    def _http(self, method: str, url: str, body: dict[str, Any] | None) -> tuple[int, bytes]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("X-Vault-Token", self.token)
        if self.namespace:
            request.add_header("X-Vault-Namespace", self.namespace)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise VaultError(f"cannot reach Vault at {self.addr}: {exc.reason}") from exc

    @staticmethod
    def _fail(action: str, path: str, status: int, raw: bytes) -> VaultError:
        body = raw.decode(errors="replace")
        return VaultError(f"Vault {action} {path!r} failed (HTTP {status}): {body}")

    def _read_fields(self, path: str) -> dict[str, str] | None:
        status, raw = self._http("GET", self._data_url(path), None)
        if status == 404:
            return None
        if status != 200:
            raise self._fail("read", path, status, raw)
        payload = json.loads(raw)
        data = payload.get("data", {})
        # KV v2 nests the secret under data.data; KV v1 has it under data.
        fields = data.get("data") if self.kv_version == 2 else data
        return fields or {}

    def _write_fields(self, path: str, fields: dict[str, str]) -> None:
        body = {"data": fields} if self.kv_version == 2 else fields
        status, raw = self._http("POST", self._data_url(path), body)
        if status not in (200, 204):
            raise self._fail("write", path, status, raw)

    def _delete_path(self, path: str) -> None:
        url = self._metadata_url(path) if self.kv_version == 2 else self._data_url(path)
        status, raw = self._http("DELETE", url, None)
        if status not in (200, 204, 404):
            raise self._fail("delete", path, status, raw)

    # --- Backend interface ------------------------------------------------

    def store(self, ref: VaultRef, label: str, secret: bytes) -> None:
        # Merge into any existing fields so sibling env vars are preserved.
        fields = self._read_fields(ref.path) or {}
        fields[ref.field] = secret.decode("utf-8")
        self._write_fields(ref.path, fields)

    def lookup(self, ref: VaultRef) -> bytes | None:
        fields = self._read_fields(ref.path)
        if fields is None or ref.field not in fields:
            return None
        return fields[ref.field].encode("utf-8")

    def lookup_many(self, refs: list[VaultRef]) -> list[bytes | None]:
        # Read each distinct path once -- all [[env]] secrets share one path, so
        # this is a single GET regardless of how many env vars there are.
        cache: dict[str, dict[str, str] | None] = {}
        results: list[bytes | None] = []
        for ref in refs:
            if ref.path not in cache:
                cache[ref.path] = self._read_fields(ref.path)
            fields = cache[ref.path]
            if fields is None or ref.field not in fields:
                results.append(None)
            else:
                results.append(fields[ref.field].encode("utf-8"))
        return results

    def clear(self, ref: VaultRef) -> None:
        if ref.whole_path:
            self._delete_path(ref.path)
            return
        fields = self._read_fields(ref.path)
        if not fields or ref.field not in fields:
            return
        del fields[ref.field]
        if fields:
            self._write_fields(ref.path, fields)
        else:
            self._delete_path(ref.path)


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------


def make_backend(rc: RcSecret) -> Backend:
    """Construct the backend selected by the ``.secretrc``."""

    if rc.backend == "vault":
        cfg = rc.vault
        token = os.environ.get(cfg.token_env)
        if not token:
            raise VaultError(
                f"Vault token not found in ${cfg.token_env}; "
                f"export it, e.g. `export {cfg.token_env}=$(vault print token)`"
            )
        addr = cfg.addr or os.environ.get(cfg.addr_env)
        if not addr:
            raise VaultError(
                f"Vault address not set; set `vault.addr` in .secretrc or export ${cfg.addr_env}"
            )
        namespace = cfg.namespace or (
            os.environ.get(cfg.namespace_env) if cfg.namespace_env else None
        )
        return VaultBackend(
            addr,
            token,
            mount=cfg.mount,
            kv_version=cfg.kv_version,
            namespace=namespace,
        )
    return SecretServiceBackend()
