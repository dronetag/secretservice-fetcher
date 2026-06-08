"""Command line interface for ss-fetcher."""

from __future__ import annotations

import contextlib
import os
import shlex
import sys
import tempfile
from pathlib import Path

import click

from . import debug, runner
from .backend import Backend, BackendError, make_backend
from .config import (
    SECRETRC_FILENAME,
    ConfigEntry,
    EnvEntry,
    RcSecret,
    load,
)

EXAMPLE_SECRETRC = """\
# .secretrc -- describes config files and env-var secrets kept in a secret store.
# Contains NO secrets, only where each lives. Safe to commit next to your code.

# Logical identity; namespaces the prepare dir and Vault base path.
id = "myapp"

# Backend: "secret-service" (default, via secret-tool) or "vault".
backend = "secret-service"

# Directory used to materialise files at runtime. Leave it unset: it defaults to
# $XDG_RUNTIME_DIR (which systemd sets for user services and which equals %t), so
# the tool and the unit agree. Set it only for non-systemd use ({uid} -> uid):
# runtime_dir = "/run/user/{uid}"

[defaults]
# Attributes merged into every config's Secret Service lookup key.
attributes = { app = "myapp" }
# Optional label prefix; the logical name is appended.
label_prefix = "myapp"
mode = "0600"

# HashiCorp Vault settings (used only when backend = "vault").
# The token is read from $VAULT_TOKEN; the address from vault.addr or $VAULT_ADDR.
[vault]
addr = "https://vault.example.com:8200"
mount = "secret"
kv_version = 2
# path = "myapp"      # base path under the mount (defaults to id)

[[configs]]
name = "prod.yaml"
label = "prod app config"
attributes = { kind = "config" }
develop_path = "./config/prod.yaml"
env = "APP_CONFIG"          # exports APP_CONFIG=<materialised file path>
default = true
# writeback = true          # save the file back if the program rewrites it
                            # (e.g. OAuth refresh-token rotation)

# [[env]] entries inject a scalar secret VALUE as an environment variable.
# Store one with `ss-fetcher set-env O365_CLIENT_ID`.
[[env]]
var = "O365_CLIENT_ID"               # the env var the program reads
name = "PERSONAL_O365_CLIENT_ID"     # keyring name (omit if it equals var)
attributes = { kind = "env" }

[[env]]
var = "O365_CLIENT_SECRET"
attributes = { kind = "env" }
"""

# A self-contained, marker-delimited block for a direnv .envrc. It is guarded so
# it no-ops (with a notice) when ss-fetcher isn't installed -- using `if/else`
# rather than `return`, so it is safe to append to an existing .envrc and the
# rest of that file still runs either way.
ENVRC_BEGIN = "# >>> ss-fetcher (managed by `ss-fetcher install-direnv`) >>>"
ENVRC_END = "# <<< ss-fetcher <<<"
ENVRC_BLOCK = """\
# >>> ss-fetcher (managed by `ss-fetcher install-direnv`) >>>
# Exports the [[env]] secrets declared in .secretrc into your shell on `cd` in
# (direnv unsets them on leave). No-ops with a notice when ss-fetcher isn't
# installed, so the rest of this .envrc still runs. Re-run: ss-fetcher install-direnv
if command -v ss-fetcher >/dev/null 2>&1; then
  eval "$(ss-fetcher env-export)"
else
  _msg="ss-fetcher not installed; skipping [[env]] secret expansion"
  if command -v log_error >/dev/null 2>&1; then log_error "$_msg"; else echo "$_msg" >&2; fi
fi
# <<< ss-fetcher <<<
"""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _load(ctx: click.Context) -> RcSecret:
    try:
        return load(ctx.obj["secretrc"])
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:  # pydantic / toml validation
        raise click.ClickException(f"invalid {SECRETRC_FILENAME}: {exc}") from exc


def _backend(rc: RcSecret) -> Backend:
    try:
        return make_backend(rc)
    except BackendError as exc:
        raise click.ClickException(str(exc)) from exc


def _select(rc: RcSecret, names: tuple[str, ...]) -> list[ConfigEntry]:
    if not names:
        try:
            return [rc.default_entry()]
        except LookupError as exc:
            raise click.ClickException(str(exc)) from exc
    entries: list[ConfigEntry] = []
    for name in names:
        try:
            entries.append(rc.get(name))
        except KeyError as exc:
            raise click.ClickException(f"unknown config: {name!r}") from exc
    return entries


def _env_entry(rc: RcSecret, var: str) -> EnvEntry:
    try:
        return rc.get_env(var)
    except KeyError as exc:
        raise click.ClickException(
            f"unknown env var: {var!r} (declare it under [[env]] in .secretrc)"
        ) from exc


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-r",
    "--secretrc",
    "secretrc",
    type=click.Path(dir_okay=False, path_type=Path),
    help=f"Path to the config file (default: $SECRETRC, else search "
    f"cwd/parents for {SECRETRC_FILENAME}).",
)
@click.option(
    "-v",
    "--verbose",
    "--debug",
    "verbose",
    is_flag=True,
    help="Verbose debug tracing to stderr (or set SSFETCHER_DEBUG=1). "
    "Never prints secret values.",
)
@click.pass_context
def main(ctx: click.Context, secretrc: Path | None, verbose: bool) -> None:
    """Store and load config files and env secrets via Secret Service or Vault."""

    debug.enable(verbose)
    debug.log(f"secretrc={secretrc} argv={sys.argv[1:]}")
    ctx.ensure_object(dict)
    ctx.obj["secretrc"] = secretrc


@main.command()
@click.option("--force", is_flag=True, help="Overwrite an existing file.")
@click.argument(
    "path",
    required=False,
    type=click.Path(dir_okay=False, path_type=Path),
)
def init(path: Path | None, force: bool) -> None:
    """Write an example .secretrc to PATH (default: ./.secretrc)."""

    target = path or Path(SECRETRC_FILENAME)
    if target.exists() and not force:
        raise click.ClickException(f"{target} exists (use --force to overwrite)")
    target.write_text(EXAMPLE_SECRETRC)
    click.echo(f"wrote {target}")


@main.command(name="install-direnv")
@click.argument("path", required=False, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--print", "print_only", is_flag=True, help="Print the block; write nothing.")
def install_direnv(path: Path | None, print_only: bool) -> None:
    """Add (or update) the ss-fetcher block in a .envrc for direnv.

    Idempotent: writes a self-contained, marker-delimited block that runs
    `eval "$(ss-fetcher env-export)"` on `cd` in and no-ops (with a notice) when
    ss-fetcher isn't installed -- safe to append to an existing .envrc, which
    still runs either way. Re-running updates the block in place.
    """

    block = ENVRC_BLOCK.strip("\n")
    if print_only:
        click.echo(block)
        return

    target = path or Path(".envrc")
    if target.exists():
        text = target.read_text()
        if ENVRC_BEGIN in text and ENVRC_END in text:
            start = text.index(ENVRC_BEGIN)
            end = text.index(ENVRC_END) + len(ENVRC_END)
            new = text[:start] + block + text[end:]
            action = "updated ss-fetcher block in"
        else:
            body = text.rstrip("\n")
            new = (body + "\n\n" if body else "") + block + "\n"
            action = "appended ss-fetcher block to"
    else:
        new = block + "\n"
        action = "created"
    target.write_text(new)
    click.echo(f"{action} {target}")
    click.echo("next: `direnv allow`, and store secrets with `ss-fetcher set-env`")


@main.command(name="list")
@click.pass_context
def list_cmd(ctx: click.Context) -> None:
    """List configs/env secrets and whether each is present in the backend."""

    rc = _load(ctx)
    backend = _backend(rc)

    def status(ref: object) -> str:
        try:
            present = backend.exists(ref)
        except BackendError as exc:
            raise click.ClickException(str(exc)) from exc
        return click.style("stored", fg="green") if present else click.style("missing", fg="yellow")

    click.echo(f"# {rc.source_path}  (backend: {rc.backend})")
    if rc.configs:
        click.echo("configs (materialised as files):")
    for entry in rc.configs:
        ref = backend.config_ref(rc, entry)
        flag = " (default)" if entry.default else ""
        click.echo(f"  {entry.name:<24} {status(ref)}{flag}")
        click.echo(f"      {backend.describe(ref)}")
    if rc.env:
        click.echo("env (injected as environment variables):")
    for env_entry in rc.env:
        ref = backend.env_ref(rc, env_entry)
        flag = " (optional)" if env_entry.optional else ""
        click.echo(f"  ${env_entry.var:<23} {status(ref)}{flag}")
        click.echo(f"      {backend.describe(ref)}")


@main.command()
@click.argument("name", required=False)
@click.option(
    "--from",
    "source",
    type=click.Path(dir_okay=False, allow_dash=True, path_type=Path),
    help="Read the secret from this file ('-' for stdin). Defaults to the config's develop_path.",
)
@click.pass_context
def save(ctx: click.Context, name: str | None, source: Path | None) -> None:
    """Store a config file into the backend."""

    rc = _load(ctx)
    backend = _backend(rc)
    entry = _select(rc, (name,) if name else ())[0]

    if source is not None and str(source) == "-":
        data = sys.stdin.buffer.read()
        origin = "<stdin>"
    else:
        path = source or entry.develop_path
        if path is None:
            raise click.ClickException(
                f"no source for {entry.name!r}: pass --from or set develop_path"
            )
        if not Path(path).is_file():
            raise click.ClickException(f"file not found: {path}")
        data = Path(path).read_bytes()
        origin = str(path)

    try:
        backend.store(backend.config_ref(rc, entry), rc.effective_label(entry), data)
    except BackendError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"stored {entry.name} ({len(data)} bytes) from {origin}")


@main.command(name="load")
@click.argument("name", required=False)
@click.pass_context
def load_cmd(ctx: click.Context, name: str | None) -> None:
    """Print a config from the backend to stdout."""

    rc = _load(ctx)
    backend = _backend(rc)
    entry = _select(rc, (name,) if name else ())[0]
    try:
        secret = backend.lookup(backend.config_ref(rc, entry))
    except BackendError as exc:
        raise click.ClickException(str(exc)) from exc
    if secret is None:
        raise click.ClickException(f"config {entry.name!r} not found")
    sys.stdout.buffer.write(secret)


@main.command(name="set-env")
@click.argument("var")
@click.option("--value", help="The secret value (otherwise prompt or read --from).")
@click.option(
    "--from",
    "source",
    type=click.Path(dir_okay=False, allow_dash=True, path_type=Path),
    help="Read the value from this file ('-' for stdin).",
)
@click.pass_context
def set_env(ctx: click.Context, var: str, value: str | None, source: Path | None) -> None:
    """Store the secret value for an [[env]] entry into the backend."""

    rc = _load(ctx)
    backend = _backend(rc)
    entry = _env_entry(rc, var)

    if value is not None:
        data = value.encode()
    elif source is not None:
        if str(source) == "-":
            data = sys.stdin.buffer.read()
        else:
            if not Path(source).is_file():
                raise click.ClickException(f"file not found: {source}")
            data = Path(source).read_bytes()
    else:
        data = click.prompt(f"value for ${var}", hide_input=True, confirmation_prompt=True).encode()

    try:
        backend.store(backend.env_ref(rc, entry), rc.effective_env_label(entry), data)
    except BackendError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"stored ${var} ({len(data)} bytes)")


@main.command(name="get-env")
@click.argument("var")
@click.pass_context
def get_env(ctx: click.Context, var: str) -> None:
    """Print the value of an [[env]] secret to stdout."""

    rc = _load(ctx)
    backend = _backend(rc)
    entry = _env_entry(rc, var)
    try:
        secret = backend.lookup(backend.env_ref(rc, entry))
    except BackendError as exc:
        raise click.ClickException(str(exc)) from exc
    if secret is None:
        raise click.ClickException(f"env var {var!r} not found")
    sys.stdout.buffer.write(secret)


@main.command(name="env-export")
@click.option("--quiet", is_flag=True, help="Suppress stderr warnings.")
@click.pass_context
def env_export(ctx: click.Context, quiet: bool) -> None:
    """Print `export VAR=value` lines for every [[env]] secret, for `eval`.

    Made for shell/direnv integration -- the same in every project, since the
    names come from .secretrc:

    \b
      eval "$(ss-fetcher env-export)"

    Export lines go to stdout; advisory warnings (a missing secret, or a
    [[configs]] develop_path that isn't expanded yet) go to stderr. No files are
    written -- config files are still expanded by hand with `develop`.
    """

    rc = _load(ctx)
    backend = _backend(rc)

    try:
        secrets = backend.lookup_many([backend.env_ref(rc, e) for e in rc.env])
    except BackendError as exc:
        raise click.ClickException(str(exc)) from exc
    for entry, secret in zip(rc.env, secrets):
        if secret is None:
            if not entry.optional and not quiet:
                click.echo(
                    f"ss-fetcher: env ${entry.var} not stored (skipped); "
                    f"`ss-fetcher set-env {entry.var}`",
                    err=True,
                )
            continue
        click.echo(f"export {entry.var}={shlex.quote(runner.env_value(secret))}")

    if not quiet:
        for cfg in rc.configs:
            if cfg.develop_path is not None and not cfg.develop_path.exists():
                click.echo(
                    f"ss-fetcher: config {cfg.name} not expanded ({cfg.develop_path}) "
                    f"-- run: ss-fetcher develop {cfg.name}",
                    err=True,
                )


@main.command(name="list-env")
@click.option("-l", "--long", is_flag=True, help="Also show keyring name + stored status.")
@click.pass_context
def list_env(ctx: click.Context, long: bool) -> None:
    """List the env var names declared in .secretrc (one per line)."""

    rc = _load(ctx)
    if not long:
        for entry in rc.env:
            click.echo(entry.var)
        return

    backend = _backend(rc)
    try:
        secrets = backend.lookup_many([backend.env_ref(rc, e) for e in rc.env])
    except BackendError as exc:
        raise click.ClickException(str(exc)) from exc
    for entry, secret in zip(rc.env, secrets):
        status = "stored" if secret is not None else "missing"
        notes = []
        if entry.logical_name() != entry.var:
            notes.append(f"<- {entry.logical_name()}")
        if entry.optional:
            notes.append("optional")
        suffix = ("  " + " ".join(notes)) if notes else ""
        click.echo(f"  ${entry.var:<26} {status}{suffix}")


def _render_env_editor(entries: list[EnvEntry], current: dict[str, str]) -> str:
    lines = [
        "# ss-fetcher edit-env -- edit values below, then save & quit.",
        "# One VAR=value per line; the value is everything after the first '='.",
        "# Unchanged lines are left as-is. Comments (#) and unknown vars are ignored.",
        "# Values are single-line; don't wrap or insert newlines.",
        "",
    ]
    lines += [f"{entry.var}={current.get(entry.var, '')}" for entry in entries]
    return "\n".join(lines) + "\n"


def _parse_env_editor(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value  # value kept verbatim (after the first '=')
    return result


@main.command(name="edit-env")
@click.pass_context
def edit_env(ctx: click.Context) -> None:
    """Open all env var values in $EDITOR (vim), then save them back."""

    rc = _load(ctx)
    backend = _backend(rc)
    if not rc.env:
        click.echo("no [[env]] entries declared in .secretrc")
        return

    try:
        secrets = backend.lookup_many([backend.env_ref(rc, e) for e in rc.env])
    except BackendError as exc:
        raise click.ClickException(str(exc)) from exc
    current: dict[str, str] = {
        entry.var: (runner.env_value(secret) if secret is not None else "")
        for entry, secret in zip(rc.env, secrets)
    }

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vim"
    # Keep the editor's temp file in the (tmpfs) runtime dir, not on disk.
    base = rc.runtime_base()
    previous_tmpdir = os.environ.get("TMPDIR")
    if base.is_dir():
        os.environ["TMPDIR"] = str(base)
    try:
        edited = click.edit(
            _render_env_editor(rc.env, current),
            editor=editor,
            extension=".env",
            require_save=True,
        )
    finally:
        if previous_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous_tmpdir

    if edited is None:
        click.echo("no changes (editor not saved)")
        return

    new_values = _parse_env_editor(edited)
    changed = 0
    for entry in rc.env:
        if entry.var not in new_values or new_values[entry.var] == current[entry.var]:
            continue
        try:
            backend.store(
                backend.env_ref(rc, entry),
                rc.effective_env_label(entry),
                new_values[entry.var].encode(),
            )
        except BackendError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"updated ${entry.var}")
        changed += 1
    click.echo(f"{changed} value(s) updated" if changed else "no values changed")


def _history_files(explicit: tuple[Path, ...]) -> list[Path]:
    if explicit:
        return [p for p in explicit if p.is_file()]
    candidates: list[Path] = []
    histfile = os.environ.get("HISTFILE")
    if histfile:
        candidates.append(Path(histfile))
    home = Path.home()
    candidates += [home / ".zsh_history", home / ".bash_history"]
    out: list[Path] = []
    for path in candidates:
        resolved = path.expanduser()
        if resolved.is_file() and resolved not in out:
            out.append(resolved)
    return out


def _scan_history(path: Path, secrets: list[str]) -> tuple[int, bytes, list[tuple[int, str]]]:
    # Read as bytes (zsh history may not be valid UTF-8) and round-trip safely.
    text = path.read_bytes().decode("utf-8", errors="surrogateescape")
    kept: list[str] = []
    previews: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(keepends=True), 1):
        if any(secret in line for secret in secrets):
            redacted = line.rstrip("\n")
            for secret in secrets:
                redacted = redacted.replace(secret, "<redacted>")
            previews.append((lineno, redacted[:200]))
        else:
            kept.append(line)
    kept_bytes = "".join(kept).encode("utf-8", errors="surrogateescape")
    return len(previews), kept_bytes, previews


def _atomic_write(path: Path, data: bytes) -> None:
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".sshist-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@main.command(name="clean-history")
@click.option("--dry-run", is_flag=True, help="Show what would be removed; change nothing.")
@click.option("-y", "--yes", is_flag=True, help="Don't prompt before rewriting files.")
@click.option(
    "--min-length",
    default=8,
    show_default=True,
    help="Ignore secret values shorter than this (avoids matching common strings).",
)
@click.option(
    "--history-file",
    "history_files",
    multiple=True,
    type=click.Path(path_type=Path),
    help="History file(s) to clean (default: $HISTFILE, ~/.zsh_history, ~/.bash_history).",
)
@click.pass_context
def clean_history(
    ctx: click.Context,
    dry_run: bool,
    yes: bool,
    min_length: int,
    history_files: tuple[Path, ...],
) -> None:
    """Remove shell-history lines containing a stored [[env]] secret value.

    Scans zsh/bash history for any line that contains a secret's *value* (e.g. a
    token you once typed into `set-env --value ...`) and deletes those lines. No
    backup is written -- a backup would just re-expose the secret. Use --dry-run
    first.
    """

    rc = _load(ctx)
    backend = _backend(rc)

    try:
        raw = backend.lookup_many([backend.env_ref(rc, e) for e in rc.env])
    except BackendError as exc:
        raise click.ClickException(str(exc)) from exc
    secrets: set[str] = set()
    skipped: list[str] = []
    for entry, secret in zip(rc.env, raw):
        if secret is None:
            continue
        value = runner.env_value(secret)
        if len(value) >= min_length:
            secrets.add(value)
        elif value.strip():
            skipped.append(entry.var)
    if skipped:
        click.echo(
            f"skipping short values (< {min_length} chars): {', '.join(skipped)}",
            err=True,
        )
    if not secrets:
        click.echo("no stored secret values to scan for")
        return

    targets = _history_files(history_files)
    if not targets:
        click.echo("no history files found")
        return

    secret_list = list(secrets)
    results: list[tuple[Path, int, bytes, list[tuple[int, str]]]] = []
    total = 0
    for path in targets:
        removed, kept_bytes, previews = _scan_history(path, secret_list)
        if removed:
            results.append((path, removed, kept_bytes, previews))
            total += removed

    if total == 0:
        click.echo("no matching history lines found")
        return

    for path, removed, _kept, previews in results:
        click.echo(f"{path}: {removed} line(s)")
        if dry_run:
            for lineno, redacted in previews:
                click.echo(f"  {lineno}: {redacted}")

    if dry_run:
        click.echo(f"(dry run) would remove {total} line(s)")
        return
    if not yes and not click.confirm(
        f"Remove {total} line(s) from {len(results)} file(s)? No backup is kept"
    ):
        click.echo("aborted")
        return

    for path, removed, kept_bytes, _ in results:
        _atomic_write(path, kept_bytes)
        click.echo(f"cleaned {removed} line(s) from {path}")
    click.echo("note: your current shell holds history in memory; run `exec $SHELL` to reload")


@main.command(name="import-env")
@click.option(
    "--from",
    "source",
    required=True,
    type=click.Path(dir_okay=False, allow_dash=True, path_type=Path),
    help="JSON object or .env file to import ('-' for stdin).",
)
@click.option(
    "--prefix",
    default="",
    help="Prepended to each key to form the keyring name (e.g. PERSONAL_).",
)
@click.option("--kind", default="env", help="Value of the 'kind' attribute (default: env).")
@click.pass_context
def import_env(ctx: click.Context, source: Path, prefix: str, kind: str) -> None:
    """Bulk-store a JSON/.env file as individual env secrets.

    Each key K is stored under name=<prefix>K. Afterwards the matching [[env]]
    blocks to paste into .secretrc are printed.
    """

    rc = _load(ctx)
    backend = _backend(rc)

    raw = sys.stdin.read() if str(source) == "-" else Path(source).read_text()
    pairs = _parse_kv(raw)
    if not pairs:
        raise click.ClickException("no key/value pairs found to import")

    blocks: list[str] = []
    for key, val in pairs.items():
        name = f"{prefix}{key}"
        entry = EnvEntry(
            var=key,
            name=name if name != key else None,
            attributes={"kind": kind} if kind else {},
        )
        try:
            backend.store(backend.env_ref(rc, entry), rc.effective_env_label(entry), val.encode())
        except BackendError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"stored {name} ({len(val)} bytes) -> ${key}")
        extra = f'\nattributes = {{ kind = "{kind}" }}' if kind else ""
        name_line = f'\nname = "{name}"' if name != key else ""
        blocks.append(f'[[env]]\nvar = "{key}"{name_line}{extra}')

    click.echo("\n# paste into .secretrc:\n")
    click.echo("\n\n".join(blocks))


def _parse_kv(raw: str) -> dict[str, str]:
    """Parse a JSON object or a simple KEY=VALUE (.env) document."""

    import json

    stripped = raw.lstrip()
    if stripped.startswith("{"):
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise click.ClickException("JSON import must be an object")
        return {str(k): str(v) for k, v in data.items()}
    result: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        result[key.strip()] = value
    return result


@main.command()
@click.argument("name", required=False)
@click.option("--force", is_flag=True, help="Overwrite an existing develop file.")
@click.pass_context
def develop(ctx: click.Context, name: str | None, force: bool) -> None:
    """Materialise a config to its develop_path for editing."""

    rc = _load(ctx)
    backend = _backend(rc)
    entry = _select(rc, (name,) if name else ())[0]
    if entry.develop_path is None:
        raise click.ClickException(f"{entry.name!r} has no develop_path set")
    if entry.develop_path.is_file() and not force:
        raise click.ClickException(f"{entry.develop_path} exists (use --force to overwrite)")
    try:
        path = runner.materialise(rc, entry, backend, entry.develop_path.parent)
    except (BackendError, LookupError) as exc:
        raise click.ClickException(str(exc)) from exc
    # materialise writes to directory/filename; ensure it lands on develop_path.
    if path != entry.develop_path:
        path.replace(entry.develop_path)
    click.echo(f"materialised {entry.name} -> {entry.develop_path} (edit, then `save`)")


@main.command()
@click.argument("name", required=False)
@click.pass_context
def rm(ctx: click.Context, name: str | None) -> None:
    """Remove a config from the backend."""

    rc = _load(ctx)
    backend = _backend(rc)
    entry = _select(rc, (name,) if name else ())[0]
    try:
        backend.clear(backend.config_ref(rc, entry))
    except BackendError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"removed {entry.name}")


# --------------------------------------------------------------------------
# systemd-friendly prepare / cleanup (no wrapper process)
# --------------------------------------------------------------------------


@main.command()
@click.pass_context
def prepare(ctx: click.Context) -> None:
    """Materialise configs + an EnvironmentFile to a deterministic dir.

    For systemd ExecStartPre=, so the real program is started directly by
    systemd (which stays the parent). Reference the printed paths via
    --config and EnvironmentFile=, and pair with `cleanup` in ExecStopPost=.
    """

    rc = _load(ctx)
    backend = _backend(rc)
    try:
        result = runner.prepare(rc, backend)
    except (BackendError, LookupError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"prepared in {result.directory}")
    for name, path in result.configs.items():
        click.echo(f"  config {name}: {path}")
    if result.env_file is not None:
        click.echo(f"  EnvironmentFile: {result.env_file}")


@main.command()
@click.pass_context
def cleanup(ctx: click.Context) -> None:
    """Remove the directory created by `prepare` (for ExecStopPost=).

    Any `writeback` config the program changed is saved back to the store first.
    """

    rc = _load(ctx)
    backend = None
    if any(c.writeback for c in rc.configs):
        try:
            backend = _backend(rc)
        except click.ClickException as exc:
            # Don't let a write-back failure block the cleanup itself.
            click.echo(
                f"warning: cannot save writeback configs ({exc.message}); cleaning up anyway",
                err=True,
            )
    directory = runner.cleanup(rc, backend)
    click.echo(f"removed {directory}")


@main.command()
@click.option("--env-file", "env_file", is_flag=True, help="Print only the env file path.")
@click.option("--config", "config_name", help="Print only this config's prepared path.")
@click.pass_context
def paths(ctx: click.Context, env_file: bool, config_name: str | None) -> None:
    """Print the deterministic paths used by `prepare` (for writing units)."""

    rc = _load(ctx)
    if env_file:
        click.echo(rc.env_file())
        return
    if config_name:
        try:
            click.echo(rc.prepared_config_path(rc.get(config_name)))
        except KeyError as exc:
            raise click.ClickException(f"unknown config: {config_name!r}") from exc
        return
    click.echo(f"prepare dir: {rc.prepare_directory()}")
    for entry in rc.configs:
        click.echo(f"  config {entry.name}: {rc.prepared_config_path(entry)}")
    if rc.env:
        click.echo(f"  EnvironmentFile: {rc.env_file()}")


@main.command(context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
@click.option(
    "-c",
    "--config",
    "configs",
    multiple=True,
    help="Config name(s) to materialise (default: the default config).",
)
@click.option(
    "--develop",
    is_flag=True,
    help="Use existing develop_path files instead of the backend; no temp cleanup.",
)
@click.option(
    "--no-env",
    is_flag=True,
    help="Do not inject the [[env]] secrets into the process environment.",
)
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=True)
@click.pass_context
def run(
    ctx: click.Context,
    configs: tuple[str, ...],
    develop: bool,
    no_env: bool,
    command: tuple[str, ...],
) -> None:
    """Run COMMAND with configs materialised and [[env]] secrets injected.

    Use the {config} or {config:NAME} placeholder in COMMAND to receive the
    materialised file path, and/or rely on the config's `env` variable. Every
    [[env]] entry is looked up and exported into the environment (unless
    --no-env), so the wrapped program just reads os.environ as usual.

    \b
    Example:
      ss-fetcher run -- myapp --config {config}
    """

    rc = _load(ctx)
    backend = _backend(rc)

    # Select config files. With no -c: the default config if any exist, else
    # none (an env-only run is valid).
    if configs:
        entries = _select(rc, configs)
    elif rc.configs:
        try:
            entries = [rc.default_entry()]
        except LookupError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        entries = []

    # Determine which name {config} resolves to.
    default_name: str | None
    if len(entries) == 1:
        default_name = entries[0].name
    else:
        try:
            default_name = rc.default_entry().name
        except LookupError:
            default_name = None

    try:
        code = runner.run(
            rc,
            entries,
            backend,
            list(command),
            default_name=default_name,
            develop=develop,
            env_entries=[] if no_env else rc.env,
        )
    except (BackendError, LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    sys.exit(code)


if __name__ == "__main__":
    main()
