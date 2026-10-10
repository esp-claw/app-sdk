# app-sdk

SDK for esp-claw WASM apps: host API header, manifest schema, build, sign and package tools.

Contains `claw.h`, which declares the host functions (log, tools, per-app key-value store, credential-profile HTTP, Telegram confirmation), the manifest format, and scripts that build, sign and package apps as `.ecapp` files.

**Status:** in development. The code will be published here once the esp-claw WASM app runtime has been tested on hardware.

Part of [esp-claw (community)](https://github.com/esp-claw), an unofficial community fork of espressif/esp-claw that is not affiliated with Espressif.

## License

Apache-2.0
