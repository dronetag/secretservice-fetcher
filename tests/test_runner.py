"""Tests for materialising secrets and running wrapped commands."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from secretservice_fetcher import runner
from secretservice_fetcher.config import RcSecret

# A tiny program that records how it received its secrets.
PROG = r"""
import os, json, sys
out, cfg = sys.argv[1], sys.argv[2]
json.dump({
    "config_arg": cfg,
    "config_exists": os.path.exists(cfg),
    "config_text": open(cfg).read() if os.path.exists(cfg) else None,
    "APP_CONFIG": os.environ.get("APP_CONFIG"),
    "O365_CLIENT_ID": os.environ.get("O365_CLIENT_ID"),
    "SHEET": os.environ.get("SHEET"),
    "MAYBE": os.environ.get("MAYBE", "<unset>"),
}, open(out, "w"))
"""


def _run(rc, backend, out, *, develop=False):
    code = runner.run(
        rc,
        [rc.get("prod.yaml")],
        backend,
        [sys.executable, "-c", PROG, str(out), "{config}"],
        default_name="prod.yaml",
        develop=develop,
        env_entries=rc.env,
    )
    return code, json.loads(out.read_text())


# --- env_value -------------------------------------------------------------


def test_env_value_strips_single_trailing_newline():
    assert runner.env_value(b"x\n") == "x"
    assert runner.env_value(b"x") == "x"
    assert runner.env_value(b"x\r\n") == "x"
    assert runner.env_value(b"a\nb\n") == "a\nb"  # embedded newline preserved


# --- materialise -----------------------------------------------------------


def test_materialise_writes_private_file(rc, stored, tmp_path):
    out_dir = tmp_path / "mat"
    out_dir.mkdir()
    path = runner.materialise(rc, rc.get("prod.yaml"), stored, out_dir)
    assert path.read_text() == "db: prod\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_materialise_missing_raises(rc, fake_backend, tmp_path):
    with pytest.raises(LookupError, match="not found"):
        runner.materialise(rc, rc.get("prod.yaml"), fake_backend, tmp_path)


def test_materialise_develop_uses_existing_file(rc, fake_backend, tmp_path):
    dev = rc.get("prod.yaml").develop_path
    dev.parent.mkdir(parents=True)
    dev.write_text("local edit")
    # No secret stored, but develop mode reads the on-disk file.
    path = runner.materialise(rc, rc.get("prod.yaml"), fake_backend, tmp_path, develop=True)
    assert path == dev
    assert path.read_text() == "local edit"


# --- load_env --------------------------------------------------------------


def test_load_env_injects_and_renames(rc, stored):
    values = runner.load_env(rc, rc.env, stored)
    assert values == {"O365_CLIENT_ID": "the-client-id", "SHEET": "Sheet1"}
    assert "MAYBE" not in values  # optional + missing -> skipped


def test_load_env_required_missing_raises(rc, fake_backend):
    entry = rc.get_env("SHEET")  # required, not stored
    with pytest.raises(LookupError, match="SHEET"):
        runner.load_env(rc, [entry], fake_backend)


# --- run -------------------------------------------------------------------


def test_run_expands_file_and_env(rc, stored, tmp_path):
    out = tmp_path / "out.json"
    code, data = _run(rc, stored, out)
    assert code == 0
    # config materialised, placeholder substituted, and the same path in env
    assert data["config_exists"] is True
    assert data["config_text"] == "db: prod\n"
    assert data["config_arg"] == data["APP_CONFIG"]
    # env-var secrets injected (with the rename)
    assert data["O365_CLIENT_ID"] == "the-client-id"
    assert data["SHEET"] == "Sheet1"
    assert data["MAYBE"] == "<unset>"


def test_run_cleans_up_temp_file(rc, stored, tmp_path):
    out = tmp_path / "out.json"
    _, data = _run(rc, stored, out)
    # The materialised path existed during the run but is gone afterwards.
    assert not os.path.exists(data["config_arg"])


def test_run_develop_uses_on_disk_file(rc, fake_backend, stored, tmp_path):
    dev = rc.get("prod.yaml").develop_path
    dev.parent.mkdir(parents=True)
    dev.write_text("LOCAL\n")
    out = tmp_path / "out.json"
    code, data = _run(rc, stored, out, develop=True)
    assert code == 0
    assert data["config_text"] == "LOCAL\n"
    assert data["config_arg"] == str(dev)  # used the develop path directly


def test_run_propagates_exit_code(rc, stored):
    code = runner.run(
        rc,
        [],
        stored,
        [sys.executable, "-c", "import sys; sys.exit(7)"],
        default_name=None,
        env_entries=[],
    )
    assert code == 7


# --- prepare / cleanup -----------------------------------------------------


def test_prepare_writes_config_and_env_file(rc, stored):
    result = runner.prepare(rc, stored)
    cfg = result.configs["prod.yaml"]
    assert cfg == rc.prepared_config_path(rc.get("prod.yaml"))
    assert cfg.read_text() == "db: prod\n"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600

    assert result.env_file == rc.env_file()
    env_text = result.env_file.read_text()
    assert 'O365_CLIENT_ID="the-client-id"' in env_text
    assert 'SHEET="Sheet1"' in env_text
    assert "MAYBE" not in env_text  # optional missing skipped
    assert stat.S_IMODE(result.env_file.stat().st_mode) == 0o600


def test_prepare_env_file_is_sourceable(rc, stored, tmp_path):
    # Values round-trip through a POSIX `set -a; . env` source.
    runner.prepare(rc, stored)
    import subprocess

    script = f'set -a; . "{rc.env_file()}"; set +a; printf "%s|%s" "$O365_CLIENT_ID" "$SHEET"'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.stdout == "the-client-id|Sheet1"


def test_cleanup_removes_prepare_dir(rc, stored):
    runner.prepare(rc, stored)
    assert rc.prepare_directory().exists()
    runner.cleanup(rc)
    assert not rc.prepare_directory().exists()


def test_prepare_writes_into_systemd_runtime_directory(rc, stored, monkeypatch, tmp_path):
    # As if started by a unit with RuntimeDirectory=ss-fetcher/demo.
    rd = tmp_path / "sd-runtime"
    rd.mkdir()
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(rd))
    result = runner.prepare(rc, stored)
    assert result.directory == rd
    assert (rd / "prod.yaml").read_text() == "db: prod\n"
    assert (rd / "env").exists()


def test_format_env_line_escapes():
    assert runner._format_env_line("K", 'a"b\\c') == 'K="a\\"b\\\\c"'
    assert runner._format_env_line("K", "line1\nline2") == 'K="line1\\nline2"'


# --- writeback (refresh-token rotation) ------------------------------------

# Appends to the config file given as argv[1] (mimics a token refresh on disk).
APPEND_PROG = "import sys; open(sys.argv[1], 'a').write('REFRESHED')"


def _writeback_rc(tmp_path):
    (tmp_path / "run").mkdir(exist_ok=True)
    return RcSecret.model_validate(
        {
            "id": "demo",
            "runtime_dir": str(tmp_path / "run"),
            "defaults": {"attributes": {"app": "demo"}},
            "configs": [
                {
                    "name": "prod.yaml",
                    "attributes": {"kind": "config"},
                    "writeback": True,
                    "default": True,
                }
            ],
        }
    )


def _count_stores(backend):
    calls = []
    original = backend.store

    def counting(ref, label, secret):
        calls.append(ref)
        original(ref, label, secret)

    backend.store = counting
    return calls


def test_run_writeback_saves_changed_config(fake_backend, tmp_path):
    rc = _writeback_rc(tmp_path)
    cfg = rc.get("prod.yaml")
    fake_backend.store(fake_backend.config_ref(rc, cfg), "l", b"orig\n")
    calls = _count_stores(fake_backend)

    code = runner.run(
        rc,
        [cfg],
        fake_backend,
        [sys.executable, "-c", APPEND_PROG, "{config}"],
        default_name="prod.yaml",
        env_entries=[],
    )
    assert code == 0
    # the rotated file was saved back
    assert fake_backend.lookup(fake_backend.config_ref(rc, cfg)) == b"orig\nREFRESHED"
    assert len(calls) == 1


def test_run_writeback_skips_unchanged(fake_backend, tmp_path):
    rc = _writeback_rc(tmp_path)
    cfg = rc.get("prod.yaml")
    fake_backend.store(fake_backend.config_ref(rc, cfg), "l", b"orig\n")
    calls = _count_stores(fake_backend)
    # program reads but does not modify the file
    runner.run(
        rc,
        [cfg],
        fake_backend,
        [sys.executable, "-c", "pass"],
        default_name="prod.yaml",
        env_entries=[],
    )
    assert calls == []  # nothing written back


def test_run_writeback_ignored_in_develop_mode(fake_backend, tmp_path):
    rc = _writeback_rc(tmp_path)
    cfg = rc.get("prod.yaml")
    rc.configs[0].develop_path = tmp_path / "prod.yaml"
    rc.configs[0].develop_path.write_text("local\n")
    calls = _count_stores(fake_backend)
    runner.run(
        rc,
        [cfg],
        fake_backend,
        [sys.executable, "-c", APPEND_PROG, "{config}"],
        default_name="prod.yaml",
        env_entries=[],
        develop=True,
    )
    assert calls == []  # develop edits are saved via `save`, not auto-written


def test_prepare_cleanup_writeback(fake_backend, tmp_path):
    rc = _writeback_rc(tmp_path)
    cfg = rc.get("prod.yaml")
    fake_backend.store(fake_backend.config_ref(rc, cfg), "l", b"orig\n")
    runner.prepare(rc, fake_backend)
    prepared = rc.prepared_config_path(cfg)
    assert (rc.prepare_directory() / runner.WRITEBACK_MANIFEST).is_file()

    prepared.write_text("orig\nREFRESHED")  # the service rotated the token
    runner.cleanup(rc, fake_backend)
    assert fake_backend.lookup(fake_backend.config_ref(rc, cfg)) == b"orig\nREFRESHED"
    assert not rc.prepare_directory().exists()


def test_cleanup_writeback_skips_unchanged(fake_backend, tmp_path):
    rc = _writeback_rc(tmp_path)
    cfg = rc.get("prod.yaml")
    fake_backend.store(fake_backend.config_ref(rc, cfg), "l", b"orig\n")
    runner.prepare(rc, fake_backend)
    calls = _count_stores(fake_backend)
    runner.cleanup(rc, fake_backend)  # file untouched
    assert calls == []


def test_cleanup_without_backend_still_removes(fake_backend, tmp_path):
    rc = _writeback_rc(tmp_path)
    cfg = rc.get("prod.yaml")
    fake_backend.store(fake_backend.config_ref(rc, cfg), "l", b"orig\n")
    runner.prepare(rc, fake_backend)
    runner.cleanup(rc)  # no backend -> no writeback, but dir is gone
    assert not rc.prepare_directory().exists()
