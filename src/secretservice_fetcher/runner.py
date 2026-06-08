"""Materialise secrets to disk and run a wrapped command.

The ``run`` command is the primary use case: in a systemd unit you prepend

    ExecStart=ss-fetcher run -- /path/to/app --config {config}

We look the config up in the Secret Service, write it to a private file in the
runtime directory, substitute the ``{config}`` placeholder in the argv with that
path (and/or export it as an env var), run the real command, then shred the
file when it exits.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from . import debug
from .backend import Backend, BackendError
from .config import ConfigEntry, EnvEntry, RcSecret

# Records the original hash of writeback configs so `cleanup` can detect changes.
WRITEBACK_MANIFEST = ".ss-fetcher-writeback.json"

# Matches {config} and {config:NAME}.
_PLACEHOLDER = re.compile(r"\{config(?::(?P<name>[^}]+))?\}")


@dataclass
class Materialised:
    entry: ConfigEntry
    path: Path


def _write_private(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open with O_CREAT|O_EXCL is overkill here; we want a fresh 0600 file.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, mode)


def materialise(
    rc: RcSecret,
    entry: ConfigEntry,
    backend: Backend,
    directory: Path,
    *,
    develop: bool = False,
) -> Path:
    """Write ``entry`` into ``directory`` and return the path.

    In ``develop`` mode, if the entry has an existing ``develop_path`` on disk,
    that path is used as-is (live editing) and nothing is fetched or written.
    """

    if develop and entry.develop_path and entry.develop_path.is_file():
        return entry.develop_path

    ref = backend.config_ref(rc, entry)
    secret = backend.lookup(ref)
    if secret is None:
        raise LookupError(
            f"config {entry.name!r} not found ({backend.describe(ref)}); "
            f"store it first with `ss-fetcher save {entry.name}`"
        )
    target = directory / entry.effective_filename()
    _write_private(target, secret, rc.effective_mode(entry))
    return target


def env_value(secret: bytes) -> str:
    """Decode a stored secret for use as an env var value.

    A single trailing newline is stripped (files stored via ``secret-tool store``
    from a text file usually carry one); embedded newlines are preserved.
    """

    text = secret.decode("utf-8")
    if text.endswith("\n"):
        text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
    return text


def load_env(rc: RcSecret, entries: list[EnvEntry], backend: Backend) -> dict[str, str]:
    """Look each env entry up in the backend and return ``{var: value}``."""

    refs = [backend.env_ref(rc, entry) for entry in entries]
    secrets = backend.lookup_many(refs)  # one aggregated round-trip
    result: dict[str, str] = {}
    for entry, ref, secret in zip(entries, refs, secrets):
        if secret is None:
            if entry.optional:
                continue
            raise LookupError(
                f"env var {entry.var!r} not found ({backend.describe(ref)}); "
                f"store it with `ss-fetcher set-env {entry.var}` "
                "or mark it optional"
            )
        result[entry.var] = env_value(secret)
    return result


def _substitute(argv: Sequence[str], paths: dict[str, Path], default: str | None) -> list[str]:
    def repl(match: re.Match[str]) -> str:
        name = match.group("name")
        if name is None:
            if default is None:
                raise LookupError("{config} used but no default config selected")
            name = default
        if name not in paths:
            raise LookupError(f"{{config:{name}}} references an unmaterialised config")
        return str(paths[name])

    return [_PLACEHOLDER.sub(repl, token) for token in argv]


def _writeback(rc: RcSecret, entry: ConfigEntry, backend: Backend, content: bytes) -> None:
    """Store ``content`` back as the config's secret, reporting on stderr."""

    try:
        backend.store(backend.config_ref(rc, entry), rc.effective_label(entry), content)
    except BackendError as exc:
        print(
            f"ss-fetcher: WARNING: could not save updated {entry.name}: {exc}",
            file=sys.stderr,
        )
        return
    print(f"ss-fetcher: saved updated {entry.name} back to the store", file=sys.stderr)


def _writeback_if_changed(
    rc: RcSecret, entry: ConfigEntry, backend: Backend, path: Path, original: bytes
) -> None:
    if not path.is_file():
        return
    current = path.read_bytes()
    if current != original:
        _writeback(rc, entry, backend, current)


def run(
    rc: RcSecret,
    entries: list[ConfigEntry],
    backend: Backend,
    command: Sequence[str],
    *,
    default_name: str | None,
    develop: bool = False,
    env_entries: list[EnvEntry] | None = None,
) -> int:
    """Materialise ``entries``, run ``command``, clean up, return exit code.

    ``env_entries`` are looked up in the keyring and injected as environment
    variables for the wrapped process.
    """

    if not command:
        raise ValueError("no command given to run")

    with ExitStack() as stack:
        if develop:
            base = rc.runtime_directory()
        else:
            # Materialise under systemd's $RUNTIME_DIRECTORY when present, else
            # $XDG_RUNTIME_DIR -- both are private, tmpfs-backed, auto-cleaned.
            base = Path(
                stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="ss-fetcher-", dir=rc.runtime_base())
                )
            )

        paths: dict[str, Path] = {}
        originals: dict[str, bytes] = {}
        env = os.environ.copy()
        for entry in entries:
            path = materialise(rc, entry, backend, base, develop=develop)
            paths[entry.name] = path
            if entry.env:
                env[entry.env] = str(path)
            # Snapshot writeback configs so we can detect in-place changes.
            if entry.writeback and not develop:
                originals[entry.name] = path.read_bytes()

        env.update(load_env(rc, env_entries or [], backend))

        argv = _substitute(command, paths, default_name)
        code = _spawn(argv, env)

        # Save any writeback config the program changed, before cleanup shreds it.
        for entry in entries:
            if entry.name in originals:
                _writeback_if_changed(rc, entry, backend, paths[entry.name], originals[entry.name])
        return code


@dataclass
class Prepared:
    directory: Path
    configs: dict[str, Path]
    env_file: Path | None


def _format_env_line(key: str, value: str) -> str:
    """Render one ``KEY=VALUE`` line for a systemd EnvironmentFile.

    Values are double-quoted with backslash/quote/newline escaped so systemd
    parses them back verbatim. EnvironmentFile is best suited to single-line
    values; embedded newlines are escaped as ``\\n``.
    """

    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'{key}="{escaped}"'


def prepare(rc: RcSecret, backend: Backend) -> Prepared:
    """Materialise configs + an EnvironmentFile to a deterministic directory.

    Intended for systemd ``ExecStartPre=``: the real program is started by
    systemd (so it stays the parent), reading the config path(s) and
    ``EnvironmentFile=`` that this writes. Pair with :func:`cleanup` in
    ``ExecStopPost=``.
    """

    directory = rc.prepare_directory()
    debug.log(
        f"prepare: dir={directory} configs={len(rc.configs)} env_vars={len(rc.env)}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)

    configs: dict[str, Path] = {}
    manifest: dict[str, str] = {}
    for entry in rc.configs:
        path = materialise(rc, entry, backend, directory)
        configs[entry.name] = path
        if entry.writeback:
            manifest[entry.effective_filename()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if manifest:
        # Record originals so `cleanup` can detect what the program changed.
        _write_private(directory / WRITEBACK_MANIFEST, json.dumps(manifest).encode(), 0o600)

    env_file: Path | None = None
    if rc.env:
        values = load_env(rc, rc.env, backend)
        lines = [_format_env_line(k, v) for k, v in values.items()]
        body = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
        env_file = rc.env_file()
        _write_private(env_file, body, 0o600)

    return Prepared(directory=directory, configs=configs, env_file=env_file)


def cleanup(rc: RcSecret, backend: Backend | None = None) -> Path:
    """Remove the directory created by :func:`prepare`.

    If ``backend`` is given, any ``writeback`` config the program changed while
    running is saved back to the store first (e.g. rotated refresh tokens).
    """

    directory = rc.prepare_directory()
    if backend is not None:
        _writeback_prepared(rc, backend, directory)
    shutil.rmtree(directory, ignore_errors=True)
    return directory


def _writeback_prepared(rc: RcSecret, backend: Backend, directory: Path) -> None:
    manifest_path = directory / WRITEBACK_MANIFEST
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return
    for entry in rc.configs:
        if not entry.writeback:
            continue
        path = directory / entry.effective_filename()
        if not path.is_file():
            continue
        current = path.read_bytes()
        if hashlib.sha256(current).hexdigest() != manifest.get(entry.effective_filename()):
            _writeback(rc, entry, backend, current)


def _spawn(argv: list[str], env: dict[str, str]) -> int:
    """Run ``argv`` forwarding termination signals to the child."""

    proc = subprocess.Popen(argv, env=env)

    def forward(signum: int, _frame: object) -> None:
        proc.send_signal(signum)

    previous: dict[int, object] = {}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous[sig] = signal.signal(sig, forward)
    try:
        return proc.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)  # type: ignore[arg-type]
