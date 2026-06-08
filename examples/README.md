# Examples

A complete, runnable walkthrough lives in [`dummy/`](dummy/): a service that
needs a config file and several API-key env vars which should never sit on disk
in plaintext.

| file                                              | what it is                                              |
| ------------------------------------------------- | ------------------------------------------------------- |
| [`dummy/.secretrc`](dummy/.secretrc)              | declares the `prod.yaml` config + `[[env]]` secrets     |
| [`dummy/config/prod.yaml`](dummy/config/prod.yaml)| the (fake) secret config you store into the backend     |
| [`dummy/app.py`](dummy/app.py)                    | pydantic-settings app: merges the config file **and** env vars |
| [`dummy/walkthrough.sh`](dummy/walkthrough.sh)    | runs store → run → prepare → develop end-to-end         |
| [`dummy/.envrc`](dummy/.envrc)                    | direnv block from `ss-fetcher install-direnv` (env only)|
| [`systemd/`](systemd/)                            | unit files for both the wrap and prepare patterns       |

> `app.py` uses [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
> to load the YAML config file and the environment variables into one validated
> `Settings` object (env wins over the file). Install its deps first:
> `pip install pydantic-settings pyyaml`.

## Quick start

```bash
cd examples/dummy
./walkthrough.sh
```

The script stores `config/prod.yaml` and the env secrets into the backend,
deletes the plaintext, then exercises both integration patterns:

- **wrap** — `ss-fetcher run -- app --config {config}` materialises the
  config to a private `0600` file in `$XDG_RUNTIME_DIR`, injects the env vars,
  and shreds the file on exit.
- **prepare** — `ss-fetcher prepare` writes the config + an
  `EnvironmentFile`, the app runs directly against them, then `cleanup` removes
  them (this is the systemd-friendly, no-wrapper flow).

## systemd

See [`systemd/`](systemd/) for both patterns. Wrap is a one-line `ExecStart`
change:

```diff
-ExecStart=/opt/dummy/venv/bin/python3 /opt/dummy/app.py
+ExecStart=ss-fetcher -r /opt/dummy/.secretrc run -- \
+    /opt/dummy/venv/bin/python3 /opt/dummy/app.py --config {config}
```

Prepare keeps your program as the process systemd supervises, and lets systemd
own the run directory via `RuntimeDirectory=` (created `0700` before
`ExecStartPre`, auto-removed on stop):

```ini
RuntimeDirectory=ss-fetcher/dummy
ExecStartPre=ss-fetcher -r /opt/dummy/.secretrc prepare
EnvironmentFile=-%t/ss-fetcher/dummy/env
ExecStart=/opt/dummy/venv/bin/python3 /opt/dummy/app.py --config %t/ss-fetcher/dummy/prod.yaml
ExecStopPost=ss-fetcher -r /opt/dummy/.secretrc cleanup
```

### Runtime directory

The tool writes to `<base>/ss-fetcher/<id>` and the unit reads `%t/ss-fetcher/<id>`;
these agree only when `<base>` equals `%t`. The reliable way is to base both on
**`$XDG_RUNTIME_DIR`**:

- **Leave `runtime_dir` unset** in `.secretrc` — it defaults to
  `$XDG_RUNTIME_DIR`, which systemd sets for `--user` services and which *is*
  `%t`. (Don't hardcode `/run/user/{uid}`: it only matches user units by luck and
  breaks system units.)
- For **system** services (`%t` = `/run`, `XDG_RUNTIME_DIR` unset), add
  `Environment=XDG_RUNTIME_DIR=%t` so the tool's base matches `%t`.

See [systemd/README.md → Runtime directory](systemd/README.md#runtime-directory)
for the full rationale.
