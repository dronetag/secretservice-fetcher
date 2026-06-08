"""Tests for the pydantic config model and its derived helpers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from secretservice_fetcher.config import RcSecret, load


def test_effective_attributes_merge_and_implicit_name(rc):
    cfg = rc.get("prod.yaml")
    assert rc.effective_attributes(cfg) == {
        "app": "demo",
        "kind": "config",
        "name": "prod.yaml",
    }


def test_effective_label_uses_prefix(rc):
    assert rc.effective_label(rc.get("prod.yaml")) == "demo prod.yaml"


def test_env_attributes_and_rename(rc):
    entry = rc.get_env("O365_CLIENT_ID")
    # env var name and keyring (logical) name differ -> rename
    assert entry.logical_name() == "PERSONAL_O365_CLIENT_ID"
    assert rc.effective_env_attributes(entry)["name"] == "PERSONAL_O365_CLIENT_ID"
    # logical name defaults to var when not given
    assert rc.get_env("SHEET").logical_name() == "SHEET"


def test_identity_and_prepare_paths(rc, tmp_path):
    assert rc.identity() == "demo"
    base = tmp_path / "run" / "ss-fetcher" / "demo"
    assert rc.prepare_directory() == base
    assert rc.prepared_config_path(rc.get("prod.yaml")) == base / "prod.yaml"
    assert rc.env_file() == base / "env"


def test_prepare_directory_honours_systemd_runtime_directory(rc, monkeypatch, tmp_path):
    # When systemd provides $RUNTIME_DIRECTORY, that exact dir is used as-is.
    rd = tmp_path / "run" / "ss-fetcher" / "demo"
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(rd))
    assert rc.prepare_directory() == rd
    assert rc.env_file() == rd / "env"
    assert rc.prepared_config_path(rc.get("prod.yaml")) == rd / "prod.yaml"
    assert rc.runtime_base() == rd


def test_runtime_directory_takes_first_colon_entry(rc, monkeypatch, tmp_path):
    monkeypatch.setenv("RUNTIME_DIRECTORY", f"{tmp_path}/a:{tmp_path}/b")
    assert rc.prepare_directory() == tmp_path / "a"


def test_runtime_base_falls_back_without_systemd(rc, tmp_path):
    # autouse fixture clears RUNTIME_DIRECTORY -> falls back to runtime_dir.
    assert rc.runtime_base() == tmp_path / "run"


def test_identity_defaults_to_app_attribute():
    model = RcSecret.model_validate({"defaults": {"attributes": {"app": "svc"}}})
    assert model.identity() == "svc"
    assert RcSecret.model_validate({}).identity() == "default"


def test_vault_base_path_defaults_to_identity():
    model = RcSecret.model_validate({"id": "myapp", "backend": "vault"})
    assert model.vault_base_path() == "myapp"
    model2 = RcSecret.model_validate(
        {"id": "myapp", "backend": "vault", "vault": {"path": "team/myapp"}}
    )
    assert model2.vault_base_path() == "team/myapp"


def test_duplicate_config_names_rejected():
    with pytest.raises(ValidationError):
        RcSecret.model_validate({"configs": [{"name": "a"}, {"name": "a"}]})


def test_multiple_defaults_rejected():
    with pytest.raises(ValidationError):
        RcSecret.model_validate(
            {"configs": [{"name": "a", "default": True}, {"name": "b", "default": True}]}
        )


def test_duplicate_env_vars_rejected():
    with pytest.raises(ValidationError):
        RcSecret.model_validate({"env": [{"var": "X"}, {"var": "X"}]})


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        RcSecret.model_validate({"nonsense": 1})


def test_default_entry_requires_marking_when_many():
    model = RcSecret.model_validate({"configs": [{"name": "a"}, {"name": "b"}]})
    with pytest.raises(LookupError):
        model.default_entry()
    single = RcSecret.model_validate({"configs": [{"name": "a"}]})
    assert single.default_entry().name == "a"


def test_load_from_path(tmp_path):
    path = tmp_path / ".secretrc"
    path.write_text('id = "x"\n[[configs]]\nname = "c.yaml"\ndefault = true\n')
    model = load(path)
    assert model.source_path == path
    assert model.get("c.yaml").default is True
