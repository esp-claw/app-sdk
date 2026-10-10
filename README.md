# app-sdk

SDK for esp-claw WASM apps: sandboxed modules that add LLM tools to the device. The runtime lives in the firmware, [esp-claw/esp-claw](https://github.com/esp-claw/esp-claw), component `cap_wasm_apps` (WAMR interpreter). Each app has its own linear memory, a time budget per call, and only the host functions its manifest grants.

```
include/claw.h            host API v1 (import module "claw") + helper macros
include/claw_util.h       optional output buffer and minimal JSON reader (header-only, freestanding)
build-app.sh              builds <app>/build/{app.wasm,manifest.json}
tools/ecapp.py            packs, signs and verifies .ecapp packages; prints public keys
tools/manifest_check.py   checks manifest.json against the firmware's rules
tools/wasm_check.py       checks a built app.wasm: imports, exports, memory, size
examples/minimal/         smallest app: one tool, minimal_ping, returns "pong"
tests/selftest.sh         SDK self-test (run by CI)
.github/workflows/app-build.yml   reusable build + check workflow for app repositories
```

Apps use this repository as a git submodule named `sdk`. To start a new app, use [app-template](https://github.com/esp-claw/app-template). Example apps: [app-hello](https://github.com/esp-claw/app-hello) and [app-ping](https://github.com/esp-claw/app-ping).

Part of [esp-claw (community)](https://github.com/esp-claw), an unofficial community fork of espressif/esp-claw. It is not affiliated with Espressif.

## Build and check

You need clang with the wasm32 target and `wasm-ld`. Check with `clang --print-targets | grep wasm32`. Install with `apt install clang lld` (Debian/Ubuntu) or `dnf install clang lld` (Fedora). `CC="zig cc"` also works. The tools need only Python 3.

```sh
sdk/build-app.sh .                         # -> build/app.wasm, build/manifest.json
python3 sdk/tools/manifest_check.py .      # manifest rules, same as the firmware
python3 sdk/tools/wasm_check.py build      # imports/exports/memory, same as the runtime
```

The linear memory is sized from `limits.memory_kb`. If you get link errors about undefined symbols, the code needs something besides `claw.*`. Apps have no libc, so for example a struct copy the compiler could not turn into `memory.copy` needs libc's `memcpy` and fails to link.

`wasm_check.py` rejects the following:
- any import that is not a function of module `claw` declared in `claw.h`
- a host function whose permission the manifest does not grant
- missing or mistyped `app_init` / `app_alloc` / `tool_call` exports
- an exported `__heap_base` / `__data_end` / `__stack_pointer`
- a memory whose initial or maximum size differs from `limits.memory_kb`
- a module larger than 256 KB

The firmware refuses all of these at load time, so CI catches them first.

## App contract

The app exports three functions. `app_init()` registers tools with `claw_tool_register()`. `app_alloc(size)` returns a buffer for the next call's argument JSON (`CLAW_DEFINE_ARENA()` provides one). `tool_call(tool, args, len)` handles a call: it sets the result with `claw_tool_result()` and returns 0. See the comments in `include/claw.h` and `examples/minimal/main.c`.

The LLM sees each tool as `<app>_<tool>` in the capability group `cap_wasm_apps`. That group is LLM-visible by default. If `llm_visible_cap_groups` is set explicitly, add `cap_wasm_apps` to it. Tool names are at most 48 chars including the prefix, and the device allows at most 32 app tools in total.

## manifest.json

```json
{
  "name": "cf",
  "version": "1.0.0",
  "description": "shown in /apps and apps_status",
  "permissions": { "log": true, "kv": true, "http_profiles": ["cloudflare"], "confirm": true, "ping": true },
  "limits": { "memory_kb": 64, "call_timeout_ms": 5000 }
}
```

| field | rule |
|---|---|
| `name` | required, 1-24 chars `[a-z0-9_]`, starts with a letter; the app's directory on the device (`apps/<name>/`) and its tool prefix |
| `version` | required, 1-16 chars, `MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]` |
| `description` | optional, <= 200 bytes |
| `permissions.log` / `kv` / `confirm` / `ping` | optional booleans, default false |
| `permissions.http_profiles` | optional, 1-4 distinct names (`[A-Za-z0-9_-]`, <= 31 chars) of `api_profiles` entries on the device |
| `limits.memory_kb` | linear memory cap, multiple of 64, 64-512, default 64 |
| `limits.call_timeout_ms` | budget per tool call, 100-30000, default 5000 (`app_init`: fixed 2000) |

The device rejects an app with unknown keys or permissions. It also rejects an app that imports a host function its manifest does not grant. `tools/manifest_check.py` mirrors `cap_wasm_manifest.c` in the firmware. Its header comment lists the source files to compare when the firmware changes.

## Versioning

Versions follow semver ordering: `1.2.0 < 1.10.0`, and `1.0.0-rc.1 < 1.0.0`. Build metadata (`+…`) does not affect the order. The device refuses to install a lower version than the installed one unless you pass `--force`. Bump `version` in `manifest.json` for every release, and tag the release `v<version>`. The reusable workflow refuses to attach files to a release whose tag does not match.

`claw.h` is host API v1, and packages declare `"sdk": 1` in `signature.json`. A firmware that does not support an app's SDK version refuses to install it.

## Packages and signing

The device installs `.ecapp` packages. A package must be signed by a key listed in the device config `app_trusted_keys`. That setting is a JSON array of base64 raw 32-byte Ed25519 public keys. For development, set `app_allow_unsigned` to `true` to also load unsigned apps. The device reads both settings at boot.

```sh
openssl genpkey -algorithm ed25519 -out key.pem                     # once; never commit it
python3 sdk/tools/ecapp.py pubkey key.pem                           # base64 key + key_id for app_trusted_keys
python3 sdk/tools/ecapp.py pack build --key key.pem                 # -> build/<name>-<version>.ecapp
python3 sdk/tools/ecapp.py verify build/<name>-<version>.ecapp --pub <base64>
```

`ecapp.py` uses the `cryptography` package if it is installed and falls back to `openssl` (3.x) otherwise. The private key is only read to sign.

An `.ecapp` starts with `"ECAP"`, u8 format 1, u8 entry count 3 and u16 0. Each entry follows as u8 name length, name, u32 little-endian length, data. The entries are `manifest.json`, `app.wasm` and `signature.json`, at most 4 KB, 256 KB and 1 KB, and 512 KB in total. `signature.json`:

```json
{"alg":"ed25519","key_id":"<first 8 hex of sha256(public key)>","sdk":1,
 "signed":{"name":"hello","version":"1.0.0","manifest_sha256":"<hex>","wasm_sha256":"<hex>"},
 "sig":"<base64 Ed25519 signature>"}
```

`sig` signs the canonical bytes of `signed`: the keys in exactly that order, no whitespace, lowercase hex. On install and on every load, the device checks:
- the format
- `sdk`
- name and version against the manifest
- both hashes
- that `key_id` is trusted
- the signature

### Trusted keys

Apps published under [github.com/esp-claw](https://github.com/esp-claw) are signed with the esp-claw community key:

```
XHQYDdKaRZ6pNzXYcrGAB8bTyPa3D22NU+lVzAt2Acw=   key_id 7087d6a8
```

To trust it, set `app_trusted_keys` to `["XHQYDdKaRZ6pNzXYcrGAB8bTyPa3D22NU+lVzAt2Acw="]`. To also trust your own key, add it to the array (at most 8 keys).

## Releasing an app

The [reusable workflow](.github/workflows/app-build.yml) builds and checks every push and pull request. When a release is published, it attaches `app.wasm` and `manifest.json` to the release. CI never signs. A maintainer signs the CI-built files locally and uploads the package:

```sh
v=1.0.1                                                    # = manifest version
gh release create "v$v" --generate-notes                   # CI attaches app.wasm + manifest.json
gh release download "v$v" -p app.wasm -p manifest.json -D dist   # once the workflow has finished
python3 sdk/tools/ecapp.py pack dist --key /path/to/key.pem      # -> dist/<name>-$v.ecapp
python3 sdk/tools/ecapp.py verify dist/<name>-$v.ecapp --pub XHQYDdKaRZ6pNzXYcrGAB8bTyPa3D22NU+lVzAt2Acw=
gh release upload "v$v" dist/<name>-$v.ecapp
```

## Install on the device

Host the `.ecapp` on any https URL, for example a GitHub release, then send in Telegram:

```
/app install <https-url>            download (<= 512 KB), verify, confirm, install
/app install --force <https-url>    same, but allows a lower version than installed
/app update <name> [https-url]      update; without a URL, from where it was installed
/app remove <name>                  delete the app, its files and its key-value data
/apps                               list: version, state, key, PSRAM, last use, tools, last error
/apps enable|disable <name>         persisted; disabled apps have no tools
/apps load|unload <name>            load into / free from memory now
/apps reload <name>                 reload from <storage>/apps/<name>/
```

Install and remove always ask for confirmation in the chat. That includes the LLM tools `apps_install(url)` and `apps_remove(name)`, and the device refuses these calls from outside Telegram. The new version is staged first. The old one is restored if the new one fails to load. You can still copy files to `<storage>/apps/<name>/` by hand: include `signature.json`, then send `/apps reload <name>`.

Apps load on demand. At boot, an app registers its cached tool list without being loaded. The first call loads it, and it is unloaded again after 10 minutes without calls, or earlier when PSRAM runs low.

## Runtime limits

- Each tool call runs under `limits.call_timeout_ms` of wall-clock time. Time blocked in HTTP requests or Telegram confirmations does not count. When the budget runs out, the device stops the call and discards the instance. The next call recreates it, so `app_init` runs again and must register the same tools. After 3 consecutive traps or timeouts the app is disabled until `/apps reload` or reboot.
- The device refuses to load an app when that would leave less than 1 MB of PSRAM free, even after unloading every other app.
- Results <= 16 KB. HTTP responses <= 32 KB. Key-value values <= 1 KB, 32 keys per app, 16 writes per call. 32 log lines per call.

## License

Apache-2.0
