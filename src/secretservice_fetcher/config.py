"""Pydantic models and loader for the ``.secretrc`` configuration file.

A ``.secretrc`` file (TOML) describes one or more configuration files that live
in the freedesktop Secret Service.  Each entry maps a *logical name* to:

* the Secret Service lookup *attributes* (the same key/value pairs you would
  pass to ``secret-tool``),
* a human readable *label*,
* an optional *develop_path* where the file is written when you want to edit it,
* an optional *env* variable that receives the materialised file path at runtime.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SECRETRC_FILENAME = ".secretrc"


class Defaults(BaseModel):
    """Settings merged into every config entry."""

    model_config = ConfigDict(extra="forbid")

    # Attributes merged into every entry's lookup attributes (entry wins on clash).
    attributes: dict[str, str] = Field(default_factory=dict)
    # Default label prefix, e.g. "myapp" -> "myapp prod.yaml".
    label_prefix: str | None = None
    # Default file permissions for materialised files.
    mode: str = "0600"


class ConfigEntry(BaseModel):
    """A single configuration file stored in the Secret Service."""

    model_config = ConfigDict(extra="forbid")

    # Logical id used on the CLI and in ``{config:NAME}`` placeholders.
    name: str
    # Secret Service label. Defaults to ``"<label_prefix> <name>"`` or ``name``.
    label: str | None = None
    # Extra lookup attributes (merged on top of ``defaults.attributes``).
    attributes: dict[str, str] = Field(default_factory=dict)
    # Filename used when materialising. Defaults to ``name``.
    filename: str | None = None
    # On-disk path used by ``develop`` / ``save`` and by ``run --develop``.
    develop_path: Path | None = None
    # Permission bits (octal string) for the materialised file.
    mode: str | None = None
    # If set, the materialised path is exported as this environment variable.
    env: str | None = None
    # Marks the config picked by ``{config}`` and by ``run`` with no ``-c``.
    default: bool = False
    # If true, changes the program makes to the materialised file are saved back
    # to the store before cleanup (e.g. OAuth refresh-token rotation).
    writeback: bool = False

    def effective_filename(self) -> str:
        return self.filename or self.name

    def octal_mode(self, fallback: str) -> int:
        return int(self.mode or fallback, 8)


class EnvEntry(BaseModel):
    """A single secret value exported as an environment variable.

    Unlike a :class:`ConfigEntry` (which materialises a *file* and exposes its
    *path*), an env entry looks a scalar secret up in the Secret Service and
    injects its *value* into the wrapped process's environment.

    Example: keyring item ``name=PERSONAL_O365_CLIENT_ID`` -> ``$O365_CLIENT_ID``::

        [[env]]
        var = "O365_CLIENT_ID"
        name = "PERSONAL_O365_CLIENT_ID"
        attributes = { kind = "env" }
    """

    model_config = ConfigDict(extra="forbid")

    # Environment variable name handed to the wrapped process.
    var: str
    # Logical/keyring name (the implicit ``name`` attribute). Defaults to ``var``.
    name: str | None = None
    # Extra lookup attributes (merged on top of ``defaults.attributes``).
    attributes: dict[str, str] = Field(default_factory=dict)
    # If true, a missing secret is skipped instead of being an error.
    optional: bool = False

    def logical_name(self) -> str:
        return self.name or self.var


class VaultConfig(BaseModel):
    """HashiCorp Vault KV settings (used when ``backend = "vault"``)."""

    model_config = ConfigDict(extra="forbid")

    # Vault address. Falls back to $<addr_env> (default $VAULT_ADDR).
    addr: str | None = None
    addr_env: str = "VAULT_ADDR"
    # Env var holding the token. The token itself never lives in .secretrc.
    token_env: str = "VAULT_TOKEN"
    # KV mount point and engine version.
    mount: str = "secret"
    kv_version: Literal[1, 2] = 2
    # Base path under the mount. Defaults to the secretrc identity.
    path: str | None = None
    # Leaf path (under base) grouping all [[env]] secrets as fields.
    env_path: str = "env"
    # Vault Enterprise namespace (X-Vault-Namespace). Literal or via env.
    namespace: str | None = None
    namespace_env: str | None = None


class RcSecret(BaseModel):
    """Root model for a ``.secretrc`` file."""

    model_config = ConfigDict(extra="forbid")

    # Logical identity; namespaces the prepare dir and the Vault base path.
    # Defaults to defaults.attributes["app"] or "default".
    id: str | None = None
    # Which storage backend to use.
    backend: Literal["secret-service", "vault"] = "secret-service"
    vault: VaultConfig = Field(default_factory=VaultConfig)
    # Directory where files are materialised in ``run`` mode.
    # ``{uid}`` expands to the current uid. Defaults to $XDG_RUNTIME_DIR.
    runtime_dir: str | None = None
    defaults: Defaults = Field(default_factory=Defaults)
    configs: list[ConfigEntry] = Field(default_factory=list)
    env: list[EnvEntry] = Field(default_factory=list)

    # Path this model was loaded from (populated by ``load``); not part of the file.
    source_path: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _check_unique_names(self) -> RcSecret:
        seen: set[str] = set()
        for entry in self.configs:
            if entry.name in seen:
                raise ValueError(f"duplicate config name: {entry.name!r}")
            seen.add(entry.name)
        defaults = [c for c in self.configs if c.default]
        if len(defaults) > 1:
            raise ValueError(
                "only one config may be marked default: " + ", ".join(c.name for c in defaults)
            )
        env_seen: set[str] = set()
        for entry in self.env:
            if entry.var in env_seen:
                raise ValueError(f"duplicate env var: {entry.var!r}")
            env_seen.add(entry.var)
        return self

    # --- derived helpers -------------------------------------------------

    @staticmethod
    def systemd_runtime_directory() -> Path | None:
        """The directory from systemd's ``$RUNTIME_DIRECTORY``, if present.

        systemd exports ``$RUNTIME_DIRECTORY`` to a unit's ``Exec*`` commands
        when the unit declares ``RuntimeDirectory=`` -- the absolute path(s) of
        the directory it created (``0700``, owned by the unit's user) and removes
        on stop. It may be a colon-separated list; we use the first entry.
        """

        value = os.environ.get("RUNTIME_DIRECTORY")
        if value:
            return Path(value.split(":")[0])
        return None

    def runtime_directory(self) -> Path:
        if self.runtime_dir:
            return Path(self.runtime_dir.format(uid=os.getuid()))
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg:
            return Path(xdg)
        return Path(f"/run/user/{os.getuid()}")

    def runtime_base(self) -> Path:
        """Base dir for runtime files; prefers systemd's ``$RUNTIME_DIRECTORY``."""

        return self.systemd_runtime_directory() or self.runtime_directory()

    def identity(self) -> str:
        if self.id:
            return self.id
        return self.defaults.attributes.get("app", "default")

    def prepare_directory(self) -> Path:
        """Deterministic dir used by `prepare`/`cleanup` (systemd pattern).

        If systemd provides ``$RUNTIME_DIRECTORY`` (the unit declared
        ``RuntimeDirectory=``), that exact directory is used -- so the tool tracks
        whatever systemd created and cleans up. Otherwise it falls back to
        ``<runtime_dir>/ss-fetcher/<identity>``, i.e. ``%t/ss-fetcher/<identity>``
        inside a unit (so a matching ``RuntimeDirectory=ss-fetcher/<id>`` lines
        up either way).
        """

        systemd = self.systemd_runtime_directory()
        if systemd is not None:
            return systemd
        return self.runtime_directory() / "ss-fetcher" / self.identity()

    def env_file(self) -> Path:
        return self.prepare_directory() / "env"

    def prepared_config_path(self, entry: ConfigEntry) -> Path:
        return self.prepare_directory() / entry.effective_filename()

    def vault_base_path(self) -> str:
        return (self.vault.path or self.identity()).strip("/")

    def get(self, name: str) -> ConfigEntry:
        for entry in self.configs:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def default_entry(self) -> ConfigEntry:
        marked = [c for c in self.configs if c.default]
        if marked:
            return marked[0]
        if len(self.configs) == 1:
            return self.configs[0]
        raise LookupError(
            "no default config: mark one entry with `default = true` "
            "or select one explicitly with -c/--config"
        )

    def effective_attributes(self, entry: ConfigEntry) -> dict[str, str]:
        attrs = dict(self.defaults.attributes)
        attrs.update(entry.attributes)
        attrs.setdefault("name", entry.name)
        return attrs

    def effective_label(self, entry: ConfigEntry) -> str:
        if entry.label:
            return entry.label
        if self.defaults.label_prefix:
            return f"{self.defaults.label_prefix} {entry.name}"
        return entry.name

    def effective_mode(self, entry: ConfigEntry) -> int:
        return entry.octal_mode(self.defaults.mode)

    def get_env(self, var: str) -> EnvEntry:
        for entry in self.env:
            if entry.var == var:
                return entry
        raise KeyError(var)

    def effective_env_attributes(self, entry: EnvEntry) -> dict[str, str]:
        attrs = dict(self.defaults.attributes)
        attrs.update(entry.attributes)
        attrs.setdefault("name", entry.logical_name())
        return attrs

    def effective_env_label(self, entry: EnvEntry) -> str:
        if self.defaults.label_prefix:
            return f"{self.defaults.label_prefix} {entry.logical_name()}"
        return entry.logical_name()


def find_secretrc(start: Path | None = None) -> Path | None:
    """Search ``start`` and its parents for a ``.secretrc`` file."""

    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / SECRETRC_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load(path: Path | None = None) -> RcSecret:
    """Load a ``.secretrc`` file.

    If ``path`` is None, honour ``$SECRETRC`` then search up from the cwd.
    """

    if path is None:
        env_path = os.environ.get("SECRETRC")
        path = Path(env_path) if env_path else find_secretrc()
    if path is None:
        raise FileNotFoundError(
            f"no {SECRETRC_FILENAME} found (searched cwd and parents); "
            "pass --secretrc or set $SECRETRC"
        )
    path = Path(path)
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    model = RcSecret.model_validate(data)
    model.source_path = path
    return model
