#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 esp-claw contributors
# SPDX-License-Identifier: Apache-2.0
# SDK self-test, run by CI and locally: tests/selftest.sh [headers|example|manifest|wasm|ecapp ...]
# Needs clang with the wasm32 target, wasm-ld, python3 and openssl (3.x). Without arguments all run.
# The ecapp test signs with a throwaway key generated in a temp dir; it never touches a real key.
set -euo pipefail
sdk="$(cd "$(dirname "$0")/.." && pwd)"
CC=${CC:-clang}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fails=0

ok() { echo "  ok: $*"; }
bad() { echo "  FAIL: $*" >&2; fails=$((fails + 1)); }
expect_ok() { # expect_ok <label> <cmd...>: the command must succeed
    local label=$1
    shift
    if "$@" >"$tmp/out" 2>&1; then ok "$label"; else bad "$label: $(tail -3 "$tmp/out")"; fi
}
expect_fail() { # expect_fail <label> <cmd...>: the command must exit non-zero
    local label=$1
    shift
    if "$@" >"$tmp/out" 2>&1; then bad "$label: accepted"; else ok "$label: rejected ($(head -1 "$tmp/out"))"; fi
}
wasm_cc() { "$CC" --target=wasm32 -mcpu=mvp -mbulk-memory -O2 -nostdlib -ffreestanding -I "$sdk/include" "$@"; }

test_headers() {
    expect_ok "JSON null regression compile" "$CC" -Wno-unknown-attributes -fsanitize=undefined,address -I "$sdk/include" "$sdk/tests/json_null.c" -o "$tmp/json_null"
    expect_ok "JSON null regression run" "$tmp/json_null"
    echo "headers: each compiles standalone for wasm32 with -Wall -Wextra -Werror -std=c11"
    for h in claw.h claw_util.h; do
        printf '#include "%s"\nint claw_tu_not_empty;\n' "$h" > "$tmp/tu.c"
        expect_ok "$h" wasm_cc -std=c11 -Wall -Wextra -Werror -Wpedantic -c -o "$tmp/tu.o" "$tmp/tu.c"
    done
    printf '#define CLAW_ARENA_SIZE 64\n#include "claw.h"\n#include "claw_util.h"\nCLAW_DEFINE_ARENA()\n' > "$tmp/tu.c"
    expect_ok "CLAW_DEFINE_ARENA" wasm_cc -std=c11 -Wall -Wextra -Werror -Wpedantic -c -o "$tmp/tu.o" "$tmp/tu.c"
}

test_example() {
    echo "example: build examples/minimal and check it"
    rm -rf "$sdk/examples/minimal/build"
    expect_ok "build" "$sdk/build-app.sh" "$sdk/examples/minimal"
    expect_ok "manifest" python3 "$sdk/tools/manifest_check.py" "$sdk/examples/minimal" --dir-name minimal
    expect_ok "wasm" python3 "$sdk/tools/wasm_check.py" "$sdk/examples/minimal/build"
}

test_manifest() {
    echo "manifest_check: valid and invalid manifests"
    local good=(
        '{"name":"a","version":"0.0.1"}'
        '{"name":"cf","version":"1.2.3-rc.1+b5","description":"","permissions":{"log":false,"kv":true,"confirm":true,"ping":true,"http_profiles":["cloudflare","a-b_C"]},"limits":{"memory_kb":512,"call_timeout_ms":100}}'
        '{"name":"abcdefghijklmnopqrstuvwx","version":"1.0.0","limits":{"memory_kb":64.0,"call_timeout_ms":30000}}'
        '{"name":"a","version":"1.0.0","settings":[]}'
        '{"name":"a","version":"1.0.0","settings":[{"key":"city","type":"string","default":"Wien","description":"x"},{"key":"token","type":"string","secret":true},{"key":"n","type":"int","default":-2147483648},{"key":"abcdefghijklmno","type":"bool","default":false,"secret":false}]}'
    )
    local badm=(
        '[]' '{"version":"1.0.0"}' '{"name":"a"}' '{"name":"","version":"1.0.0"}'
        '{"name":"A","version":"1.0.0"}' '{"name":"1a","version":"1.0.0"}' '{"name":"a-b","version":"1.0.0"}'
        '{"name":"abcdefghijklmnopqrstuvwxy","version":"1.0.0"}' '{"name":"a","version":"1.0"}'
        '{"name":"a","version":"1.0.0_x"}' '{"name":"a","version":"1.0.0-abcdefghijkl"}' '{"name":"a","version":"1.0.0+"}'
        '{"name":"a","version":"1.0.0","extra":1}' '{"name":"a","version":"1.0.0","permissions":{"wifi":true}}'
        '{"name":"a","version":"1.0.0","permissions":{"log":1}}' '{"name":"a","version":"1.0.0","permissions":[]}'
        '{"name":"a","version":"1.0.0","permissions":{"http_profiles":[]}}'
        '{"name":"a","version":"1.0.0","permissions":{"http_profiles":["a","b","c","d","e"]}}'
        '{"name":"a","version":"1.0.0","permissions":{"http_profiles":["a b"]}}'
        '{"name":"a","version":"1.0.0","permissions":{"http_profiles":["x","x"]}}'
        '{"name":"a","version":"1.0.0","limits":{"memory_kb":96}}' '{"name":"a","version":"1.0.0","limits":{"memory_kb":576}}'
        '{"name":"a","version":"1.0.0","limits":{"memory_kb":true}}' '{"name":"a","version":"1.0.0","limits":{"call_timeout_ms":99}}'
        '{"name":"a","version":"1.0.0","limits":{"call_timeout_ms":30001}}' '{"name":"a","version":"1.0.0","limits":{"stack_kb":8}}'
        "{\"name\":\"a\",\"version\":\"1.0.0\",\"description\":\"$(printf 'x%.0s' $(seq 201))\"}"
        '{"name":"a","version":"1.0.0","settings":{}}' '{"name":"a","version":"1.0.0","settings":[{"type":"int"}]}'
        '{"name":"a","version":"1.0.0","settings":[{"key":"a","type":"float"}]}'
        '{"name":"a","version":"1.0.0","settings":[{"key":"_a","type":"int"}]}'
        '{"name":"a","version":"1.0.0","settings":[{"key":"abcdefghijklmnop","type":"int"}]}'
        '{"name":"a","version":"1.0.0","settings":[{"key":"a","type":"int"},{"key":"a","type":"int"}]}'
        '{"name":"a","version":"1.0.0","settings":[{"key":"a","type":"int","x":1}]}'
        '{"name":"a","version":"1.0.0","settings":[{"key":"a","type":"string","secret":true,"default":"x"}]}'
        '{"name":"a","version":"1.0.0","settings":[{"key":"a","type":"int","default":2147483648}]}'
        '{"name":"a","version":"1.0.0","settings":[{"key":"a","type":"int","default":"1"}]}'
        '{"name":"a","version":"1.0.0","settings":[{"key":"a","type":"bool","default":0}]}'
        '{"name":"a","version":"1.0.0","settings":[{"key":"a","type":"string","default":""}]}'
        '{"name":"a","version":"1.0.0","settings":[{"key":"a","type":"int"},{"key":"b","type":"int"},{"key":"c","type":"int"},{"key":"d","type":"int"},{"key":"e","type":"int"},{"key":"f","type":"int"},{"key":"g","type":"int"},{"key":"h","type":"int"},{"key":"i","type":"int"}]}'
    )
    for m in "${good[@]}"; do
        echo "$m" > "$tmp/manifest.json"
        expect_ok "accepts $m" python3 "$sdk/tools/manifest_check.py" "$tmp/manifest.json"
    done
    for m in "${badm[@]}"; do
        echo "$m" > "$tmp/manifest.json"
        expect_fail "${m:0:70}" python3 "$sdk/tools/manifest_check.py" "$tmp/manifest.json"
    done
    echo '{"name":"a","version":"1.0.0"}' > "$tmp/manifest.json"
    expect_fail "--dir-name mismatch" python3 "$sdk/tools/manifest_check.py" "$tmp/manifest.json" --dir-name b
}

test_wasm() {
    echo "wasm_check: modules the runtime would reject"
    # shellcheck disable=SC2054 # commas belong to the -Wl, linker flags
    local ld=(-Wl,--no-entry -Wl,--export=app_init -Wl,--export=app_alloc -Wl,--export=tool_call
              -Wl,--initial-memory=65536 -Wl,--max-memory=65536 -Wl,-z,stack-size=8192)
    local d="$tmp/w" src="$sdk/examples/minimal/main.c"
    mkdir -p "$d"
    echo '{"name":"t","version":"1.0.0"}' > "$d/manifest.json"
    wasm_cc "${ld[@]}" -o "$d/app.wasm" "$src"
    expect_ok "baseline accepted" python3 "$sdk/tools/wasm_check.py" "$d"

    echo '{"name":"t","version":"1.0.0","limits":{"memory_kb":128}}' > "$d/manifest.json"
    expect_fail "memory differs from limits.memory_kb" python3 "$sdk/tools/wasm_check.py" "$d"
    echo '{"name":"t","version":"1.0.0"}' > "$d/manifest.json"

    printf '#include "claw.h"\nvoid use_log(void) { CLAW_LOG(CLAW_LOG_INFO, "x"); }\n' > "$tmp/log.c"
    wasm_cc "${ld[@]}" -Wl,--export=use_log -o "$d/app.wasm" "$src" "$tmp/log.c"
    expect_fail "claw.log without permission log" python3 "$sdk/tools/wasm_check.py" "$d"
    echo '{"name":"t","version":"1.0.0","permissions":{"log":true}}' > "$d/manifest.json"
    expect_ok "claw.log with permission log accepted" python3 "$sdk/tools/wasm_check.py" "$d"
    echo '{"name":"t","version":"1.0.0"}' > "$d/manifest.json"

    printf 'int env_fn(void);\nint use_env(void) { return env_fn(); }\n' > "$tmp/env.c"
    wasm_cc "${ld[@]}" -Wl,--allow-undefined -Wl,--export=use_env -o "$d/app.wasm" "$src" "$tmp/env.c"
    expect_fail "import from module env" python3 "$sdk/tools/wasm_check.py" "$d"

    printf '__attribute__((import_module("claw"), import_name("exec"))) int claw_exec(void);\nint use_exec(void) { return claw_exec(); }\n' > "$tmp/exec.c"
    wasm_cc "${ld[@]}" -Wl,--export=use_exec -o "$d/app.wasm" "$src" "$tmp/exec.c"
    expect_fail "claw.exec (not in claw.h)" python3 "$sdk/tools/wasm_check.py" "$d"

    wasm_cc "${ld[@]}" -Wl,--export=__heap_base -o "$d/app.wasm" "$src"
    expect_fail "exports __heap_base" python3 "$sdk/tools/wasm_check.py" "$d"

    sed 's/CLAW_EXPORT("tool_call")/CLAW_EXPORT("tool_kall")/' "$src" > "$tmp/notool.c"
    wasm_cc -Wl,-z,stack-size=8192 -Wl,--no-entry -Wl,--initial-memory=65536 -Wl,--max-memory=65536 \
        -o "$d/app.wasm" "$tmp/notool.c"
    expect_fail "missing tool_call export" python3 "$sdk/tools/wasm_check.py" "$d"

    wasm_cc -Wl,-z,stack-size=8192 -Wl,--no-entry -Wl,--export=app_init -Wl,--export=app_alloc -Wl,--export=tool_call \
        -Wl,--initial-memory=65536 -o "$d/app.wasm" "$src"
    expect_fail "memory without maximum" python3 "$sdk/tools/wasm_check.py" "$d"

    printf 'not wasm' > "$d/app.wasm"
    expect_fail "not a wasm file" python3 "$sdk/tools/wasm_check.py" "$d"
}

test_ecapp() {
    echo "ecapp.py: pack -> verify round trip with a throwaway key"
    local d="$tmp/e" ecapp=(python3 "$sdk/tools/ecapp.py") pub other
    mkdir -p "$d/pkg" "$d/keys"
    [ -f "$sdk/examples/minimal/build/app.wasm" ] || "$sdk/build-app.sh" "$sdk/examples/minimal" >/dev/null
    cp "$sdk/examples/minimal/build/app.wasm" "$sdk/examples/minimal/build/manifest.json" "$d/pkg/"
    openssl genpkey -algorithm ed25519 -out "$d/keys/test.pem" 2>/dev/null
    openssl genpkey -algorithm ed25519 -out "$d/keys/other.pem" 2>/dev/null
    pub=$("${ecapp[@]}" pubkey "$d/keys/test.pem" | cut -d' ' -f1)
    other=$("${ecapp[@]}" pubkey "$d/keys/other.pem" | cut -d' ' -f1)
    expect_ok "pack" "${ecapp[@]}" pack "$d/pkg" --key "$d/keys/test.pem"
    local pkg="$d/pkg/minimal-1.0.0.ecapp"
    expect_ok "default name minimal-1.0.0.ecapp" [ -f "$pkg" ]
    expect_ok "verify with the signing key" "${ecapp[@]}" verify "$pkg" --pub "$pub"
    expect_ok "verify with several trusted keys" "${ecapp[@]}" verify "$pkg" --pub "$other" --pub "$pub"
    expect_fail "verify with an untrusted key" "${ecapp[@]}" verify "$pkg" --pub "$other"

    python3 - "$pkg" "$d/tampered_wasm.ecapp" "$d/tampered_sig.ecapp" "$d/truncated.ecapp" <<'EOF'
import sys
blob = bytearray(open(sys.argv[1], "rb").read())
wasm = bytearray(blob)
wasm[blob.index(b"\0asm") + 20] ^= 1
open(sys.argv[2], "wb").write(wasm)
sig = bytearray(blob)
i = blob.index(b'"sig":"') + 8
sig[i] = ord("A") if sig[i] != ord("A") else ord("B")
open(sys.argv[3], "wb").write(sig)
open(sys.argv[4], "wb").write(blob[:-10])
EOF
    expect_fail "tampered app.wasm" "${ecapp[@]}" verify "$d/tampered_wasm.ecapp" --pub "$pub"
    expect_fail "tampered signature" "${ecapp[@]}" verify "$d/tampered_sig.ecapp" --pub "$pub"
    expect_fail "truncated package" "${ecapp[@]}" verify "$d/truncated.ecapp" --pub "$pub"
}

tests=("$@")
[ ${#tests[@]} -gt 0 ] || tests=(headers example manifest wasm ecapp)
for t in "${tests[@]}"; do "test_$t"; done
if [ "$fails" -gt 0 ]; then
    echo "$fails check(s) failed" >&2
    exit 1
fi
echo "all checks passed"
