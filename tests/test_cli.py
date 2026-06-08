"""CLI tests driven through click's CliRunner with a fake backend."""

from __future__ import annotations

import json
import sys

import pytest
from click.testing import CliRunner

from secretservice_fetcher import cli


@pytest.fixture
def rc_path(tmp_path):
    (tmp_path / "run").mkdir(exist_ok=True)
    path = tmp_path / ".secretrc"
    path.write_text(
        f"""
id = "demo"
runtime_dir = "{tmp_path / "run"}"
[defaults]
attributes = {{ app = "demo" }}
label_prefix = "demo"
[[configs]]
name = "prod.yaml"
attributes = {{ kind = "config" }}
develop_path = "{tmp_path / "config" / "prod.yaml"}"
env = "APP_CONFIG"
default = true
[[env]]
var = "O365_CLIENT_ID"
name = "PERSONAL_O365_CLIENT_ID"
attributes = {{ kind = "env" }}
[[env]]
var = "SHEET"
attributes = {{ kind = "env" }}
[[env]]
var = "MAYBE"
optional = true
"""
    )
    return path


@pytest.fixture
def invoke(monkeypatch, stored, rc_path):
    """Invoke the CLI with the fake backend wired in."""

    monkeypatch.setattr(cli, "make_backend", lambda rc: stored)
    runner = CliRunner()

    def run(*args, **kwargs):
        return runner.invoke(cli.main, ["-r", str(rc_path), *args], **kwargs)

    run.backend = stored  # type: ignore[attr-defined]
    return run


def test_list_shows_configs_and_env(invoke):
    res = invoke("list")
    assert res.exit_code == 0
    assert "prod.yaml" in res.output and "stored" in res.output
    assert "$O365_CLIENT_ID" in res.output


def test_load_prints_secret(invoke):
    res = invoke("load", "prod.yaml")
    assert res.exit_code == 0
    assert res.output == "db: prod\n"


def test_get_env_prints_value(invoke):
    res = invoke("get-env", "O365_CLIENT_ID")
    assert res.output == "the-client-id\n"


def test_set_env_then_get(invoke):
    assert invoke("set-env", "SHEET", "--value", "Sheet2").exit_code == 0
    assert invoke("get-env", "SHEET").output == "Sheet2"


def test_set_env_unknown_var_errors(invoke):
    res = invoke("set-env", "NOPE", "--value", "x")
    assert res.exit_code != 0
    assert "unknown env var" in res.output


def test_env_export_emits_exports(invoke):
    res = invoke("env-export")
    assert res.exit_code == 0
    assert "export O365_CLIENT_ID=the-client-id" in res.output
    assert "export SHEET=Sheet1" in res.output
    assert "export MAYBE" not in res.output  # optional + missing -> silently skipped


def test_env_export_warns_about_unexpanded_config(invoke):
    res = invoke("env-export")  # develop_path doesn't exist yet
    assert "config prod.yaml not expanded" in res.output
    assert "ss-fetcher develop prod.yaml" in res.output


def test_env_export_no_warning_when_config_expanded(invoke, tmp_path):
    cfg = tmp_path / "config" / "prod.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("x")
    res = invoke("env-export")
    assert "not expanded" not in res.output


def test_env_export_quiet_suppresses_warnings(invoke):
    res = invoke("env-export", "--quiet")
    assert "not expanded" not in res.output
    assert "export O365_CLIENT_ID=the-client-id" in res.output


def test_env_export_warns_on_missing_required_secret(monkeypatch, rc_path, fake_backend):
    # empty backend -> required secrets missing
    monkeypatch.setattr(cli, "make_backend", lambda rc: fake_backend)
    res = CliRunner().invoke(cli.main, ["-r", str(rc_path), "env-export"])
    assert res.exit_code == 0
    assert "env $O365_CLIENT_ID not stored" in res.output
    assert "export O365_CLIENT_ID" not in res.output
    assert "MAYBE" not in res.output  # optional stays silent


def test_env_export_quotes_values_with_spaces(invoke):
    invoke("set-env", "SHEET", "--value", "two words")
    res = invoke("env-export")
    assert "export SHEET='two words'" in res.output


def test_install_direnv_creates_file(tmp_path):
    envrc = tmp_path / ".envrc"
    res = CliRunner().invoke(cli.main, ["install-direnv", str(envrc)])
    assert res.exit_code == 0
    text = envrc.read_text()
    assert 'eval "$(ss-fetcher env-export)"' in text
    assert text.count(cli.ENVRC_BEGIN) == 1 and text.count(cli.ENVRC_END) == 1


def test_install_direnv_is_idempotent(tmp_path):
    envrc = tmp_path / ".envrc"
    CliRunner().invoke(cli.main, ["install-direnv", str(envrc)])
    CliRunner().invoke(cli.main, ["install-direnv", str(envrc)])
    text = envrc.read_text()
    assert text.count(cli.ENVRC_BEGIN) == 1  # not duplicated


def test_install_direnv_preserves_surrounding_content(tmp_path):
    envrc = tmp_path / ".envrc"
    envrc.write_text("layout python\nexport FOO=bar\n")
    CliRunner().invoke(cli.main, ["install-direnv", str(envrc)])
    text = envrc.read_text()
    assert "layout python" in text and "export FOO=bar" in text
    # add content after the block and re-run -> still one block, both sides kept
    envrc.write_text(text + "\nexport AFTER=1\n")
    CliRunner().invoke(cli.main, ["install-direnv", str(envrc)])
    text2 = envrc.read_text()
    assert "export FOO=bar" in text2 and "export AFTER=1" in text2
    assert text2.count(cli.ENVRC_BEGIN) == 1


def test_install_direnv_print_only_writes_nothing(tmp_path):
    envrc = tmp_path / ".envrc"
    res = CliRunner().invoke(cli.main, ["install-direnv", "--print", str(envrc)])
    assert res.exit_code == 0
    assert cli.ENVRC_BEGIN in res.output
    assert not envrc.exists()


def test_list_env_prints_names(invoke):
    res = invoke("list-env")
    assert res.exit_code == 0
    assert res.output.split() == ["O365_CLIENT_ID", "SHEET", "MAYBE"]


def test_list_env_long_shows_status(invoke):
    res = invoke("list-env", "--long")
    assert "O365_CLIENT_ID" in res.output and "stored" in res.output  # stored fixture
    assert "MAYBE" in res.output and "missing" in res.output  # not stored
    assert "<- PERSONAL_O365_CLIENT_ID" in res.output  # rename shown


def test_edit_env_saves_changed_values(invoke, monkeypatch):
    def fake_edit(text, **kwargs):
        # the editor shows current values; change one, leave the rest
        assert "O365_CLIENT_ID=the-client-id" in text
        return text.replace("O365_CLIENT_ID=the-client-id", "O365_CLIENT_ID=NEWID")

    monkeypatch.setattr(cli.click, "edit", fake_edit)
    res = invoke("edit-env")
    assert "updated $O365_CLIENT_ID" in res.output
    assert "1 value(s) updated" in res.output
    assert invoke("get-env", "O365_CLIENT_ID").output == "NEWID"
    # untouched value stays
    assert invoke("get-env", "SHEET").output == "Sheet1"


def test_edit_env_no_save(invoke, monkeypatch):
    monkeypatch.setattr(cli.click, "edit", lambda text, **kw: None)  # quit without saving
    res = invoke("edit-env")
    assert "no changes" in res.output


def test_edit_env_saved_unchanged(invoke, monkeypatch):
    monkeypatch.setattr(cli.click, "edit", lambda text, **kw: text)  # saved, nothing edited
    res = invoke("edit-env")
    assert "no values changed" in res.output


def test_edit_env_helpers_roundtrip():
    from secretservice_fetcher.config import EnvEntry

    entries = [EnvEntry(var="A"), EnvEntry(var="B")]
    text = cli._render_env_editor(entries, {"A": "1", "B": "two words"})
    parsed = cli._parse_env_editor(text)
    assert parsed == {"A": "1", "B": "two words"}


def _history(tmp_path):
    # 'the-client-id' (stored, len 13) appears on 2 lines; 'Sheet1' is too short.
    hist = tmp_path / ".zsh_history"
    hist.write_text(
        "ls -la\n"
        "ss-fetcher set-env O365_CLIENT_ID --value the-client-id\n"
        "curl -H 'token: the-client-id' https://api\n"
        "open Sheet1 please\n"  # short value -> must NOT be removed
        "echo done\n"
    )
    hist.chmod(0o600)
    return hist


def test_clean_history_removes_secret_lines(invoke, tmp_path):
    hist = _history(tmp_path)
    res = invoke("clean-history", "--yes", "--history-file", str(hist))
    assert res.exit_code == 0
    assert "cleaned 2 line(s)" in res.output
    text = hist.read_text()
    assert "the-client-id" not in text
    assert text.splitlines() == ["ls -la", "open Sheet1 please", "echo done"]
    # permissions preserved
    import stat

    assert stat.S_IMODE(hist.stat().st_mode) == 0o600


def test_clean_history_dry_run_changes_nothing(invoke, tmp_path):
    hist = _history(tmp_path)
    before = hist.read_text()
    res = invoke("clean-history", "--dry-run", "--history-file", str(hist))
    assert "would remove 2 line(s)" in res.output
    assert "<redacted>" in res.output  # secret masked in preview
    assert "the-client-id" not in res.output  # not leaked
    assert hist.read_text() == before  # untouched


def test_clean_history_no_matches(invoke, tmp_path):
    hist = tmp_path / ".bash_history"
    hist.write_text("ls\necho hi\n")
    res = invoke("clean-history", "--yes", "--history-file", str(hist))
    assert "no matching history lines found" in res.output


def test_clean_history_warns_about_short_values(invoke, tmp_path):
    hist = tmp_path / ".zsh_history"
    hist.write_text("echo Sheet1\n")
    res = invoke("clean-history", "--yes", "--history-file", str(hist))
    # SHEET=Sheet1 (len 6) is skipped by the default min-length
    assert "skipping short values" in res.output and "SHEET" in res.output
    assert hist.read_text() == "echo Sheet1\n"  # not removed


def test_import_env_json(invoke, tmp_path):
    src = tmp_path / "secrets.json"
    src.write_text(json.dumps({"O365_CLIENT_ID": "imported"}))
    res = invoke("import-env", "--from", str(src), "--prefix", "PERSONAL_")
    assert res.exit_code == 0
    assert "stored PERSONAL_O365_CLIENT_ID" in res.output
    assert 'name = "PERSONAL_O365_CLIENT_ID"' in res.output  # suggested block
    # routed through the backend under the renamed name
    assert invoke("get-env", "O365_CLIENT_ID").output == "imported"


def test_paths_subcommands(invoke, tmp_path):
    base = tmp_path / "run" / "ss-fetcher" / "demo"
    assert invoke("paths", "--config", "prod.yaml").output.strip() == str(base / "prod.yaml")
    assert invoke("paths", "--env-file").output.strip() == str(base / "env")
    full = invoke("paths").output
    assert "prepare dir" in full and "EnvironmentFile" in full


def test_prepare_then_cleanup(invoke, tmp_path):
    base = tmp_path / "run" / "ss-fetcher" / "demo"
    res = invoke("prepare")
    assert res.exit_code == 0
    assert (base / "prod.yaml").read_text() == "db: prod\n"
    assert 'O365_CLIENT_ID="the-client-id"' in (base / "env").read_text()

    assert invoke("cleanup").exit_code == 0
    assert not base.exists()


def test_run_injects_env(invoke, tmp_path):
    out = tmp_path / "o.txt"
    prog = f"import os; open(r'{out}','w').write(os.environ.get('O365_CLIENT_ID',''))"
    res = invoke("run", "--", sys.executable, "-c", prog)
    assert res.exit_code == 0
    assert out.read_text() == "the-client-id"


def test_run_no_env_skips_injection(invoke, tmp_path):
    out = tmp_path / "o.txt"
    prog = f"import os; open(r'{out}','w').write(os.environ.get('O365_CLIENT_ID','MISSING'))"
    res = invoke("run", "--no-env", "--", sys.executable, "-c", prog)
    assert res.exit_code == 0
    assert out.read_text() == "MISSING"


def test_missing_secretrc_errors():
    runner = CliRunner()
    res = runner.invoke(cli.main, ["-r", "/nonexistent/.secretrc", "list"])
    assert res.exit_code != 0
