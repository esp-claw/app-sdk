#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 esp-claw contributors
# SPDX-License-Identifier: Apache-2.0
#
# Device test for signed app installs on a flashed esp-claw device:
#
#   tools/device_test.sh setup <device-ip> <ed25519.pem> [unsigned-build-dir]
#       trust the key (app_trusted_keys), optionally copy an unsigned build to /apps/<name>/, reboot
#   tools/device_test.sh serve <ed25519.pem> <build-dir> [build-dir ...]
#       sign each build (manifest.json + app.wasm), add an update, a downgrade and a tampered package
#       of the first one, serve everything over https and print the Telegram steps
#   tools/device_test.sh check <device-ip> [app-name ...]
#       show apps/index.json and the cached tool lists of the named apps
#
# `serve` needs `cloudflared` (quick tunnel) or BASE_URL=https://... pointing at a host where you
# upload the files it prints. RELEASE_URL is the GitHub release package used for the redirect test
# (default: app-hello v1.0.0). The private key is only passed to ecapp.py, never printed.
set -euo pipefail
TOOLS=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-/tmp/ecapp-device-test}
PORT=${PORT:-8765}
RELEASE_URL=${RELEASE_URL:-https://github.com/esp-claw/app-hello/releases/download/v1.0.0/hello-1.0.0.ecapp}

ecapp() { python3 "$TOOLS/ecapp.py" "$@"; }
json_get() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }

wait_up() {
    for _ in $(seq 60); do
        curl -fsS -m 2 "http://$1/api/status" >/dev/null 2>&1 && return 0
        sleep 2
    done
    echo "device $1 did not come back" >&2
    exit 1
}

setup() {
    local ip=$1 pem=$2 unsigned=${3:-} pub name
    pub=$(ecapp pubkey "$pem" | cut -d' ' -f1)
    curl -fsS -X POST "http://$ip/api/config" -H 'Content-Type: application/json' \
        -d "{\"app_trusted_keys\":\"[\\\"$pub\\\"]\",\"app_allow_unsigned\":\"false\"}"
    echo
    if [ -n "$unsigned" ]; then
        # An unsigned copy must show up as "unsigned" and not load.
        name=$(json_get "$unsigned/manifest.json" name)
        curl -fsS -X POST "http://$ip/api/files/mkdir" -d "{\"path\":\"/apps/$name\",\"recursive\":true}" >/dev/null
        for f in manifest.json app.wasm; do
            curl -fsS -X POST --data-binary "@$unsigned/$f" "http://$ip/api/files/upload?path=/apps/$name/$f" >/dev/null
        done
    fi
    curl -fsS -X POST "http://$ip/api/restart" >/dev/null || true
    sleep 5
    wait_up "$ip"
    echo "Trusted key $pub; device rebooted."
    if [ -n "$unsigned" ]; then
        echo "Telegram: /apps  ->  $name ... unsigned, last error: not loaded: no signature.json"
    fi
}

serve() {
    local pem=$1 base first name version dir others=""
    shift
    rm -rf "$OUT" && mkdir -p "$OUT/src"
    first=$(json_get "$1/manifest.json" name)
    version=$(json_get "$1/manifest.json" version)
    for dir in "$@"; do
        name=$(json_get "$dir/manifest.json" name)
        ecapp pack "$dir" --key "$pem" -o "$OUT/$name.ecapp" >/dev/null
        [ "$dir" = "$1" ] || others="$others $name"
    done
    # Same code, other versions: an update and a downgrade.
    for v in 99.0.1 0.0.1; do
        mkdir -p "$OUT/src/$v" && cp "$1/app.wasm" "$OUT/src/$v/"
        sed "s/\"version\": *\"[^\"]*\"/\"version\": \"$v\"/" "$1/manifest.json" > "$OUT/src/$v/manifest.json"
        ecapp pack "$OUT/src/$v" --key "$pem" -o "$OUT/$first-$v.ecapp" >/dev/null
    done
    # Signed package whose app.wasm was changed afterwards.
    python3 - "$OUT/$first.ecapp" "$OUT/tampered.ecapp" <<'EOF'
import sys
blob = bytearray(open(sys.argv[1], "rb").read())
i = blob.index(b"\x00asm")
blob[i + 20] ^= 1
open(sys.argv[2], "wb").write(blob)
EOF
    rm -rf "$OUT/src"
    if [ -n "${BASE_URL:-}" ]; then
        base=$BASE_URL
        echo "Upload these files to $base/:"
        ls -1 "$OUT"
    else
        command -v cloudflared >/dev/null || { echo "need cloudflared or BASE_URL" >&2; exit 1; }
        (cd "$OUT" && python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1) &
        cloudflared tunnel --url "http://127.0.0.1:$PORT" > "$OUT.tunnel.log" 2>&1 &
        trap 'kill $(jobs -p) 2>/dev/null' EXIT
        for _ in $(seq 30); do
            base=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$OUT.tunnel.log" | head -1 || true)
            [ -n "$base" ] && break
            sleep 1
        done
        [ -n "$base" ] || { echo "no tunnel URL, see $OUT.tunnel.log" >&2; exit 1; }
    fi
    cat <<EOF

Telegram steps (each install/remove shows a confirmation; press the button unless noted):
 1. /app install $base/tampered.ecapp       -> Error: package rejected: app.wasm does not match its signed hash
 2. /app install $base/$first.ecapp         -> Installed \`$first\` $version: N tools.
 3. Ask the agent to call one of the ${first}_* tools; /apps -> $first $version — loaded, psram ... KB, key ...
 4. /app install $base/$first-0.0.1.ecapp   -> Error: $first 0.0.1 is older than the installed $version ...
 5. /app install --force $base/$first-0.0.1.ecapp -> confirmation shows "⚠️ Downgrade"; press ❌ -> Not installed: cancelled
 6. /app update $first $base/$first-99.0.1.ecapp  -> Installed \`$first\` 99.0.1: N tools.
 7. /app update $first                      -> confirmation for 99.0.1 again (URL from index.json); press ❌
 8. /apps disable $first, then reboot (curl -X POST http://<ip>/api/restart)
    /apps                                   -> $first disabled; the agent no longer sees ${first}_* tools
    /apps enable $first                     -> Enabled \`$first\` 99.0.1: N tools.
 9. After that reboot: /apps shows $first "unloaded" (tools registered from tools.json); the first
    tool call logs "[$first] v99.0.1 loaded on demand in N ms".
10. Ask the agent to use apps_status, apps_unload($first), apps_load($first), apps_set_enabled($first,false/true)
                                            -> no confirmation; apps_status shows loaded/psram_bytes/last_used_s_ago
11. Leave $first loaded 10 min without calls -> log: "[$first] unloaded after 10 min without calls"
12. GitHub release (302 to a ~1 KB signed release-assets.githubusercontent.com URL; key must be trusted):
    /app install $RELEASE_URL
                                            -> Installed ... (log: download ...: ESP_OK, HTTP 200; no query string logged)
EOF
    for name in $others; do
        echo "13. Ask: \"install $base/$name.ecapp\" -> agent calls apps_install, confirmation appears; then /app remove $name"
    done
    cat <<EOF
14. tools/device_test.sh check <ip> $first$others

Serving $OUT at $base (Ctrl-C to stop).
EOF
    [ -n "${BASE_URL:-}" ] || wait
}

check() {
    local ip=$1 app
    shift
    curl -fsS "http://$ip/files/apps/index.json"
    echo
    for app in "$@"; do
        echo "--- $app/tools.json"
        curl -fsS "http://$ip/files/apps/$app/tools.json" | head -c 400 || echo "(none)"
        echo
    done
}

usage() {
    sed -n '5,17p' "$0"
    exit 1
}

case "${1:-}" in
    setup) [ $# -ge 3 ] || usage; setup "$2" "$3" "${4:-}" ;;
    serve) [ $# -ge 3 ] || usage; shift; serve "$@" ;;
    check) [ $# -ge 2 ] || usage; shift; check "$@" ;;
    *) usage ;;
esac
