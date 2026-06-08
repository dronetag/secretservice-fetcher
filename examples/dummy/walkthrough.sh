#!/usr/bin/env bash
# End-to-end walkthrough of ss-fetcher using the dummy app.
# Run from this directory:  ./walkthrough.sh
# The app uses pydantic-settings:  pip install pydantic-settings pyyaml
set -euo pipefail
cd "$(dirname "$0")"

# Use an installed `ss-fetcher`, or fall back to running from ../../src.
if command -v ss-fetcher >/dev/null 2>&1; then
    SM=(ss-fetcher)
else
    SM=(env "PYTHONPATH=$(cd ../../src && pwd)" python3 -m secretservice_fetcher.cli)
    echo "note: ss-fetcher not installed; running from ../../src"
fi
sm() { "${SM[@]}" "$@"; }

echo "== 1. what configs does .secretrc declare? =="
sm list
echo

echo "== 2. store config/prod.yaml into the backend =="
sm save prod.yaml
echo

echo "== 3. remove the plaintext; it now lives only in the backend =="
rm -f config/prod.yaml
sm list
echo

echo "== 4. store individual secrets as env vars =="
sm set-env O365_CLIENT_ID     --value "client-id-from-azure"
sm set-env O365_CLIENT_SECRET --value "super-secret-value"
echo

echo "== 5. run the app -- config file materialised + env vars injected =="
# $O365_CLIENT_ID comes from keyring name PERSONAL_O365_CLIENT_ID (a rename).
sm run -- python3 app.py --config '{config}'
echo

echo "== 6. systemd pattern: prepare files + EnvironmentFile, then run directly =="
sm prepare
CONF="$(sm paths --config prod.yaml)"
ENVF="$(sm paths --env-file)"
# This mimics systemd: ExecStartPre=prepare, then ExecStart runs the app itself
# (so the app -- not ss-fetcher -- is the process systemd supervises).
( set -a; . "$ENVF"; set +a; python3 app.py --config "$CONF" )
sm cleanup           # ExecStopPost=cleanup
echo

echo "== 7. develop mode: bring the config back to disk to edit =="
sm develop prod.yaml
echo "   (edit config/prod.yaml, then 'ss-fetcher save prod.yaml' to push it back)"
echo

echo "== 8. run --develop uses the on-disk file directly (env vars still injected) =="
sm run --develop -- python3 app.py --config '{config}'
echo

echo "Done. To remove the stored secrets:  ss-fetcher rm prod.yaml  (and set-env entries)"
