# app-sdk

SDK for esp-claw WASM apps: sandboxed modules that add LLM tools to the device. The runtime lives in the firmware, [esp-claw/esp-claw](https://github.com/esp-claw/esp-claw), component `cap_wasm_apps` (WAMR interpreter). Each app has its own linear memory, a time budget per call, and only the host functions its manifest grants.

```
include/claw.h            host API v2 (import module "claw") + helper macros
include/claw_util.h       optional output buffer and minimal JSON reader (header-only, freestanding)
build-app.sh              builds <app>/build/{app.wasm,manifest.json}
tools/ecapp.py            packs, signs and verifies .ecapp packages; writes signed update indexes; prints public keys
tools/manifest_check.py   checks manifest.json against the firmware's rules
tools/wasm_check.py       checks a built app.wasm: imports (permissions, api), exports, memory, size
tools/device_test.sh      install/update/remove test steps for a flashed device (signed packages)
examples/minimal/         smallest app: one tool, minimal_ping, returns "pong"
tests/selftest.sh         SDK self-test (run by CI)
tests/ecapp_vectors.json  test key, signature record and index vectors shared with the firmware's host tests
tests/make_vectors.py     regenerates (or --check verifies) ecapp_vectors.json
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
- missing or mistyped `app_init` / `app_alloc` / `tool_call` exports (and `on_timer` with permission `timer`)
- an exported `__heap_base` / `__data_end` / `__stack_pointer`
- a memory whose initial or maximum size differs from `limits.memory_kb`
- a module larger than 256 KB

The firmware refuses all of these at load time, so CI catches them first.

## App contract

The app exports three functions. `app_init()` registers tools with `claw_tool_register()`. `app_alloc(size)` returns a buffer for the next call's argument JSON (`CLAW_DEFINE_ARENA()` provides one). `tool_call(tool, args, len)` handles a call: it sets the result with `claw_tool_result()` and returns 0. See the comments in `include/claw.h` and `examples/minimal/main.c`.

### Timers and notifications

An app with permission `timer` exports `void on_timer(void)` and starts its timer with `claw_timer_set(interval_s)` (at least `min_interval_s`; 0 cancels). The device stores the schedule, so it survives reboots and idle unloads: when the timer is due, the device loads the app and calls `on_timer()` under `limits.call_timeout_ms`. Timers of at least an hour keep their last run across reboots; shorter ones restart their interval at boot. Calling `claw_timer_set()` with the current interval keeps the schedule, so `app_init` may set it on every load. At most 4 apps can have a timer. After 3 consecutive traps or timeouts the timer stops and the allowed Telegram users get a message; `/apps reload <name>` starts it again. `/apps` shows the interval, the next run and failures.

`on_timer()` has no Telegram chat: `claw_confirm()` returns `-CLAW_ENOTSUP` and HTTP mutations (which need a confirmation) are refused. To tell the user something, use permission `notify` and `claw_notify(text)`: it sends `` 📣 App `<name>`: <text> `` to the device's allowed Telegram users, at most 10 messages per hour per app.

### Time

`claw_now_ms()` returns the device clock in unix milliseconds, but gives no hint whether that clock was ever set. `claw_time_utc()` (api 2, no permission) fills a `claw_time_t` {`uint64_t unix_ms`, `uint8_t quality`} and returns 0, or `CLAW_TIME_UNKNOWN` (-1) when the wall clock was never set. The device writes 9 bytes: u64 little-endian unix milliseconds, then the quality: `CLAW_TIME_NONE` (0, never set), `CLAW_TIME_SYNCED` (1, SNTP or RTC) or `CLAW_TIME_AUTHENTICATED` (2, signed time; esp-claw never produces it, other hosts of this API may). Use it for anything where a wrong date matters (expiry checks, timestamps you store).

The LLM sees each tool as `<app>_<tool>` in the capability group `cap_wasm_apps`. That group is LLM-visible by default. If `llm_visible_cap_groups` is set explicitly, add `cap_wasm_apps` to it. Tool names are at most 48 chars including the prefix, and the device allows at most 32 app tools in total.

## manifest.json

```json
{
  "name": "cf",
  "version": "1.0.0",
  "api": 2,
  "description": "shown in /apps and apps_status",
  "permissions": { "log": true, "kv": true, "http_profiles": ["cloudflare"], "confirm": true, "ping": true },
  "limits": { "memory_kb": 64, "call_timeout_ms": 5000 },
  "settings": [
    { "key": "zone", "type": "string", "default": "example.com", "description": "DNS zone to manage" },
    { "key": "api_token", "type": "string", "secret": true, "description": "Cloudflare API token" }
  ]
}
```

| field | rule |
|---|---|
| `name` | required, 1-24 chars `[a-z0-9_]`, starts with a letter; the app's directory on the device (`apps/<name>/`) and its tool prefix |
| `version` | required, 1-16 chars, `MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]` |
| `api` | required, 1-65535: the `claw.h` host API version the app needs (`CLAW_API_VERSION` is 2; declare the lowest version whose functions you import, 1 unless you use `claw_time_utc()`). Signed as part of the package. The device refuses an app whose api is above what it implements ("needs claw api N, firmware has M") and one that imports a function newer than its api |
| `description` | optional, <= 200 bytes |
| `permissions.log` / `kv` / `confirm` / `ping` / `notify` | optional booleans, default false |
| `permissions.timer` | optional object `{"min_interval_s": N}`, N 60-604800 (default 60); requires the export `on_timer` |
| `permissions.http_profiles` | optional, 1-4 distinct names (`[A-Za-z0-9_-]`, <= 31 chars) of `api_profiles` entries on the device |
| `limits.memory_kb` | linear memory cap, multiple of 64, 64-512, default 64 |
| `limits.call_timeout_ms` | budget per tool call, 100-30000, default 5000 (`app_init`: fixed 2000) |
| `settings` | optional, at most 8 user settings, see below |

### Settings

Each `settings` entry declares one value the user configures on the device:

| field | rule |
|---|---|
| `key` | required, 1-15 chars `[a-z0-9_]`, starts with a letter, unique |
| `type` | required, `string`, `int` (int32) or `bool` |
| `default` | optional, of that type; strings 1-64 bytes. Not allowed for secrets |
| `secret` | optional boolean. Secret values are never shown to the LLM and are masked in `/app config` |
| `description` | optional, <= 100 bytes, shown in `/app config` and to the LLM |

The user sets values in Telegram, only with the slash command (the LLM cannot change settings; its `apps_config` tool lists them without secret values):

```
/app config <app>                    list settings (secrets as "•••• (set, N chars)")
/app config <app> <key> <value>      set (strings up to 256 bytes; bool: true/false, on/off, yes/no, 1/0)
/app config <app> <key> --reset      back to the default (or unset)
```

The app reads a value with `claw_config_get()` as text. Values are kept when the app is updated. Values of keys the new manifest no longer declares, or that no longer fit a changed type, are dropped. `/app remove` deletes them.

The device rejects an app with unknown keys or permissions. It also rejects an app that imports a host function its manifest does not grant. `tools/manifest_check.py` mirrors `cap_wasm_manifest.c` in the firmware. Its header comment lists the source files to compare when the firmware changes.

## Versioning

Versions follow semver ordering: `1.2.0 < 1.10.0`, and `1.0.0-rc.1 < 1.0.0`. Build metadata (`+…`) does not affect the order. Bump `version` in `manifest.json` for every release, and tag the release `v<version>`. The reusable workflow refuses to attach files to a release whose tag does not match.

The device keeps a per-app **version floor**: the highest version it ever installed, also after `/app remove`. It refuses to install a version below the installed one or below the floor (anti-rollback: an old, signed package with a known bug cannot be pushed back onto a device) unless the user passes `--allow-downgrade`, which is confirmed with a button that says "Downgrade". After such an install the floor is the downgraded version.

`claw.h` is host API v2 (`CLAW_API_VERSION`); the manifest's `api` says which version an app needs. `signature.json`'s `"sdk"` is the package format (2, or 1 for older firmware), see below.

## Packages and signing

The device installs `.ecapp` packages. A package must be signed by a key listed in the device config `app_trusted_keys`. That setting is a JSON array of base64 raw 32-byte Ed25519 public keys. For development, set `app_allow_unsigned` to `true` to also load unsigned apps. The device reads both settings at boot.

```sh
openssl genpkey -algorithm ed25519 -out key.pem                     # once; never commit it
python3 sdk/tools/ecapp.py pubkey key.pem                           # base64 key + key_id for app_trusted_keys
python3 sdk/tools/ecapp.py pack build --key key.pem                 # -> build/<name>-<version>.ecapp (format 2)
python3 sdk/tools/ecapp.py pack build --key key.pem --format 1      # for firmware that predates format 2
python3 sdk/tools/ecapp.py verify build/<name>-<version>.ecapp --pub <base64>
```

`ecapp.py` uses the `cryptography` package if it is installed and falls back to `openssl` (3.x) otherwise. The private key is only read to sign.

An `.ecapp` starts with `"ECAP"`, u8 format (2, or 1), u8 entry count 3 and u16 0. Each entry follows as u8 name length, name, u32 little-endian length, data. The entries are `manifest.json`, `app.wasm` and `signature.json`, at most 4 KB, 256 KB and 1 KB, and 512 KB in total. The format byte and `signature.json`'s `sdk` are the same number and must agree.

#### Format 2 (default)

```json
{"alg":"ed25519","key_id":"<first 8 hex of sha256(public key)>","sdk":2,"sig":"<base64 Ed25519 signature>"}
```

Nothing is repeated in `signature.json`: `sig` signs a fixed binary record that the device rebuilds from the manifest and the entry bytes, so there is no JSON canonicalization to get byte-exact on either side:

| offset | size | value |
|---|---|---|
| 0 | 13 | `ecapp-sig-v2` followed by one NUL byte (domain separation) |
| 13 | 1 | u8 format = 2 |
| 14 | 2 | u16 little-endian `api` from the manifest |
| 16 | 1 | u8 name length N (1-24) |
| 17 | N | `name` as in the manifest, no NUL |
| 17+N | 1 | u8 version length V (1-16) |
| 18+N | V | `version` as in the manifest, no NUL |
| 18+N+V | 32 | SHA-256 of the `manifest.json` bytes |
| 50+N+V | 32 | SHA-256 of the `app.wasm` bytes |

At most 122 bytes. The manifest must declare `api`. `tests/ecapp_vectors.json` holds a test key (seed `sha256("ecapp test key, not a secret")`, key_id `490d27f5`), the record for name `hello`, version `1.2.3`, api 2, `manifest.json` `{"name":"hello","version":"1.2.3","api":2}` and an 8-byte `app.wasm` header, as hex, with the Ed25519 signature; the firmware's host tests (`cap_wasm_apps/test/host/ecapp_vectors.json`) check the same bytes. `tests/make_vectors.py --check` proves the file matches `ecapp.py`.

#### Format 1 (still accepted)

```json
{"alg":"ed25519","key_id":"<first 8 hex of sha256(public key)>","sdk":1,
 "signed":{"name":"hello","version":"1.0.0","manifest_sha256":"<hex>","wasm_sha256":"<hex>"},
 "sig":"<base64 Ed25519 signature>"}
```

`sig` signs the canonical bytes of `signed`: the keys in exactly that order, no whitespace, lowercase hex. A manifest without `api` is read as api 1.

On install and on every load, the device checks the format, `sdk`, the manifest's `api` against what it implements, (format 1) name, version and both hashes against the manifest and the bytes, that `key_id` is trusted for this app name, and the signature.

### Trusted keys

Apps published under [github.com/esp-claw](https://github.com/esp-claw) are signed with the esp-claw community key:

```
XHQYDdKaRZ6pNzXYcrGAB8bTyPa3D22NU+lVzAt2Acw=   key_id 7087d6a8
```

To trust it, set `app_trusted_keys` to `["XHQYDdKaRZ6pNzXYcrGAB8bTyPa3D22NU+lVzAt2Acw="]`. To also trust your own key, add it to the array (at most 8 keys).

A key can be **scoped** to app names: `{"key":"<base64>","apps":["cf","hello"]}` trusts it only for apps whose name starts with one of the 1-4 prefixes. A package, or an update index entry, for any other app signed by it is refused, and a scoped key never verifies a firmware image. Use this for a key that signs one app, so that a leaked key cannot replace the others.

## Releasing an app

The [reusable workflow](.github/workflows/app-build.yml) builds and checks every push and pull request. When a release is published, it attaches `app.wasm` and `manifest.json` to the release. Only that attach job gets `contents: write`, and only on release events; the build itself runs with a read-only token. CI never signs. A maintainer signs the CI-built files locally and uploads the package:

```sh
v=1.0.1                                                    # = manifest version
gh release create "v$v" --generate-notes                   # CI attaches app.wasm + manifest.json
gh release download "v$v" -p app.wasm -p manifest.json -D dist   # once the workflow has finished
python3 sdk/tools/ecapp.py pack dist --key /path/to/key.pem      # -> dist/<name>-$v.ecapp
python3 sdk/tools/ecapp.py verify dist/<name>-$v.ecapp --pub XHQYDdKaRZ6pNzXYcrGAB8bTyPa3D22NU+lVzAt2Acw=
python3 sdk/tools/ecapp.py index dist --key /path/to/key.pem \                 # indexes the *.ecapp files
    --base-url "https://github.com/<owner>/<repo>/releases/download/v$v"   # -> dist/index.json + index.sig
gh release upload "v$v" dist/<name>-$v.ecapp dist/index.json dist/index.sig
```

### Update index

Devices do not ask the GitHub API which release is the latest: that answer is unsigned, so anyone in the path could hide new releases (a freeze attack) or offer a different package. Instead every release carries `index.json` and `index.sig`, and the device reads them from `releases/latest/download/` (for an app installed from any other https URL: from the package's directory).

`index.json` is `{"format":1,"issued":<unix>,"expires":<unix>,"apps":[{"name","version","url","sha256","size"},...]}` (at most 8 KB). `index.sig` is `{"alg":"ed25519","key_id":"<8 hex>","expires":<unix>,"sig":"<base64>"}` where `sig` signs `"ecapp-index-v1"` + NUL (15 bytes) + SHA-256 of the `index.json` bytes exactly as served (32 bytes) + u64 little-endian `expires` (8 bytes). The `expires` inside `index.json` must equal the signed one. The device refuses an index that

- is not signed by a trusted key (a scoped key only for the entries inside its scope),
- has expired (`--days`, default 90: re-run `ecapp.py index` and upload the two files again before then, even without a new release),
- was issued before the last index it accepted for that app (a replayed old index),
- or cannot be judged because the device clock is not synced: "clock not synced, cannot verify index freshness".

The package named by the entry must then match the entry's `size` and `sha256`, and its manifest the entry's `version`, before the usual signature and version checks. `ecapp.py index-verify <dir> --pub <base64> [--now <unix>] [--name <app>]` checks the two files the way the device does; `tests/ecapp_vectors.json` holds a signed example index.

### Reusable workflow versions

Apps call the workflow at the major tag `v1`, never at `main`:

```yaml
jobs:
  build:
    uses: esp-claw/app-sdk/.github/workflows/app-build.yml@v1
    permissions:
      contents: write
```

`v1` is a moving tag. Maintainers move it to a newer commit on `main` only after CI on that commit is green. The `api` manifest key is a stricter check that existing apps fail until they add it, so the commit that introduced it belongs to a new major tag `v2`:

```sh
git tag -f v1 <commit> && git push -f origin v1
```

Changes that keep existing app repositories working (bug fixes, pinned action updates, new optional checks) move `v1`. A breaking change, such as new required inputs, a different repository layout or stricter checks that existing apps fail, gets a new major tag `v2`. App repositories then switch to `@v2` on their own schedule. All actions inside the workflow are pinned to commit SHAs, and Dependabot proposes updates.

## Install on the device

Host the `.ecapp` on any https URL, for example a GitHub release, then send in Telegram:

```
/app install <https-url>            download (<= 512 KB), verify, confirm, install
/app install --allow-downgrade <https-url>   same, but allows a version below the installed one or
                                    the anti-rollback floor (confirmed with a "Downgrade" button)
/app update <name> [https-url]      update from the signed index of where it was installed, or from a URL
/app remove <name>                  delete the app, its files, key-value data and settings
/app config <name> [<key> <value>|<key> --reset]   list or change the app's settings
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
