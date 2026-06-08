"""Shared test fixtures."""

from __future__ import annotations

import pytest

from secretservice_fetcher.backend import Backend
from secretservice_fetcher.config import RcSecret


@pytest.fixture(autouse=True)
def _clean_runtime_env(monkeypatch):
    """Ensure systemd's $RUNTIME_DIRECTORY doesn't leak into the fallback tests."""

    monkeypatch.delenv("RUNTIME_DIRECTORY", raising=False)


class FakeBackend(Backend):
    """In-memory backend mirroring the Secret Service (per-name items)."""

    def __init__(self) -> None:
        self.items: dict[tuple[tuple[str, str], ...], bytes] = {}
        self.labels: dict[tuple[tuple[str, str], ...], str] = {}

    @staticmethod
    def _key(ref: dict[str, str]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(ref.items()))

    def config_ref(self, rc: RcSecret, entry) -> dict[str, str]:
        return rc.effective_attributes(entry)

    def env_ref(self, rc: RcSecret, entry) -> dict[str, str]:
        return rc.effective_env_attributes(entry)

    def describe(self, ref: dict[str, str]) -> str:
        return " ".join(f"{k}={v}" for k, v in ref.items())

    def store(self, ref: dict[str, str], label: str, secret: bytes) -> None:
        self.items[self._key(ref)] = secret
        self.labels[self._key(ref)] = label

    def lookup(self, ref: dict[str, str]) -> bytes | None:
        return self.items.get(self._key(ref))

    def clear(self, ref: dict[str, str]) -> None:
        self.items.pop(self._key(ref), None)


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def rc(tmp_path) -> RcSecret:
    """A representative config with one file and two env vars (one renamed)."""

    model = RcSecret.model_validate(
        {
            "id": "demo",
            "runtime_dir": str(tmp_path / "run"),
            "defaults": {"attributes": {"app": "demo"}, "label_prefix": "demo"},
            "configs": [
                {
                    "name": "prod.yaml",
                    "attributes": {"kind": "config"},
                    "develop_path": str(tmp_path / "config" / "prod.yaml"),
                    "env": "APP_CONFIG",
                    "default": True,
                }
            ],
            "env": [
                {
                    "var": "O365_CLIENT_ID",
                    "name": "PERSONAL_O365_CLIENT_ID",
                    "attributes": {"kind": "env"},
                },
                {"var": "SHEET", "attributes": {"kind": "env"}},
                {"var": "MAYBE", "optional": True},
            ],
        }
    )
    (tmp_path / "run").mkdir()
    return model


@pytest.fixture
def stored(rc, fake_backend):
    """Pre-populate the fake backend with the rc's secrets."""

    cfg = rc.get("prod.yaml")
    fake_backend.store(fake_backend.config_ref(rc, cfg), "l", b"db: prod\n")
    fake_backend.store(
        fake_backend.env_ref(rc, rc.get_env("O365_CLIENT_ID")), "l", b"the-client-id\n"
    )
    fake_backend.store(fake_backend.env_ref(rc, rc.get_env("SHEET")), "l", b"Sheet1")
    return fake_backend
