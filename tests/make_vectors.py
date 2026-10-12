#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 esp-claw contributors
# SPDX-License-Identifier: Apache-2.0
"""Writes tests/ecapp_vectors.json: a deterministic test key, a format 2 signature record and an update
index with their Ed25519 signatures, computed with tools/ecapp.py. The firmware's host tests
(esp-claw, cap_wasm_apps/test/host/ecapp_vectors.json) and rust-claw use the same file; the output is
reproducible, so `make_vectors.py --check` verifies the committed copy. The key is public test data.
"""
import base64
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import ecapp  # noqa: E402

SEED = hashlib.sha256(b"ecapp test key, not a secret").digest()
PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")  # DER PrivateKeyInfo for a raw Ed25519 seed
MANIFEST = b'{"name":"hello","version":"1.2.3","api":2}'
WASM = b"\0asm\1\0\0\0"
ISSUED, EXPIRES = 1780000000, 1800000000
BASE_URL = "https://example.com/apps"


def pem_from_seed(path):
    der = PKCS8_PREFIX + SEED
    body = base64.encodebytes(der).decode().replace("\n", "")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\n")


def vectors():
    with tempfile.TemporaryDirectory() as d:
        pem = os.path.join(d, "key.pem")
        pem_from_seed(pem)
        pub = ecapp.public_raw(pem)
        manifest_sha, wasm_sha = hashlib.sha256(MANIFEST).digest(), hashlib.sha256(WASM).digest()
        record = ecapp.record_v2(2, "hello", "1.2.3", manifest_sha, wasm_sha)
        canon = ecapp.canonical("hello", "1.2.3", manifest_sha.hex(), wasm_sha.hex())
        sig_v2 = ecapp.signature_json(pem, 2, "hello", "1.2.3", 2, MANIFEST, WASM)
        sig_v1 = ecapp.signature_json(pem, 1, "hello", "1.2.3", 2, MANIFEST, WASM)
        pkg_v2 = ecapp.pack_entries([("manifest.json", MANIFEST), ("app.wasm", WASM), ("signature.json", sig_v2)], 2)
        entries = [{"name": "hello", "version": "1.2.3", "url": f"{BASE_URL}/hello-1.2.3.ecapp",
                    "sha256": hashlib.sha256(pkg_v2).hexdigest(), "size": len(pkg_v2)},
                   {"name": "other", "version": "0.9.0", "url": f"{BASE_URL}/other-0.9.0.ecapp",
                    "sha256": "0" * 64, "size": 1000}]
        index, index_sig = ecapp.index_documents(pem, entries, ISSUED, EXPIRES)
    return {
        "key": {"seed_hex": SEED.hex(), "pub_hex": pub.hex(), "pub_b64": base64.b64encode(pub).decode(),
                "key_id": ecapp.key_id(pub)},
        "sig_v2": {"manifest": MANIFEST.decode(), "wasm_hex": WASM.hex(), "name": "hello", "version": "1.2.3",
                   "api": 2, "manifest_sha256": manifest_sha.hex(), "wasm_sha256": wasm_sha.hex(),
                   "record_hex": record.hex(), "sig_b64": json.loads(sig_v2)["sig"],
                   "signature_json": sig_v2.decode()},
        "sig_v1": {"canonical": canon.decode(), "sig_b64": json.loads(sig_v1)["sig"], "signature_json": sig_v1.decode()},
        "index_v1": {"index_json": index.decode(), "issued": ISSUED, "expires": EXPIRES,
                     "record_hex": ecapp.index_record(index, EXPIRES).hex(),
                     "sig_b64": json.loads(index_sig)["sig"], "index_sig": index_sig.decode()},
    }


def main():
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecapp_vectors.json")
    text = json.dumps(vectors(), indent=2) + "\n"
    if "--check" in sys.argv:
        with open(out, encoding="utf-8") as f:
            if f.read() != text:
                sys.exit(f"{out} differs from the generated vectors")
        print(f"{out}: up to date")
        return
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
