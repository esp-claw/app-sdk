#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 esp-claw contributors
# SPDX-License-Identifier: Apache-2.0
"""Build, sign and check esp-claw WASM app packages (.ecapp) and signed update indexes.

  ecapp.py pack <dir> --key <ed25519.pem> [--format 2|1] [-o out.ecapp]   dir holds manifest.json + app.wasm
  ecapp.py verify <file.ecapp> --pub <base64> [--pub ...]
  ecapp.py pubkey <ed25519.pem>                              prints the base64 public key + key_id
  ecapp.py index <dir> --key <ed25519.pem> --base-url <url> [--days 90] [-o outdir]
                                                             dir holds the release's *.ecapp files
  ecapp.py index-verify <dir | index.json> --pub <base64> [--pub ...] [--now <unix>] [--name <app>]

Package (see esp-claw components/claw_capabilities/cap_wasm_apps/src/cap_wasm_pkg.h): "ECAP", u8 format
(1 or 2), u8 entry count 3, u16 0, then per entry u8 name length, name, u32 LE length, data; entries are
manifest.json, app.wasm and signature.json.

Format 2 (default): signature.json is {"alg","key_id","sdk":2,"sig"} and "sig" signs the binary record
  "ecapp-sig-v2\\0" u8 2 u16le api u8 len name u8 len version sha256(manifest.json) sha256(app.wasm)
built from the manifest (which must declare "api") and the entry bytes. Format 1: signature.json also has
a "signed" object {name, version, manifest_sha256, wasm_sha256} and "sig" signs its canonical JSON (that
key order, no whitespace). The device accepts both; new apps should use format 2.

Update index: index.json {"format":1,"issued","expires","apps":[{name,version,url,sha256,size}]} and
index.sig {"alg","key_id","expires","sig"}, where "sig" signs "ecapp-index-v1\\0" sha256(index.json bytes)
u64le expires. Signing uses the `cryptography` package, or `openssl pkeyutl` without it.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time

FORMAT_DEFAULT = 2
FORMATS = (1, 2)
SIG_V2_PREFIX = b"ecapp-sig-v2\0"
INDEX_PREFIX = b"ecapp-index-v1\0"
INDEX_FORMAT = 1
INDEX_DAYS_DEFAULT = 90
INDEX_MAX = 8 * 1024
ENTRIES = ("manifest.json", "app.wasm", "signature.json")
MAX = {"manifest.json": 4096, "app.wasm": 256 * 1024, "signature.json": 1024}
PKG_MAX = 512 * 1024
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")  # DER SubjectPublicKeyInfo for a raw Ed25519 key

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
except ImportError:
    serialization = None


def fail(msg):
    sys.exit(f"ecapp: {msg}")


def canonical(name, version, manifest_sha, wasm_sha):
    """Format 1: the signed bytes."""
    return (f'{{"name":"{name}","version":"{version}","manifest_sha256":"{manifest_sha}",'
            f'"wasm_sha256":"{wasm_sha}"}}').encode()


def record_v2(api, name, version, manifest_sha, wasm_sha):
    """Format 2: the signed bytes (cap_wasm_sig_record() in the firmware); hashes are raw 32 bytes."""
    name, version = name.encode(), version.encode()
    return (SIG_V2_PREFIX + bytes([2]) + struct.pack("<H", api) + bytes([len(name)]) + name +
            bytes([len(version)]) + version + manifest_sha + wasm_sha)


def index_record(index_bytes, expires):
    """The signed bytes of an update index (cap_wasm_index_record() in the firmware)."""
    return INDEX_PREFIX + hashlib.sha256(index_bytes).digest() + struct.pack("<Q", expires)


def key_id(pub_raw):
    return hashlib.sha256(pub_raw).hexdigest()[:8]


def version_key(version):
    """Sort key matching cap_wasm_version_cmp() on the device."""
    m = VERSION_RE.match(version)
    if not m:
        raise ValueError(f"version '{version}' is not MAJOR.MINOR.PATCH[-PRE][+BUILD]")
    pre = m.group(4)
    ids = [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")] if pre else []
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), pre is None, ids)


def openssl(args, data=None):
    return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout


def private_key(pem_path):
    if serialization is None:
        return None
    with open(pem_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def public_raw(pem_path):
    key = private_key(pem_path)
    if key is not None:
        return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    der = openssl(["pkey", "-in", pem_path, "-pubout", "-outform", "DER"])
    if not der.startswith(SPKI_PREFIX) or len(der) != len(SPKI_PREFIX) + 32:
        fail(f"{pem_path} is not an Ed25519 private key")
    return der[len(SPKI_PREFIX):]


def sign(pem_path, data):
    key = private_key(pem_path)
    if key is not None:
        return key.sign(data)
    with tempfile.NamedTemporaryFile() as msg:
        msg.write(data)
        msg.flush()
        return openssl(["pkeyutl", "-sign", "-rawin", "-inkey", pem_path, "-in", msg.name])


def verify_sig(pub_raw, sig, data):
    if serialization is not None:
        try:
            Ed25519PublicKey.from_public_bytes(pub_raw).verify(sig, data)
            return True
        except InvalidSignature:
            return False
    with tempfile.TemporaryDirectory() as d:
        paths = {n: os.path.join(d, n) for n in ("pub.der", "msg", "sig")}
        for n, content in (("pub.der", SPKI_PREFIX + pub_raw), ("msg", data), ("sig", sig)):
            with open(paths[n], "wb") as f:
                f.write(content)
        try:
            openssl(["pkeyutl", "-verify", "-rawin", "-pubin", "-keyform", "DER", "-inkey", paths["pub.der"],
                     "-in", paths["msg"], "-sigfile", paths["sig"]])
            return True
        except subprocess.CalledProcessError:
            return False


def trusted_keys(pub_list):
    keys = {}
    for b64 in pub_list:
        raw = base64.b64decode(b64, validate=True)
        if len(raw) != 32:
            fail(f"--pub {b64} is not a 32-byte Ed25519 key")
        keys[key_id(raw)] = raw
    return keys


def check_manifest(manifest_bytes, need_api):
    """Returns (name, version, api); api is None when the manifest has none (format 1 only)."""
    try:
        manifest = json.loads(manifest_bytes)
        name, version, api = manifest["name"], manifest["version"], manifest.get("api")
    except (ValueError, KeyError, TypeError) as e:
        fail(f"manifest.json: {e}")
    if not isinstance(name, str) or not NAME_RE.match(name):
        fail("manifest name must be 1-24 chars of [a-z0-9_] starting with a letter")
    if not isinstance(version, str) or len(version) > 16:
        fail("manifest version must be a string of at most 16 chars")
    try:
        version_key(version)
    except ValueError as e:
        fail(str(e))
    if api is not None and (isinstance(api, bool) or not isinstance(api, int) or not 1 <= api <= 65535):
        fail("manifest api must be an integer in [1, 65535]")
    if need_api and api is None:
        fail("a format 2 package needs \"api\" in manifest.json (the claw.h API version it was built for)")
    return name, version, api


def pack_entries(entries, fmt):
    out = bytearray(b"ECAP" + bytes([fmt, len(entries), 0, 0]))
    for name, data in entries:
        out += bytes([len(name)]) + name.encode() + struct.pack("<I", len(data)) + data
    return bytes(out)


def unpack(blob):
    """Returns (format, {entry name: bytes})."""
    if blob[:4] != b"ECAP" or blob[4] not in FORMATS or blob[5:8] != bytes([3, 0, 0]):
        fail("not an .ecapp package (format 1 or 2)")
    off, entries = 8, {}
    for _ in range(3):
        if off >= len(blob):
            fail("truncated package")
        n = blob[off]
        name = blob[off + 1:off + 1 + n].decode(errors="replace")
        (size,) = struct.unpack_from("<I", blob, off + 1 + n)
        off += 5 + n
        if name not in MAX or name in entries:
            fail(f"unexpected or repeated entry '{name}'")
        if size == 0 or size > MAX[name] or off + size > len(blob):
            fail(f"{name} is empty, truncated or larger than {MAX[name]} bytes")
        entries[name] = blob[off:off + size]
        off += size
    if off != len(blob):
        fail("trailing data after the last entry")
    return blob[4], entries


def signature_json(pem_path, fmt, name, version, api, manifest, wasm):
    pub = public_raw(pem_path)
    manifest_sha, wasm_sha = hashlib.sha256(manifest).digest(), hashlib.sha256(wasm).digest()
    if fmt == 2:
        sig = sign(pem_path, record_v2(api, name, version, manifest_sha, wasm_sha))
        doc = {"alg": "ed25519", "key_id": key_id(pub), "sdk": 2, "sig": base64.b64encode(sig).decode()}
    else:
        signed = {"name": name, "version": version, "manifest_sha256": manifest_sha.hex(), "wasm_sha256": wasm_sha.hex()}
        sig = sign(pem_path, canonical(name, version, signed["manifest_sha256"], signed["wasm_sha256"]))
        doc = {"alg": "ed25519", "key_id": key_id(pub), "sdk": 1, "signed": signed,
               "sig": base64.b64encode(sig).decode()}
    return json.dumps(doc, separators=(",", ":")).encode()


def cmd_pack(args):
    with open(os.path.join(args.dir, "manifest.json"), "rb") as f:
        manifest = f.read()
    with open(os.path.join(args.dir, "app.wasm"), "rb") as f:
        wasm = f.read()
    for name, data in (("manifest.json", manifest), ("app.wasm", wasm)):
        if not data or len(data) > MAX[name]:
            fail(f"{name} must be 1-{MAX[name]} bytes")
    name, version, api = check_manifest(manifest, args.format == 2)
    signature = signature_json(args.key, args.format, name, version, api, manifest, wasm)
    out = args.output or os.path.join(args.dir, f"{name}-{version}.ecapp")
    with open(out, "wb") as f:
        f.write(pack_entries([("manifest.json", manifest), ("app.wasm", wasm), ("signature.json", signature)],
                             args.format))
    print(f"{out}: {name} {version}, format {args.format}, key {key_id(public_raw(args.key))}, wasm {len(wasm)} bytes")


def verify_package(blob, keys):
    """Checks a package like the device does; returns (name, version, format, key_id) or fails."""
    fmt, entries = unpack(blob)
    manifest, wasm = entries["manifest.json"], entries["app.wasm"]
    try:
        sig = json.loads(entries["signature.json"])
        if sig.get("sdk") not in FORMATS:
            fail(f"unsupported sdk {sig.get('sdk')!r} (1 or 2)")
        if sig["sdk"] != fmt:
            fail(f"signature format {sig['sdk']} in a format {fmt} package")
        expected = {"alg", "key_id", "sdk", "sig"} | ({"signed"} if fmt == 1 else set())
        if set(sig) != expected:
            fail("signature.json has missing or unknown keys")
        if sig["alg"] != "ed25519":
            fail(f"unsupported alg {sig['alg']!r}")
        raw_sig = base64.b64decode(sig["sig"], validate=True)
    except (ValueError, KeyError, TypeError) as e:
        fail(f"signature.json: {e}")
    name, version, api = check_manifest(manifest, fmt == 2)
    manifest_sha, wasm_sha = hashlib.sha256(manifest).digest(), hashlib.sha256(wasm).digest()
    if fmt == 2:
        message = record_v2(api, name, version, manifest_sha, wasm_sha)
    else:
        signed = sig["signed"]
        if not isinstance(signed, dict) or set(signed) != {"name", "version", "manifest_sha256", "wasm_sha256"}:
            fail("signature.json has missing or unknown signed keys")
        if signed["name"] != name or signed["version"] != version:
            fail("signed name/version do not match manifest.json")
        if signed["manifest_sha256"] != manifest_sha.hex():
            fail("manifest.json does not match its signed hash")
        if signed["wasm_sha256"] != wasm_sha.hex():
            fail("app.wasm does not match its signed hash")
        message = canonical(name, version, signed["manifest_sha256"], signed["wasm_sha256"])
    if sig["key_id"] not in keys:
        fail(f"signed by untrusted key {sig['key_id']}")
    if len(raw_sig) != 64 or not verify_sig(keys[sig["key_id"]], raw_sig, message):
        fail(f"invalid signature (key {sig['key_id']})")
    return name, version, fmt, sig["key_id"]


def cmd_verify(args):
    with open(args.file, "rb") as f:
        blob = f.read()
    name, version, fmt, kid = verify_package(blob, trusted_keys(args.pub))
    print(f"{args.file}: OK {name} {version}, format {fmt}, key {kid}, {len(blob)} bytes")


def cmd_pubkey(args):
    pub = public_raw(args.key)
    print(f"{base64.b64encode(pub).decode()}  key_id {key_id(pub)}")


def index_entries(release_dir, base_url):
    """One entry per *.ecapp in release_dir (structurally checked; the device verifies signatures)."""
    entries = []
    for file in sorted(os.listdir(release_dir)):
        if not file.endswith(".ecapp"):
            continue
        with open(os.path.join(release_dir, file), "rb") as f:
            blob = f.read()
        if len(blob) > PKG_MAX:
            fail(f"{file} is larger than {PKG_MAX} bytes")
        fmt, parts = unpack(blob)
        name, version, _ = check_manifest(parts["manifest.json"], fmt == 2)
        if any(e["name"] == name for e in entries):
            fail(f"two packages for app '{name}' in {release_dir}")
        entries.append({"name": name, "version": version, "url": f"{base_url.rstrip('/')}/{file}",
                        "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)})
    if not entries:
        fail(f"no *.ecapp files in {release_dir}")
    return entries


def index_documents(pem_path, entries, issued, expires):
    """Returns (index.json bytes, index.sig bytes)."""
    index = json.dumps({"format": INDEX_FORMAT, "issued": issued, "expires": expires, "apps": entries},
                       separators=(",", ":")).encode() + b"\n"
    if len(index) > INDEX_MAX:
        fail(f"index.json is {len(index)} bytes, the device limit is {INDEX_MAX}")
    sig = sign(pem_path, index_record(index, expires))
    doc = {"alg": "ed25519", "key_id": key_id(public_raw(pem_path)), "expires": expires,
           "sig": base64.b64encode(sig).decode()}
    return index, json.dumps(doc, separators=(",", ":")).encode() + b"\n"


def cmd_index(args):
    if not args.base_url.startswith("https://"):
        fail("--base-url must start with https://")
    if args.days < 1:
        fail("--days must be at least 1")
    issued = int(time.time())
    index, sig = index_documents(args.key, index_entries(args.dir, args.base_url), issued, issued + args.days * 86400)
    out = args.output or args.dir
    for file, data in (("index.json", index), ("index.sig", sig)):
        with open(os.path.join(out, file), "wb") as f:
            f.write(data)
    print(f"{out}/index.json + index.sig: {len(json.loads(index)['apps'])} app(s), valid {args.days} days, "
          f"key {key_id(public_raw(args.key))}")


def verify_index(index, sig_bytes, keys, now, name=None, last_issued=0):
    """Checks an index like the device does; returns (index object, key_id, entry for `name` or None)."""
    try:
        sig = json.loads(sig_bytes)
        if set(sig) != {"alg", "key_id", "expires", "sig"} or sig["alg"] != "ed25519":
            fail("index.sig has missing or unknown keys, or alg is not ed25519")
        raw_sig = base64.b64decode(sig["sig"], validate=True)
        expires = sig["expires"]
        if isinstance(expires, bool) or not isinstance(expires, int) or not 0 <= expires < 2**64:
            fail("index.sig expires must be an integer")
    except (ValueError, KeyError, TypeError) as e:
        fail(f"index.sig: {e}")
    if sig["key_id"] not in keys:
        fail(f"index signed by untrusted key {sig['key_id']}")
    if len(raw_sig) != 64 or not verify_sig(keys[sig["key_id"]], raw_sig, index_record(index, expires)):
        fail(f"invalid index signature (key {sig['key_id']})")
    try:
        doc = json.loads(index)
        if set(doc) != {"format", "issued", "expires", "apps"} or doc["format"] != INDEX_FORMAT:
            fail("index.json has missing or unknown keys, or an unsupported format")
        if doc["expires"] != expires:
            fail("index.json expires does not match the signed expires")
        if not isinstance(doc["issued"], int) or doc["issued"] > expires:
            fail("index.json issued must be an integer not after expires")
        if not isinstance(doc["apps"], list):
            fail("index.json apps must be an array")
    except (ValueError, KeyError, TypeError) as e:
        fail(f"index.json: {e}")
    if now > expires:
        fail(f"index expired at {expires} (now {now})")
    if doc["issued"] < last_issued:
        fail(f"index issued {doc['issued']} is older than the last accepted index {last_issued}")
    entry = None
    for e in doc["apps"]:
        if not isinstance(e, dict) or set(e) != {"name", "version", "url", "sha256", "size"}:
            fail("index.json apps entries need exactly name, version, url, sha256 and size")
        if name is not None and e["name"] == name:
            entry = e
    if name is not None and entry is None:
        fail(f"no entry for app '{name}' in the index")
    return doc, sig["key_id"], entry


def cmd_index_verify(args):
    path = os.path.join(args.path, "index.json") if os.path.isdir(args.path) else args.path
    with open(path, "rb") as f:
        index = f.read()
    with open(os.path.join(os.path.dirname(path), "index.sig"), "rb") as f:
        sig = f.read()
    doc, kid, entry = verify_index(index, sig, trusted_keys(args.pub), args.now or int(time.time()), args.name)
    print(f"{path}: OK, {len(doc['apps'])} app(s), issued {doc['issued']}, expires {doc['expires']}, key {kid}"
          + (f", {entry['name']} {entry['version']} {entry['url']}" if entry else ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack", help="sign and pack <dir>/{manifest.json,app.wasm}")
    p.add_argument("dir")
    p.add_argument("--key", required=True, help="Ed25519 private key (PKCS#8 PEM)")
    p.add_argument("--format", type=int, choices=FORMATS, default=FORMAT_DEFAULT,
                   help=f"package/signature format (default {FORMAT_DEFAULT}; 1 for firmware before format 2)")
    p.add_argument("-o", "--output", help="default: <dir>/<name>-<version>.ecapp")
    p.set_defaults(fn=cmd_pack)
    p = sub.add_parser("verify", help="check a package against trusted public keys")
    p.add_argument("file")
    p.add_argument("--pub", action="append", required=True, help="base64 raw 32-byte public key (repeatable)")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("pubkey", help="print the base64 public key and key_id of a private key")
    p.add_argument("key")
    p.set_defaults(fn=cmd_pubkey)
    p = sub.add_parser("index", help="write and sign index.json + index.sig for the *.ecapp files in <dir>")
    p.add_argument("dir")
    p.add_argument("--key", required=True, help="Ed25519 private key (PKCS#8 PEM)")
    p.add_argument("--base-url", required=True, help="https URL the packages are served under")
    p.add_argument("--days", type=int, default=INDEX_DAYS_DEFAULT, help=f"validity (default {INDEX_DAYS_DEFAULT})")
    p.add_argument("-o", "--output", help="directory for index.json and index.sig (default: <dir>)")
    p.set_defaults(fn=cmd_index)
    p = sub.add_parser("index-verify", help="check index.json + index.sig like the device does")
    p.add_argument("path", help="index.json or the directory containing it and index.sig")
    p.add_argument("--pub", action="append", required=True, help="base64 raw 32-byte public key (repeatable)")
    p.add_argument("--now", type=int, help="unix time to check expiry against (default: now)")
    p.add_argument("--name", help="require an entry for this app")
    p.set_defaults(fn=cmd_index_verify)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
