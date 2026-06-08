# systemd usage

Two integration patterns, four example units for the `dummy` service.

### Wrap pattern — `ss-fetcher` is the parent

| file                                              | how the program gets its secrets                          |
| ------------------------------------------------- | --------------------------------------------------------- |
| [`dummy.before.service`](dummy.before.service)    | plaintext file on disk (the thing we're replacing)        |
| [`dummy.wrap.service`](dummy.wrap.service)        | wrapped; `{config}` → materialised path + `[[env]]` vars  |
| [`dummy.env.service`](dummy.env.service)          | wrapped; config path via `$APP_CONFIG` (no CLI flag)      |

`ss-fetcher run` execs your program as a child, injects everything, and
shreds the temp files on exit. Simple, but **systemd supervises
`ss-fetcher`, not your program**.

### Prepare pattern — systemd is the parent

| file                                              | how the program gets its secrets                          |
| ------------------------------------------------- | --------------------------------------------------------- |
| [`dummy.prepare.service`](dummy.prepare.service)  | `ExecStartPre=prepare` + `EnvironmentFile=` + `ExecStopPost=cleanup` |

Here **systemd runs your program directly** (it stays the parent — MainPID,
cgroup, signals, `sd_notify` all point at your process). `ss-fetcher
prepare` writes the config file(s) and an `EnvironmentFile` into
`%t/ss-fetcher/<id>/` before `ExecStart`; `cleanup` removes them after.
Reach for this when wrapping causes problems.

`%t` is the unit runtime dir (`$XDG_RUNTIME_DIR` for user units) — the same
location `prepare` writes to. Print the exact paths with:

```bash
ss-fetcher paths
ss-fetcher paths --config prod.yaml   # just one path (scripting)
ss-fetcher paths --env-file
```

## One-time setup

```bash
cd /opt/dummy
ss-fetcher save prod.yaml                       # store the config
ss-fetcher set-env O365_CLIENT_ID --value ...   # store env secrets
rm prod.yaml                                          # remove plaintext
```

## Install

```bash
cp dummy.prepare.service ~/.config/systemd/user/dummy.service   # or dummy.wrap.service
systemctl --user daemon-reload
systemctl --user start dummy.service
journalctl --user -u dummy.service -e
```

## Runtime directory

The tool and the unit must agree on **one** directory. The tool writes to
`<base>/ss-fetcher/<id>`; the unit references `%t/ss-fetcher/<id>`. They line up
when `<base>` equals `%t`, and the clean way to guarantee that is to base
everything on **`$XDG_RUNTIME_DIR`**:

- The tool's `runtime_dir` defaults to `$XDG_RUNTIME_DIR` — so **leave
  `runtime_dir` unset** in `.secretrc` (the dummy example does). Don't hardcode
  `/run/user/{uid}`: it only matches by luck for user units and is plain wrong
  for system units (and may not even exist for a daemon `User=`).
- **User services** (`systemctl --user`): systemd always sets
  `XDG_RUNTIME_DIR=/run/user/<uid>`, which **is** `%t`. Nothing else to do — this
  is the recommended setup and it works at boot too (`loginctl enable-linger`).
- Let systemd own the directory with **`RuntimeDirectory=ss-fetcher/<id>`**: it
  creates `%t/ss-fetcher/<id>` at `0700`, owned by the unit's user, *before*
  `ExecStartPre`, and **removes it on stop** — so `ExecStopPost=… cleanup`
  becomes optional (kept in the example as an explicit shred). systemd also
  exports **`$RUNTIME_DIRECTORY`** with that path, and **ss-fetcher auto-detects
  it**: `prepare`/`cleanup`/`paths` then write to exactly that directory, so
  alignment is automatic — the `RuntimeDirectory=` name need not match `id`, and
  you don't have to reason about `runtime_dir`/`%t` at all. (Manual runs, where
  `$RUNTIME_DIRECTORY` is unset, fall back to the computed path below.)
- **System services** (not `--user`): `XDG_RUNTIME_DIR` is unset and `%t` is
  `/run`, so add **`Environment=XDG_RUNTIME_DIR=%t`** to pin the tool's base to
  `/run` (the units carry this as a commented line).

In short: unset `runtime_dir` + `RuntimeDirectory=ss-fetcher/<id>` (+
`Environment=XDG_RUNTIME_DIR=%t` for system units). Use `ss-fetcher paths` to
print the exact resolved paths.

## Notes / gotchas

- **`EnvironmentFile` ordering:** systemd reads it when spawning `ExecStart`,
  which happens *after* `ExecStartPre`, so `prepare` generating it first works.
  Keep the leading `-` (`EnvironmentFile=-...`) so a missing file (no `[[env]]`
  vars) isn't fatal. `EnvironmentFile` values should be single-line.
- **PATH:** systemd user units have a minimal `PATH`. If `ss-fetcher` isn't
  found, install it onto the user `PATH` (e.g. `pipx` → `~/.local/bin`) or use
  the absolute path to the console script.
- **Backend must be reachable** when the unit runs:
  - *secret-service:* a Secret Service provider must be on the session D-Bus and
    its store unlocked (graphical session / `gnome-keyring`, or KeePassXC with
    its DB open).
  - *vault:* `$VAULT_TOKEN` (and `$VAULT_ADDR`) must be in the unit's
    environment — supply via a private `EnvironmentFile=` or systemd
    credentials. Never put the token in the committed `.secretrc`.
- **`%t`/`%h`** expand to the runtime dir / home inside units — handy so paths
  aren't user-specific.
