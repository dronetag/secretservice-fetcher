# secretservice-fetcher

> Distribution name **`secretservice-fetcher`**; the CLI command it installs is **`ss-fetcher`**.

Store and load **config files** and **environment-variable secrets** in a secret
store — the freedesktop **Secret Service** (GNOME Keyring, KWallet, KeePassXC, …)
or **HashiCorp Vault** — and feed them to the programs that need them so the
plaintext never has to live on disk.

The primary use case: in a systemd unit you prepend `ss-fetcher run` to
your `ExecStart`. At start the config is pulled from the store into a private
`0600` file under `$XDG_RUNTIME_DIR`, the path is handed to your program, env
secrets are injected, and the file is shredded when the program exits.

```ini
# ~/.config/systemd/user/myapp.service
ExecStart=ss-fetcher run -- /path/to/venv/bin/python app.py --config {config}
```

If wrapping is a problem (you need systemd to supervise *your* program as the
parent), use the **prepare** pattern instead — `ExecStartPre=` materialises the
files + an `EnvironmentFile`, `ExecStart=` runs your program directly, and
`ExecStopPost=` cleans up. See [Backends](#backends) and
[systemd: wrap vs prepare](#systemd-wrap-vs-prepare).

A complete, runnable walkthrough and ready-to-copy systemd units live in
[`examples/`](examples/) — start there if you prefer reading code.

## Concepts

A **`.secretrc`** file (TOML, validated with pydantic) describes two kinds of
secrets — **config files** (`[[configs]]`, materialised to a path) and
**environment variables** (`[[env]]`, injected as values). It contains *no
secrets* — only where each lives in the keyring. Commit it next to your code.

### `[[configs]]` — files

Each config entry maps a logical `name` to:

| field          | meaning                                                            |
| -------------- | ----------------------------------------------------------------- |
| `attributes`   | Secret Service lookup key (the item's D-Bus attributes)          |
| `label`        | human-readable keyring label                                      |
| `develop_path` | where `develop` writes the file for editing / `save` reads it     |
| `env`          | env var that receives the materialised path at runtime            |
| `default`      | the entry used by `{config}` and by commands with no `-c`         |
| `writeback`    | save the file back to the store if the program changed it (tokens) |

Lookup attributes are merged: `defaults.attributes` + the entry's `attributes`
+ an implicit `name = <entry name>`.

### `[[env]]` — environment variables

Each env entry looks a *scalar* secret up and injects its **value** into the
wrapped process's environment (vs. a config, which exposes a file *path*). This
is how you feed something like a `secrets.json` of API keys to a program:

| field        | meaning                                                          |
| ------------ | ---------------------------------------------------------------- |
| `var`        | the environment variable name the program reads                  |
| `name`       | keyring name (the `name` attribute); defaults to `var`           |
| `attributes` | extra lookup attributes (merged with `defaults.attributes`)      |
| `optional`   | if true, a missing secret is skipped instead of an error         |

Because `var` and `name` are separate, the keyring key can differ from the env
var — e.g. keyring `PERSONAL_O365_CLIENT_ID` → `$O365_CLIENT_ID`. The stored
value is exported verbatim, minus a single trailing newline. Every `[[env]]`
entry is injected on `run` (use `--no-env` to skip).

### Example `.secretrc`

```toml
id = "myapp"                             # namespaces the prepare dir + Vault base
runtime_dir = "/run/user/{uid}"          # {uid} -> your uid; default $XDG_RUNTIME_DIR

[defaults]
attributes = { app = "myapp" }
label_prefix = "myapp"
mode = "0600"

[[configs]]
name = "prod.yaml"
attributes = { kind = "config" }
develop_path = "./config/prod.yaml"
env = "APP_CONFIG"                        # exports APP_CONFIG=<file path>
default = true

[[env]]
var = "O365_CLIENT_ID"                    # program reads $O365_CLIENT_ID
name = "PERSONAL_O365_CLIENT_ID"          # …stored under this keyring name
attributes = { kind = "env" }

[[env]]
var = "O365_CLIENT_SECRET"                # name defaults to the var
attributes = { kind = "env" }
```

## Install

```bash
pipx install ./secretservice-fetcher        # or: pip install -e .
```

The Secret Service backend talks D-Bus via `secretstorage` (installed
automatically); it needs a running Secret Service provider (GNOME Keyring,
KWallet, KeePassXC, …) on the session bus. The Vault backend needs no extras.

## Commands

```bash
ss-fetcher init                  # scaffold a .secretrc
ss-fetcher list                  # show configs + whether each is stored

ss-fetcher save prod.yaml        # store ./config/prod.yaml into the keyring
cat prod.yaml | ss-fetcher save prod.yaml --from -   # …from stdin

ss-fetcher load prod.yaml        # print the stored config to stdout
ss-fetcher develop prod.yaml     # write it to develop_path for editing
# …edit the file…
ss-fetcher save prod.yaml        # push your edits back into the keyring

# environment-variable secrets
ss-fetcher list-env                                  # list the [[env]] var names (-l for status)
ss-fetcher set-env O365_CLIENT_ID                    # prompt for the value
ss-fetcher set-env O365_CLIENT_ID --value abc123     # or pass it / --from file|-
ss-fetcher get-env O365_CLIENT_ID                    # print the stored value
ss-fetcher edit-env                                  # edit all values in $EDITOR, save back
ss-fetcher clean-history --dry-run                   # find shell-history lines leaking a secret value
ss-fetcher import-env --from secrets.json --prefix PERSONAL_   # bulk import
eval "$(ss-fetcher env-export)"                      # export all [[env]] into the shell (direnv)
ss-fetcher install-direnv                            # add the env-export block to ./.envrc

ss-fetcher run -- myapp --config {config}            # wrap a command
ss-fetcher run -c a -c b -- myapp --a {config:a} --b {config:b}
ss-fetcher run --develop -- myapp --config {config}  # use on-disk file live
ss-fetcher run --no-env -- myapp --config {config}   # skip env injection

# systemd-friendly (no wrapper process; see below)
ss-fetcher prepare               # write config files + an EnvironmentFile
ss-fetcher paths                 # print the deterministic paths it uses
ss-fetcher cleanup               # remove them again

ss-fetcher rm prod.yaml          # delete from the store
```

`list-env` lists the `[[env]]` var names from `.secretrc` (`-l` adds the keyring
name and stored/missing status). `edit-env` opens **all** their values at once in
`$EDITOR` (vim) as a `VAR=value` document; on save it writes the changed ones
back to the store — the temp file lives only in `$XDG_RUNTIME_DIR` (tmpfs) and is
removed afterwards. Quit without saving and nothing changes.

`clean-history` scrubs your shell history of secrets you once typed (e.g.
`set-env --value <token>`): it reads each stored `[[env]]` value and deletes any
line in `~/.zsh_history` / `~/.bash_history` (and `$HISTFILE`) that **contains
that value**. It matches by value, skips values shorter than `--min-length` (8,
to avoid nuking common strings), prompts before rewriting (`--yes` to skip,
`--dry-run` to preview with the secret redacted), and writes no backup (which
would re-expose the secret). Reload your shell afterwards (`exec $SHELL`).

`run` materialises the selected config(s), substitutes `{config}` /
`{config:NAME}` in the command, exports each config's `env` path var, injects
every `[[env]]` secret as an environment variable, forwards termination signals
to the child, propagates its exit code, and deletes the materialised files
afterwards. With `--develop` it uses your `develop_path`
files directly (live editing, no cleanup) — handy while iterating.

**Write-back (refresh tokens):** mark a config `writeback = true` and, if the
program rewrites the materialised file while running (e.g. it rotates an OAuth
access token and persists the new refresh token), ss-fetcher saves the changed
file back to the store before shredding it — so the rotation isn't lost. This
works for both patterns: `run` saves on child exit, and the `prepare`/`cleanup`
pair saves on `cleanup` (so the keyring must be reachable at service stop). It's
skipped for `--develop` runs (use `save` there) and only fires when the bytes
actually changed.

Point at a specific config file with `-r/--secretrc PATH` or `$SECRETRC`;
otherwise `.secretrc` is searched for in the cwd and its parents.

## Workflow

```
                save                        run
  prod.yaml  ─────────▶  secret store  ─────────▶  /run/user/UID/…/prod.yaml
     ▲                     │   ▲                          │ (0600, auto-deleted)
     └─────── develop ─────┘   └──── save (after edit) ───┘
```

## Backends

Select the backend in `.secretrc` with `backend = "secret-service"` (default)
or `backend = "vault"`.

### Secret Service (default)

Talks D-Bus via `secretstorage` over a single connection, so a batch of lookups
(e.g. `env-export`) unlocks the keyring **once**. Items are unlocked before
reading or deleting, so `store`/`lookup`/`clear` (and `rm`) all work even with
**KeePassXC**, which advertises items as locked while its database is open — no
GUI clicking, no per-item prompts. Needs a Secret Service provider on the session
bus (so for a boot-time systemd unit, ensure the keyring is up/unlocked).

### HashiCorp Vault

Talks to Vault's KV engine over HTTP (standard library only — no extra
dependency). The **token is read from an environment variable** (`$VAULT_TOKEN`
by default); it never lives in `.secretrc`. The address comes from `vault.addr`
or `$VAULT_ADDR`.

```toml
backend = "vault"

[vault]
addr = "https://vault.example.com:8200"   # or $VAULT_ADDR
token_env = "VAULT_TOKEN"                  # env var holding the token
mount = "secret"
kv_version = 2
# path = "myapp"        # base path under the mount (defaults to id)
# namespace = "team"    # Vault Enterprise (or namespace_env = "VAULT_NAMESPACE")
```

Layout: each config is its own KV secret at `<mount>/<base>/<name>` (field
`value`); all `[[env]]` secrets are fields under one path `<mount>/<base>/env`
(idiomatic for a bag of variables). `<base>` defaults to the secretrc `id`.

## systemd: wrap vs prepare

Two integration patterns (full units in [`examples/systemd/`](examples/systemd/)):

**Wrap** — `ss-fetcher` is the process systemd supervises; it execs your
program as a child, injects everything, and shreds the temp files on exit:

```ini
ExecStart=ss-fetcher -r /opt/app/.secretrc run -- /opt/app/app --config {config}
```

**Prepare** — systemd runs *your* program directly (it stays the parent, so
MainPID/cgroup/signals/`sd_notify` all point at it). `ss-fetcher` only runs
as short pre/post steps:

```ini
RuntimeDirectory=ss-fetcher/<id>
ExecStartPre=ss-fetcher -r /opt/app/.secretrc prepare
EnvironmentFile=-%t/ss-fetcher/<id>/env
ExecStart=/opt/app/app --config %t/ss-fetcher/<id>/prod.yaml
ExecStopPost=ss-fetcher -r /opt/app/.secretrc cleanup
```

`%t` is the unit runtime dir; `prepare` writes into `%t/ss-fetcher/<id>/`.
`RuntimeDirectory=` makes systemd create that dir (`0700`) and clean it up, and
sets `$RUNTIME_DIRECTORY` — which **ss-fetcher honours**, writing exactly there
(so it tracks systemd even if the directory name differs from `<id>`; it falls
back to `$XDG_RUNTIME_DIR` for manual runs). `ss-fetcher paths` prints the exact
paths. systemd reads `EnvironmentFile=` when spawning `ExecStart` (after
`ExecStartPre`), so generating it first works; keep the leading `-` so a missing
file isn't fatal. See [examples/systemd/](examples/systemd/) for the full units.

## Local dev with direnv (`.envrc`)

For local development, [direnv](https://direnv.net) can export your `[[env]]`
secrets into your shell the moment you `cd` into the project. Install the
integration with one command:

```bash
ss-fetcher install-direnv      # add the block to ./.envrc (idempotent)
direnv allow
```

`install-direnv` writes a self-contained, marker-delimited block to `.envrc`
(creating or updating it). The block is **generic** (no secret/config names — it
runs `eval "$(ss-fetcher env-export)"`, which reads the names from `.secretrc`)
and **enclosed**: it's guarded with `if/else` instead of `return`, so it no-ops
with a notice when `ss-fetcher` isn't installed and is safe to append to an
existing `.envrc` — the rest of that file still runs. (`--print` outputs the
block instead of writing.)

`ss-fetcher env-export` prints `export VAR=value` for every `[[env]]` secret
(stdout, shell-quoted) and writes advisory warnings to stderr — for any secret
that isn't stored yet, and for any `[[configs]]` `develop_path` that isn't
expanded. It handles **env vars only**; config files stay a manual
`ss-fetcher develop <name>` step (the warning reminds you).

**Unloading:** direnv reverts every variable the `.envrc` exports when you leave
the directory — automatically. Nothing is written to disk, so there's nothing to
clean up.

The ready-to-copy block lives at [`examples/dummy/.envrc`](examples/dummy/.envrc).

## Development

Standard dronetag Python layout (setuptools, ruff, pre-commit, semantic-release).

```bash
pip install -e '.[dev]'      # installs the package + dev tools
pre-commit install           # ruff + commitlint on commit
pytest                       # tests run against the installed package
ruff check . && ruff format .
```

The version is dynamic: local builds report `99.99` (from
[`__version__.py`](src/secretservice_fetcher/__version__.py)); CI injects the
real semantic-release version at build time. CI lives in
[`.github/workflows/`](.github/workflows/) — `get-version` → `build` →
`test` (matrix 3.11–3.13) → `release`.

The test suite covers config-model validation, file/env materialisation and
injection, `prepare`/`cleanup`, ref derivation, and the Vault KV client (with a
faked transport) — no real keyring or Vault needed.
