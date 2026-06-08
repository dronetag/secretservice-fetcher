"""Tests for the backend interface, ref derivation, and the Vault client."""

from __future__ import annotations

import json
import types

import pytest

from secretservice_fetcher import backend as backend_mod
from secretservice_fetcher.backend import (
    SecretServiceBackend,
    VaultBackend,
    VaultError,
    VaultRef,
    make_backend,
)
from secretservice_fetcher.config import RcSecret


# --- Secret Service (fake secretstorage; no real D-Bus) --------------------


def test_secret_service_refs(rc):
    be = object.__new__(SecretServiceBackend)  # skip __init__ (no D-Bus)
    cfg_ref = be.config_ref(rc, rc.get("prod.yaml"))
    assert cfg_ref == {"app": "demo", "kind": "config", "name": "prod.yaml"}
    assert "name=prod.yaml" in be.describe(cfg_ref)
    env_ref = be.env_ref(rc, rc.get_env("O365_CLIENT_ID"))
    assert env_ref["name"] == "PERSONAL_O365_CLIENT_ID"


def _make_ss_backend(initial=None):
    """A SecretServiceBackend wired to an in-memory fake secretstorage.

    Pre-existing items start *locked* (like KeePassXC); freshly created ones are
    unlocked. `get_secret` requires the item to be unlocked first.
    """

    def key(attrs):
        return tuple(sorted(attrs.items()))

    store = {key(a): s for a, s in (initial or [])}
    unlocked: set = set()
    calls = {"unlock": 0}

    def path_of(k):
        return "/item/" + ";".join(f"{a}={b}" for a, b in k)

    class Item:
        def __init__(self, k):
            self.k = k
            self.item_path = path_of(k)

        def is_locked(self):
            return self.k not in unlocked

        def get_secret(self):
            if self.is_locked():
                raise RuntimeError("Item is locked!")
            return store[self.k]

        def delete(self):
            store.pop(self.k, None)

    class Collection:
        def is_locked(self):
            return False

        def unlock(self):
            pass

        def search_items(self, attrs):
            for sk in list(store):
                sd = dict(sk)
                if all(sd.get(a) == b for a, b in attrs.items()):
                    yield Item(sk)

        def create_item(self, label, attrs, secret, replace=False):
            k = key(attrs)
            store[k] = secret
            unlocked.add(k)
            return Item(k)

    def unlock_objects(conn, paths):
        calls["unlock"] += 1
        for sk in list(store):
            if path_of(sk) in paths:
                unlocked.add(sk)
        return False

    be = object.__new__(SecretServiceBackend)
    be._ss = types.SimpleNamespace(
        get_default_collection=lambda conn: Collection(),
        get_all_collections=lambda conn: [Collection()],
        # Service-wide search (across all collections), as the real module
        # provides; here there is one collection backed by the same store.
        search_items=lambda conn, attrs: Collection().search_items(attrs),
    )
    be._conn = object()
    be._unlock_objects = unlock_objects
    return be, store, calls


def test_secret_service_store_and_lookup_roundtrip():
    be, _store, _calls = _make_ss_backend()
    ref = {"app": "a", "kind": "env", "name": "X"}
    assert be.lookup(ref) is None
    be.store(ref, "label", b"value")
    assert be.lookup(ref) == b"value"
    assert be.exists(ref) is True


def test_secret_service_lookup_many_unlocks_once():
    a = {"app": "a", "kind": "env", "name": "A"}
    b = {"app": "a", "kind": "env", "name": "B"}
    be, _store, calls = _make_ss_backend([(a, b"aval"), (b, b"bval")])  # start locked
    out = be.lookup_many([a, b, {"app": "a", "kind": "env", "name": "MISSING"}])
    assert out == [b"aval", b"bval", None]
    assert calls["unlock"] == 1  # one unlock call for the whole batch


def test_secret_service_clear_deletes_after_unlock():
    ref = {"app": "a", "kind": "env", "name": "A"}
    be, store, _calls = _make_ss_backend([(ref, b"v")])  # locked
    be.clear(ref)
    assert be.lookup(ref) is None
    assert store == {}


def test_secret_service_exists_does_not_unlock():
    ref = {"app": "a", "kind": "env", "name": "A"}
    be, _store, calls = _make_ss_backend([(ref, b"v")])
    assert be.exists(ref) is True
    assert be.exists({"app": "a", "kind": "env", "name": "nope"}) is False
    assert calls["unlock"] == 0  # presence check never unlocks/decrypts


def test_secret_service_lookup_falls_back_to_other_collection():
    """A secret in a non-default collection (or behind an unstable `default`
    alias) is still found, via the service-wide search -- like `secret-tool`."""

    ref = {"app": "a", "kind": "env", "name": "A"}

    class Item:
        item_path = "/item/A"

        def is_locked(self):
            return False

        def get_secret(self):
            return b"val"

    class EmptyDefault:  # the default collection holds nothing
        def is_locked(self):
            return False

        def unlock(self):
            pass

        def search_items(self, attrs):
            return iter(())

    be = object.__new__(SecretServiceBackend)
    be._ss = types.SimpleNamespace(
        get_default_collection=lambda conn: EmptyDefault(),
        get_all_collections=lambda conn: [EmptyDefault()],
        search_items=lambda conn, attrs: iter([Item()]),  # but it exists service-wide
    )
    be._conn = object()
    be._unlock_objects = lambda conn, paths: False

    assert be.lookup(ref) == b"val"
    assert be.lookup_many([ref]) == [b"val"]
    assert be.exists(ref) is True


def test_secret_service_errors_are_wrapped():
    from secretservice_fetcher.backend import SecretServiceError

    be = object.__new__(SecretServiceBackend)
    be._ss = types.SimpleNamespace(
        get_default_collection=lambda conn: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    be._conn = object()
    be._unlock_objects = lambda conn, paths: None
    with pytest.raises(SecretServiceError, match="lookup failed"):
        be.lookup({"name": "x"})


# --- Vault ref derivation --------------------------------------------------


@pytest.fixture
def vault_rc():
    return RcSecret.model_validate(
        {
            "id": "demo",
            "backend": "vault",
            "vault": {"addr": "https://v.example:8200", "mount": "secret"},
            "configs": [{"name": "prod.yaml", "default": True}],
            "env": [{"var": "TOKEN_A"}, {"var": "TOKEN_B"}],
        }
    )


def test_vault_config_ref_is_dedicated_path(vault_rc):
    be = VaultBackend("https://v.example:8200", "t", mount="secret")
    ref = be.config_ref(vault_rc, vault_rc.get("prod.yaml"))
    assert ref == VaultRef(path="demo/prod.yaml", field="value", whole_path=True)
    assert be.describe(ref) == "secret/demo/prod.yaml#value"


def test_vault_env_refs_group_under_one_path(vault_rc):
    be = VaultBackend("https://v.example:8200", "t")
    a = be.env_ref(vault_rc, vault_rc.get_env("TOKEN_A"))
    b = be.env_ref(vault_rc, vault_rc.get_env("TOKEN_B"))
    assert a == VaultRef(path="demo/env", field="TOKEN_A")
    assert b == VaultRef(path="demo/env", field="TOKEN_B")


@pytest.mark.parametrize("version", [1, 2])
def test_vault_url_building(version):
    be = VaultBackend("https://v/", "t", mount="kv", kv_version=version)
    if version == 2:
        assert be._data_url("a/b") == "https://v/v1/kv/data/a/b"
    else:
        assert be._data_url("a/b") == "https://v/v1/kv/a/b"
    assert be._metadata_url("a/b") == "https://v/v1/kv/metadata/a/b"


# --- Vault HTTP roundtrip (fake transport) ---------------------------------


def _fake_http(version: int, mount: str = "secret"):
    store: dict[str, dict[str, str]] = {}

    def http(method: str, url: str, body):
        tail = url.split(f"/v1/{mount}/", 1)[1]
        if version == 2 and tail.startswith("metadata/"):
            path = tail[len("metadata/") :]
        elif version == 2:
            path = tail[len("data/") :]
        else:
            path = tail
        if method == "GET":
            if path not in store:
                return 404, b""
            payload = {"data": {"data": store[path]}} if version == 2 else {"data": store[path]}
            return 200, json.dumps(payload).encode()
        if method == "POST":
            store[path] = body["data"] if version == 2 else body
            return 204, b""
        if method == "DELETE":
            store.pop(path, None)
            return 204, b""
        raise AssertionError(method)

    return http, store


@pytest.mark.parametrize("version", [1, 2])
def test_vault_config_store_lookup_clear(version):
    be = VaultBackend("https://v", "t", kv_version=version)
    be._http, store = _fake_http(version)
    ref = VaultRef(path="demo/prod.yaml", field="value", whole_path=True)

    assert be.lookup(ref) is None
    be.store(ref, "label", b"db: prod\n")
    assert be.lookup(ref) == b"db: prod\n"
    assert be.exists(ref) is True
    be.clear(ref)
    assert be.lookup(ref) is None


@pytest.mark.parametrize("version", [1, 2])
def test_vault_env_fields_share_path_and_merge(version):
    be = VaultBackend("https://v", "t", kv_version=version)
    be._http, store = _fake_http(version)
    a = VaultRef(path="demo/env", field="A")
    b = VaultRef(path="demo/env", field="B")

    be.store(a, "l", b"aval")
    be.store(b, "l", b"bval")
    # both live under one path (idiomatic Vault layout)
    assert store["demo/env"] == {"A": "aval", "B": "bval"}
    assert be.lookup(a) == b"aval"
    assert be.lookup(b) == b"bval"

    be.clear(a)  # removes only field A, keeps B
    assert be.lookup(a) is None
    assert be.lookup(b) == b"bval"


def test_vault_lookup_many_reads_each_path_once():
    be = VaultBackend("https://v", "t", kv_version=2)
    http, store = _fake_http(2)
    calls = {"GET": 0}

    def counting(method, url, body):
        if method == "GET":
            calls["GET"] += 1
        return http(method, url, body)

    be._http = counting
    a = VaultRef(path="demo/env", field="A")
    b = VaultRef(path="demo/env", field="B")
    be.store(a, "l", b"aval")
    be.store(b, "l", b"bval")
    calls["GET"] = 0  # reset after the read-modify-write stores

    out = be.lookup_many([a, b, VaultRef(path="demo/env", field="MISSING")])
    assert out == [b"aval", b"bval", None]
    assert calls["GET"] == 1  # all three fields came from a single read


def test_vault_request_sets_token_and_namespace(monkeypatch):
    captured = {}

    class FakeResp:
        status = 200

        def read(self):
            return json.dumps({"data": {"data": {"value": "x"}}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = dict(request.header_items())
        return FakeResp()

    monkeypatch.setattr(backend_mod.urllib.request, "urlopen", fake_urlopen)
    be = VaultBackend("https://v", "s3cr3t", mount="secret", namespace="team/x")
    be.lookup(VaultRef(path="demo/prod.yaml", field="value"))

    assert captured["url"] == "https://v/v1/secret/data/demo/prod.yaml"
    assert captured["method"] == "GET"
    assert captured["headers"]["X-vault-token"] == "s3cr3t"
    assert captured["headers"]["X-vault-namespace"] == "team/x"


# --- factory ---------------------------------------------------------------


def test_make_backend_vault_reads_token_from_env(monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "tok")
    monkeypatch.setenv("VAULT_ADDR", "https://vault:8200")
    rc = RcSecret.model_validate({"id": "x", "backend": "vault"})
    be = make_backend(rc)
    assert isinstance(be, VaultBackend)
    assert be.token == "tok"
    assert be.addr == "https://vault:8200"


def test_make_backend_vault_custom_token_env(monkeypatch):
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    monkeypatch.setenv("MY_TOKEN", "abc")
    rc = RcSecret.model_validate(
        {"id": "x", "backend": "vault", "vault": {"addr": "https://v", "token_env": "MY_TOKEN"}}
    )
    assert make_backend(rc).token == "abc"


def test_make_backend_vault_missing_token_errors(monkeypatch):
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    rc = RcSecret.model_validate({"id": "x", "backend": "vault", "vault": {"addr": "https://v"}})
    with pytest.raises(VaultError, match="token"):
        make_backend(rc)


def test_make_backend_vault_missing_addr_errors(monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "tok")
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    rc = RcSecret.model_validate({"id": "x", "backend": "vault"})
    with pytest.raises(VaultError, match="address"):
        make_backend(rc)
